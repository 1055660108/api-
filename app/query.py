from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any

import httpx

from .accounts import clear_account_current_task, exhaust_account_quota, refund_account_quota, settle_account_quota
from .automation import is_final_generation_failure
from .store import STATUS_FAILED, STATUS_SUBMITTED, STATUS_SUCCESS, clear_transient_result, expire_task_if_timeout, get_meta, load_result, mark_account_refund_once, mark_failed, mark_result_once, mark_success, record_failed_account, record_retry, retry_submitted_task, save_result
from .temp_access import refund_temp_quota_hash


_QUERY_LOCKS: dict[str, asyncio.Lock] = {}


def _query_lock(task_id: str) -> asyncio.Lock:
    lock = _QUERY_LOCKS.get(task_id)
    if lock is None:
        lock = asyncio.Lock()
        _QUERY_LOCKS[task_id] = lock
    return lock
from .textfix import repair_text


GENERATING_TEXT = "正在为您生成视频，请稍候...本次使用 Seedance 2.0生成，预计等待 1-3 分钟。"
RETRY_GENERATING_TEXT = "视频生成中请稍后..."
SUCCESS_TEXT = "已成功"
POLICY_RETRY_TEXT = "你的输入可能包含违规内容请重试！"
ACCOUNT_QUOTA_RETRY_TEXT = "当前账号额度不足，正在切换账号重试"


def refund_temp_quota_once(task_id: str, owner_hash: str) -> None:
    if owner_hash and refund_temp_quota_hash(owner_hash, task_id):
        mark_result_once(task_id, "temp_quota_refunded", True)


def refund_account_quota_once(task_id: str, account_id: str, charge_id: str = "") -> None:
    if account_id and refund_account_quota(account_id, charge_id or task_id):
        mark_account_refund_once(task_id, account_id)


RECENT_CONV_URL = (
    "https://www.dola.com/im/chain/recent_conv?"
    "version_code=20800&language=zh&device_platform=web&aid=495671&real_aid=495671"
    "&pkg_type=release_version&device_id=111&pc_version=3.23.7&web_id=111"
    "&tea_uuid=111&region=JP&sys_region=JP&samantha_web=1&web_platform=browser"
    "&use-olympus-account=1&web_tab_id=111"
)

SINGLE_CHAIN_URL = (
    "https://www.dola.com/im/chain/single?"
    "version_code=20800&language=zh&device_platform=web&aid=495671&real_aid=495671"
    "&pkg_type=release_version&device_id=111&pc_version=3.23.7&web_id=111"
    "&tea_uuid=111&region=JP&sys_region=JP&samantha_web=1&web_platform=browser"
    "&use-olympus-account=1&web_tab_id=111"
)

QUERY_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
QUERY_CLIENT_HINTS = {
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
FAILURE_TEXT_MARKERS = ("失败", "无法生成", "违规", "游客模式", "请登录后再试")
SENSITIVE_DIAGNOSTIC_RE = re.compile(
    r"(?i)(cookie|authorization|oauth_token(?:_v2)?|sessionid|sid_tt|sid_guard|odin_tt|msToken|passport_csrf_token(?:_default)?)"
    r"(\s*[:=]\s*)([^\s,;\]}]+)"
)
SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:token|access_token|refresh_token|msToken|sessionid|sid_tt|oauth_token|a_bogus)=)[^&#\s]+"
)
BEARER_TOKEN_RE = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;\]}]+")


