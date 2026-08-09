from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.doubao_automation import DOUBAO_MODEL_CODES, DOUBAO_ORIGINAL_VIDEO_SCORE, DOUBAO_PREHANDLE_ATTACHMENTS_SCRIPT, DOUBAO_PREPARE_UPLOAD_BODY, DOUBAO_RESULT_WAIT_SECONDS, DOUBAO_SINGLE_CHAIN_SCRIPT, DOUBAO_SUBMISSION_MARKER, DOUBAO_SUBMIT_SCRIPT, QAAB_SALT, DoubaoReferenceImageUploader, DoubaoVideoAutomation, best_doubao_video_candidate, cache_doubao_video, classify_doubao_submission, collect_doubao_response_candidates, collect_doubao_video_candidates, decode_qaab_url, detect_doubao_generation_acknowledgement, detect_doubao_video_creation_page_refusal, doubao_confirmation_prompt_detected, doubao_payload_has_submission_marker, doubao_reference_upload_progress_visible, doubao_ui_generation_acknowledged, doubao_video_candidate_is_acceptable, doubao_video_url_score, extract_doubao_assistant_response_text, extract_doubao_conversation_id, extract_doubao_fallback_apis, fallback_payload_video_url, fetch_doubao_generation_result, is_doubao_account_quota_insufficient, is_doubao_text_only_video_response, normalize_doubao_submission_acknowledgement, parse_doubao_generation_result, semantic_drag_selection_complete, semantic_drag_selection_confirmed, should_use_doubao_video_creation_page, unwatermarked_fallback_url
from app.qianwen_automation import QianwenVideoAutomation
from app.slider_solver import SliderSolveResult


