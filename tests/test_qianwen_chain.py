from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.qianwen_automation import (
    QIANWEN_CHAT_API_URL,
    QianwenVideoAutomation,
    parse_qianwen_generation_result,
    parse_qianwen_submission,
    qianwen_cookie_value,
)


class QianwenSubmissionTests(unittest.TestCase):
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
        self.assertEqual(parsed["attachment_ids"], ["material-1"])

    def test_rejects_ordinary_chat_submission(self) -> None:
        self.assertEqual(parse_qianwen_submission(json.dumps({"session_id": "chat-only"})), {})

    def test_cookie_value_accepts_escaped_cookie_name(self) -> None:
        cookie = "XSRF-TOKEN=csrf; b-user-id=user-1; *samesite\\_flag*=true"
        self.assertEqual(qianwen_cookie_value(cookie, "b-user-id"), "user-1")

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


class QianwenResultTests(unittest.TestCase):
    def test_prefers_download_video_over_web_playback_video(self) -> None:
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
                                                "video": [{"url": "https://cdn.example/watermarked.mp4"}],
                                                "download_video": [{"url": "https://cdn.example/original.mp4"}],
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
        self.assertIn("download_video", parsed["video_source"])

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

    def test_worker_watches_qianwen_submitted_tasks(self) -> None:
        root = Path(__file__).parents[1] / "app"
        worker_source = (root / "worker.py").read_text(encoding="utf-8")
        qianwen_source = (root / "qianwen_automation.py").read_text(encoding="utf-8")
        self.assertIn("qianwen_submitted_rows", worker_source)
        self.assertIn('platform="qianwen"', worker_source)
        self.assertIn("QIANWEN_RESULT_WATCH_DEADLINE_MINUTES", worker_source)
        self.assertIn('"keep_account_claimed": True', qianwen_source)
        self.assertIn('if account and not outcome.get("keep_account_claimed"):', worker_source)
        self.assertIn('get_by_role("button", name="参考", exact=True)', qianwen_source)
        self.assertIn("preview_count > baseline_preview_count", qianwen_source)
        self.assertIn('await editor.press("Enter")', qianwen_source)


if __name__ == "__main__":
    unittest.main()