class DolaQueryError(RuntimeError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def is_generation_failure_text(text: str) -> bool:
    value = str(text or "")
    return any(marker in value for marker in FAILURE_TEXT_MARKERS)


def is_suspected_policy_false_positive(text: str) -> bool:
    value = str(text or "")
    return "输入可能包含违规内容" in value or "可能包含违规" in value


def is_account_quota_insufficient(text: str) -> bool:
    value = re.sub(r"\s+", "", str(text or ""))
    direct_markers = ("额度不足", "额度已用完", "次数不足", "次数已用完", "余额不足")
    if any(marker in value for marker in direct_markers):
        return True
    return "视频生成额度" in value and "剩余" in value and "无法生成" in value


def sanitize_query_diagnostic(value: Any) -> str:
    text = repair_text(str(value or "")).replace("\r", " ").replace("\n", " ")
    text = BEARER_TOKEN_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    text = SENSITIVE_DIAGNOSTIC_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    text = SENSITIVE_QUERY_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    return re.sub(r"\s+", " ", text).strip()[:500]


def classify_query_error(exc: Exception) -> str:
    if isinstance(exc, DolaQueryError):
        return exc.category
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    if isinstance(exc, httpx.NetworkError):
        return "network"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return "invalid_response"
    return "unexpected"


def query_error_diagnostic(exc: Exception) -> dict[str, str]:
    return {
        "last_query_error": sanitize_query_diagnostic(exc),
        "last_query_error_category": classify_query_error(exc),
    }


def _headers(cookie: str) -> dict[str, str]:
    return {
        "agw-js-conv": "str",
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json; encoding=utf-8",
        "user-agent": QUERY_UA,
        "cookie": cookie,
        **QUERY_CLIENT_HINTS,
    }


def _recent_payload() -> dict[str, Any]:
    return {
        "cmd": 3200,
        "uplink_body": {
            "pull_recent_conv_chain_uplink_body": {
                "limit": 10,
                "message_count_per_conv": 10,
                "api_version": 1,
                "conv_version": 0,
                "direction": 3,
                "option": {
                    "not_need_message": True,
                    "need_complete_conversation": True,
                    "need_coco_conversation": True,
                    "need_coco_bot": True,
                },
            }
        },
        "sequence_id": "111",
        "channel": 2,
        "version": "1",
    }


def _single_payload(conversation_id: str) -> dict[str, Any]:
    return {
        "cmd": 3100,
        "uplink_body": {
            "pull_singe_chain_uplink_body": {
                "conversation_id": conversation_id,
                "anchor_index": 111,
                "conversation_type": 3,
                "direction": 1,
                "limit": 20,
                "ext": {},
                "filter": {"index_list": []},
                "evaluate_ab_params": "",
                "evaluate_common_params": "",
            }
        },
        "sequence_id": "111",
        "channel": 2,
        "version": "1",
    }


def _try_parse_json_string(value: str) -> Any:
    text = value.strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _walk(value: Any, depth: int = 0):
    if depth > 40:
        return
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item, depth + 1)
    elif isinstance(value, str):
        parsed = _try_parse_json_string(value)
        if parsed is not None:
            yield from _walk(parsed, depth + 1)


def extract_conversation_id(data: Any) -> str:
    candidates: list[tuple[tuple[int, int], str]] = []
    for position, item in enumerate(_walk(data)):
        if not isinstance(item, dict):
            continue
        cid = _normalize_conversation_id(item.get("conversation_id"))
        if not cid:
            continue
        candidates.append((_item_order_key(item, position), cid))
    return max(candidates, default=((0, 0), ""))[1]


