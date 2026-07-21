from __future__ import annotations

import asyncio
import base64
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import query


def single_chain(conversation_id: str, messages: list[dict]) -> dict:
    return {
        "downlink_body": {
            "pull_singe_chain_downlink_body": {
                "conversation_id": conversation_id,
                "messages": messages,
            }
        }
    }


class DolaQueryTests(unittest.TestCase):
    def test_query_does_not_fall_back_to_recent_conversation(self) -> None:
        with patch.object(query, "expire_task_if_timeout"), patch.object(
            query, "get_meta", return_value={"status": query.STATUS_SUBMITTED}
        ), patch.object(
            query, "load_result", return_value={"cookie_string": "sessionid=secret"}
        ), patch.object(query, "save_result") as save_result, patch.object(
            query, "fetch_recent_conversation_id", new=AsyncMock(return_value="12345678901234567")
        ) as recent:
            result = asyncio.run(query._query_task_once("0" * 32))
        self.assertEqual(result, {"code": "1", "text": "没有文本", "url": ""})
        recent.assert_not_awaited()
        self.assertEqual(save_result.call_args.kwargs["extra"]["last_query_error_category"], "missing_submission_conversation")

    def test_recent_conversation_selects_latest_ordered_item(self) -> None:
        data = {
            "conversations": [
                {"conversation_id": "12345678901234567", "update_time": 100},
                {"conversation_id": "22345678901234567", "update_time": 300},
                {"conversation_id": "32345678901234567", "update_time": 200},
            ]
        }
        self.assertEqual(query.extract_conversation_id(data), "22345678901234567")

    def test_latest_message_controls_text_and_video_selection(self) -> None:
        old_url = base64.b64encode(b"https://example.com/old.mp4").decode()
        new_url = base64.b64encode(b"https://example.com/new.mp4").decode()
        data = single_chain(
            "12345678901234567",
            [
                {"message_index": 7, "tts_content": "旧消息预计等待 9 分钟", "video_model": {"main_url": old_url}},
                {"message_index": 9, "tts_content": "新消息预计等待 1 分钟", "video_model": {"main_url": new_url}},
                {"message_index": 8, "tts_content": "中间消息"},
            ],
        )
        self.assertEqual(query.extract_main_url(data), new_url)
        self.assertEqual(query.extract_tts_content(data), "新消息预计等待 1 分钟")

    def test_latest_message_without_video_does_not_reuse_stale_url(self) -> None:
        old_url = base64.b64encode(b"https://example.com/old.mp4").decode()
        data = single_chain(
            "12345678901234567",
            [
                {"message_index": 1, "video_model": {"main_url": old_url}},
                {"message_index": 2, "tts_content": "新任务生成中"},
            ],
        )
        self.assertEqual(query.extract_main_url(data), "")

    def test_message_list_order_breaks_equal_order_values(self) -> None:
        data = single_chain(
            "12345678901234567",
            [
                {"message_index": 1, "tts_content": "第一条"},
                {"message_index": 1, "tts_content": "第二条"},
            ],
        )
        self.assertEqual(query.extract_tts_content(data), "第二条")

    def test_conversation_ownership_rejects_mismatched_chain(self) -> None:
        data = single_chain("22345678901234567", [{"message_index": 1, "tts_content": "其他会话"}])
        with self.assertRaises(query.DolaQueryError) as context:
            query.validate_conversation_ownership(data, "12345678901234567")
        self.assertEqual(context.exception.category, "conversation_mismatch")

    def test_fetch_single_chain_validates_ownership(self) -> None:
        data = single_chain("22345678901234567", [])
        with patch.object(query, "_post_json", new=AsyncMock(return_value=data)):
            with self.assertRaises(query.DolaQueryError):
                asyncio.run(query.fetch_single_chain("sessionid=secret", "12345678901234567"))

    def test_diagnostic_redacts_credentials_and_classifies_timeout(self) -> None:
        error = httpx.ReadTimeout(
            "cookie: sessionid=secret; oauth_token=token authorization=Bearer bearer-token "
            "https://example.com?a_bogus=signature&token=query-secret"
        )
        diagnostic = query.query_error_diagnostic(error)
        self.assertEqual(diagnostic["last_query_error_category"], "timeout")
        self.assertNotIn("secret", diagnostic["last_query_error"])
        self.assertNotIn("bearer-token", diagnostic["last_query_error"])
        self.assertNotIn("signature", diagnostic["last_query_error"])

    def test_diagnostic_classifies_http_and_structured_errors(self) -> None:
        request = httpx.Request("POST", "https://www.dola.com/im/chain/single")
        response = httpx.Response(401, request=request)
        http_error = httpx.HTTPStatusError("unauthorized", request=request, response=response)
        self.assertEqual(query.classify_query_error(http_error), "http_401")
        self.assertEqual(
            query.classify_query_error(query.DolaQueryError("conversation_mismatch", "mismatch")),
            "conversation_mismatch",
        )

    def test_account_quota_insufficient_text_is_detected(self) -> None:
        self.assertTrue(query.is_account_quota_insufficient("本次视频生成需要消耗 3 个视频生成额度，今日剩余 1 个视频生成额度，无法生成该视频"))
        self.assertTrue(query.is_account_quota_insufficient("今日额度不足"))
        self.assertFalse(query.is_account_quota_insufficient("正在为您生成视频"))

    def test_policy_text_uses_client_message(self) -> None:
        self.assertEqual(query.POLICY_RETRY_TEXT, "你的输入可能包含违规内容请重试！")

    def test_policy_result_immediately_finishes_task_as_failed(self) -> None:
        task_id = "0" * 32
        result_data = {
            "cookie_string": "sessionid=secret",
            "conversation_id": "12345678901234567",
            "account_id": "account-1",
            "account_quota_charge_id": "charge-1",
        }
        meta = {"status": query.STATUS_SUBMITTED, "owner_token_hash": "owner-hash"}
        with patch.object(query, "expire_task_if_timeout"), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "load_result", return_value=result_data), patch.object(
            query, "fetch_single_chain", new=AsyncMock(return_value=("", query.POLICY_RETRY_TEXT))
        ), patch.object(query, "save_result"), patch.object(
            query, "clear_account_current_task"
        ) as clear_account, patch.object(query, "refund_account_quota_once") as refund_account, patch.object(
            query, "mark_failed"
        ) as mark_failed, patch.object(query, "refund_temp_quota_once") as refund_temp, patch.object(
            query, "retry_submitted_task"
        ) as retry_task:
            response = asyncio.run(query._query_task_once(task_id))
        self.assertEqual(response, {"code": "0", "text": query.POLICY_RETRY_TEXT, "url": ""})
        clear_account.assert_called_once_with("account-1", task_id)
        refund_account.assert_called_once_with(task_id, "account-1", "charge-1")
        mark_failed.assert_called_once_with(task_id, query.POLICY_RETRY_TEXT)
        refund_temp.assert_called_once_with(task_id, "owner-hash")
        retry_task.assert_not_called()

    def test_stale_pending_policy_task_is_reconciled_to_failed(self) -> None:
        task_id = "0" * 32
        meta = {"status": "pending", "owner_token_hash": "owner-hash", "error": query.POLICY_RETRY_TEXT}
        with patch.object(query, "expire_task_if_timeout"), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "mark_failed") as mark_failed, patch.object(
            query, "refund_temp_quota_once"
        ) as refund_temp:
            response = asyncio.run(query._query_task_once(task_id))
        self.assertEqual(response, {"code": "0", "text": query.POLICY_RETRY_TEXT, "url": ""})
        mark_failed.assert_called_once_with(task_id, query.POLICY_RETRY_TEXT)
        refund_temp.assert_called_once_with(task_id, "owner-hash")

    def test_quota_insufficient_exhausts_account_and_requeues_task(self) -> None:
        quota_text = "本次视频生成需要消耗 3 个视频生成额度，今日剩余 1 个视频生成额度，无法生成该视频"
        result_data = {
            "cookie_string": "sessionid=secret",
            "conversation_id": "12345678901234567",
            "account_id": "account-1",
            "account_quota_charge_id": "charge-1",
        }
        with patch.object(query, "expire_task_if_timeout"), patch.object(
            query, "get_meta", return_value={"status": query.STATUS_SUBMITTED, "owner_token_hash": ""}
        ), patch.object(query, "load_result", return_value=result_data), patch.object(
            query, "fetch_single_chain", new=AsyncMock(return_value=("", quota_text))
        ), patch.object(query, "save_result"), patch.object(
            query, "clear_account_current_task"
        ) as clear_account, patch.object(query, "exhaust_account_quota") as exhaust_account, patch.object(
            query, "record_failed_account"
        ) as record_failed, patch.object(query, "retry_submitted_task", return_value=1) as retry_task, patch.object(
            query, "clear_transient_result"
        ):
            response = asyncio.run(query._query_task_once("0" * 32))
        self.assertEqual(response, {"code": "1", "text": query.ACCOUNT_QUOTA_RETRY_TEXT, "url": ""})
        clear_account.assert_called_once_with("account-1", "0" * 32)
        exhaust_account.assert_called_once_with("account-1", "charge-1")
        record_failed.assert_called_once_with("0" * 32, "account-1")
        retry_task.assert_called_once_with("0" * 32, query.ACCOUNT_QUOTA_RETRY_TEXT, max_retries=5, delay_seconds=10)


if __name__ == "__main__":
    unittest.main()