class DoubaoAutomationTests(unittest.TestCase):
    def test_confirmation_prompt_is_detected_for_ui_follow_up(self) -> None:
        self.assertTrue(doubao_confirmation_prompt_detected("确认就按这版生成吗？"))
        self.assertTrue(doubao_confirmation_prompt_detected("是否需要我创建这个视频"))
        self.assertFalse(doubao_confirmation_prompt_detected("视频生成已提交\n本次使用 Seedance 2.0 Fast 生成"))

    @staticmethod
    def runner(proxy_session) -> DoubaoVideoAutomation:
        runner = DoubaoVideoAutomation.__new__(DoubaoVideoAutomation)
        runner.proxy_session = proxy_session
        runner.settings = SimpleNamespace(task_timeout_seconds=600, doubao_submit_retry_limit=2)
        runner.task_id = "doubao-task"
        return runner

    def test_unwatermarked_video_is_streamed_to_task_cache(self) -> None:
        payload = b"video-bytes"

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["referer"], "https://www.doubao.com/chat/")
            self.assertEqual(request.headers["cookie"], "session=value")
            return httpx.Response(200, headers={"content-type": "video/mp4"}, content=payload)

        async def run(target: Path) -> int:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await cache_doubao_video(client, "session=value", "https://video.example/result.mp4", target)

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "task" / "video.mp4"
            size = asyncio.run(run(target))
            self.assertEqual(size, len(payload))
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(target.with_name(".video.mp4.tmp").exists())

    def test_shared_proxy_is_passed_to_browser_and_released(self) -> None:
        proxy = {"server": "http://proxy.example:18080"}
        session = SimpleNamespace(
            acquire_browser_proxy=AsyncMock(return_value=proxy),
            release_browser_proxy=AsyncMock(),
        )
        runner = self.runner(session)
        runner._run_browser = AsyncMock(return_value={"success": True})

        outcome = asyncio.run(runner._run_profile())

        self.assertTrue(outcome["success"])
        runner._run_browser.assert_awaited_once_with(proxy)
        session.acquire_browser_proxy.assert_awaited_once()
        session.release_browser_proxy.assert_awaited_once()

    def test_shared_proxy_is_released_when_browser_fails(self) -> None:
        session = SimpleNamespace(
            acquire_browser_proxy=AsyncMock(return_value={"server": "http://proxy.example:18080"}),
            release_browser_proxy=AsyncMock(),
            mark_browser_proxy_unavailable=Mock(),
        )
        runner = self.runner(session)
        runner._run_browser = AsyncMock(side_effect=RuntimeError("browser failed"))

        with self.assertRaisesRegex(RuntimeError, "browser failed"):
            asyncio.run(runner._run_profile())

        session.release_browser_proxy.assert_awaited_once()
        session.mark_browser_proxy_unavailable.assert_called_once_with(reason="doubao_browser_failure")

    def test_video_creation_retries_same_context_after_verification_is_solved(self) -> None:
        runner = DoubaoVideoAutomation.__new__(DoubaoVideoAutomation)
        runner._submit_via_video_creation_page_ui = AsyncMock(side_effect=[
            {"ok": True, "accepted": False, "slider_verification": True},
            {"ok": True, "accepted": True, "conversation_id": "12345678901234567"},
        ])
        runner._resolve_submission_verification = AsyncMock(
            return_value=SliderSolveResult(status="success", attempts=1)
        )
        runner._refresh_cookies = AsyncMock()
        page = SimpleNamespace(context=SimpleNamespace(), wait_for_timeout=AsyncMock())

        with patch("app.doubao_automation.secrets.randbelow", return_value=1000):
            result = asyncio.run(runner._submit_via_video_creation_page(page, image_count=0))

        self.assertTrue(result["accepted"])
        self.assertTrue(result["verification_recovery_attempted"])
        self.assertEqual(result["verification_solver_status"], "success")
        self.assertEqual(result["verification_recovery_delay_ms"], 4000)
        self.assertTrue(result["verification_recovery_reconfigured"])
        self.assertEqual(runner._submit_via_video_creation_page_ui.await_count, 2)
        runner._refresh_cookies.assert_awaited_once_with(page.context)
        page.wait_for_timeout.assert_awaited_once_with(4000)

    def test_doubao_solver_supports_current_rmc_captcha_iframe(self) -> None:
        with patch("app.doubao_automation.load_settings", return_value=SimpleNamespace()):
            runner = DoubaoVideoAutomation("task", "prompt", "16:9", "Seedance 2.0 Fast")

        selector = runner.verification_solver.settings.iframe_selector
        self.assertIn("rmc.bytedance.com/verifycenter/captcha", selector)
        self.assertIn("bdcaptcha.html", selector)

    def test_semantic_verification_is_not_sent_to_gap_slider_solver(self) -> None:
        runner = DoubaoVideoAutomation.__new__(DoubaoVideoAutomation)
        runner.task_id = "task"
        runner._set_phase = Mock()
        runner.verification_solver = SimpleNamespace(
            settings=SimpleNamespace(iframe_selector="iframe[src*='verifycenter/captcha']"),
            solve=AsyncMock(),
        )
        runner.semantic_verification_solver = SimpleNamespace(enabled=False)
        semantic = SimpleNamespace(count=AsyncMock(return_value=1))
        body = SimpleNamespace(inner_text=AsyncMock(return_value="Tap all matching objects"))
        basic = SimpleNamespace(count=AsyncMock(return_value=0))
        frame = SimpleNamespace(
            locator=Mock(side_effect=lambda selector: {
                "#captcha_click_image, .captcha-prompt-bar": semantic,
                "body": body,
                "img[alt='basicImg']": basic,
            }[selector])
        )
        frames = SimpleNamespace(
            count=AsyncMock(return_value=1),
            nth=Mock(return_value=SimpleNamespace(is_visible=AsyncMock(return_value=True))),
        )
        frame_selector = SimpleNamespace(nth=Mock(return_value=frame))
        page = SimpleNamespace(
            context=SimpleNamespace(pages=[]),
            locator=Mock(return_value=frames),
            frame_locator=Mock(return_value=frame_selector),
            wait_for_timeout=AsyncMock(),
        )
        page.context.pages = [page]

        with patch("app.doubao_automation.find_slider_page", new=AsyncMock(return_value=page)), patch(
            "app.doubao_automation.task_exists", return_value=False
        ):
            result = asyncio.run(runner._resolve_submission_verification(page))

        self.assertEqual(result.status, "semantic_challenge")
        self.assertEqual(result.attempts, 0)
        runner.verification_solver.solve.assert_not_awaited()

    def test_semantic_verification_refreshes_after_incorrect_coordinate_answer(self) -> None:
        runner = DoubaoVideoAutomation.__new__(DoubaoVideoAutomation)
        runner.task_id = "task"
        runner._set_phase = Mock()
        runner.verification_solver = SimpleNamespace(
            settings=SimpleNamespace(
                iframe_selector="iframe[src*='verifycenter/captcha']",
                max_attempts=3,
            ),
            solve=AsyncMock(),
        )
        runner.semantic_verification_solver = SimpleNamespace(enabled=True)
        runner._solve_semantic_submission_verification = AsyncMock(side_effect=[
            SliderSolveResult(
                status="semantic_challenge",
                attempts=1,
                error="semantic captcha remained visible after submission",
            ),
            SliderSolveResult(status="success", attempts=2),
        ])
        semantic = SimpleNamespace(count=AsyncMock(return_value=1))
        body = SimpleNamespace(inner_text=AsyncMock(return_value="可以在天空中观察到的东西"))
        basic = SimpleNamespace(count=AsyncMock(return_value=0))
        refresh_parent = SimpleNamespace(
            is_visible=AsyncMock(return_value=True),
            click=AsyncMock(),
        )
        refresh_candidate = SimpleNamespace(
            is_visible=AsyncMock(return_value=True),
            locator=Mock(return_value=refresh_parent),
        )
        refresh_text = SimpleNamespace(
            count=AsyncMock(return_value=1),
            nth=Mock(return_value=refresh_candidate),
        )
        frame = SimpleNamespace(
            locator=Mock(side_effect=lambda selector: {
                "#captcha_click_image, .captcha-prompt-bar": semantic,
                "body": body,
                "img[alt='basicImg']": basic,
            }[selector]),
            get_by_text=Mock(return_value=refresh_text),
        )
        frame_element = SimpleNamespace(is_visible=AsyncMock(return_value=True))
        frames = SimpleNamespace(
            count=AsyncMock(return_value=1),
            nth=Mock(return_value=frame_element),
        )
        frame_selector = SimpleNamespace(nth=Mock(return_value=frame))
        page = SimpleNamespace(
            context=SimpleNamespace(pages=[]),
            locator=Mock(return_value=frames),
            frame_locator=Mock(return_value=frame_selector),
            wait_for_timeout=AsyncMock(),
        )
        page.context.pages = [page]

        with patch("app.doubao_automation.find_slider_page", new=AsyncMock(return_value=page)):
            result = asyncio.run(runner._resolve_submission_verification(page))

        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(runner._solve_semantic_submission_verification.await_count, 2)
        refresh_parent.click.assert_awaited_once_with(force=True)

    def test_semantic_drag_comment_translates_known_category(self) -> None:
        comment = DoubaoVideoAutomation._semantic_captcha_comment(
            "属于动物的\n请选择所有符合上文描述的图片，并拖拽到下方",
            drag=True,
        )

        self.assertIn("objects that are animals", comment)
        self.assertIn("rectangle", comment)

        point_comment = DoubaoVideoAutomation._semantic_captcha_comment(
            "属于动物的，请拖拽到下方",
            drag=True,
            rectangles=False,
        )
        self.assertIn("click point at the center", point_comment)

        combined_comment = DoubaoVideoAutomation._semantic_captcha_comment(
            "谷物和蔬菜\n请选择所有符合上文描述的图片，并拖拽到下方",
            drag=True,
            rectangles=False,
        )
        self.assertIn("objects that are grains or vegetables", combined_comment)

        sky_comment = DoubaoVideoAutomation._semantic_captcha_comment(
            "可以在天空中观察到的东西\n请选择所有符合上文描述的图片，并拖拽到下方",
            drag=True,
            rectangles=False,
        )
        self.assertIn("objects that can be seen in the sky", sky_comment)
        self.assertIn("可以在天空中观察到的东西", sky_comment)

        thirst_comment = DoubaoVideoAutomation._semantic_captcha_comment(
            "能满足人的口渴的东西\n请选择所有符合上文描述的图片，并拖拽到下方",
            drag=True,
            rectangles=False,
        )
        camping_comment = DoubaoVideoAutomation._semantic_captcha_comment(
            "露营时可以用到的东西\n请选择所有符合上文描述的图片，并拖拽到下方",
            drag=True,
            rectangles=False,
        )
        pet_comment = DoubaoVideoAutomation._semantic_captcha_comment(
            "常见的家养宠物\n请选择所有符合上文描述的图片，并拖拽到下方",
            drag=True,
            rectangles=False,
        )
        self.assertIn("satisfy thirst", thirst_comment)
        self.assertIn("camping", camping_comment)
        self.assertIn("domesticated pets", pet_comment)

    def test_semantic_drag_does_not_accept_selected_class_without_registration(self) -> None:
        tile_key = (135, 195, 110, 110)
        before = {
            "selectedTileKeys": [],
            "registeredTileKeys": [],
            "dropPayloadCount": 0,
        }
        selected_only = {
            "selectedTileKeys": [list(tile_key)],
            "registeredTileKeys": [],
            "dropPayloadCount": 0,
        }

        confirmed, source = semantic_drag_selection_confirmed(before, selected_only, tile_key)

        self.assertFalse(confirmed)
        self.assertEqual(source, "")

    def test_semantic_drag_accepts_exact_registered_tile_or_drop_payload(self) -> None:
        tile_key = (135, 195, 110, 110)
        before = {
            "selectedTileKeys": [],
            "registeredTileKeys": [],
            "dropPayloadCount": 0,
        }
        registered = {
            "selectedTileKeys": [list(tile_key)],
            "registeredTileKeys": [list(tile_key)],
            "dropPayloadCount": 0,
        }
        dropped = {
            "selectedTileKeys": [],
            "registeredTileKeys": [],
            "dropPayloadCount": 1,
        }

        self.assertEqual(
            semantic_drag_selection_confirmed(before, registered, tile_key),
            (True, "internal_registered_selection"),
        )
        self.assertEqual(
            semantic_drag_selection_confirmed(before, dropped, tile_key),
            (True, "drop_area_payload"),
        )

    def test_semantic_drag_final_audit_rejects_missing_or_extra_tiles(self) -> None:
        first = (15, 75, 110, 110)
        second = (135, 195, 110, 110)
        initial = {
            "selectedTileKeys": [],
            "registeredTileKeys": [],
            "dropPayloadCount": 0,
        }
        exact = {
            "selectedTileKeys": [list(first), list(second)],
            "registeredTileKeys": [list(first), list(second)],
            "dropPayloadCount": 0,
        }
        extra = {
            "selectedTileKeys": [list(first), list(second), [255, 315, 110, 110]],
            "registeredTileKeys": [list(first), list(second), [255, 315, 110, 110]],
            "dropPayloadCount": 0,
        }

        self.assertEqual(
            semantic_drag_selection_complete(initial, exact, {first, second}),
            (True, "internal_registered_selection"),
        )
        self.assertEqual(
            semantic_drag_selection_complete(initial, extra, {first, second}),
            (False, ""),
        )

    def test_video_creation_keeps_verification_result_when_challenge_is_not_rendered(self) -> None:
        runner = DoubaoVideoAutomation.__new__(DoubaoVideoAutomation)
        runner._submit_via_video_creation_page_ui = AsyncMock(return_value={
            "ok": True,
            "accepted": False,
            "slider_verification": True,
        })
        runner._resolve_submission_verification = AsyncMock(
            return_value=SliderSolveResult(status="not_present", attempts=0)
        )
        runner._refresh_cookies = AsyncMock()

        result = asyncio.run(
            runner._submit_via_video_creation_page(SimpleNamespace(), image_count=0)
        )

        self.assertTrue(result["slider_verification"])
        self.assertEqual(result["verification_solver_status"], "not_present")
        runner._submit_via_video_creation_page_ui.assert_awaited_once()
        runner._refresh_cookies.assert_not_awaited()

    def test_proxy_refresh_defers_without_consuming_normal_retry(self) -> None:
        class ProxyRefreshError(RuntimeError):
            retry_after = 7
            queue_reason = "正在刷新API代理，任务已自动排队"
            queue_category = "proxy_refresh"

        session = SimpleNamespace(mark_browser_proxy_unavailable=lambda **_kwargs: None)
        runner = self.runner(session)
        runner._run_once = AsyncMock(side_effect=ProxyRefreshError("proxy refreshing"))

        outcome = asyncio.run(runner.run())

        self.assertTrue(outcome["retryable"])
        self.assertTrue(outcome["infrastructure_fault"])
        self.assertTrue(outcome["defer_only"])
        self.assertEqual(outcome["retry_after"], 7)
        self.assertEqual(outcome["defer_category"], "proxy_refresh")

    def test_browser_timeout_does_not_requeue_an_already_submitted_task(self) -> None:
        runner = self.runner(SimpleNamespace())
        runner._run_once = AsyncMock(side_effect=asyncio.TimeoutError)

        with patch("app.doubao_automation.task_exists", return_value=True), patch(
            "app.doubao_automation.get_meta", return_value={"status": "submitted"}
        ), patch("app.doubao_automation.mark_pending") as mark_pending:
            outcome = asyncio.run(runner.run())

        self.assertFalse(outcome["retryable"])
        mark_pending.assert_not_called()

    def test_browser_timeout_requeues_before_submission(self) -> None:
        runner = self.runner(SimpleNamespace())
        runner._run_once = AsyncMock(side_effect=asyncio.TimeoutError)

        with patch("app.doubao_automation.task_exists", return_value=True), patch(
            "app.doubao_automation.get_meta", return_value={"status": "running"}
        ), patch("app.doubao_automation.mark_pending") as mark_pending:
            outcome = asyncio.run(runner.run())

        self.assertTrue(outcome["retryable"])
        mark_pending.assert_called_once_with("doubao-task", "doubao browser timeout")

    def test_region_restriction_recognizes_redirect_and_page_message(self) -> None:
        self.assertTrue(
            DoubaoVideoAutomation._is_region_restricted(
                "https://www.doubao.com/security/doubao-region-ban?source=1",
                "",
            )
        )
        self.assertTrue(DoubaoVideoAutomation._is_region_restricted(DOUBAO_CHAT_URL, "当前地区暂不支持豆包"))
        self.assertTrue(DoubaoVideoAutomation._is_region_restricted(
            DOUBAO_CHAT_URL,
            "新对话 视频生成 受区域限制，\n请先登录再使用豆包",
        ))
        self.assertTrue(DoubaoVideoAutomation._is_region_restricted(
            "https://www.doubao.com/security/doubao-region-ban?source=1",
            "",
        ))
        self.assertFalse(DoubaoVideoAutomation._is_region_restricted(
            DOUBAO_CHAT_URL,
            "历史对话 当前地区暂不支持测试 新对话 AI 创作 视频生成",
        ))
        self.assertFalse(DoubaoVideoAutomation._is_region_restricted(DOUBAO_CHAT_URL, "开始视频生成"))

    def test_service_frequent_observation_marks_login_immediately(self) -> None:
        runner = self.runner(SimpleNamespace())
        runner._set_phase = Mock()
        runner._login_required = AsyncMock(return_value=True)
        body = SimpleNamespace(inner_text=AsyncMock(return_value="登录豆包"))
        page = SimpleNamespace(locator=Mock(return_value=body), wait_for_timeout=AsyncMock())

        with patch("app.doubao_automation.save_result") as save:
            state = asyncio.run(runner._observe_service_frequent(page, seconds=15))

        self.assertEqual(state, "login_invalid")
        page.wait_for_timeout.assert_not_awaited()
        self.assertEqual(save.call_args.kwargs["extra"]["doubao_service_frequent_state"], "login_invalid")

    def test_direct_submit_script_matches_captured_doubao_contract(self) -> None:
        for fragment in (
            'aid: "497858"',
            'bot_id: "7338286299411103781"',
            "ability_type: 17",
            '"agw-js-conv": "str, str"',
            '`${location.origin}/chat/completion?',
            'text.includes("710022002")',
            'text.includes("710022004")',
            'text.includes("SSE_REPLY_END")',
            "asksForVideoConfirmation(text)",
            "generationInstruction(prompt, true)",
            "body.option.need_create_conversation = !conversationId",
            "body.client_meta.conversation_id = conversationId",
            "auto_confirmation_sent: autoConfirmationSent",
            "attachments: attachments.map(item =>",
            "block_type: 10052",
            "submitted_with_images: Boolean(attachments && attachments.length)",
            'ability_param: JSON.stringify({ratio: ratio || "auto", model, duration: seconds})',
            "if (conversationId) body.messages = [textMessage]",
        ):
            self.assertIn(fragment, DOUBAO_SUBMIT_SCRIPT)
        self.assertIn("would you like|do you want|shall i|should i", DOUBAO_SUBMIT_SCRIPT)
        self.assertIn("是否|请问", DOUBAO_SUBMIT_SCRIPT)
        self.assertIn("生成视频：${promptText}，${ratioText}，${seconds}s", DOUBAO_SUBMIT_SCRIPT)
        self.assertIn("is_old_user: false", DOUBAO_SUBMIT_SCRIPT)
        self.assertIn("related_deleted_message_ids: {}", DOUBAO_SUBMIT_SCRIPT)
        self.assertNotIn("不要只回复文字", DOUBAO_SUBMIT_SCRIPT)
        self.assertIn("/alice/message/pre_handle_v2_without_conv", DOUBAO_PREHANDLE_ATTACHMENTS_SCRIPT)
        self.assertEqual(DOUBAO_MODEL_CODES["Seedance 2.0 Mini"], "seedance_v2.0_mini")
        self.assertEqual(DOUBAO_MODEL_CODES["Seedance 2.0 Fast"], "seedance_v2.0")

    def test_reference_upload_uses_captured_doubao_scene(self) -> None:
        page = SimpleNamespace(evaluate=AsyncMock(return_value={
            "ok": True,
            "status": 200,
            "json": {"code": 0, "data": {"service_id": "doubao-imagex"}},
        }))
        uploader = DoubaoReferenceImageUploader.__new__(DoubaoReferenceImageUploader)

        result = asyncio.run(uploader._prepare_image_upload(page))

        self.assertEqual(DOUBAO_PREPARE_UPLOAD_BODY, {"tenant_id": "5", "scene_id": "5", "resource_type": 2})
        self.assertEqual(result, {"service_id": "doubao-imagex"})
        self.assertEqual(page.evaluate.await_args.args[1], {"body": DOUBAO_PREPARE_UPLOAD_BODY})

    def test_submit_script_waits_for_generation_ack_before_accepting(self) -> None:
        for fragment in (
            'rawText.includes("视频生成已提交")',
            "textOnlySubmissionClaim(latestAttemptText)",
            "freshConversationRetryUsed",
            "generation_wait_message_detected: Boolean(detectedWaitMessage)",
            "accepted: Boolean(detectedWaitMessage || videoUrl)",
            "sameAccountResendCount >= maxResends",
            "setTimeout(resolve, resendDelayMs)",
            "const resendDelayMs = Math.max(5000, Number(retryDelayMs) || 15000)",
            "const maxResends = Math.max(0, Math.min(10",
            "performAttempt(resendPayload, fallbackConversationId)",
            "const retryInstruction = generationInstruction(prompt, true)",
            "conversationPayload(payload, conversationId, latestAttemptText, retryInstruction)",
        ):
            self.assertIn(fragment, DOUBAO_SUBMIT_SCRIPT)
        self.assertNotIn('accepted: text.includes("SSE_REPLY_END")', DOUBAO_SUBMIT_SCRIPT)

    def test_generation_acknowledgement_requires_structured_submission_marker(self) -> None:
        false_messages = (
            "本次使用 **Seedance 2.0 Fast** 生成，预计等待 5 分钟",
            "视频正在生成处理中，请稍作等待。",
            "已提交，正在渲染生成中，请耐心等待生成完成。",
            "提示词原样提交，正在渲染生成，请等待加载完成。",
            "已为你调用Seedance 2.0 Fast模型，视频正在生成处理中。",
            "已为你调用Seedance 2.0 Fast模型，生成中，请耐心等待。",
        )
        for message in false_messages:
            chunk = json.dumps({"text": message}, ensure_ascii=False)
            finish = json.dumps({"msg_finish_attr": {"brief": message}}, ensure_ascii=False)
            response = f"event: CHUNK_DELTA\ndata: {chunk}\n\nevent: SSE_REPLY_END\ndata: {finish}\n\n"
            with self.subTest(message=message):
                self.assertIn(message, extract_doubao_assistant_response_text(response))
                self.assertEqual(detect_doubao_generation_acknowledgement(response), "")
        marker_payload = {"message": {"user_type": 2, "content_block": [{"text": DOUBAO_SUBMISSION_MARKER}]}}
        marker_response = f"event: FULL_MSG_NOTIFY\ndata: {json.dumps(marker_payload, ensure_ascii=False)}\n\n"
        self.assertEqual(detect_doubao_generation_acknowledgement(marker_response), DOUBAO_SUBMISSION_MARKER)
        self.assertTrue(doubao_payload_has_submission_marker(marker_payload))
        self.assertFalse(doubao_payload_has_submission_marker({"message": {"user_type": 1, "text": DOUBAO_SUBMISSION_MARKER}}))

    def test_generation_acknowledgement_rejects_instructions_and_refusals(self) -> None:
        responses = (
            "当前对话页面不支持直接触发视频生成，请进入创作页面。",
            "我无法直接触发视频生成指令，请粘贴提示词后提交生成。",
            "这是一段可直接用于AI视频生成的分镜描述。",
            "需要我再调整光影强弱或者横竖屏版本吗？",
        )
        for response_text in responses:
            user_notice = json.dumps({"message": {"user_type": 1, "content": "视频正在生成处理中"}}, ensure_ascii=False)
            assistant = json.dumps({"text": response_text}, ensure_ascii=False)
            response = f"event: FULL_MSG_NOTIFY\ndata: {user_notice}\n\nevent: CHUNK_DELTA\ndata: {assistant}\n\n"
            with self.subTest(response=response_text):
                self.assertEqual(extract_doubao_assistant_response_text(response), response_text)
                self.assertEqual(detect_doubao_generation_acknowledgement(response), "")

    def test_video_creation_page_refusal_is_detected_from_assistant_response_only(self) -> None:
        responses = (
            "无法直接生成，请进入视频创作页面。",
            "我无法直接生成视频，请前往视频创作页面完成操作。",
            "当前对话页面不支持直接触发视频生成，请进入创作页面。",
            "当前无法直接渲染生成视频文件，你可以复制完整参数，在豆包视频创作入口提交生成。",
        )
        for response_text in responses:
            assistant = json.dumps({"text": response_text}, ensure_ascii=False)
            response = f"event: CHUNK_DELTA\ndata: {assistant}\n\n"
            with self.subTest(response=response_text):
                self.assertTrue(detect_doubao_video_creation_page_refusal(response))
        user_only = json.dumps({"message": {"user_type": 1, "content": responses[0]}}, ensure_ascii=False)
        self.assertEqual(detect_doubao_video_creation_page_refusal(f"event: FULL_MSG_NOTIFY\ndata: {user_only}\n\n"), "")

    def test_video_creation_page_refusal_switches_to_real_creation_page(self) -> None:
        error, category = classify_doubao_submission({
            "ok": True,
            "accepted": False,
            "video_creation_page_refusal_repeated": True,
        })

        self.assertEqual((error, category), ("豆包要求改用视频创作页面", "generation_ack_missing"))
        for fragment in (
            "videoCreationPageRefusal(latestAttemptText)",
            "if (creationPageRefusal || unverifiedTextClaim)",
            "video_creation_page_refusal_repeated: videoCreationPageRefusalRepeated",
        ):
            self.assertIn(fragment, DOUBAO_SUBMIT_SCRIPT)
        self.assertNotIn('conversationPayload(payload, "", latestAttemptText, generationInstruction(prompt))', DOUBAO_SUBMIT_SCRIPT)
        self.assertTrue(should_use_doubao_video_creation_page({"ok": True, "accepted": False}))
        self.assertFalse(should_use_doubao_video_creation_page({"ok": True, "accepted": True}))
        self.assertFalse(should_use_doubao_video_creation_page({"ok": True, "accepted": False, "service_frequent": True}))

    def test_conversation_id_is_extracted_from_ui_network_and_route(self) -> None:
        self.assertEqual(
            extract_doubao_conversation_id('{"conversation_id":"38436620180658434"}'),
            "38436620180658434",
        )
        self.assertEqual(
            extract_doubao_conversation_id("https://www.doubao.com/chat/38436620180658434"),
            "38436620180658434",
        )

    def test_python_acknowledgement_fallback_prevents_generation_retry(self) -> None:
        message = DOUBAO_SUBMISSION_MARKER
        response = "event: CHUNK_DELTA\ndata: " + json.dumps({"text": message}, ensure_ascii=False) + "\n\n"
        result = normalize_doubao_submission_acknowledgement({
            "ok": True,
            "accepted": False,
            "conversation_id": "38436620180658434",
            "initial_response_preview": response,
            "response_preview": response,
        })

        self.assertTrue(result["accepted"])
        self.assertTrue(result["generation_wait_message_detected"])
        self.assertEqual(result["generation_wait_message"], DOUBAO_SUBMISSION_MARKER)
        self.assertEqual(result["generation_ack_source"], "python_response_fallback")
        self.assertEqual(classify_doubao_submission(result), ("", ""))

    def test_missing_generation_acknowledgement_requests_account_switch(self) -> None:
        error, category = classify_doubao_submission({"ok": True, "accepted": False})

        self.assertEqual(error, "doubao generation acknowledgement missing")
        self.assertEqual(category, "generation_ack_missing")

    def test_verify_stream_error_is_classified_without_waiting_for_ack(self) -> None:
        response = 'event: STREAM_ERROR\ndata: {"error_code":710022004,"error_msg":"rate limited"}\n\n'
        result = normalize_doubao_submission_acknowledgement({
            "ok": True,
            "accepted": False,
            "response_preview": response,
        })

        self.assertTrue(result["slider_verification"])
        self.assertEqual(result["upstream_error_code"], "710022004")
        self.assertEqual(classify_doubao_submission(result), ("doubao verification required", "slider_verification"))

    def test_missing_video_settings_panel_never_uses_internal_request_fallback(self) -> None:
        runner = DoubaoVideoAutomation.__new__(DoubaoVideoAutomation)
        runner._submit_via_video_creation_page_ui = AsyncMock(return_value={
            "ok": False,
            "status": 0,
            "accepted": False,
            "video_creation_ui_error": "doubao video settings panel unavailable",
        })
        runner._submit_via_video_creation_internal_request = AsyncMock(return_value={
            "ok": True,
            "status": 200,
            "accepted": True,
            "conversation_id": "38436759004541698",
        })

        result = asyncio.run(runner._submit_via_video_creation_page(SimpleNamespace(), image_count=0))

        self.assertFalse(result["accepted"])
        self.assertEqual(
            classify_doubao_submission(result),
            ("doubao video settings panel unavailable", "video_creation_ui_error"),
        )
        runner._submit_via_video_creation_page_ui.assert_awaited_once()
        runner._submit_via_video_creation_internal_request.assert_not_awaited()

    def test_native_submit_without_network_request_stays_unconfirmed(self) -> None:
        runner = DoubaoVideoAutomation.__new__(DoubaoVideoAutomation)
        runner._submit_via_video_creation_page_ui = AsyncMock(return_value={
            "ok": True,
            "status": 200,
            "accepted": False,
            "native_submission_request_seen": False,
        })
        runner._submit_via_video_creation_internal_request = AsyncMock(return_value={
            "ok": True,
            "status": 200,
            "accepted": True,
            "conversation_id": "38436759004541698",
        })

        result = asyncio.run(runner._submit_via_video_creation_page(SimpleNamespace(), image_count=0))

        self.assertFalse(result["accepted"])
        self.assertEqual(classify_doubao_submission(result), ("doubao generation acknowledgement missing", "generation_ack_missing"))
        runner._submit_via_video_creation_internal_request.assert_not_awaited()

    def test_native_submit_with_network_request_never_duplicates_submission(self) -> None:
        runner = DoubaoVideoAutomation.__new__(DoubaoVideoAutomation)
        runner._submit_via_video_creation_page_ui = AsyncMock(return_value={
            "ok": True,
            "status": 200,
            "accepted": False,
            "native_submission_request_seen": True,
        })
        runner._submit_via_video_creation_internal_request = AsyncMock()

        result = asyncio.run(runner._submit_via_video_creation_page(SimpleNamespace(), image_count=0))

        self.assertFalse(result["accepted"])
        runner._submit_via_video_creation_internal_request.assert_not_awaited()

    def test_text_only_video_response_is_classified_for_account_quarantine(self) -> None:
        responses = (
            "很抱歉，我目前没办法直接生成视频文件。不过给你两段可以直接用于 AI 视频生成工具的提示词。",
            "版本 1（卡通梦幻风格）提示词如下。版本 2（奇幻写实风格）提示词如下。你可以把提示词导入剪映 AI、可灵 AI 等视频生成工具。",
            "我无法为你直接生成视频文件，请复制到 AI 视频生成工具。",
            "以下是 AI 视频提示词，提示词如下：镜头从城市上空缓慢推进。",
        )
        for response in responses:
            with self.subTest(response=response):
                self.assertTrue(is_doubao_text_only_video_response(response))
        for response in (
            "视频生成已提交\n本次使用 Seedance 2.0 Fast 生成。",
            "本次使用 Seedance 2.0 Fast 生成，预计等待 5 分钟。",
            "请生成两个版本的视频，提示词中保留角色一致性。",
        ):
            with self.subTest(response=response):
                self.assertFalse(is_doubao_text_only_video_response(response))
        self.assertEqual(
            classify_doubao_submission({"ok": True, "accepted": False, "text_only_response": True}),
            ("豆包仅返回文本，未提交视频生成", "text_only_response"),
        )

    def test_reference_upload_progress_must_disappear_before_submission(self) -> None:
        for text in ("参考图上传 29%", "76%", "100%", "正在上传", "图片处理中"):
            with self.subTest(text=text):
                self.assertTrue(doubao_reference_upload_progress_visible(text))
        self.assertFalse(doubao_reference_upload_progress_visible("参考图已上传完成"))

    def test_quota_exhaustion_is_a_terminal_submission_signal(self) -> None:
        for text in ("今日额度不足", "视频生成额度已用完", "当前剩余 0 次", "今日视频生成免费次数用完了"):
            self.assertTrue(is_doubao_account_quota_insufficient(text))
        error, category = classify_doubao_submission({"ok": True, "quota_insufficient": True})
        self.assertEqual((error, category), ("豆包账号额度不足或已耗尽", "quota_insufficient"))
        self.assertIn("quota_insufficient: quotaInsufficient(text)", DOUBAO_SUBMIT_SCRIPT)
        self.assertIn("return quotaInsufficient(text)", DOUBAO_SUBMIT_SCRIPT)

    def test_ui_acknowledgement_requires_marker_and_selected_model(self) -> None:
        notice = (
            "视频生成已提交\n"
            "本次使用 Seedance 2.0 Fast 生成，预计等待 5 分钟。"
            "视频生成好后，我会主动发送给你。本次生成将消耗每日免费额度。"
        )
        self.assertTrue(doubao_ui_generation_acknowledged(notice, "Seedance 2.0 Fast"))
        self.assertTrue(doubao_ui_generation_acknowledged(
            "视频生成已提交\n本次使用 Seedance 2.0 Fast 生成，请稍候查看结果。",
            "Seedance 2.0 Fast",
        ))
        self.assertFalse(doubao_ui_generation_acknowledged(notice, "Seedance 2.0 Mini"))
        self.assertFalse(doubao_ui_generation_acknowledged("视频生成已提交", "Seedance 2.0 Fast"))

    def test_runtime_submission_uses_native_video_page_controls(self) -> None:
        source = inspect.getsource(DoubaoVideoAutomation._run_browser)
        self.assertIn("_submit_via_video_creation_page", source)
        self.assertNotIn("DOUBAO_SUBMIT_SCRIPT", source)
        self.assertNotIn("page.evaluate", source)
        request_source = inspect.getsource(DoubaoVideoAutomation._submit_via_video_creation_page)
        self.assertIn("_submit_via_video_creation_page_ui", request_source)
        self.assertIn("_resolve_submission_verification", request_source)
        self.assertIn("verification_recovery_attempted", request_source)
        ui_source = inspect.getsource(DoubaoVideoAutomation._submit_via_video_creation_page_ui)
        self.assertIn("await page.goto(DOUBAO_URL", ui_source)
        self.assertIn('name="AI 创作"', ui_source)
        self.assertNotIn('name="新对话"', ui_source)
        self.assertNotIn('name="视频生成"', ui_source)
        self.assertIn("editor.fill", ui_source)
        self.assertIn("send_button.click", ui_source)
        self.assertIn("select_video_setting", ui_source)
        self.assertIn("submission_request_seen", ui_source)
        self.assertIn('editor.press("Enter")', ui_source)
        self.assertIn('await editor.fill("生成")', ui_source)
        self.assertNotIn("_submit_via_video_creation_internal_request", request_source)

    def test_direct_video_is_an_accepted_submission(self) -> None:
        error, category = classify_doubao_submission({"ok": True, "accepted": True, "video_url": "https://example.com/video.mp4"})

        self.assertEqual((error, category), ("", ""))

    def test_submit_script_returns_interface_poll_identity(self) -> None:
        for fragment in ("web_id: webId", "region,", "conversation_id: conversationId"):
            self.assertIn(fragment, DOUBAO_SUBMIT_SCRIPT)

    def test_interface_result_parser_extracts_completed_video(self) -> None:
        url = "https://media.example/generated.mp4?mime_type=video_mp4"
        encoded = base64.b64encode(url.encode()).decode()
        body = json.dumps({
            "downlink_body": {
                "pull_singe_chain_downlink_body": {
                    "messages": [{"message_index": 5, "tts_content": "生成完成", "video_model": {"main_url": encoded}}]
                }
            }
        })

        result = parse_doubao_generation_result(body)

        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["candidate"]["url"], url)
        self.assertEqual(result["candidate"]["source"], "single_chain")

    def test_interface_result_parser_recognizes_quota_exhaustion(self) -> None:
        body = json.dumps({
            "downlink_body": {
                "pull_singe_chain_downlink_body": {
                    "messages": [{"message_index": 5, "tts_content": "今日视频生成额度已耗尽，无法生成该视频"}]
                }
            }
        })

        result = parse_doubao_generation_result(body)

        self.assertEqual(result["state"], "quota_insufficient")
        self.assertIn("额度已耗尽", result["text"])

    def test_interface_result_parser_recognizes_policy_rejection_as_terminal(self) -> None:
        body = json.dumps({
            "downlink_body": {
                "pull_singe_chain_downlink_body": {
                    "messages": [{
                        "message_index": 5,
                        "tts_content": "生成内容中疑似包含侵权/违规内容，无法返回该内容，换个主题再试试，生成额度未扣除。",
                    }]
                }
            }
        }, ensure_ascii=False)

        result = parse_doubao_generation_result(body)

        self.assertEqual(result["state"], "failed")
        self.assertIn("无法返回该内容", result["text"])

    def test_interface_result_parser_requires_real_submission_marker(self) -> None:
        text_only = json.dumps({
            "downlink_body": {
                "pull_singe_chain_downlink_body": {
                    "messages": [{"message_index": 5, "user_type": 2, "tts_content": "已提交，正在渲染处理中"}]
                }
            }
        }, ensure_ascii=False)
        structured = json.dumps({
            "downlink_body": {
                "pull_singe_chain_downlink_body": {
                    "messages": [{
                        "message_index": 5,
                        "user_type": 2,
                        "tts_content": "预计等待 5 分钟",
                        "content_block": [{"text": DOUBAO_SUBMISSION_MARKER}],
                    }]
                }
            }
        }, ensure_ascii=False)

        self.assertEqual(parse_doubao_generation_result(text_only)["state"], "submission_unconfirmed")
        self.assertEqual(parse_doubao_generation_result(structured)["state"], "generating")

    def test_unwatermarked_fallback_url_replaces_required_parameters(self) -> None:
        value = unwatermarked_fallback_url(
            "https://video.example/fallback?id=123&channel=old&codec_type=2&logo_type=video_gen_watermark_dyn"
        )

        query = parse_qs(urlsplit(value).query)
        self.assertEqual(query["id"], ["123"])
        self.assertEqual(query["channel"], ["no"])
        self.assertEqual(query["codec_type"], ["8"])
        self.assertEqual(query["logo_type"], ["unwatermarked"])

    def test_fallback_api_is_only_read_from_latest_message(self) -> None:
        payload = {
            "downlink_body": {
                "pull_singe_chain_downlink_body": {
                    "messages": [
                        {"message_index": 8, "video_model": {"fallback_api": "https://video.example/old"}},
                        {"message_index": 9, "video_model": {"main_url": "latest"}},
                    ]
                }
            }
        }

        self.assertEqual(extract_doubao_fallback_apis(payload), [])

    def test_decode_qaab_url_uses_key_seed_for_aes_cbc(self) -> None:
        url = "https://media.example/original.mp4?signature=abc"
        seed = bytes(range(32))
        digest1 = hashlib.sha512(seed).digest()
        digest2 = hashlib.sha512(digest1 + QAAB_SALT).digest()
        key, iv = digest2[:16], digest2[16:32]
        padder = padding.PKCS7(algorithms.AES.block_size).padder()
        padded = padder.update(url.encode()) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        token = base64.b64encode(b"\xa8\x00\x01\x00" + encrypted).decode().rstrip("=")
        key_seed = base64.b64encode(seed).decode().rstrip("=")

        self.assertTrue(token.startswith("qAAB"))
        self.assertEqual(decode_qaab_url(token, key_seed), url)

    def test_fallback_payload_selects_highest_quality_video(self) -> None:
        low = "https://media.example/low.mp4"
        high = "https://media.example/high.mp4"
        payload = {
            "video_info": {
                "data": {
                    "video_list": {
                        "video_1": {
                            "main_url": base64.b64encode(low.encode()).decode(),
                            "width": 640,
                            "height": 360,
                            "bitrate": 100,
                        },
                        "video_2": {
                            "main_url": base64.b64encode(high.encode()).decode(),
                            "width": 1920,
                            "height": 1080,
                            "bitrate": 1000,
                        },
                    }
                }
            }
        }

        self.assertEqual(fallback_payload_video_url(payload), high)

    def test_fallback_payload_uses_lower_quality_backup_when_best_token_cannot_decode(self) -> None:
        backup = "https://media.example/backup-original.mp4"
        payload = {
            "video_info": {
                "data": {
                    "video_list": {
                        "video_1": {"main_url": "invalid-token", "width": 1920, "height": 1080},
                        "video_2": {"main_url": base64.b64encode(backup.encode()).decode(), "width": 1280, "height": 720},
                    }
                }
            }
        }

        self.assertEqual(fallback_payload_video_url(payload), backup)

    def test_interface_fetch_prefers_unwatermarked_fallback_response(self) -> None:
        watermarked = "https://media.example/watermarked.mp4?lr=video_gen_watermark_dyn"
        original = "https://media.example/original.mp4"
        fallback_api = "https://video.example/fallback?id=123&logo_type=video_gen_watermark_dyn"
        single_body = json.dumps({
            "downlink_body": {
                "pull_singe_chain_downlink_body": {
                    "messages": [{
                        "message_index": 5,
                        "tts_content": "鐢熸垚瀹屾垚",
                        "video_model": {
                            "main_url": base64.b64encode(watermarked.encode()).decode(),
                            "fallback_api": fallback_api,
                        },
                    }]
                }
            }
        }).encode()
        fallback_body = json.dumps({
            "video_info": {
                "data": {
                    "video_list": {
                        "video_1": {"main_url": base64.b64encode(original.encode()).decode(), "width": 1920, "height": 1080}
                    }
                }
            }
        }).encode()

        class Response:
            def __init__(self, content: bytes):
                self.content = content

            def raise_for_status(self) -> None:
                return None

        class Client:
            def __init__(self):
                self.fallback_url = ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                return Response(single_body)

            async def get(self, url, **_kwargs):
                self.fallback_url = url
                return Response(fallback_body)

        client = Client()
        with patch("app.doubao_automation.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_doubao_generation_result("session=value", "123456789"))

        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["candidate"]["url"], original)
        self.assertEqual(result["candidate"]["source"], "fallback_unwatermarked")
        self.assertEqual(result["candidate"]["watermark_status"], "original")
        query = parse_qs(urlsplit(client.fallback_url).query)
        self.assertEqual(query["logo_type"], ["unwatermarked"])
        self.assertEqual(query["codec_type"], ["8"])

    def test_interface_fetch_tries_all_unwatermarked_fallback_apis(self) -> None:
        original = "https://media.example/second-fallback-original.mp4"
        single_body = json.dumps({
            "downlink_body": {
                "pull_singe_chain_downlink_body": {
                    "messages": [{
                        "message_index": 5,
                        "video_model": json.dumps({
                            "main_url": base64.b64encode(b"https://media.example/watermarked.mp4").decode(),
                            "fallback_api": ["https://video.example/first?id=1", "https://video.example/second?id=2"],
                        }),
                    }]
                }
            }
        }).encode()
        fallback_body = json.dumps({
            "video_info": {"data": {"video_list": {"video_1": {"main_url": base64.b64encode(original.encode()).decode()}}}}
        }).encode()

        class Response:
            def __init__(self, content: bytes):
                self.content = content

            def raise_for_status(self) -> None:
                return None

        class Client:
            def __init__(self):
                self.urls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                return Response(single_body)

            async def get(self, url, **_kwargs):
                self.urls.append(url)
                return Response(b"not-json" if len(self.urls) == 1 else fallback_body)

        client = Client()
        with patch("app.doubao_automation.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_doubao_generation_result("session=value", "123456789"))

        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["candidate"]["url"], original)
        self.assertEqual(result["unwatermarked_attempts"], 2)
        self.assertEqual(result["unwatermarked_errors"], ["fallback_invalid_response"])
        self.assertEqual(len(client.urls), 2)

    def test_interface_fetch_uses_explicit_unwatermarked_backup_field(self) -> None:
        watermarked = "https://media.example/watermarked.mp4"
        original = "https://media.example/explicit-original.mp4"
        body = json.dumps({
            "downlink_body": {
                "pull_singe_chain_downlink_body": {
                    "messages": [{
                        "message_index": 5,
                        "video_model": {
                            "main_url": base64.b64encode(watermarked.encode()).decode(),
                            "original_download_url": base64.b64encode(original.encode()).decode(),
                        },
                    }]
                }
            }
        }).encode()

        class Response:
            content = body

            def raise_for_status(self) -> None:
                return None

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                return Response()

        with patch("app.doubao_automation.httpx.AsyncClient", return_value=Client()):
            result = asyncio.run(fetch_doubao_generation_result("session=value", "123456789"))

        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["candidate"]["url"], original)
        self.assertEqual(result["candidate"]["source"], "single_chain_explicit_unwatermarked")
        self.assertEqual(result["candidate"]["watermark_status"], "original")

    def test_interface_fetch_keeps_polling_when_only_watermarked_single_chain_exists(self) -> None:
        url = "https://media.example/watermarked.mp4?lr=video_gen_watermark_dyn"
        body = json.dumps({
            "downlink_body": {
                "pull_singe_chain_downlink_body": {
                    "messages": [{"message_index": 5, "video_model": {"main_url": base64.b64encode(url.encode()).decode()}}]
                }
            }
        }).encode()

        class Response:
            content = body

            def raise_for_status(self) -> None:
                return None

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                return Response()

        with patch("app.doubao_automation.httpx.AsyncClient", return_value=Client()):
            result = asyncio.run(fetch_doubao_generation_result("session=value", "123456789"))

        self.assertEqual(result["state"], "awaiting_unwatermarked")
        self.assertEqual(result["watermarked_candidate"]["url"], url)
        self.assertEqual(result["unwatermarked_status"], "fallback_api_missing")
        self.assertNotIn("candidate", result)

    def test_interface_result_parser_recognizes_rate_limit_without_failing_task(self) -> None:
        result = parse_doubao_generation_result('{"code":710022002,"message":"当前服务访问频繁"}')

        self.assertEqual(result, {"state": "rate_limited", "text": "豆包当前服务访问频繁"})

    def test_confirmed_conversation_releases_browser_for_interface_polling(self) -> None:
        acknowledgement = DOUBAO_SUBMISSION_MARKER
        acknowledgement_response = (
            "event: CHUNK_DELTA\ndata: "
            + json.dumps({"text": acknowledgement}, ensure_ascii=False)
            + "\n\n"
        )
        body = SimpleNamespace(inner_text=AsyncMock(return_value="豆包已登录"))
        page = SimpleNamespace(
            url="https://www.doubao.com/chat/",
            goto=AsyncMock(),
            wait_for_timeout=AsyncMock(),
            locator=Mock(return_value=body),
            evaluate=AsyncMock(return_value={
                "ok": True,
                "status": 200,
                "accepted": False,
                "conversation_id": "12345678901234567",
                "web_id": "22345678901234567",
                "region": "JP",
                "generation_wait_message_detected": False,
                "generation_wait_message": "",
                "initial_response_preview": acknowledgement_response,
                "response_preview": acknowledgement_response,
                "main_url": "https://media.example/browser-watermarked.mp4",
            }),
        )
        context = SimpleNamespace(pages=[page], add_init_script=AsyncMock(), close=AsyncMock())

        @asynccontextmanager
        async def runtime():
            yield SimpleNamespace()

        pool = SimpleNamespace(playwright_context=Mock(side_effect=runtime))
        runner = DoubaoVideoAutomation.__new__(DoubaoVideoAutomation)
        runner.task_id = "doubao-task"
        runner.prompt = "测试视频"
        runner.ratio = "9:16"
        runner.model = "Seedance 2.0 Mini"
        runner.duration = 10
        runner.account = {"id": "account-1", "name": "豆包账号", "quota_charge_id": "charge-1"}
        runner.browser_pool = pool
        runner.submission_pacer = AsyncMock()
        runner.settings = SimpleNamespace(
            browser_executable_path="",
            headless=True,
            doubao_submit_retry_limit=2,
        )
        runner._login_required = AsyncMock(return_value=False)
        runner._refresh_cookies = AsyncMock(return_value=[{"name": "session", "value": "value"}])
        runner._context_storage_state = Mock(return_value=None)
        runner._launch_persistent_context = AsyncMock(return_value=context)
        runner._save_video_success = AsyncMock()
        runner._submit_via_video_creation_page = AsyncMock(return_value={
            "ok": True,
            "status": 200,
            "accepted": True,
            "conversation_id": "12345678901234567",
            "web_id": "22345678901234567",
            "region": "JP",
            "generation_wait_message_detected": True,
            "generation_wait_message": acknowledgement,
            "generation_ack_source": "video_creation_page_dom",
            "video_creation_page_used": True,
        })

        with patch("app.doubao_automation.task_exists", return_value=False), patch(
            "app.doubao_automation.begin_task_submission", return_value=True
        ), patch("app.doubao_automation.mark_submitted") as mark_submitted, patch(
            "app.doubao_automation.save_result"
        ) as save_result:
            outcome = asyncio.run(runner._run_browser(None))

        self.assertTrue(outcome["success"])
        self.assertTrue(outcome["confirmation_pending"])
        self.assertTrue(outcome["keep_account_claimed"])
        runner._submit_via_video_creation_page.assert_awaited_once_with(page, image_count=0)
        page.evaluate.assert_not_awaited()
        mark_submitted.assert_called_once_with("doubao-task", result_poll_delay_seconds=20)
        self.assertTrue(any(call.kwargs["extra"].get("doubao_result_mode") == "interface_poll" for call in save_result.call_args_list))
        self.assertTrue(any(call.kwargs["extra"].get("doubao_generation_ack_source") == "video_creation_page_dom" for call in save_result.call_args_list))
        runner._save_video_success.assert_not_awaited()
        runner._launch_persistent_context.assert_awaited_once()
        context.close.assert_awaited_once()

    def test_missing_direct_ack_uses_creation_page_then_releases_browser(self) -> None:
        body = SimpleNamespace(inner_text=AsyncMock(return_value="豆包已登录"))
        page = SimpleNamespace(
            url="https://www.doubao.com/chat/",
            goto=AsyncMock(),
            wait_for_timeout=AsyncMock(),
            locator=Mock(return_value=body),
            evaluate=AsyncMock(return_value={
                "ok": True,
                "status": 200,
                "accepted": False,
                "conversation_id": "",
                "web_id": "22345678901234567",
                "region": "JP",
                "response_preview": "请进入豆包视频创作入口提交生成",
            }),
        )
        context = SimpleNamespace(pages=[page], add_init_script=AsyncMock(), close=AsyncMock())

        @asynccontextmanager
        async def runtime():
            yield SimpleNamespace()

        pool = SimpleNamespace(playwright_context=Mock(side_effect=runtime))
        runner = DoubaoVideoAutomation.__new__(DoubaoVideoAutomation)
        runner.task_id = "doubao-task"
        runner.prompt = "测试视频"
        runner.ratio = "16:9"
        runner.model = "Seedance 2.0 Fast"
        runner.duration = 10
        runner.account = {"id": "account-1", "name": "豆包账号", "quota_charge_id": "charge-1"}
        runner.browser_pool = pool
        runner.submission_pacer = AsyncMock()
        runner.settings = SimpleNamespace(
            browser_executable_path="",
            headless=True,
            doubao_submit_retry_limit=2,
        )
        runner._login_required = AsyncMock(return_value=False)
        runner._refresh_cookies = AsyncMock(return_value=[{"name": "session", "value": "value"}])
        runner._context_storage_state = Mock(return_value=None)
        runner._launch_persistent_context = AsyncMock(return_value=context)
        runner._submit_via_video_creation_page = AsyncMock(return_value={
            "ok": True,
            "status": 200,
            "accepted": True,
            "conversation_id": "38436620180658434",
            "generation_wait_message_detected": True,
            "generation_wait_message": DOUBAO_SUBMISSION_MARKER,
            "generation_ack_source": "video_creation_page_dom",
            "video_creation_page_used": True,
            "video_creation_selected_model": "Seedance 2.0 Fast",
            "video_creation_selected_ratio": "16:9",
            "video_creation_selected_duration": 10,
        })

        with patch("app.doubao_automation.task_exists", return_value=False), patch(
            "app.doubao_automation.begin_task_submission", return_value=True
        ), patch("app.doubao_automation.mark_submitted") as mark_submitted, patch(
            "app.doubao_automation.save_result"
        ) as save_result:
            outcome = asyncio.run(runner._run_browser(None))

        self.assertTrue(outcome["success"])
        self.assertTrue(outcome["confirmation_pending"])
        runner._submit_via_video_creation_page.assert_awaited_once_with(page, image_count=0)
        mark_submitted.assert_called_once_with("doubao-task", result_poll_delay_seconds=20)
        saved = [call.kwargs["extra"] for call in save_result.call_args_list]
        self.assertTrue(any(item.get("doubao_video_creation_page_used") for item in saved))
        self.assertTrue(any(item.get("doubao_result_mode") == "interface_poll" for item in saved))
        self.assertTrue(any(item.get("doubao_generation_ack_source") == "video_creation_page_dom" for item in saved))
        runner._launch_persistent_context.assert_awaited_once()
        context.close.assert_awaited_once()

    def test_context_storage_state_merges_saved_state_and_latest_account_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "doubao.json"
            state_path.write_text(
                json.dumps({
                    "cookies": [
                        {"name": "session", "value": "old", "domain": ".doubao.com", "path": "/"},
                        {"name": "saved_only", "value": "keep", "domain": ".doubao.com", "path": "/"},
                    ],
                    "origins": [{"origin": "https://www.doubao.com", "localStorage": [{"name": "device", "value": "known"}]}],
                }),
                encoding="utf-8",
            )
            runner = DoubaoVideoAutomation.__new__(DoubaoVideoAutomation)
            runner.state_path = state_path
            runner.account = {
                "cookies": [
                    {"name": "session", "value": "latest", "domain": ".doubao.com", "path": "/"},
                    {"name": "account_only", "value": "new", "domain": ".doubao.com", "path": "/"},
                ]
            }

            state = runner._context_storage_state()

        self.assertIsNotNone(state)
        cookies = {item["name"]: item["value"] for item in state["cookies"]}
        self.assertEqual(cookies, {"session": "latest", "saved_only": "keep", "account_only": "new"})
        self.assertEqual(state["origins"][0]["localStorage"][0]["value"], "known")

    def test_context_refresh_persists_indexed_db(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = DoubaoVideoAutomation.__new__(DoubaoVideoAutomation)
            runner.state_path = Path(directory) / "doubao.json"
            runner.account = {"id": "account-1"}
            context = SimpleNamespace(
                cookies=AsyncMock(return_value=[{"name": "session", "value": "fresh"}]),
                storage_state=AsyncMock(),
            )

            with patch("app.doubao_automation.update_account_cookies") as update:
                cookies = asyncio.run(runner._refresh_cookies(context))

        self.assertEqual(cookies, [{"name": "session", "value": "fresh"}])
        update.assert_called_once_with("account-1", cookies)
        context.storage_state.assert_awaited_once_with(path=str(runner.state_path), indexed_db=True)

    def test_persistent_context_uses_account_profile_and_merges_saved_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = DoubaoVideoAutomation.__new__(DoubaoVideoAutomation)
            runner.task_id = "task"
            runner.profile_path = root / "profile"
            runner.profile_path.mkdir()
            (runner.profile_path / "SingletonLock").write_text("stale", encoding="utf-8")
            runner.state_path = root / "state.json"
            runner.state_path.write_text(
                json.dumps({
                    "cookies": [{"name": "saved", "value": "one", "domain": ".doubao.com", "path": "/"}],
                    "origins": [{"origin": "https://www.doubao.com", "localStorage": [{"name": "device", "value": "known"}]}],
                }),
                encoding="utf-8",
            )
            runner.account = {
                "id": "account-1",
                "cookies": [{"name": "session", "value": "latest", "domain": ".doubao.com", "path": "/"}],
            }
            runner.settings = SimpleNamespace(headless=True)
            context = SimpleNamespace(add_cookies=AsyncMock(), add_init_script=AsyncMock())
            launch = AsyncMock(return_value=context)
            playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

            with patch("app.doubao_automation.task_exists", return_value=False):
                returned = asyncio.run(runner._launch_persistent_context(
                    playwright,
                    executable_path="/usr/bin/chromium",
                    proxy_config={"server": "http://proxy.example:8080"},
                    browser_args=["--no-sandbox"],
                    context_options={"locale": "zh-CN"},
                ))

            self.assertIs(returned, context)
            self.assertEqual(launch.call_args.args[0], str(runner.profile_path))
            self.assertEqual(launch.call_args.kwargs["proxy"], {"server": "http://proxy.example:8080"})
            self.assertTrue(launch.call_args.kwargs["headless"])
            merged = {item["name"]: item["value"] for item in context.add_cookies.call_args.args[0]}
            self.assertEqual(merged, {"saved": "one", "session": "latest"})
            self.assertIn("device", context.add_init_script.call_args.args[0])
            self.assertFalse((runner.profile_path / "SingletonLock").exists())

    def test_video_response_recognizes_doubao_media_url(self) -> None:
        response = SimpleNamespace(
            url="https://v6-default.douyin.com/video/path?mime_type=video_mp4",
            headers={"content-type": "video/mp4"},
        )
        self.assertEqual(DoubaoVideoAutomation._response_video_url(response), response.url)
        self.assertEqual(
            DoubaoVideoAutomation._response_video_url(SimpleNamespace(url="https://example.com/cover.png", headers={"content-type": "image/png"})),
            "",
        )

    def test_doubao_prefers_original_download_url_over_watermarked_preview(self) -> None:
        preview = "https://media.example/preview-watermark.mp4?watermark=1"
        playback = "https://media.example/play.mp4"
        original = "https://media.example/original.mp4"
        candidates = {}

        collect_doubao_video_candidates(
            {
                "preview_video_url": preview,
                "video_url": playback,
                "download_url_without_watermark": original,
            },
            candidates,
        )
        selected = best_doubao_video_candidate(candidates)

        self.assertEqual(selected["url"], original)
        self.assertGreaterEqual(selected["score"], DOUBAO_ORIGINAL_VIDEO_SCORE)
        self.assertLess(doubao_video_url_score(preview, "preview_video_url"), doubao_video_url_score(playback, "video_url"))

    def test_generic_download_and_player_urls_are_not_treated_as_original(self) -> None:
        self.assertLess(
            doubao_video_url_score("https://media.example/download.mp4", "download_url"),
            DOUBAO_ORIGINAL_VIDEO_SCORE,
        )
        self.assertLess(
            doubao_video_url_score("https://media.example/player.mp4", "dom.video_current_src"),
            DOUBAO_ORIGINAL_VIDEO_SCORE,
        )

    def test_single_chain_script_uses_dola_original_video_route(self) -> None:
        for fragment in (
            "/im/chain/single?",
            "cmd: 3100",
            "pull_singe_chain_uplink_body",
            'aid: "497858"',
            '"agw-js-conv": "str"',
        ):
            self.assertIn(fragment, DOUBAO_SINGLE_CHAIN_SCRIPT)

    def test_single_chain_candidate_uses_latest_message_main_url(self) -> None:
        old_url = "https://media.example/old-original.mp4"
        latest_url = "https://media.example/latest-original.mp4"
        body = json.dumps({
            "downlink_body": {
                "pull_singe_chain_downlink_body": {
                    "messages": [
                        {"message_index": 2, "video_model": {"main_url": old_url}},
                        {"message_index": 4, "video_model": {"main_url": latest_url}},
                    ]
                }
            }
        })
        page = SimpleNamespace(evaluate=AsyncMock(return_value={"ok": True, "status": 200, "body": body}))
        candidates = {}

        error = asyncio.run(DoubaoVideoAutomation._fetch_single_chain_candidates(page, "123456789012345", candidates))

        self.assertEqual(error, "")
        self.assertEqual(set(candidates), {latest_url})
        selected = best_doubao_video_candidate(candidates)
        self.assertEqual(selected["source"], "single_chain")
        self.assertEqual(selected["key"], "video_model.main_url")
        self.assertGreaterEqual(selected["score"], DOUBAO_ORIGINAL_VIDEO_SCORE)

    def test_doubao_collects_nested_original_url_without_mp4_suffix(self) -> None:
        original = "https://tos.example/video/source/12345?signature=abc"
        candidates = {}

        collect_doubao_video_candidates(
            {"video": {"origin": {"source_url": original}}},
            candidates,
        )

        self.assertIn(original, candidates)
        self.assertGreaterEqual(candidates[original]["score"], DOUBAO_ORIGINAL_VIDEO_SCORE)

    def test_doubao_does_not_treat_original_cover_as_video(self) -> None:
        cover = "https://media.example/original-cover.webp"
        video = "https://media.example/play.mp4"
        candidates = {}

        collect_doubao_video_candidates(
            {"video": {"origin_cover_url": cover, "play_url": video}},
            candidates,
        )

        self.assertNotIn(cover, candidates)
        self.assertEqual(best_doubao_video_candidate(candidates)["url"], video)

    def test_doubao_response_json_collects_original_video_candidate(self) -> None:
        runner = self.runner(SimpleNamespace())
        candidates = {}
        response = SimpleNamespace(
            request=SimpleNamespace(resource_type="xhr"),
            status=200,
            url="https://www.doubao.com/chat/conversation/detail",
            headers={"content-type": "application/json"},
            text=AsyncMock(return_value=json.dumps({
                "video": {
                    "play_url": "https://media.example/watermark.mp4?watermark=1",
                    "original_download_url": "https://media.example/original.mp4",
                }
            })),
        )

        asyncio.run(runner._capture_video_candidates(response, candidates))

        self.assertEqual(best_doubao_video_candidate(candidates)["url"], "https://media.example/original.mp4")

    def test_doubao_response_sse_preserves_main_url_field_priority(self) -> None:
        original = "https://media.example/generated.mp4"
        candidates = {}

        collect_doubao_response_candidates(
            f'event: message\ndata: {{"video_model":{{"main_url":"{original}"}}}}\n\n',
            candidates,
        )

        selected = best_doubao_video_candidate(candidates)
        self.assertEqual(selected["url"], original)
        self.assertGreaterEqual(selected["score"], DOUBAO_ORIGINAL_VIDEO_SCORE)

    def test_page_video_url_reads_current_src_and_source_children(self) -> None:
        video_locator = SimpleNamespace(evaluate_all=AsyncMock(return_value=["https://media.example/result.mp4"]))
        empty_locator = SimpleNamespace(evaluate_all=AsyncMock(return_value=[]))
        page = SimpleNamespace(locator=Mock(side_effect=lambda selector: video_locator if selector == "video" else empty_locator))

        url, source = asyncio.run(DoubaoVideoAutomation._page_video_url(page, []))

        self.assertEqual(url, "https://media.example/result.mp4")
        self.assertEqual(source, "video_current_src")

    def test_web_video_candidate_is_never_accepted_as_success(self) -> None:
        candidate = {
            "url": "https://media.example/page-watermarked.mp4",
            "source": "video_current_src",
            "score": 30,
        }

        self.assertFalse(doubao_video_candidate_is_acceptable(candidate))

    def test_completed_video_poster_activates_player_wrapper(self) -> None:
        poster = SimpleNamespace(click=AsyncMock())
        posters = SimpleNamespace(count=AsyncMock(return_value=1), last=poster)
        wrapper = SimpleNamespace(click=AsyncMock())
        wrappers = SimpleNamespace(count=AsyncMock(return_value=1), last=wrapper)
        page = SimpleNamespace(
            locator=Mock(side_effect=lambda selector: posters if selector.startswith("img") else wrappers),
            wait_for_timeout=AsyncMock(),
        )

        activated = asyncio.run(DoubaoVideoAutomation._activate_completed_video(page))

        self.assertTrue(activated)
        wrapper.click.assert_awaited_once_with(force=True, timeout=5000)
        poster.click.assert_not_awaited()
        page.wait_for_timeout.assert_awaited_once_with(1500)
        self.assertGreaterEqual(DOUBAO_RESULT_WAIT_SECONDS, 600)

    def test_video_success_writes_shared_task_video_result(self) -> None:
        runner = self.runner(SimpleNamespace())
        runner._refresh_cookies = AsyncMock()
        context = SimpleNamespace()
        page = SimpleNamespace(url="https://www.doubao.com/chat/123456789012345678")

        with patch("app.doubao_automation.save_result") as save, patch("app.doubao_automation.mark_success") as mark_success:
            outcome = asyncio.run(
                runner._save_video_success(
                    context,
                    page,
                    "https://media.example/result.mp4",
                    "fallback_unwatermarked",
                    score=900,
                    candidate_key="video_model.fallback_api",
                )
            )

        self.assertTrue(outcome["success"])
        runner._refresh_cookies.assert_awaited_once_with(context)
        self.assertEqual(save.call_args.kwargs["extra"]["decoded_main_url"], "https://media.example/result.mp4")
        self.assertEqual(save.call_args.kwargs["extra"]["doubao_video_detection_source"], "fallback_unwatermarked")
        self.assertEqual(save.call_args.kwargs["extra"]["doubao_result_source"], "fallback_unwatermarked")
        self.assertEqual(save.call_args.kwargs["extra"]["doubao_watermark_status"], "original")
        mark_success.assert_called_once_with("doubao-task")


class QianwenProxyAutomationTests(unittest.TestCase):
    def test_shared_proxy_is_passed_to_qianwen_browser_and_released(self) -> None:
        proxy = {"server": "http://proxy.example:18080"}
        session = SimpleNamespace(acquire_browser_proxy=AsyncMock(return_value=proxy), release_browser_proxy=AsyncMock())
        runner = QianwenVideoAutomation.__new__(QianwenVideoAutomation)
        runner.proxy_session = session
        runner._run_browser = AsyncMock(return_value={"success": True})

        outcome = asyncio.run(runner._run_profile())

        self.assertTrue(outcome["success"])
        runner._run_browser.assert_awaited_once_with(proxy)
        session.acquire_browser_proxy.assert_awaited_once()
        session.release_browser_proxy.assert_awaited_once()

    def test_qianwen_duration_accepts_supported_video_lengths(self) -> None:
        runner = QianwenVideoAutomation.__new__(QianwenVideoAutomation)
        control = SimpleNamespace(count=AsyncMock(return_value=0))
        filtered = SimpleNamespace(last=control)
        page = SimpleNamespace(locator=Mock(return_value=SimpleNamespace(filter=Mock(return_value=filtered))))
        runner._select_video_setting = AsyncMock(return_value=False)
        for duration in (5, 10, 15):
            runner.duration = duration
            self.assertFalse(asyncio.run(runner._ensure_video_duration(page)))
        self.assertEqual(runner._select_video_setting.await_count, 3)

        runner.duration = 20
        self.assertFalse(asyncio.run(runner._ensure_video_duration(page)))
        self.assertEqual(runner._select_video_setting.await_count, 3)

    def test_qianwen_duration_clicks_enabled_outer_button_and_confirms_control(self) -> None:
        runner = QianwenVideoAutomation.__new__(QianwenVideoAutomation)
        runner.duration = 15
        control = SimpleNamespace(
            count=AsyncMock(return_value=1),
            inner_text=AsyncMock(side_effect=["720P·5s", "720P·15s"]),
        )
        control_filtered = SimpleNamespace(last=control)
        option = SimpleNamespace(
            is_visible=AsyncMock(return_value=True),
            is_enabled=AsyncMock(return_value=True),
            inner_text=AsyncMock(return_value="15秒"),
            click=AsyncMock(),
        )
        options = SimpleNamespace(count=AsyncMock(return_value=1), nth=Mock(return_value=option))
        page = SimpleNamespace(
            locator=Mock(side_effect=lambda selector: (
                SimpleNamespace(filter=Mock(return_value=control_filtered))
                if selector == "button:visible"
                else SimpleNamespace(filter=Mock(return_value=options))
            )),
            wait_for_timeout=AsyncMock(),
        )

        self.assertTrue(asyncio.run(runner._ensure_video_duration(page)))
        self.assertIn("button:visible:not([disabled])", [call.args[0] for call in page.locator.call_args_list])
        option.click.assert_awaited_once_with(force=True)


DOUBAO_CHAT_URL = "https://www.doubao.com/chat/"


if __name__ == "__main__":
    unittest.main()