def extract_conversation_id_from_sse(text: str) -> str:
    if not text:
        return ""
    patterns = (
        r'\\?"conversation_id\\?"\s*:\s*\\?"?(\d{17})',
        r"conversation_id(?:\\\\?\"|)\s*[:=]\s*(?:\\\\?\")?(\d{17})",
        r"/chat/(\d{17})(?:\D|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def extract_main_url(data: Any) -> str:
    message = _latest_single_chain_message(data)
    for item in _walk(message):
        if isinstance(item, dict) and "video_model" in item:
            video_model = item.get("video_model")
            parsed = _try_parse_json_string(video_model) if isinstance(video_model, str) else video_model
            for nested in _walk(parsed):
                if isinstance(nested, dict):
                    main_url = nested.get("main_url")
                    if isinstance(main_url, str) and main_url:
                        return main_url
        if isinstance(item, dict):
            main_url = item.get("main_url")
            if isinstance(main_url, str) and main_url:
                return main_url
    if message:
        return ""
    for item in _walk(data):
        if isinstance(item, dict) and "video_model" in item:
            video_model = item.get("video_model")
            parsed = _try_parse_json_string(video_model) if isinstance(video_model, str) else video_model
            for nested in _walk(parsed):
                if isinstance(nested, dict):
                    main_url = nested.get("main_url")
                    if isinstance(main_url, str) and main_url:
                        return main_url
    for item in _walk(data):
        if isinstance(item, dict):
            main_url = item.get("main_url")
            if isinstance(main_url, str) and main_url:
                return main_url
    return ""


def _single_chain_messages(data: Any) -> list[dict[str, Any]]:
    body = data.get("downlink_body", {}) if isinstance(data, dict) else {}
    chain = body.get("pull_singe_chain_downlink_body", {}) if isinstance(body, dict) else {}
    messages = chain.get("messages", []) if isinstance(chain, dict) else []
    return [item for item in messages if isinstance(item, dict)]


def _normalize_conversation_id(value: Any) -> str:
    text = str(value or "")
    return text if text.isdigit() and len(text) == 17 else ""


def _numeric_order_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _item_order_key(item: dict[str, Any], position: int) -> tuple[int, int]:
    for key in ("message_index", "index", "create_time_ms", "create_time", "update_time", "message_id"):
        parsed = _numeric_order_value(item.get(key))
        if parsed is not None:
            return parsed, position
    return 0, position


def _latest_single_chain_message(data: Any) -> dict[str, Any]:
    messages = _single_chain_messages(data)
    return max(enumerate(messages), key=lambda pair: _item_order_key(pair[1], pair[0]), default=(-1, {}))[1]


def validate_conversation_ownership(data: Any, conversation_id: str) -> None:
    expected = _normalize_conversation_id(conversation_id)
    if not expected:
        raise DolaQueryError("invalid_conversation_id", "invalid Dola conversation id")
    body = data.get("downlink_body", {}) if isinstance(data, dict) else {}
    chain = body.get("pull_singe_chain_downlink_body", {}) if isinstance(body, dict) else {}
    observed = {
        normalized
        for item in _walk(chain)
        if isinstance(item, dict)
        for normalized in [_normalize_conversation_id(item.get("conversation_id"))]
        if normalized
    }
    chain_id = _normalize_conversation_id(chain.get("conversation_id")) if isinstance(chain, dict) else ""
    if chain_id:
        observed.add(chain_id)
    if observed and expected not in observed:
        raise DolaQueryError("conversation_mismatch", "Dola conversation ownership mismatch")


def _collect_strings(value: Any, depth: int = 0) -> list[str]:
    if depth > 40:
        return []
    if isinstance(value, str):
        parsed = _try_parse_json_string(value)
        if parsed is not None:
            return [value, *_collect_strings(parsed, depth + 1)]
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_collect_strings(item, depth + 1))
        return out
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_collect_strings(item, depth + 1))
        return out
    return []


def _extract_wait_text(data: Any) -> str:
    values: list[str] = []
    pattern = re.compile(r"预计等待\s*[^。！？\n\r，,]*?(?:分钟|秒|小时)")
    for raw_text in _collect_strings(data):
        text = repair_text(raw_text)
        for match in pattern.findall(text):
            if match and match not in values:
                values.append(match)
    return "，".join(values)


def extract_tts_content(data: Any) -> str:
    message = _latest_single_chain_message(data)
    text = ""
    tts = message.get("tts_content")
    if isinstance(tts, str):
        text = repair_text(tts.strip())
    wait_text = _extract_wait_text(message)
    if wait_text:
        return text if wait_text in text else f"{text}{wait_text}" if text else wait_text
    return text


