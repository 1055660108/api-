from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, call, patch

import httpx

from app.accounts import local_today
from app.qianwen_ai_studio import QIANWEN_AI_STUDIO_CREDIT_COUNTRIES, QianwenAIStudioAutomation, acquire_qianwen_ai_studio_credit_proxy, parse_qianwen_ai_studio_credit, parse_qianwen_ai_studio_result, qianwen_ai_studio_model, qianwen_ai_studio_submission_payload
from app.qianwen_automation import (
    QIANWEN_CHAT_API_URL,
    QIANWEN_CHAT_SNAP_API_URL,
    QianwenVideoAutomation,
    enable_qianwen_wan27_audio,
    is_qianwen_chat_api_url,
    is_qianwen_ai_studio_redirect,
    is_qianwen_user_validation_error,
    is_qianwen_account_quota_insufficient,
    is_qianwen_content_rejection,
    parse_qianwen_generation_result,
    parse_qianwen_submission,
    qianwen_cookie_value,
    qianwen_model_requires_reference,
    qianwen_request_post_data,
    qianwen_interface_response_confirmed,
)
from app.query import fail_qianwen_content_rejection, retry_qianwen_quota_insufficient_result, retry_qianwen_text_only_result


class QianwenSubmissionTests(unittest.TestCase):
    def test_reference_interface_posts_body_without_passing_content_to_client_constructor(self) -> None:
        runner = QianwenVideoAutomation.__new__(QianwenVideoAutomation)
        runner.settings = SimpleNamespace(qianwen_reference_interface_submit_enabled=True)
        runner.audio_request_patch_count = 0
        runner.qianwen_interface_submit_attempts = 0
        runner.qianwen_interface_submit_successes = 0
        runner.qianwen_interface_submit_fallbacks = 0
        runner.network_events = []
        runner._interface_proxy_server = ""
        post_data = json.dumps({
            "session_id": "session-1",
            "req_id": "request-1",
            "ai_tool_scene": "zaodian_generate_video",
            "biz_data": json.dumps({
                "bizScene": "genVideo",
                "req": {
                    "rootModel": "wan27",
                    "params": {
                        "duration": 10,
                        "size": "16:9",
                        "attachments": [{"materialId": "material-1"}],
                    },
                },
            }),
        })
        request = SimpleNamespace(
            url=QIANWEN_CHAT_API_URL,
            post_data=post_data,
            all_headers=AsyncMock(return_value={"content-type": "application/json", "content-length": "123"}),
        )
        response = SimpleNamespace(
            status_code=200,
            text='{"communication":{"sessionid":"session-1","reqid":"request-1"}}',
            headers={"content-type": "text/event-stream"},
        )
        client = SimpleNamespace(post=AsyncMock(return_value=response))
        client_context = AsyncMock()
        client_context.__aenter__.return_value = client
        client_context.__aexit__.return_value = None
        route = SimpleNamespace(fulfill=AsyncMock(), continue_=AsyncMock())

        with patch("app.qianwen_automation.httpx.AsyncClient", return_value=client_context) as factory:
            asyncio.run(runner._route_chat_submission(route, request))

        self.assertEqual(runner.qianwen_interface_submit_attempts, 1)
        self.assertEqual(runner.qianwen_interface_submit_successes, 1)
        self.assertEqual(runner.qianwen_interface_submit_fallbacks, 0)
        self.assertNotIn("content", factory.call_args.kwargs)
        patched_data, _changed = enable_qianwen_wan27_audio(post_data)
        client.post.assert_awaited_once_with(
            QIANWEN_CHAT_API_URL,
            headers={"content-type": "application/json"},
            content=patched_data.encode("utf-8"),
        )
        route.fulfill.assert_awaited_once()
        route.continue_.assert_not_awaited()

    def test_interface_submit_response_requires_session_and_request_ids(self) -> None:
        self.assertTrue(qianwen_interface_response_confirmed(200, '{"communication":{"sessionid":"session-1","reqid":"request-1"}}'))
        self.assertFalse(qianwen_interface_response_confirmed(200, '{"ret":["FAIL_SYS_USER_VALIDATE"],"data":{"url":"action=captcha"}}'))
        self.assertFalse(qianwen_interface_response_confirmed(200, '{"communication":{"sessionid":"session-1"}}'))
        self.assertFalse(qianwen_interface_response_confirmed(500, '{"communication":{"sessionid":"session-1","reqid":"request-1"}}'))
    @staticmethod
    def _studio_runner(cookies: list[dict] | None = None) -> QianwenAIStudioAutomation:
        cookie_items = cookies if cookies is not None else [
            {"name": "tongyi_sso_ticket", "value": "ticket-value"},
            {"name": "XSRF-TOKEN", "value": "xsrf-value"},
        ]
        return QianwenAIStudioAutomation(
            "task-id",
            "test prompt",
            "16:9",
            "HappyHorse 1.1",
            10,
            account={"id": "account-id", "cookies": cookie_items, "qianwen_ai_studio_credit_sync_date": local_today()},
        )

    @staticmethod
    def _studio_client(response_payload: dict, side_effect: list | None = None) -> tuple[Mock, AsyncMock]:
        response = Mock()
        response.raise_for_status = Mock()
        response.json.return_value = response_payload
        post = AsyncMock(side_effect=side_effect or [response])
        client = SimpleNamespace(post=post)
        context = Mock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=None)
        return context, post

    def test_ai_studio_redirect_is_detected(self) -> None:
        self.assertTrue(is_qianwen_ai_studio_redirect("https://create.qianwen.com/r/ai-studio-pc/main/gen?tab=video"))
        self.assertTrue(is_qianwen_ai_studio_redirect("https://ai-studio-create.qianwen.com/api/web"))
        self.assertFalse(is_qianwen_ai_studio_redirect("https://www.qianwen.com/"))

    def test_user_validation_error_is_detected(self) -> None:
        self.assertTrue(is_qianwen_user_validation_error('{"ret":["FAIL_SYS_USER_VALIDATE","RGV587_ERROR::SM"]}'))
        self.assertTrue(is_qianwen_user_validation_error("", "https://chat2.qianwen.com/punish?action=captcha"))
        self.assertFalse(is_qianwen_user_validation_error('{"ok":true}', "https://chat2.qianwen.com/api/v2/chat"))

    def test_text_only_response_is_detected(self) -> None:
        from app.qianwen_automation import is_qianwen_text_only_response

        self.assertTrue(is_qianwen_text_only_response("抱歉，我无法回答这个问题。我们聊聊别的吧。"))
        self.assertTrue(is_qianwen_text_only_response("-ZS11403- input query audit rejected"))
        self.assertFalse(is_qianwen_text_only_response("视频生成已提交"))

    def test_ai_studio_missing_ticket_is_an_account_login_failure(self) -> None:
        runner = self._studio_runner([{"name": "XSRF-TOKEN", "value": "xsrf-value"}])

        outcome = asyncio.run(runner._run_locked())

        self.assertTrue(outcome["account_login_invalid"])
        self.assertTrue(outcome["account_fault"])
        self.assertTrue(outcome["switch_account"])
        self.assertIn("tongyi_sso_ticket", outcome["reason"])

    def test_ai_studio_credit_info_is_parsed(self) -> None:
        parsed = parse_qianwen_ai_studio_credit({
            "code": 0,
            "data": {"totalAmount": 66, "purchase": 0, "signIn": 66, "gift": 0},
        })

        self.assertEqual(parsed, {"total_amount": 66, "sign_in_amount": 66})

    def test_ai_studio_daily_credit_uses_taiwan_and_hong_kong_subscription_nodes(self) -> None:
        settings = SimpleNamespace(
            proxy_enabled=True,
            proxy_subscription_url="https://subscription.example/list",
            proxy_api_timeout_seconds=20,
            proxy_subscription_scheme="http",
            proxy_subscription_refresh_seconds=900,
            proxy_latency_threshold_ms=800,
        )
        lease = {"server": "http://127.0.0.1:19090", "node_id": "credit-node"}

        with patch("app.qianwen_ai_studio.acquire_dola_subscription_proxy", new=AsyncMock(return_value=lease)) as acquire:
            result = asyncio.run(acquire_qianwen_ai_studio_credit_proxy(settings))

        self.assertEqual(result, lease)
        self.assertEqual(QIANWEN_AI_STUDIO_CREDIT_COUNTRIES, ("台湾", "香港"))
        self.assertEqual(acquire.await_args.kwargs["selected_countries"], ("台湾", "香港"))
        self.assertFalse(acquire.await_args.kwargs["auto_select"])
        self.assertTrue(acquire.await_args.kwargs["random_select"])

    def test_ai_studio_upstream_missing_ticket_is_an_account_login_failure(self) -> None:
        runner = self._studio_runner()
        context, _post = self._studio_client({"code": 1003, "msg": "cookie中tongyi_sso_ticket不能为空"})

        with patch("app.qianwen_ai_studio.begin_task_submission", return_value=True), patch(
            "app.qianwen_ai_studio.httpx.AsyncClient", return_value=context
        ):
            outcome = asyncio.run(runner._run_locked())

        self.assertTrue(outcome["account_login_invalid"])
        self.assertTrue(outcome["account_fault"])
        self.assertTrue(outcome["switch_account"])

    def test_ai_studio_15001_marks_account_quota_insufficient(self) -> None:
        runner = self._studio_runner()
        context, _post = self._studio_client({"code": 15001, "msg": "积分不足"})

        with patch("app.qianwen_ai_studio.begin_task_submission", return_value=True), patch(
            "app.qianwen_ai_studio.httpx.AsyncClient", return_value=context
        ):
            outcome = asyncio.run(runner._run_locked())

        self.assertTrue(outcome["account_quota_insufficient"])
        self.assertTrue(outcome["account_fault"])
        self.assertTrue(outcome["switch_account"])

    def test_ai_studio_connect_failure_retries_three_times(self) -> None:
        runner = self._studio_runner()
        request = httpx.Request("POST", "https://ai-studio-create.qianwen.com/api/web/ai/video/function")
        response = Mock()
        response.raise_for_status = Mock()
        response.json.return_value = {"code": 0, "data": {"recordId": "record-id"}}
        context, post = self._studio_client(
            {},
            [httpx.ConnectError("connect-1", request=request), httpx.ConnectTimeout("connect-2", request=request), response],
        )

        with patch("app.qianwen_ai_studio.begin_task_submission", return_value=True), patch(
            "app.qianwen_ai_studio.httpx.AsyncClient", return_value=context
        ), patch("app.qianwen_ai_studio.save_result"), patch(
            "app.qianwen_ai_studio.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            outcome = asyncio.run(runner._run_locked())

        self.assertTrue(outcome["submitted"])
        self.assertEqual(post.await_count, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [0.5, 1.0])

    def test_ai_studio_first_daily_task_syncs_credit_before_submit(self) -> None:
        runner = self._studio_runner()
        runner.account["qianwen_ai_studio_credit_sync_date"] = ""
        runner.account["quota_cost"] = 25
        runner._sync_daily_credit = AsyncMock(return_value={"ok": True, "total_amount": 66, "sign_in_amount": 66})
        context, post = self._studio_client({"code": 0, "data": {"recordId": "record-id"}})

        with patch("app.qianwen_ai_studio.begin_task_submission", return_value=True), patch(
            "app.qianwen_ai_studio.httpx.AsyncClient", return_value=context
        ), patch("app.qianwen_ai_studio.save_result"):
            outcome = asyncio.run(runner._run_locked())

        self.assertTrue(outcome["submitted"])
        runner._sync_daily_credit.assert_awaited_once()
        self.assertEqual(post.await_count, 1)

    def test_current_qianwen_video_models_require_a_reference(self) -> None:
        for model in ("万相 2.7", "万相 2.6", "HappyHorse 1.0"):
            self.assertTrue(qianwen_model_requires_reference(model))
        self.assertFalse(qianwen_model_requires_reference("AI生图"))

    def test_accepts_both_live_qianwen_submission_endpoints(self) -> None:
        self.assertTrue(is_qianwen_chat_api_url(QIANWEN_CHAT_API_URL))
        self.assertTrue(is_qianwen_chat_api_url(QIANWEN_CHAT_SNAP_API_URL))
        self.assertFalse(is_qianwen_chat_api_url("https://chat2.qianwen.com/api/v1/session/req/detail"))

    def test_parses_real_video_submission_fields(self) -> None:
        payload = {
            "session_id": "session-1",
            "req_id": "request-1",
            "ai_tool_scene": "zaodian_generate_video",
            "biz_data": json.dumps(
                {
                    "bizScene": "genVideo",
                    "req": {
                        "rootModel": "happyhorse",
                        "genMode": "multi_ref",
                        "params": {
                            "duration": 10,
                            "resolution": "720P",
                            "size": "16:9",
                            "audio": False,
                            "attachments": [{"type": "image", "materialId": "material-1"}],
                        },
                    },
                    "videoReportParams": {"model": "HappyHorse 1.0"},
                }
            ),
        }

        parsed = parse_qianwen_submission(json.dumps(payload))

        self.assertEqual(parsed["session_id"], "session-1")
        self.assertEqual(parsed["req_id"], "request-1")
        self.assertEqual(parsed["model"], "HappyHorse 1.0")
        self.assertEqual(parsed["duration"], 10)
        self.assertEqual(parsed["ratio"], "16:9")
        self.assertFalse(parsed["audio"])
        self.assertEqual(parsed["attachment_ids"], ["material-1"])

    def test_wan27_submission_audio_is_enabled(self) -> None:
        payload = {
            "session_id": "session-1",
            "ai_tool_scene": "zaodian_generate_video",
            "biz_data": json.dumps({
                "bizScene": "genVideo",
                "req": {
                    "rootModel": "wan27",
                    "params": {"duration": 10, "size": "16:9", "audio": False},
                },
            }),
        }

        patched, changed = enable_qianwen_wan27_audio(json.dumps(payload))
        parsed = parse_qianwen_submission(patched)

        self.assertTrue(changed)
        self.assertTrue(parsed["audio"])

    def test_non_wan27_submission_audio_is_unchanged(self) -> None:
        payload = {
            "session_id": "session-1",
            "ai_tool_scene": "zaodian_generate_video",
            "biz_data": json.dumps({
                "bizScene": "genVideo",
                "req": {
                    "rootModel": "wan26",
                    "params": {"duration": 10, "size": "16:9", "audio": False},
                },
            }),
        }
        original = json.dumps(payload)

        patched, changed = enable_qianwen_wan27_audio(original)

        self.assertFalse(changed)
        self.assertEqual(patched, original)
        self.assertFalse(parse_qianwen_submission(patched)["audio"])

    def test_binary_request_body_is_ignored(self) -> None:
        request = Mock()
        type(request).post_data = PropertyMock(
            side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        )

        self.assertEqual(qianwen_request_post_data(request), "")

    def test_rejects_ordinary_chat_submission(self) -> None:
        self.assertEqual(parse_qianwen_submission(json.dumps({"session_id": "chat-only"})), {})

    def test_cookie_value_accepts_escaped_cookie_name(self) -> None:
        cookie = "XSRF-TOKEN=csrf; b-user-id=user-1; *samesite\\_flag*=true"
        self.assertEqual(qianwen_cookie_value(cookie, "b-user-id"), "user-1")

    def test_quota_insufficient_dialog_is_detected(self) -> None:
        self.assertTrue(is_qianwen_account_quota_insufficient("额度不足 当前剩余 1 额度，不足以完成本次视频生成"))
        self.assertFalse(is_qianwen_account_quota_insufficient("视频生成已提交"))

    def test_content_rejection_is_detected_separately_from_text_only_audit(self) -> None:
        self.assertTrue(is_qianwen_content_rejection("当前内容无法生成，请修改后重试"))
        self.assertFalse(is_qianwen_content_rejection("-ZS11403- input query audit rejected"))

    def test_ai_studio_text_video_payloads_use_native_model_keys(self) -> None:
        cases = {
            "HappyHorse 1.1": ("happyhorse11", "hh11_t2v"),
            "万相 2.7": ("wan27", "wan27_t2v"),
            "万相 2.6": ("wan26", "wan26_t2v"),
        }
        for model, (root_model, scene) in cases.items():
            payload, result_scene = qianwen_ai_studio_submission_payload("测试提示词", model, "16:9", 10, req_id="request", chid="channel")
            self.assertEqual(payload["model"], root_model)
            self.assertEqual(payload["rootModel"], root_model)
            self.assertEqual(payload["scene"], "gen_video")
            self.assertEqual(payload["genMode"], "vid_gen")
            self.assertEqual(payload["params"], {"size": "16:9", "resolution": "720P", "duration": 10, "attachmentType": 0, "attachments": []})
            self.assertEqual((payload["req_id"], payload["chid"], result_scene), ("request", "channel", scene))

    def test_happyhorse_1_0_is_not_routed_to_ai_studio_without_a_reference(self) -> None:
        self.assertIsNone(qianwen_ai_studio_model("HappyHorse 1.0"))
        self.assertIsNotNone(qianwen_ai_studio_model("HappyHorse 1.1"))

    def test_submission_request_is_captured_before_response_arrives(self) -> None:
        runner = QianwenVideoAutomation.__new__(QianwenVideoAutomation)
        runner.remote_submission = {}
        runner.remote_session_id = ""
        runner.remote_req_id = ""
        runner.submission_request_event = asyncio.Event()
        request = SimpleNamespace(
            method="POST",
            url=QIANWEN_CHAT_API_URL,
            post_data=json.dumps({
                "session_id": "session-1",
                "req_id": "request-1",
                "ai_tool_scene": "zaodian_generate_video",
                "biz_data": json.dumps({
                    "bizScene": "genVideo",
                    "req": {"rootModel": "happyhorse", "params": {"duration": 10, "size": "9:16"}},
                }),
            }),
        )

        runner._capture_request(request)

        self.assertTrue(runner.submission_request_event.is_set())
        self.assertEqual(runner.remote_session_id, "session-1")
        self.assertEqual(runner.remote_req_id, "request-1")

    def test_snap_submission_request_is_captured(self) -> None:
        runner = QianwenVideoAutomation.__new__(QianwenVideoAutomation)
        runner.remote_submission = {}
        runner.remote_session_id = ""
        runner.remote_req_id = ""
        runner.submission_request_event = asyncio.Event()
        request = SimpleNamespace(
            method="POST",
            url=QIANWEN_CHAT_SNAP_API_URL,
            post_data=json.dumps({
                "session_id": "snap-session",
                "req_id": "snap-request",
                "ai_tool_scene": "zaodian_generate_video",
                "biz_data": json.dumps({
                    "bizScene": "genVideo",
                    "req": {"rootModel": "wan27", "params": {"duration": 15, "size": "16:9"}},
                }),
            }),
        )

        runner._capture_request(request)

        self.assertTrue(runner.submission_request_event.is_set())
        self.assertEqual(runner.remote_session_id, "snap-session")
        self.assertEqual(runner.remote_req_id, "snap-request")

    def test_reference_upload_requires_a_new_composer_preview(self) -> None:
        runner = QianwenVideoAutomation.__new__(QianwenVideoAutomation)
        runner._reference_preview_count = AsyncMock(side_effect=[1, 1])
        body = SimpleNamespace(inner_text=AsyncMock(return_value=""))
        progress = SimpleNamespace(count=AsyncMock(return_value=0))
        page = SimpleNamespace(
            locator=Mock(side_effect=lambda selector: body if selector == "body" else progress),
            wait_for_timeout=AsyncMock(),
        )

        uploaded = asyncio.run(runner._wait_for_reference_upload(page, baseline_preview_count=0, timeout_seconds=5))

        self.assertTrue(uploaded)
        self.assertEqual(runner._reference_preview_count.await_count, 2)

    def test_reference_button_menu_can_provide_file_chooser(self) -> None:
        runner = QianwenVideoAutomation.__new__(QianwenVideoAutomation)

        class AwaitableValue:
            def __init__(self, value=None, error: Exception | None = None) -> None:
                self.value = value
                self.error = error

            def __await__(self):
                async def resolve():
                    if self.error:
                        raise self.error
                    return self.value

                return resolve().__await__()

        class ExpectFileChooser:
            def __init__(self, value) -> None:
                self.value = value

            async def __aenter__(self):
                return self

            async def __aexit__(self, _exc_type, _exc, _tb):
                return False

        chooser = SimpleNamespace(set_files=AsyncMock())
        first_attempt = ExpectFileChooser(AwaitableValue(error=asyncio.TimeoutError()))
        menu_attempt = ExpectFileChooser(AwaitableValue(value=chooser))
        reference_button = SimpleNamespace(
            is_visible=AsyncMock(return_value=True),
            is_enabled=AsyncMock(return_value=True),
            click=AsyncMock(),
        )
        reference_buttons = SimpleNamespace(count=AsyncMock(return_value=1), nth=Mock(return_value=reference_button))
        upload_option = SimpleNamespace(
            is_visible=AsyncMock(return_value=True),
            is_enabled=AsyncMock(return_value=True),
            inner_text=AsyncMock(return_value="上传图片"),
            click=AsyncMock(),
        )
        upload_options = SimpleNamespace(count=AsyncMock(return_value=1), nth=Mock(return_value=upload_option))
        empty_locator = SimpleNamespace(count=AsyncMock(return_value=0))

        def locator(selector: str):
            return upload_options if '[role="listbox"]' in selector else empty_locator

        page = SimpleNamespace(
            get_by_role=Mock(return_value=reference_buttons),
            locator=Mock(side_effect=locator),
            expect_file_chooser=Mock(side_effect=[first_attempt, menu_attempt]),
            wait_for_timeout=AsyncMock(),
        )

        selected = asyncio.run(runner._open_image_file_chooser(page))

        self.assertIs(selected, chooser)
        reference_button.click.assert_awaited_once_with(force=True)
        upload_option.click.assert_awaited_once_with(force=True)

    def test_reference_images_upload_sequentially_and_records_stable_preview_failure(self) -> None:
        runner = QianwenVideoAutomation.__new__(QianwenVideoAutomation)
        runner.reference_upload_failure_detail = ""
        chooser_one = SimpleNamespace(set_files=AsyncMock())
        chooser_two = SimpleNamespace(set_files=AsyncMock())
        runner._reference_preview_count = AsyncMock(side_effect=[0, 1])
        runner._open_image_file_chooser = AsyncMock(side_effect=[chooser_one, chooser_two])
        runner._wait_for_reference_upload = AsyncMock(return_value=True)
        page = SimpleNamespace(wait_for_timeout=AsyncMock())

        uploaded = asyncio.run(runner._upload_reference_images(page, ["one.png", "two.png"]))

        self.assertTrue(uploaded)
        chooser_one.set_files.assert_awaited_once_with("one.png")
        chooser_two.set_files.assert_awaited_once_with("two.png")
        self.assertEqual(runner._wait_for_reference_upload.await_count, 2)


class QianwenResultTests(unittest.TestCase):
    def test_content_rejection_keeps_backend_reason_and_maps_reference_client_reason(self) -> None:
        meta = {"image_count": 1, "owner_token_hash": "owner"}
        result = {"account_id": "account-1", "account_quota_charge_id": "charge-1"}
        with patch("app.query.clear_account_current_task"), patch("app.query.refund_account_quota_once"), patch(
            "app.query.mark_failed"
        ) as mark_failed_mock, patch("app.query.update_meta") as update_meta_mock, patch("app.query.refund_temp_quota_once"):
            response = fail_qianwen_content_rejection(
                "task-1", meta, result, "当前内容无法生成，请修改后重试"
            )
        mark_failed_mock.assert_called_once_with("task-1", "当前内容无法生成，请修改后重试")
        update_meta_mock.assert_called_once_with(
            "task-1", client_error="参考图内容违规", qianwen_failure_category="content_rejected"
        )
        self.assertEqual(response["text"], "参考图内容违规")

    def test_quota_insufficient_result_exhausts_account_and_requeues(self) -> None:
        meta = {"owner_token_hash": "owner"}
        result = {"account_id": "account-1", "account_quota_charge_id": "charge-1"}
        with patch("app.query.exhaust_account_quota") as exhaust, patch(
            "app.query.clear_account_current_task"
        ), patch("app.query.update_meta"), patch("app.query.retry_submitted_task", return_value=1), patch(
            "app.query.task_retry_limit", return_value=4
        ), patch("app.query.clear_transient_result"):
            response = retry_qianwen_quota_insufficient_result("task-1", meta, result, "额度不足")
        exhaust.assert_called_once_with("account-1", "charge-1")
        self.assertEqual(response["code"], "1")

    def test_first_text_only_result_retries_with_same_account_and_refunds_quota(self) -> None:
        meta = {"qianwen_text_only_retry_used": False, "owner_token_hash": "owner"}
        result = {"account_id": "account-1", "account_quota_charge_id": "charge-1"}

        with patch("app.query.clear_account_current_task") as clear_account, patch(
            "app.query.refund_account_quota_once"
        ) as refund_account, patch("app.query.update_meta") as update_meta_mock, patch(
            "app.query.retry_submitted_task", return_value=1
        ), patch("app.query.task_retry_limit", return_value=4), patch("app.query.clear_transient_result"):
            response = retry_qianwen_text_only_result("task-1", meta, result, "input query audit rejected")

        clear_account.assert_called_once_with("account-1", "task-1")
        refund_account.assert_called_once_with("task-1", "account-1", "charge-1")
        update_meta_mock.assert_called_once_with(
            "task-1", preferred_account_id="account-1", qianwen_text_only_retry_used=True
        )
        self.assertEqual(response["code"], "1")

    def test_second_text_only_result_switches_account_without_marking_it_failed(self) -> None:
        meta = {"qianwen_text_only_retry_used": True, "owner_token_hash": "owner"}
        result = {"account_id": "account-1", "account_quota_charge_id": "charge-2"}

        with patch("app.query.clear_account_current_task"), patch(
            "app.query.refund_account_quota_once"
        ), patch("app.query.update_meta") as update_meta_mock, patch(
            "app.query.retry_submitted_task", return_value=2
        ), patch("app.query.task_retry_limit", return_value=4), patch(
            "app.query.clear_transient_result"
        ), patch("app.query.record_failed_account") as record_failed:
            response = retry_qianwen_text_only_result("task-1", meta, result, "安审拒绝")

        update_meta_mock.assert_called_once_with(
            "task-1", preferred_account_id="", qianwen_text_only_retry_used=False
        )
        record_failed.assert_not_called()
        self.assertEqual(response["code"], "1")

    def test_ai_studio_result_prefers_original_oss_url(self) -> None:
        payload = {
            "code": 0,
            "data": {"list": [{"content": {"status": 1, "task_id": "remote", "extra": {
                "model_name": "HappyHorse 1.1",
                "params": {"duration": 10, "size": "16:9"},
                "result_videos": [{
                    "url": "http://oss.example/original.mp4",
                    "cdn_url": "https://cdn.example/video.mp4",
                    "download_url": "https://cdn.example/download.mp4",
                }],
            }}}]},
        }

        parsed = parse_qianwen_ai_studio_result(payload)

        self.assertEqual(parsed["state"], "succeeded")
        self.assertEqual(parsed["video_url"], "https://oss.example/original.mp4")
        self.assertEqual(parsed["video_source"], "url")
        self.assertEqual((parsed["duration"], parsed["ratio"]), (10, "16:9"))

    def test_prefers_unwatermarked_display_video_over_branded_download(self) -> None:
        payload = {
            "data": {
                "response_messages": [
                    {
                        "status": "complete",
                        "meta_data": {
                            "multi_load": [
                                {
                                    "content": {
                                        "status": "complete",
                                        "display_list": [
                                            {
                                                "video": [{"url": "https://cdn.example/original.mp4"}],
                                                "download_video": [{"url": "https://cdn.example/watermarked.mp4"}],
                                            }
                                        ],
                                    }
                                }
                            ]
                        },
                    }
                ],
                "error_code": 0,
            }
        }

        parsed = parse_qianwen_generation_result(payload)

        self.assertEqual(parsed["state"], "succeeded")
        self.assertEqual(parsed["video_url"], "https://cdn.example/original.mp4")
        self.assertRegex(parsed["video_source"], r"\.display_list\[0\]\.video\[0\]\.url$")
        self.assertTrue(parsed["watermarked_download_available"])

    def test_watermarked_download_without_original_stays_pending(self) -> None:
        payload = {
            "data": {
                "response_messages": [{
                    "status": "complete",
                    "meta_data": {"multi_load": [{"content": {
                        "status": "complete",
                        "display_list": [{"download_video": [{"url": "https://cdn.example/watermarked.mp4"}]}],
                    }}]},
                }],
                "error_code": 0,
            }
        }

        parsed = parse_qianwen_generation_result(payload)

        self.assertEqual(parsed["state"], "generating")
        self.assertEqual(parsed["video_url"], "")
        self.assertTrue(parsed["watermarked_download_available"])

    def test_processing_result_stays_generating_without_download_video(self) -> None:
        payload = {
            "data": {
                "response_messages": [
                    {
                        "status": "processing",
                        "meta_data": {"multi_load": [{"content": {"status": "processing", "task_id": "remote-1"}}]},
                    }
                ],
                "error_code": 0,
            }
        }

        parsed = parse_qianwen_generation_result(payload)

        self.assertEqual(parsed["state"], "generating")
        self.assertEqual(parsed["video_url"], "")
        self.assertEqual(parsed["task_ids"], ["remote-1"])

    def test_failed_remote_status_is_terminal(self) -> None:
        nested = json.dumps({"status": 3, "error_msg": "当前内容无法生成，请修改后重试"})
        parsed = parse_qianwen_generation_result({"data": {"result": nested, "error_code": 11003}})
        self.assertEqual(parsed["state"], "failed")
        self.assertIn(11003, parsed["error_codes"])

    def test_quota_insufficient_result_is_not_treated_as_generating(self) -> None:
        parsed = parse_qianwen_generation_result({"data": {"message": "额度不足，请购买获得更多额度"}})
        self.assertEqual(parsed["state"], "quota_insufficient")
        self.assertTrue(parsed["quota_insufficient"])

    def test_worker_watches_qianwen_submitted_tasks(self) -> None:
        root = Path(__file__).parents[1] / "app"
        worker_source = (root / "worker.py").read_text(encoding="utf-8")
        qianwen_source = (root / "qianwen_automation.py").read_text(encoding="utf-8")
        query_source = (root / "query.py").read_text(encoding="utf-8")
        self.assertIn("qianwen_submitted_rows", worker_source)
        self.assertIn('platform="qianwen"', worker_source)
        self.assertIn("QIANWEN_RESULT_WATCH_DEADLINE_MINUTES", worker_source)
        self.assertIn('"keep_account_claimed": True', qianwen_source)
        self.assertIn('if account and not outcome.get("keep_account_claimed"):', worker_source)
        self.assertIn('get_by_role("button", name="参考", exact=True)', qianwen_source)
        self.assertIn("preview_count > baseline_preview_count", qianwen_source)
        browser_flow = qianwen_source.split("async def _run_browser", 1)[1]
        self.assertLess(browser_flow.index("await self._ensure_video_duration(page)"), browser_flow.index("await self._upload_reference_images(page, reference_paths)"))
        self.assertIn("reference image preview lost before submit", browser_flow)
        self.assertIn('await editor.press("Enter")', qianwen_source)
        self.assertIn('"qianwen_result_source": "display_video_unwatermarked"', query_source)
        self.assertIn('"qianwen_watermark_status": "original"', query_source)
        self.assertIn("QianwenAIStudioAutomation", worker_source)
        self.assertIn('"qianwen_result_chain": "ai_studio"', (root / "qianwen_ai_studio.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
