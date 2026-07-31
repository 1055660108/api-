from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.doubao_automation import DOUBAO_MODEL_CODES, DOUBAO_ORIGINAL_VIDEO_SCORE, DOUBAO_RESULT_WAIT_SECONDS, DOUBAO_SUBMIT_SCRIPT, DoubaoVideoAutomation, best_doubao_video_candidate, collect_doubao_response_candidates, collect_doubao_video_candidates, doubao_video_url_score
from app.qianwen_automation import QianwenVideoAutomation


class DoubaoAutomationTests(unittest.TestCase):
    @staticmethod
    def runner(proxy_session) -> DoubaoVideoAutomation:
        runner = DoubaoVideoAutomation.__new__(DoubaoVideoAutomation)
        runner.proxy_session = proxy_session
        runner.settings = SimpleNamespace(task_timeout_seconds=600)
        runner.task_id = "doubao-task"
        return runner

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
            'confirmationPayload.messages[0].content_block[0].content.text_block.text = "需要"',
            "confirmationPayload.option.need_create_conversation = false",
            "confirmationPayload.client_meta.conversation_id = conversationId",
            "auto_confirmation_sent: autoConfirmationSent",
        ):
            self.assertIn(fragment, DOUBAO_SUBMIT_SCRIPT)
        self.assertIn("would you like|do you want|shall i|should i", DOUBAO_SUBMIT_SCRIPT)
        self.assertIn("是否|请问", DOUBAO_SUBMIT_SCRIPT)
        self.assertEqual(DOUBAO_MODEL_CODES["Seedance 2.0 Mini"], "seedance_v2.0_mini")
        self.assertEqual(DOUBAO_MODEL_CODES["Seedance 2.0 Fast"], "seedance_v2.0")

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
                    "video_current_src",
                )
            )

        self.assertTrue(outcome["success"])
        runner._refresh_cookies.assert_awaited_once_with(context)
        self.assertEqual(save.call_args.kwargs["extra"]["decoded_main_url"], "https://media.example/result.mp4")
        self.assertEqual(save.call_args.kwargs["extra"]["doubao_video_detection_source"], "video_current_src")
        self.assertEqual(save.call_args.kwargs["extra"]["doubao_watermark_status"], "fallback")
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

    def test_qianwen_duration_is_explicitly_limited_to_ten_seconds(self) -> None:
        runner = QianwenVideoAutomation.__new__(QianwenVideoAutomation)
        page = SimpleNamespace(get_by_role=Mock())
        runner.duration = 15
        self.assertFalse(asyncio.run(runner._ensure_video_duration(page)))
        page.get_by_role.assert_not_called()

        runner.duration = 10
        controls = SimpleNamespace(count=AsyncMock(return_value=0))
        page.get_by_role = Mock(return_value=controls)
        self.assertTrue(asyncio.run(runner._ensure_video_duration(page)))


DOUBAO_CHAT_URL = "https://www.doubao.com/chat/"


if __name__ == "__main__":
    unittest.main()
