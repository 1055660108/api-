from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.qianwen_automation import (
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
        source = (Path(__file__).parents[1] / "app" / "worker.py").read_text(encoding="utf-8")
        self.assertIn("qianwen_submitted_rows", source)
        self.assertIn('platform="qianwen"', source)
        self.assertIn("QIANWEN_RESULT_WATCH_DEADLINE_MINUTES", source)


if __name__ == "__main__":
    unittest.main()