def decode_main_url(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            padded = cleaned + "=" * (-len(cleaned) % 4)
            data = decoder(padded.encode("ascii"))
            text = data.decode("utf-8", errors="strict")
            if text.startswith("http://") or text.startswith("https://"):
                return text
        except Exception:
            continue
    return ""


async def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    timeout = httpx.Timeout(30.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        try:
            return json.loads(response.content.decode("utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DolaQueryError("invalid_response", "Dola returned an invalid JSON response") from exc


async def fetch_recent_conversation_id(cookie: str) -> str:
    data = await _post_json(RECENT_CONV_URL, _headers(cookie), _recent_payload())
    return extract_conversation_id(data)


async def fetch_single_chain(cookie: str, conversation_id: str) -> tuple[str, str]:
    data = await _post_json(SINGLE_CHAIN_URL, _headers(cookie), _single_payload(conversation_id))
    validate_conversation_ownership(data, conversation_id)
    return extract_main_url(data), extract_tts_content(data)


async def _query_task_once(task_id: str) -> dict[str, str]:
    expire_task_if_timeout(task_id)
    meta = get_meta(task_id)
    if meta.get("status") not in {STATUS_SUBMITTED, STATUS_SUCCESS}:
        if meta.get("status") == STATUS_FAILED and is_suspected_policy_false_positive(str(meta.get("error") or "")):
            return {"code": "0", "text": POLICY_RETRY_TEXT, "url": ""}
        if int(meta.get("retry_count") or 0) > 0 and is_final_generation_failure(str(meta.get("error") or "")):
            return {"code": "1", "text": RETRY_GENERATING_TEXT, "url": ""}
        if meta.get("status") == STATUS_FAILED and str(meta.get("error") or "") == "超时生成失败":
            return {"code": "0", "text": "超时生成失败", "url": ""}
        if meta.get("status") == STATUS_FAILED and str(meta.get("error") or "") == "任务超时未执行":
            return {"code": "0", "text": "任务超时未执行", "url": ""}
        if meta.get("status") == STATUS_FAILED and str(meta.get("error") or "") == "browser timeout":
            return {"code": "0", "text": "浏览器超时", "url": ""}
        if meta.get("status") == STATUS_FAILED and str(meta.get("error") or "") == "region restricted":
            return {"code": "0", "text": "Dola 当前地区不可用", "url": ""}
        if meta.get("status") == STATUS_FAILED and int(meta.get("retry_count") or 0) >= 2:
            return {"code": "0", "text": "多次生成失败", "url": ""}
        if meta.get("status") == STATUS_FAILED:
            return {"code": "0", "text": str(meta.get("error") or "失败"), "url": ""}
        return {"code": "0", "text": "", "url": ""}

    result = load_result(task_id)
    cached_url = str(result.get("decoded_main_url") or "")
    if cached_url:
        account_id = str(result.get("account_id") or "")
        if account_id:
            clear_account_current_task(account_id, task_id)
        mark_success(task_id)
        return {"code": "2", "text": SUCCESS_TEXT, "url": cached_url}

    cookie = str(result.get("cookie_string") or "")
    if not cookie:
        return {"code": "1", "text": "没有文本", "url": ""}

    sse_text = str(
        result.get("sse_response_text")
        or result.get("chat_response_text")
        or result.get("chat_response_preview")
        or ""
    )
    conversation_id = extract_conversation_id_from_sse(sse_text)
    conversation_source = "submit_sse" if conversation_id else ""
    if not conversation_id:
        conversation_id = str(result.get("conversation_id") or "")
        conversation_source = "submit_result" if conversation_id else ""
    if conversation_id:
        save_result(task_id, conversation_id=conversation_id, extra={"conversation_source": conversation_source})
    else:
        save_result(
            task_id,
            extra={
                "last_query_error": "Dola submission did not return a conversation id",
                "last_query_error_category": "missing_submission_conversation",
                "conversation_source": "missing",
            },
        )
        return {"code": "1", "text": "没有文本", "url": ""}

    try:
        main_url_encoded, tts_content = await fetch_single_chain(cookie, conversation_id)
    except Exception as exc:
        save_result(task_id, extra=query_error_diagnostic(exc))
        return {"code": "1", "text": "没有文本", "url": ""}

    if main_url_encoded:
        decoded = decode_main_url(main_url_encoded)
        if decoded:
            account_id = str(result.get("account_id") or "")
            if account_id:
                settle_account_quota(account_id, str(result.get("account_quota_charge_id") or ""))
                clear_account_current_task(account_id, task_id)
            save_result(
                task_id,
                extra={"decoded_main_url": decoded},
                remove={"main_url", "cookie_string", "cookies", "conversation_id", "last_query_error", "last_query_error_category"},
            )
            mark_success(task_id)
            return {"code": "2", "text": SUCCESS_TEXT, "url": decoded}

    text = tts_content or "没有文本"
    query_classification = "generating"
    if is_account_quota_insufficient(text):
        query_classification = "account_quota_insufficient"
    elif is_suspected_policy_false_positive(text):
        query_classification = "suspected_policy_text"
    elif is_generation_failure_text(text):
        query_classification = "generation_failure_text"
    save_result(
        task_id,
        extra={
            "last_query_classification": query_classification,
            "last_query_text_excerpt": sanitize_query_diagnostic(text),
            "conversation_source": conversation_source,
        },
    )
    account_id = str(result.get("account_id") or "")
    if is_account_quota_insufficient(text):
        if account_id:
            clear_account_current_task(account_id, task_id)
            exhaust_account_quota(account_id, str(result.get("account_quota_charge_id") or ""))
            record_failed_account(task_id, account_id)
        retry_count = retry_submitted_task(task_id, ACCOUNT_QUOTA_RETRY_TEXT, max_retries=5, delay_seconds=10)
        if retry_count < 5:
            clear_transient_result(task_id)
            return {"code": "1", "text": ACCOUNT_QUOTA_RETRY_TEXT, "url": ""}
        meta = get_meta(task_id)
        refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
        return {"code": "0", "text": "多个账号额度均不足，请稍后重试", "url": ""}
    if is_generation_failure_text(text):
        if is_suspected_policy_false_positive(text):
            if account_id:
                clear_account_current_task(account_id, task_id)
                record_failed_account(task_id, account_id)
                refund_account_quota_once(task_id, account_id, str(result.get("account_quota_charge_id") or ""))
            retry_count = retry_submitted_task(task_id, POLICY_RETRY_TEXT, max_retries=2, delay_seconds=45)
            if retry_count < 2:
                clear_transient_result(task_id)
                return {"code": "1", "text": POLICY_RETRY_TEXT, "url": ""}
            meta = get_meta(task_id)
            refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
            return {"code": "0", "text": POLICY_RETRY_TEXT, "url": ""}
        if account_id:
            clear_account_current_task(account_id, task_id)
        if is_final_generation_failure(text):
            if account_id:
                record_failed_account(task_id, account_id)
            retry_count = record_retry(task_id, text[:500])
            if retry_count >= 2:
                meta = get_meta(task_id)
                mark_failed(task_id, "多次生成失败")
                refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
                return {"code": "0", "text": "多次生成失败", "url": ""}
            return {"code": "1", "text": RETRY_GENERATING_TEXT, "url": ""}
        if account_id:
            record_failed_account(task_id, account_id)
            refund_account_quota_once(task_id, account_id, str(result.get("account_quota_charge_id") or ""))
        retry_count = record_retry(task_id, text[:500])
        if retry_count >= 2:
            meta = get_meta(task_id)
            mark_failed(task_id, "多次生成失败")
            refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
            return {"code": "0", "text": "多次生成失败", "url": ""}
        return {"code": "0", "text": text, "url": ""}
    return {"code": "1", "text": GENERATING_TEXT, "url": ""}


async def query_task(task_id: str) -> dict[str, str]:
    async with _query_lock(task_id):
        return await _query_task_once(task_id)
