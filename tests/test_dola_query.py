from __future__ import annotations

import asyncio
import base64
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import httpx

from app import automation, query


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
    def test_query_lock_serializes_waiters_and_releases_registry_entry(self) -> None:
        task_id = "f" * 32

        async def exercise() -> tuple[int, list[dict[str, str]]]:
            query._QUERY_LOCKS.clear()
            active = 0
            max_active = 0
            first_entered = asyncio.Event()
            release_first = asyncio.Event()

            async def fake_query(_task_id: str) -> dict[str, str]:
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                if not first_entered.is_set():
                    first_entered.set()
                    await release_first.wait()
                await asyncio.sleep(0)
                active -= 1
                return {"code": "1", "text": "生成中", "url": ""}

            with patch.object(query, "_query_task_once", new=fake_query):
                first = asyncio.create_task(query.query_task(task_id))
                await first_entered.wait()
                second = asyncio.create_task(query.query_task(task_id))
                await asyncio.sleep(0)
                self.assertEqual(query._QUERY_LOCKS[task_id].users, 2)
                release_first.set()
                results = await asyncio.gather(first, second)
            return max_active, results

        max_active, results = asyncio.run(exercise())
        self.assertEqual(max_active, 1)
        self.assertEqual(len(results), 2)
        self.assertNotIn(task_id, query._QUERY_LOCKS)

    def test_running_task_query_returns_current_execution_phase(self) -> None:
        with patch.object(query, "expire_task_if_timeout"), patch.object(
            query, "get_meta", return_value={"status": "running", "status_reason": "正在打开生成页面"}
        ):
            result = asyncio.run(query._query_task_once("0" * 32))
        self.assertEqual(result, {"code": "1", "text": "正在打开生成页面", "url": ""})

    def test_proxy_and_browser_transport_errors_are_infrastructure_failures(self) -> None:
        for reason in (
            "mihomo controller is not available",
            "mihomo DOLA proxy group is unavailable",
            "Page.evaluate: TypeError: Failed to fetch",
            "Page.evaluate: Execution context was destroyed, most likely because of a navigation",
            "[SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error",
            "browser timeout",
            "All connection attempts failed",
        ):
            self.assertTrue(automation.is_infrastructure_failure(reason))
        self.assertFalse(automation.is_infrastructure_failure("你的输入可能包含违规内容请重试！"))

    def test_query_does_not_fall_back_to_recent_conversation(self) -> None:
        with patch.object(query, "expire_task_if_timeout"), patch.object(
            query, "get_meta", return_value={"status": query.STATUS_SUBMITTED}
        ), patch.object(
            query, "load_result", return_value={"cookie_string": "sessionid=secret"}
        ), patch.object(query, "save_result") as save_result, patch.object(
            query, "fetch_matching_recent_conversation_id", new=AsyncMock(return_value="12345678901234567")
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

    def test_reference_video_accepts_direct_and_alternate_url_fields(self) -> None:
        direct_url = "https://example.com/reference-result.mp4"
        data = single_chain(
            "12345678901234567",
            [{"message_index": 2, "video_model": {"video_url": direct_url}}],
        )
        self.assertEqual(query.extract_main_url(data), direct_url)
        self.assertEqual(query.decode_main_url(direct_url), direct_url)

    def test_conversation_ids_support_current_numeric_lengths(self) -> None:
        for conversation_id in ("123456789012345", "123456789012345678901234"):
            self.assertEqual(query.extract_conversation_id_from_sse(f'{{"conversation_id":"{conversation_id}"}}'), conversation_id)

    def test_conversation_ids_accept_current_field_variants(self) -> None:
        conversation_id = "22345678901234567"
        for field in ("conversationId", "conversationID", "conv_id", "convId"):
            self.assertEqual(query.extract_conversation_id_from_sse(f'{{"{field}":"{conversation_id}"}}'), conversation_id)
            self.assertEqual(query.extract_conversation_id({field: conversation_id}), conversation_id)

    def test_reference_conversation_recovery_requires_submission_match(self) -> None:
        data = {
            "conversations": [
                {
                    "conversation_id": "12345678901234567",
                    "update_time": 100,
                    "collection_id": "collection-other",
                    "messages": [{"text": "生成视频：其他任务"}],
                },
                {
                    "conversation_id": "22345678901234567",
                    "update_time": 200,
                    "collection_id": "collection-reference",
                    "messages": [{"text": "生成视频：参考图中的人物缓慢转身"}],
                },
            ]
        }
        self.assertEqual(
            query.extract_matching_conversation_id(data, collection_id="collection-reference", prompt=""),
            "22345678901234567",
        )
        self.assertEqual(
            query.extract_matching_conversation_id(data, prompt="参考图中的人物缓慢转身"),
            "22345678901234567",
        )
        self.assertEqual(query.extract_matching_conversation_id(data, prompt="完全不相关的生成任务"), "")
        duplicate_prompt = {
            "conversations": [
                {"conversation_id": "12345678901234567", "messages": [{"text": "重复的参考图生成提示词"}]},
                {"conversation_id": "22345678901234567", "messages": [{"text": "重复的参考图生成提示词"}]},
            ]
        }
        self.assertEqual(query.extract_matching_conversation_id(duplicate_prompt, prompt="重复的参考图生成提示词"), "")

    def test_conversation_recovery_matches_local_id_and_unique_key(self) -> None:
        data = {
            "conversations": [
                {
                    "conversation_id": "12345678901234567",
                    "client_meta": {"local_conversation_id": "local_1111111111111111"},
                },
                {
                    "conversation_id": "22345678901234567",
                    "option": {"unique_key": "unique-target"},
                },
            ]
        }
        self.assertEqual(
            query.extract_matching_conversation_id(data, local_conversation_id="local_1111111111111111"),
            "12345678901234567",
        )
        self.assertEqual(
            query.extract_matching_conversation_id(data, unique_key="unique-target"),
            "22345678901234567",
        )

    def test_reference_submission_waits_for_ack_and_returns_recovery_ids(self) -> None:
        self.assertIn("attachments && attachments.length ? 120000 : 60000", automation.SUBMIT_SCRIPT)
        self.assertEqual(query.AMBIGUOUS_SUBMISSION_RECOVERY_SECONDS, 120)
        self.assertIn('text.includes("710022004")', automation.SUBMIT_SCRIPT)
        self.assertIn("slider_verification: sliderVerification", automation.SUBMIT_SCRIPT)
        self.assertIn("bdcaptcha", automation.SERVICE_FREQUENT_ACCOUNT_STATE_SCRIPT)
        self.assertIn("captcha-slider-btn", automation.SERVICE_FREQUENT_ACCOUNT_STATE_SCRIPT)
        self.assertIn("suppliedCollectionId", automation.SUBMIT_SCRIPT)
        self.assertIn("suppliedUniqueKey", automation.SUBMIT_SCRIPT)
        self.assertIn("suppliedLocalConversationId", automation.SUBMIT_SCRIPT)
        for field in ("local_conversation_id", "collection_id", "unique_key", "submitted_with_images", "responsePreview"):
            self.assertIn(field, automation.SUBMIT_SCRIPT)

    def test_submission_response_preview_redacts_session_credentials(self) -> None:
        preview = automation._submission_response_preview(
            'data: {"sessionid":"secret-session","msToken":"secret-token","conversation_id":"123"}'
        )
        self.assertNotIn("secret-session", preview)
        self.assertNotIn("secret-token", preview)
        self.assertIn("conversation_id", preview)

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

    def test_ambiguous_submission_diagnostic_keeps_safe_response_context(self) -> None:
        diagnostic = query.ambiguous_submission_diagnostic(
            {
                "chat_status": 200,
                "chat_content_type": "text/event-stream",
                "chat_response_bytes": 42,
                "sse_timed_out": True,
                "submission_response_preview": 'cookie=sessionid=secret {"code":710022002}',
            }
        )
        self.assertIn("HTTP 200", diagnostic)
        self.assertIn("响应 42 字节", diagnostic)
        self.assertIn("SSE读取达到等待上限", diagnostic)
        self.assertIn("710022002", diagnostic)
        self.assertNotIn("secret", diagnostic)

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

    def test_missing_reference_image_request_is_detected(self) -> None:
        self.assertTrue(query.is_missing_reference_image_request("请上传您提到的星澜参考图一。"))
        self.assertTrue(query.is_missing_reference_image_request("请上传星澜的参考图哦~"))
        self.assertFalse(query.is_missing_reference_image_request("参考图中的人物缓慢转身"))

    def test_supplied_reference_image_retries_with_another_account_when_not_recognized(self) -> None:
        task_id = "0" * 32
        result_data = {
            "cookie_string": "sessionid=secret",
            "conversation_id": "12345678901234567",
            "account_id": "account-reference",
            "account_quota_charge_id": "charge-reference",
        }
        meta = {"status": query.STATUS_SUBMITTED, "owner_token_hash": "owner-hash", "image_count": 1}
        with patch.object(query, "expire_task_if_timeout"), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "load_result", return_value=result_data), patch.object(
            query, "fetch_single_chain", new=AsyncMock(return_value=("", "请上传您提到的星澜参考图一。"))
        ), patch.object(query, "save_result") as save_result, patch.object(
            query, "clear_account_current_task"
        ) as clear_account, patch.object(query, "refund_account_quota_once") as refund_account, patch.object(
            query, "record_failed_account"
        ) as record_failed, patch.object(query, "mark_failed"
        ) as mark_failed, patch.object(query, "refund_temp_quota_once") as refund_temp, patch.object(
            query, "retry_submitted_task", return_value=1
        ) as retry_task, patch.object(query, "clear_transient_result") as clear_result, patch.object(
            query, "invalidate_reference_attachment_keys"
        ) as invalidate_cache, patch.object(query, "update_meta") as update_meta:
            response = asyncio.run(query._query_task_once(task_id))
        self.assertEqual(response, {"code": "1", "text": query.REFERENCE_IMAGE_RETRY_TEXT, "url": ""})
        self.assertTrue(
            any(
                call.kwargs.get("extra", {}).get("last_query_classification") == "missing_reference_image"
                for call in save_result.call_args_list
            )
        )
        clear_account.assert_called_once_with("account-reference", task_id)
        refund_account.assert_called_once_with(task_id, "account-reference", "charge-reference")
        record_failed.assert_called_once_with(task_id, "account-reference")
        retry_task.assert_called_once_with(task_id, query.REFERENCE_IMAGE_RETRY_TEXT, max_retries=2, delay_seconds=10)
        clear_result.assert_called_once_with(task_id)
        invalidate_cache.assert_called_once_with([])
        update_meta.assert_called_once_with(task_id, reference_upload_cache_bypass=True)
        mark_failed.assert_not_called()
        refund_temp.assert_not_called()

    def test_missing_reference_image_without_upload_finishes_without_retrying(self) -> None:
        task_id = "0" * 32
        result_data = {
            "cookie_string": "sessionid=secret",
            "conversation_id": "12345678901234567",
            "account_id": "account-reference",
            "account_quota_charge_id": "charge-reference",
        }
        meta = {"status": query.STATUS_SUBMITTED, "owner_token_hash": "owner-hash", "image_count": 0}
        with patch.object(query, "expire_task_if_timeout"), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "load_result", return_value=result_data), patch.object(
            query, "fetch_single_chain", new=AsyncMock(return_value=("", "请上传您提到的星澜参考图一。"))
        ), patch.object(query, "save_result"), patch.object(
            query, "clear_account_current_task"
        ) as clear_account, patch.object(query, "refund_account_quota_once") as refund_account, patch.object(
            query, "mark_failed"
        ) as mark_failed, patch.object(query, "refund_temp_quota_once") as refund_temp, patch.object(
            query, "retry_submitted_task"
        ) as retry_task, patch.object(query, "invalidate_reference_attachment_keys"), patch.object(query, "update_meta"):
            response = asyncio.run(query._query_task_once(task_id))
        self.assertEqual(response, {"code": "0", "text": query.REFERENCE_IMAGE_REQUIRED_TEXT, "url": ""})
        clear_account.assert_called_once_with("account-reference", task_id)
        refund_account.assert_called_once_with(task_id, "account-reference", "charge-reference")
        mark_failed.assert_called_once_with(task_id, query.REFERENCE_IMAGE_REQUIRED_TEXT)
        refund_temp.assert_called_once_with(task_id, "owner-hash")
        retry_task.assert_not_called()

    def test_portrait_protection_retries_once_with_another_account(self) -> None:
        task_id = "0" * 32
        rejection = "出于肖像保护考虑，视频无法使用真实人物照片生成。你可以尝试换其他参考图或文生视频。"
        result_data = {
            "cookie_string": "sessionid=secret",
            "conversation_id": "12345678901234567",
            "account_id": "account-portrait",
            "account_quota_charge_id": "charge-portrait",
            "reference_image_cache_keys": ["cached-reference"],
        }
        meta = {"status": query.STATUS_SUBMITTED, "owner_token_hash": "owner-hash", "image_count": 1, "reference_is_real_person": True}
        with patch.object(query, "expire_task_if_timeout", return_value=False), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "load_result", return_value=result_data), patch.object(
            query, "fetch_single_chain", new=AsyncMock(return_value=("", rejection))
        ), patch.object(query, "save_result"), patch.object(
            query, "invalidate_reference_attachment_keys"
        ) as invalidate_cache, patch.object(query, "update_meta") as update_meta, patch.object(
            query, "clear_account_current_task"
        ) as clear_account, patch.object(query, "refund_account_quota_once") as refund_account, patch.object(
            query, "record_failed_account"
        ) as record_failed, patch.object(query, "retry_submitted_task", return_value=1) as retry_task, patch.object(
            query, "clear_transient_result"
        ) as clear_result:
            response = asyncio.run(query._query_task_once(task_id))

        self.assertEqual(response, {"code": "1", "text": "正在重试中，请稍等！", "url": ""})
        invalidate_cache.assert_called_once_with(["cached-reference"])
        self.assertIn(call(task_id, reference_upload_cache_bypass=True, reference_face_grid_retry=True, reference_force_grid=False), update_meta.call_args_list)
        self.assertIn(call(task_id, portrait_protection_retry_count=1), update_meta.call_args_list)
        clear_account.assert_called_once_with("account-portrait", task_id)
        refund_account.assert_called_once_with(task_id, "account-portrait", "charge-portrait")
        record_failed.assert_called_once_with(task_id, "account-portrait")
        retry_task.assert_called_once_with(task_id, query.PORTRAIT_PROTECTION_RETRY_TEXT, max_retries=2, delay_seconds=10)
        clear_result.assert_called_once_with(task_id)

    def test_second_portrait_protection_rejection_returns_reference_error(self) -> None:
        task_id = "0" * 32
        rejection = "出于肖像保护考虑，视频无法使用真实人物照片生成。"
        result_data = {
            "cookie_string": "sessionid=secret",
            "conversation_id": "12345678901234567",
            "account_id": "account-second",
        }
        meta = {
            "status": query.STATUS_SUBMITTED,
            "owner_token_hash": "owner-hash",
            "image_count": 1,
            "reference_is_real_person": True,
            "portrait_protection_retry_count": 1,
        }
        with patch.object(query, "expire_task_if_timeout", return_value=False), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "load_result", return_value=result_data), patch.object(
            query, "fetch_single_chain", new=AsyncMock(return_value=("", rejection))
        ), patch.object(query, "save_result"), patch.object(
            query, "invalidate_reference_attachment_keys"
        ), patch.object(query, "update_meta"), patch.object(
            query, "clear_account_current_task"
        ), patch.object(query, "refund_account_quota_once"), patch.object(
            query, "record_failed_account"
        ), patch.object(query, "retry_submitted_task") as retry_task, patch.object(
            query, "mark_failed"
        ) as mark_failed, patch.object(query, "refund_temp_quota_once") as refund_temp:
            response = asyncio.run(query._query_task_once(task_id))

        self.assertEqual(response, {"code": "0", "text": query.REFERENCE_IMAGE_INVALID_TEXT, "url": ""})
        retry_task.assert_not_called()
        mark_failed.assert_called_once_with(task_id, query.REFERENCE_IMAGE_INVALID_TEXT)
        refund_temp.assert_called_once_with(task_id, "owner-hash")

    def test_unchecked_portrait_protection_requests_real_person_checkbox_without_retry(self) -> None:
        task_id = "0" * 32
        rejection = "出于肖像保护考虑，视频无法使用真实人物照片生成。"
        result_data = {
            "cookie_string": "sessionid=secret",
            "conversation_id": "12345678901234567",
            "account_id": "account-unchecked",
            "account_quota_charge_id": "charge-unchecked",
            "reference_image_cache_keys": ["unchecked-reference"],
        }
        meta = {
            "status": query.STATUS_SUBMITTED,
            "owner_token_hash": "owner-hash",
            "image_count": 1,
            "reference_is_real_person": False,
        }
        with patch.object(query, "expire_task_if_timeout", return_value=False), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "load_result", return_value=result_data), patch.object(
            query, "fetch_single_chain", new=AsyncMock(return_value=("", rejection))
        ), patch.object(query, "save_result"), patch.object(
            query, "invalidate_reference_attachment_keys"
        ) as invalidate_cache, patch.object(query, "update_meta") as update_meta, patch.object(
            query, "clear_account_current_task"
        ) as clear_account, patch.object(query, "refund_account_quota_once") as refund_account, patch.object(
            query, "record_failed_account"
        ) as record_failed, patch.object(query, "retry_submitted_task") as retry_task, patch.object(
            query, "mark_failed"
        ) as mark_failed, patch.object(query, "refund_temp_quota_once") as refund_temp:
            response = asyncio.run(query._query_task_once(task_id))

        self.assertEqual(response, {"code": "0", "text": query.REFERENCE_REAL_PERSON_REQUIRED_TEXT, "url": ""})
        invalidate_cache.assert_called_once_with(["unchecked-reference"])
        clear_account.assert_called_once_with("account-unchecked", task_id)
        refund_account.assert_called_once_with(task_id, "account-unchecked", "charge-unchecked")
        mark_failed.assert_called_once_with(task_id, query.REFERENCE_REAL_PERSON_REQUIRED_TEXT)
        refund_temp.assert_called_once_with(task_id, "owner-hash")
        update_meta.assert_not_called()
        record_failed.assert_not_called()
        retry_task.assert_not_called()

    def test_policy_text_uses_client_message(self) -> None:
        self.assertEqual(query.POLICY_RETRY_TEXT, "你的输入可能包含违规内容请重试！")

    def test_first_policy_result_retries_without_refunding_user(self) -> None:
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
        ) as clear_account, patch.object(query, "record_failed_account") as record_failed, patch.object(
            query, "settle_account_quota"
        ) as settle_account, patch.object(
            query, "mark_failed"
        ) as mark_failed, patch.object(query, "refund_temp_quota_once") as refund_temp, patch.object(
            query, "retry_submitted_task", return_value=1
        ) as retry_task, patch.object(query, "clear_transient_result") as clear_result:
            response = asyncio.run(query._query_task_once(task_id))
        self.assertEqual(response, {"code": "1", "text": query.POLICY_RETRYING_TEXT, "url": ""})
        clear_account.assert_called_once_with("account-1", task_id)
        record_failed.assert_called_once_with(task_id, "account-1")
        settle_account.assert_called_once_with("account-1", "charge-1")
        retry_task.assert_called_once_with(task_id, query.POLICY_RETRYING_TEXT, max_retries=1, delay_seconds=10)
        clear_result.assert_called_once_with(task_id)
        mark_failed.assert_not_called()
        refund_temp.assert_not_called()

    def test_second_policy_result_finishes_task_and_refunds_user(self) -> None:
        task_id = "0" * 32
        result_data = {
            "cookie_string": "sessionid=secret",
            "conversation_id": "12345678901234567",
            "account_id": "account-2",
            "account_quota_charge_id": "charge-2",
        }
        meta = {
            "status": query.STATUS_SUBMITTED,
            "owner_token_hash": "owner-hash",
            "retry_count": 1,
        }
        with patch.object(query, "expire_task_if_timeout"), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "load_result", return_value=result_data), patch.object(
            query, "fetch_single_chain", new=AsyncMock(return_value=("", query.POLICY_RETRY_TEXT))
        ), patch.object(query, "save_result"), patch.object(
            query, "clear_account_current_task"
        ) as clear_account, patch.object(query, "record_failed_account") as record_failed, patch.object(
            query, "settle_account_quota"
        ) as settle_account, patch.object(query, "retry_submitted_task", return_value=2) as retry_task, patch.object(
            query, "mark_failed"
        ) as mark_failed, patch.object(query, "refund_temp_quota_once") as refund_temp, patch.object(
            query, "clear_transient_result"
        ) as clear_result:
            response = asyncio.run(query._query_task_once(task_id))
        self.assertEqual(response, {"code": "0", "text": query.POLICY_RETRY_TEXT, "url": ""})
        clear_account.assert_called_once_with("account-2", task_id)
        record_failed.assert_called_once_with(task_id, "account-2")
        settle_account.assert_called_once_with("account-2", "charge-2")
        retry_task.assert_called_once_with(task_id, query.POLICY_RETRYING_TEXT, max_retries=1, delay_seconds=10)
        mark_failed.assert_called_once_with(task_id, query.POLICY_RETRY_TEXT)
        refund_temp.assert_called_once_with(task_id, "owner-hash")
        clear_result.assert_not_called()

    def test_reference_task_recovers_matching_recent_conversation(self) -> None:
        task_id = "0" * 32
        recovered_id = "22345678901234567"
        video_url = "https://example.com/reference-result.mp4"
        result_data = {
            "cookie_string": "sessionid=secret",
            "submission_collection_id": "collection-reference",
        }
        meta = {
            "status": query.STATUS_SUBMITTED,
            "image_count": 0,
            "prompt": "参考图中的人物缓慢转身",
            "owner_token_hash": "owner-hash",
        }
        with patch.object(query, "expire_task_if_timeout"), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "load_result", return_value=result_data), patch.object(
            query, "fetch_matching_recent_conversation_id", new=AsyncMock(return_value=recovered_id)
        ) as recover, patch.object(
            query, "fetch_single_chain", new=AsyncMock(return_value=(video_url, ""))
        ), patch.object(query, "save_result") as save_result, patch.object(query, "mark_success"):
            response = asyncio.run(query._query_task_once(task_id))
        self.assertEqual(response, {"code": "2", "text": query.SUCCESS_TEXT, "url": video_url})
        recover.assert_awaited_once_with(
            "sessionid=secret",
            collection_id="collection-reference",
            local_conversation_id="",
            unique_key="",
            prompt="参考图中的人物缓慢转身",
            proxy_server="",
        )
        self.assertTrue(
            any(
                call.kwargs.get("extra", {}).get("conversation_source") == "matched_recent_submission"
                for call in save_result.call_args_list
            )
        )

    def test_ambiguous_api_submission_retries_three_proxies_with_same_account(self) -> None:
        task_id = "0" * 32
        meta = {"status": query.STATUS_SUBMITTED, "prompt": "复杂提示词", "owner_token_hash": "owner-hash"}
        result_data = {
            "cookie_string": "sessionid=secret",
            "submission_collection_id": "collection-ambiguous",
            "submission_ambiguous": True,
            "submission_ambiguous_at": (datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat(),
            "account_id": "account-1",
            "account_quota_charge_id": "charge-1",
            "proxy_source": "api",
            "proxy_node_id": "node-old",
        }
        with patch.object(query, "expire_task_if_timeout", return_value=False), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "load_result", return_value=result_data), patch.object(
            query, "fetch_matching_recent_conversation_id", new=AsyncMock(return_value="")
        ), patch.object(query, "save_result"), patch.object(
            query, "refund_account_quota_once"
        ) as refund_account, patch.object(
            query, "clear_account_current_task"
        ) as clear_account, patch.object(
            query, "record_failed_account"
        ) as record_failed, patch.object(
            query, "retry_ambiguous_proxy_task", return_value=1
        ) as retry_proxy, patch.object(
            query, "retry_ambiguous_submitted_task"
        ) as retry_task, patch.object(query, "clear_transient_result") as clear_result:
            response = asyncio.run(query._query_task_once(task_id))
        self.assertEqual(response, {"code": "1", "text": "正在重试中，请稍等！", "url": ""})
        clear_account.assert_not_called()
        refund_account.assert_called_once_with(task_id, "account-1", "charge-1")
        record_failed.assert_not_called()
        retry_proxy.assert_called_once()
        retry_args, retry_kwargs = retry_proxy.call_args
        self.assertEqual((retry_args[0], retry_args[2], retry_args[3]), (task_id, "account-1", "node-old"))
        self.assertTrue(retry_args[1].startswith("提交后未取得有效会话，正在更换代理重试；后台诊断："))
        self.assertEqual(retry_kwargs, {"delay_seconds": 3})
        retry_task.assert_not_called()
        clear_result.assert_called_once_with(task_id)

    def test_ambiguous_api_submission_switches_account_after_three_proxy_retries(self) -> None:
        task_id = "0" * 32
        meta = {
            "status": query.STATUS_SUBMITTED,
            "prompt": "复杂提示词",
            "owner_token_hash": "owner-hash",
            "ambiguous_proxy_retry_count": 3,
        }
        result_data = {
            "cookie_string": "sessionid=secret",
            "submission_collection_id": "collection-ambiguous",
            "submission_ambiguous": True,
            "submission_ambiguous_at": (datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat(),
            "account_id": "account-1",
            "account_quota_charge_id": "charge-4",
            "proxy_source": "api",
            "proxy_node_id": "node-fourth",
        }
        with patch.object(query, "expire_task_if_timeout", return_value=False), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "load_result", return_value=result_data), patch.object(
            query, "fetch_matching_recent_conversation_id", new=AsyncMock(return_value="")
        ), patch.object(query, "save_result"), patch.object(
            query, "refund_account_quota_once"
        ) as refund_account, patch.object(
            query, "clear_account_current_task"
        ) as clear_account, patch.object(
            query, "record_failed_account"
        ) as record_failed, patch.object(
            query, "retry_ambiguous_proxy_task"
        ) as retry_proxy, patch.object(
            query, "retry_ambiguous_submitted_task", return_value=1
        ) as retry_task, patch.object(query, "clear_transient_result") as clear_result, patch.object(
            query, "update_meta"
        ) as update_meta:
            response = asyncio.run(query._query_task_once(task_id))
        self.assertEqual(response, {"code": "1", "text": "正在重试中，请稍等！", "url": ""})
        refund_account.assert_called_once_with(task_id, "account-1", "charge-4")
        clear_account.assert_called_once_with("account-1", task_id)
        record_failed.assert_called_once_with(task_id, "account-1")
        retry_proxy.assert_not_called()
        retry_task.assert_called_once()
        retry_args, retry_kwargs = retry_task.call_args
        self.assertEqual(retry_args[0], task_id)
        self.assertTrue(retry_args[1].startswith("提交后未取得有效会话，正在安全重试；后台诊断："))
        self.assertEqual(retry_kwargs, {"max_retries": 2, "delay_seconds": 3})
        clear_result.assert_called_once_with(task_id)
        update_meta.assert_called_once_with(task_id, proxy_retry_avoid_node_id="node-fourth")

    def test_result_query_uses_the_submission_proxy(self) -> None:
        task_id = "0" * 32
        result_data = {
            "cookie_string": "sessionid=secret",
            "conversation_id": "12345678901234567",
            "proxy_source": "api",
            "proxy_server": "http://proxy.example:18080",
        }
        meta = {"status": query.STATUS_SUBMITTED, "owner_token_hash": "owner-hash"}
        with patch.object(query, "expire_task_if_timeout"), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "load_result", return_value=result_data), patch.object(
            query, "fetch_single_chain", new=AsyncMock(return_value=("", "正在生成"))
        ) as fetch_chain, patch.object(query, "save_result"):
            asyncio.run(query._query_task_once(task_id))
        fetch_chain.assert_awaited_once_with(
            "sessionid=secret",
            "12345678901234567",
            proxy_server="http://proxy.example:18080",
        )

    def test_expired_api_query_proxy_is_refreshed_once(self) -> None:
        task_id = "0" * 32
        result_data = {"proxy_source": "api", "proxy_server": "http://old.example:18080"}
        operation = AsyncMock(side_effect=[httpx.ProxyError("expired proxy"), "recovered"])
        settings = SimpleNamespace(
            proxy_api_url="https://proxy-api.example/get",
            proxy_api_timeout_seconds=20,
            proxy_api_scheme="http",
        )
        with patch.object(query, "load_settings", return_value=settings), patch.object(
            query,
            "fetch_proxy_from_api",
            new=AsyncMock(return_value={"server": "http://new.example:18081"}),
        ) as refresh, patch.object(query, "save_result") as save_result:
            response = asyncio.run(query._run_task_query(task_id, result_data, operation))
        self.assertEqual(response, "recovered")
        self.assertEqual(operation.await_args_list[0].args, ("http://old.example:18080",))
        self.assertEqual(operation.await_args_list[1].args, ("http://new.example:18081",))
        refresh.assert_awaited_once()
        self.assertEqual(save_result.call_args.kwargs["extra"]["query_proxy_refresh_count"], 1)

    def test_confirmed_session_refreshes_api_proxy_after_twelve_minutes(self) -> None:
        task_id = "0" * 32
        result_data = {
            "proxy_source": "api",
            "proxy_server": "http://old.example:18080",
            "conversation_id": "12345678901234567",
        }
        settings = SimpleNamespace(
            proxy_api_url="https://proxy-api.example/get",
            proxy_api_timeout_seconds=20,
            proxy_api_scheme="http",
        )
        with patch.object(query, "load_settings", return_value=settings), patch.object(
            query, "fetch_proxy_from_api", new=AsyncMock(return_value={"server": "http://new.example:18081"})
        ) as refresh, patch.object(query, "save_result") as save_result:
            asyncio.run(query._refresh_confirmed_query_proxy_if_due(task_id, result_data, datetime.now(timezone.utc) - timedelta(minutes=13)))
        refresh.assert_awaited_once()
        self.assertEqual(result_data["proxy_server"], "http://new.example:18081")
        self.assertEqual(save_result.call_args.kwargs["extra"]["query_proxy_refresh_reason"], "confirmed_session_stalled")

    def test_pending_policy_retry_remains_in_progress(self) -> None:
        task_id = "0" * 32
        meta = {"status": "pending", "owner_token_hash": "owner-hash", "error": query.POLICY_RETRYING_TEXT}
        with patch.object(query, "expire_task_if_timeout"), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "mark_failed") as mark_failed, patch.object(
            query, "refund_temp_quota_once"
        ) as refund_temp:
            response = asyncio.run(query._query_task_once(task_id))
        self.assertEqual(response, {"code": "1", "text": query.POLICY_RETRYING_TEXT, "url": ""})
        mark_failed.assert_not_called()
        refund_temp.assert_not_called()

    def test_failed_policy_retry_is_terminal_and_refunded(self) -> None:
        task_id = "0" * 32
        meta = {"status": query.STATUS_FAILED, "owner_token_hash": "owner-hash", "error": query.POLICY_RETRY_TEXT}
        with patch.object(query, "expire_task_if_timeout"), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "refund_temp_quota_once") as refund_temp:
            response = asyncio.run(query._query_task_once(task_id))
        self.assertEqual(response, {"code": "0", "text": query.POLICY_RETRY_TEXT, "url": ""})
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
        ) as clear_account, patch.object(query, "exhaust_timed_out_account") as exhaust_account, patch.object(
            query, "record_failed_account"
        ) as record_failed, patch.object(query, "retry_submitted_task", return_value=1) as retry_task, patch.object(
            query, "clear_transient_result"
        ):
            response = asyncio.run(query._query_task_once("0" * 32))
        self.assertEqual(response, {"code": "1", "text": query.ACCOUNT_QUOTA_RETRY_TEXT, "url": ""})
        clear_account.assert_called_once_with("account-1", "0" * 32)
        exhaust_account.assert_called_once_with("account-1", "charge-1")
        record_failed.assert_called_once_with("0" * 32, "account-1")
        retry_task.assert_called_once_with("0" * 32, query.ACCOUNT_QUOTA_RETRY_TEXT, max_retries=2, delay_seconds=10)

    def test_generation_failure_requeues_submitted_task_and_clears_stale_result(self) -> None:
        task_id = "0" * 32
        result_data = {
            "cookie_string": "sessionid=secret",
            "conversation_id": "12345678901234567",
            "account_id": "account-1",
            "account_quota_charge_id": "charge-1",
        }
        meta = {"status": query.STATUS_SUBMITTED, "owner_token_hash": "owner-hash"}
        with patch.object(query, "expire_task_if_timeout", return_value=False), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "load_result", return_value=result_data), patch.object(
            query, "fetch_single_chain", new=AsyncMock(return_value=("", automation.FINAL_FAILURE_TEXT))
        ), patch.object(query, "save_result"), patch.object(
            query, "clear_account_current_task"
        ) as clear_account, patch.object(query, "record_failed_account") as record_failed, patch.object(
            query, "settle_account_quota"
        ) as settle_account, patch.object(query, "retry_submitted_task", return_value=1) as retry_task, patch.object(
            query, "clear_transient_result"
        ) as clear_result:
            response = asyncio.run(query._query_task_once(task_id))
        self.assertEqual(response, {"code": "1", "text": query.RETRY_GENERATING_TEXT, "url": ""})
        clear_account.assert_called_once_with("account-1", task_id)
        record_failed.assert_called_once_with(task_id, "account-1")
        settle_account.assert_called_once_with("account-1", "charge-1")
        retry_task.assert_called_once_with(task_id, automation.FINAL_FAILURE_TEXT, max_retries=2, delay_seconds=10)
        clear_result.assert_called_once_with(task_id)

    def test_guest_mode_disables_account_refunds_quota_and_retries(self) -> None:
        task_id = "0" * 32
        guest_text = "游客模式暂不支持生成图片和视频，请登录后再试"
        result_data = {
            "cookie_string": "sessionid=expired",
            "conversation_id": "12345678901234567",
            "account_id": "account-guest",
            "account_quota_charge_id": "charge-guest",
        }
        meta = {"status": query.STATUS_SUBMITTED, "owner_token_hash": "owner-hash"}
        with patch.object(query, "expire_task_if_timeout", return_value=False), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "load_result", return_value=result_data), patch.object(
            query, "fetch_single_chain", new=AsyncMock(return_value=("", guest_text))
        ), patch.object(query, "save_result"), patch.object(
            query, "clear_account_current_task"
        ) as clear_account, patch.object(query, "record_failed_account") as record_failed, patch.object(
            query, "disable_account_for_login"
        ) as disable_account, patch.object(query, "refund_account_quota_once") as refund_account, patch.object(
            query, "settle_account_quota"
        ) as settle_account, patch.object(query, "retry_submitted_task", return_value=1) as retry_task, patch.object(
            query, "clear_transient_result"
        ) as clear_result:
            response = asyncio.run(query._query_task_once(task_id))

        self.assertEqual(response, {"code": "1", "text": query.RETRY_GENERATING_TEXT, "url": ""})
        clear_account.assert_called_once_with("account-guest", task_id)
        record_failed.assert_called_once_with(task_id, "account-guest")
        disable_account.assert_called_once_with("account-guest", "Dola 登录状态失效（游客模式）")
        refund_account.assert_called_once_with(task_id, "account-guest", "charge-guest")
        settle_account.assert_not_called()
        retry_task.assert_called_once_with(task_id, guest_text, max_retries=2, delay_seconds=10)
        clear_result.assert_called_once_with(task_id)

    def test_ten_second_limit_marks_account_refunds_and_retries_with_exact_reason(self) -> None:
        task_id = "1" * 32
        result_data = {
            "cookie_string": "sessionid=ten-seconds",
            "conversation_id": "12345678901234567",
            "account_id": "account-ten-seconds",
            "account_quota_charge_id": "charge-ten-seconds",
        }
        meta = {"status": query.STATUS_SUBMITTED, "owner_token_hash": "owner-hash"}
        with patch.object(query, "expire_task_if_timeout", return_value=False), patch.object(
            query, "get_meta", return_value=meta
        ), patch.object(query, "load_result", return_value=result_data), patch.object(
            query, "fetch_single_chain", new=AsyncMock(return_value=("", query.TEN_SECOND_LIMIT_TEXT))
        ), patch.object(query, "save_result"), patch.object(
            query, "mark_account_ten_second_limit"
        ) as mark_ten_seconds, patch.object(query, "clear_account_current_task") as clear_account, patch.object(
            query, "refund_account_quota_once"
        ) as refund_account, patch.object(query, "record_failed_account") as record_failed, patch.object(
            query, "retry_submitted_task", return_value=1
        ) as retry_task, patch.object(query, "clear_transient_result") as clear_result:
            response = asyncio.run(query._query_task_once(task_id))

        self.assertEqual(response, {"code": "1", "text": query.TEN_SECOND_LIMIT_TEXT, "url": ""})
        mark_ten_seconds.assert_called_once_with("account-ten-seconds")
        clear_account.assert_called_once_with("account-ten-seconds", task_id)
        refund_account.assert_called_once_with(task_id, "account-ten-seconds", "charge-ten-seconds")
        record_failed.assert_called_once_with(task_id, "account-ten-seconds")
        retry_task.assert_called_once_with(task_id, query.TEN_SECOND_LIMIT_TEXT, max_retries=2, delay_seconds=10)
        clear_result.assert_called_once_with(task_id)

    def test_global_task_timeout_returns_terminal_failure(self) -> None:
        meta = {"status": query.STATUS_FAILED, "owner_token_hash": "owner-hash", "error": "超时生成失败"}
        with patch.object(query, "expire_task_if_timeout", return_value=True), patch.object(
            query, "get_meta", return_value=meta
        ):
            response = asyncio.run(query._query_task_once("0" * 32))
        self.assertEqual(response, {"code": "0", "text": "超时生成失败", "url": ""})


if __name__ == "__main__":
    unittest.main()
