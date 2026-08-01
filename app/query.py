from __future__ import annotations

import asyncio
import base64
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import httpx

from .account_proxies import account_proxy_entries, account_proxy_url
from .accounts import clear_account_current_task, disable_account_for_login, exhaust_account_quota, exhaust_timed_out_account, mark_account_slider_verification, mark_account_ten_second_limit, refund_account_quota, settle_account_quota
from .automation import invalidate_reference_attachment_keys, is_final_generation_failure
from .config import load_settings
from .proxy_manager import acquire_dola_subscription_proxy, fetch_proxy_from_api, release_dola_subscription_proxy
from .store import AMBIGUOUS_PROXY_RETRIES_PER_ACCOUNT, STATUS_FAILED, STATUS_SUBMITTED, STATUS_SUCCESS, clear_transient_result, expire_task_if_timeout, get_meta, load_result, mark_account_refund_once, mark_failed, mark_late_result_success, mark_result_once, mark_success, parse_time, record_failed_account, retry_ambiguous_proxy_task, retry_ambiguous_submitted_task, retry_submitted_task, save_result, task_retry_limit, update_meta
from .temp_access import refund_temp_quota_hash


@dataclass
class _QueryLockEntry:
    lock: asyncio.Lock
    users: int = 0


_QUERY_LOCKS: dict[str, _QueryLockEntry] = {}


@asynccontextmanager
async def _query_lock(task_id: str):
    entry = _QUERY_LOCKS.get(task_id)
    if entry is None:
        entry = _QueryLockEntry(asyncio.Lock())
        _QUERY_LOCKS[task_id] = entry
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users = max(0, entry.users - 1)
        if entry.users == 0 and _QUERY_LOCKS.get(task_id) is entry:
            _QUERY_LOCKS.pop(task_id, None)
from .textfix import repair_text


GENERATING_TEXT = "正在为您生成视频，请稍候...本次使用 Seedance 2.0生成，预计等待 3~8 分钟。"
AMBIGUOUS_SUBMISSION_RECOVERY_SECONDS = 120
CONFIRMED_QUERY_PROXY_REFRESH_SECONDS = 12 * 60
RETRY_GENERATING_TEXT = "视频生成中请稍后..."
SUCCESS_TEXT = "已成功"
POLICY_RETRY_TEXT = "你的输入可能包含违规内容请重试！"
POLICY_RETRYING_TEXT = "检测到内容异常，正在自动重试..."
ACCOUNT_QUOTA_RETRY_TEXT = "当前账号额度不足，正在切换账号重试"
REFERENCE_IMAGE_REQUIRED_TEXT = "未收到可用参考图，请重新上传参考图后再提交"
REFERENCE_IMAGE_RETRY_TEXT = "参考图识别异常，正在更换账号重新上传"
REFERENCE_IMAGE_INVALID_TEXT = "参考图异常，请重试！"
REFERENCE_REAL_PERSON_REQUIRED_TEXT = "请选择勾选真人按钮并重试"
PORTRAIT_PROTECTION_RETRY_TEXT = "参考图触发肖像保护，正在更换账号重试"
TEN_SECOND_LIMIT_TEXT = "Currently generating videos longer than 10 seconds is not supported, do you want to continue generating for you?"


def refund_temp_quota_once(task_id: str, owner_hash: str) -> None:
    if owner_hash and refund_temp_quota_hash(owner_hash, task_id):
        mark_result_once(task_id, "temp_quota_refunded", True)


def refund_account_quota_once(task_id: str, account_id: str, charge_id: str = "") -> None:
    if account_id and refund_account_quota(account_id, charge_id or task_id):
        mark_account_refund_once(task_id, account_id)


def consume_failed_account_quota(task_id: str, meta: dict[str, Any], account_id: str, charge_id: str = "") -> None:
    if str(meta.get("platform") or "dola") == "dola":
        settle_account_quota(account_id, charge_id)
    else:
        refund_account_quota_once(task_id, account_id, charge_id)


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


def is_account_login_invalid(text: str) -> bool:
    value = repair_text(str(text or ""))
    return "游客模式" in value or "请登录后再试" in value or "登录后再试" in value


def is_ten_second_generation_limit(text: str) -> bool:
    value = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return TEN_SECOND_LIMIT_TEXT.lower() in value


def is_suspected_policy_false_positive(text: str) -> bool:
    value = str(text or "")
    return "输入可能包含违规内容" in value or "可能包含违规" in value


def is_account_quota_insufficient(text: str) -> bool:
    value = re.sub(r"\s+", "", str(text or ""))
    direct_markers = ("额度不足", "额度已用完", "次数不足", "次数已用完", "余额不足")
    if any(marker in value for marker in direct_markers):
        return True
    return "视频生成额度" in value and "剩余" in value and "无法生成" in value


def is_missing_reference_image_request(text: str) -> bool:
    value = re.sub(r"\s+", "", repair_text(str(text or "")))
    return "请上传" in value and any(marker in value for marker in ("参考图", "参考图片", "图片", "图像"))


def is_portrait_protection_rejection(text: str) -> bool:
    value = re.sub(r"\s+", "", repair_text(str(text or "")))
    return (
        "肖像保护" in value
        and any(marker in value for marker in ("真实人物照片", "真人照片", "真人图片", "人物照片"))
        and any(marker in value for marker in ("无法使用", "不能使用", "不支持"))
    )


def sanitize_query_diagnostic(value: Any) -> str:
    text = repair_text(str(value or "")).replace("\r", " ").replace("\n", " ")
    text = BEARER_TOKEN_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    text = SENSITIVE_DIAGNOSTIC_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    text = SENSITIVE_QUERY_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    return re.sub(r"\s+", " ", text).strip()[:500]


def ambiguous_submission_diagnostic(result: dict[str, Any]) -> str:
    parts: list[str] = []
    try:
        status = int(result.get("chat_status") or 0)
    except (TypeError, ValueError):
        status = 0
    if status:
        parts.append(f"HTTP {status}")
    content_type = sanitize_query_diagnostic(result.get("chat_content_type"))
    if content_type:
        parts.append(content_type[:80])
    try:
        response_bytes = max(0, int(result.get("chat_response_bytes") or 0))
    except (TypeError, ValueError):
        response_bytes = 0
    parts.append(f"响应 {response_bytes} 字节")
    if bool(result.get("sse_timed_out")):
        parts.append("SSE读取达到等待上限")
    category = sanitize_query_diagnostic(result.get("submit_error_category"))
    if category:
        parts.append(f"提交分类 {category[:80]}")
    recovery_error = sanitize_query_diagnostic(result.get("conversation_recovery_error"))
    if recovery_error:
        parts.append(f"恢复查询 {recovery_error[:180]}")
    preview = sanitize_query_diagnostic(
        result.get("submission_response_preview")
        or result.get("chat_response_preview")
        or result.get("sse_response_text")
    )
    parts.append(f"响应摘要 {preview[:320]}" if preview else "响应正文为空")
    return "；".join(parts)[:900]


def ambiguous_retry_reason(base: str, result: dict[str, Any]) -> str:
    diagnostic = ambiguous_submission_diagnostic(result)
    return f"{base}；后台诊断：{diagnostic}"[:1000] if diagnostic else str(base or "")[:1000]


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


def _recent_payload(include_messages: bool = False) -> dict[str, Any]:
    return {
        "cmd": 3200,
        "uplink_body": {
            "pull_recent_conv_chain_uplink_body": {
                "limit": 30,
                "message_count_per_conv": 10,
                "api_version": 1,
                "conv_version": 0,
                "direction": 3,
                "option": {
                    "not_need_message": not include_messages,
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
        cid = _item_conversation_id(item)
        if not cid:
            continue
        candidates.append((_item_order_key(item, position), cid))
    return max(candidates, default=((0, 0), ""))[1]


def extract_conversation_id_from_sse(text: str) -> str:
    if not text:
        return ""
    patterns = (
        r'\\?"(?:conversation_id|conversationId|conversationID|conv_id|convId)\\?"\s*:\s*\\?"?(\d{15,24})',
        r"(?:conversation_id|conversationId|conversationID|conv_id|convId)(?:\\\\?\"|)\s*[:=]\s*(?:\\\\?\")?(\d{15,24})",
        r"/chat/(\d{15,24})(?:\D|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def _video_url_from_model(value: Any) -> str:
    parsed = _try_parse_json_string(value) if isinstance(value, str) else value
    for nested in _walk(parsed):
        if not isinstance(nested, dict):
            continue
        for key in ("main_url", "video_url", "play_url", "download_url"):
            candidate = nested.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return ""


def extract_main_url(data: Any) -> str:
    message = _latest_single_chain_message(data)
    for item in _walk(message):
        if isinstance(item, dict) and "video_model" in item:
            video_url = _video_url_from_model(item.get("video_model"))
            if video_url:
                return video_url
        if isinstance(item, dict):
            for key in ("main_url", "video_url", "play_url", "download_url"):
                video_url = item.get(key)
                if isinstance(video_url, str) and video_url:
                    return video_url
    if message:
        return ""
    for item in _walk(data):
        if isinstance(item, dict) and "video_model" in item:
            video_url = _video_url_from_model(item.get("video_model"))
            if video_url:
                return video_url
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
    return text if text.isdigit() and 15 <= len(text) <= 24 else ""


def _item_conversation_id(item: dict[str, Any]) -> str:
    for key in ("conversation_id", "conversationId", "conversationID", "conv_id", "convId"):
        normalized = _normalize_conversation_id(item.get(key))
        if normalized:
            return normalized
    return ""


def _normalized_match_text(value: str) -> str:
    return re.sub(r"\s+", "", repair_text(str(value or ""))).casefold()


def extract_matching_conversation_id(
    data: Any,
    *,
    collection_id: str = "",
    local_conversation_id: str = "",
    unique_key: str = "",
    prompt: str = "",
) -> str:
    collection_id = str(collection_id or "").strip()
    local_conversation_id = str(local_conversation_id or "").strip()
    unique_key = str(unique_key or "").strip()
    submission_identifiers = tuple(item for item in (collection_id, local_conversation_id, unique_key) if item)
    normalized_prompt = _normalized_match_text(prompt)
    prompt_probe = normalized_prompt[:120] if len(normalized_prompt) >= 8 else ""
    collection_candidates: list[tuple[tuple[int, int], str]] = []
    prompt_candidates: list[tuple[tuple[int, int], str]] = []
    for position, item in enumerate(_walk(data)):
        if not isinstance(item, dict):
            continue
        conversation_id = _item_conversation_id(item)
        if not conversation_id:
            continue
        strings = _collect_strings(item)
        raw_text = "\n".join(strings)
        collection_match = any(identifier in raw_text for identifier in submission_identifiers)
        prompt_match = bool(prompt_probe and prompt_probe in _normalized_match_text(raw_text))
        if collection_match:
            collection_candidates.append((_item_order_key(item, position), conversation_id))
        elif prompt_match:
            prompt_candidates.append((_item_order_key(item, position), conversation_id))
    if collection_candidates:
        return max(collection_candidates, default=((0, 0), ""))[1]
    prompt_conversation_ids = {conversation_id for _, conversation_id in prompt_candidates}
    if len(prompt_conversation_ids) == 1:
        return prompt_conversation_ids.pop()
    return ""


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
        for normalized in [_item_conversation_id(item)]
        if normalized
    }
    chain_id = _item_conversation_id(chain) if isinstance(chain, dict) else ""
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
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        return cleaned
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


async def _post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    *,
    proxy_server: str = "",
) -> dict[str, Any]:
    timeout = httpx.Timeout(30.0, connect=15.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        proxy=str(proxy_server or "") or None,
    ) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        try:
            return json.loads(response.content.decode("utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DolaQueryError("invalid_response", "Dola returned an invalid JSON response") from exc


async def fetch_recent_conversation_id(cookie: str, *, proxy_server: str = "") -> str:
    data = await _post_json(RECENT_CONV_URL, _headers(cookie), _recent_payload(), proxy_server=proxy_server)
    return extract_conversation_id(data)


async def fetch_matching_recent_conversation_id(
    cookie: str,
    *,
    collection_id: str = "",
    local_conversation_id: str = "",
    unique_key: str = "",
    prompt: str = "",
    proxy_server: str = "",
) -> str:
    data = await _post_json(
        RECENT_CONV_URL,
        _headers(cookie),
        _recent_payload(include_messages=True),
        proxy_server=proxy_server,
    )
    return extract_matching_conversation_id(
        data,
        collection_id=collection_id,
        local_conversation_id=local_conversation_id,
        unique_key=unique_key,
        prompt=prompt,
    )


async def fetch_single_chain(cookie: str, conversation_id: str, *, proxy_server: str = "") -> tuple[str, str]:
    data = await _post_json(
        SINGLE_CHAIN_URL,
        _headers(cookie),
        _single_payload(conversation_id),
        proxy_server=proxy_server,
    )
    validate_conversation_ownership(data, conversation_id)
    return extract_main_url(data), extract_tts_content(data)


async def _task_query_proxy_server(result: dict[str, Any]) -> str:
    source = str(result.get("proxy_source") or "").strip().lower()
    if source == "account":
        proxy_node_id = str(result.get("proxy_node_id") or "").strip()
        if proxy_node_id:
            entries = await asyncio.to_thread(account_proxy_entries, [proxy_node_id])
            if entries:
                return account_proxy_url(entries[0])
    if source in {"api", "subscription"}:
        return str(result.get("proxy_server") or "").strip()
    return ""


def _query_proxy_can_refresh(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return int(exc.response.status_code) in {407, 502, 503, 504}
    return isinstance(exc, httpx.TransportError)


async def _run_task_query(
    task_id: str,
    result: dict[str, Any],
    operation: Callable[[str], Awaitable[Any]],
) -> Any:
    proxy_server = await _task_query_proxy_server(result)
    try:
        return await operation(proxy_server)
    except Exception as exc:
        source = str(result.get("proxy_source") or "").strip().lower()
        if not _query_proxy_can_refresh(exc):
            raise
        if source == "subscription":
            settings = load_settings()
            preferred_node_id = str(result.get("proxy_node_id") or "").strip()
            if not settings.proxy_subscription_url or not preferred_node_id:
                raise
            lease = await asyncio.wait_for(
                acquire_dola_subscription_proxy(
                    settings.proxy_subscription_url,
                    timeout_seconds=settings.proxy_api_timeout_seconds,
                    scheme=settings.proxy_subscription_scheme,
                    refresh_seconds=settings.proxy_subscription_refresh_seconds,
                    auto_select=False,
                    selected_node=preferred_node_id,
                    selected_countries=(),
                    latency_threshold_ms=settings.proxy_latency_threshold_ms,
                    random_select=False,
                ),
                timeout=20,
            )
            refreshed_server = str(lease.get("server") or "").strip()
            if not refreshed_server:
                await release_dola_subscription_proxy(lease)
                raise
            save_result(
                task_id,
                extra={
                    "proxy_server": refreshed_server,
                    "query_proxy_refresh_count": max(0, int(result.get("query_proxy_refresh_count") or 0)) + 1,
                    "query_proxy_refreshed_at": datetime.now(timezone.utc).isoformat(),
                    "query_proxy_refresh_reason": "subscription_session_recovered",
                },
            )
            try:
                return await operation(refreshed_server)
            finally:
                await release_dola_subscription_proxy(lease)
        if source != "api":
            raise
        settings = load_settings()
        if not settings.proxy_api_url:
            raise
        refreshed = await fetch_proxy_from_api(
            settings.proxy_api_url,
            timeout_seconds=settings.proxy_api_timeout_seconds,
            scheme=settings.proxy_api_scheme,
        )
        refreshed_server = str(refreshed.get("server") or "").strip()
        if not refreshed_server:
            raise
        refresh_count = max(0, int(result.get("query_proxy_refresh_count") or 0)) + 1
        result["proxy_server"] = refreshed_server
        result["query_proxy_refresh_count"] = refresh_count
        save_result(
            task_id,
            extra={
                "proxy_server": refreshed_server,
                "query_proxy_refresh_count": refresh_count,
                "query_proxy_refreshed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return await operation(refreshed_server)


async def _refresh_confirmed_query_proxy_if_due(task_id: str, result: dict[str, Any], submitted_at: datetime | None) -> None:
    source = str(result.get("proxy_source") or "").strip().lower()
    if source != "api" or not submitted_at or not str(result.get("conversation_id") or "").strip():
        return
    if datetime.now(timezone.utc) - submitted_at < timedelta(seconds=CONFIRMED_QUERY_PROXY_REFRESH_SECONDS):
        return
    if str(result.get("confirmed_query_proxy_refresh_attempted_at") or "").strip():
        return
    settings = load_settings()
    attempted_at = datetime.now(timezone.utc).isoformat()
    extra: dict[str, Any] = {"confirmed_query_proxy_refresh_attempted_at": attempted_at}
    if not settings.proxy_api_url:
        extra["confirmed_query_proxy_refresh_error"] = "proxy API is not configured"
        save_result(task_id, extra=extra)
        return
    try:
        refreshed = await fetch_proxy_from_api(
            settings.proxy_api_url,
            timeout_seconds=settings.proxy_api_timeout_seconds,
            scheme=settings.proxy_api_scheme,
        )
        refreshed_server = str(refreshed.get("server") or "").strip()
        if not refreshed_server:
            raise RuntimeError("proxy API returned an empty server")
        extra.update(
            proxy_server=refreshed_server,
            query_proxy_refresh_count=max(0, int(result.get("query_proxy_refresh_count") or 0)) + 1,
            query_proxy_refreshed_at=attempted_at,
            query_proxy_refresh_reason="confirmed_session_stalled",
            confirmed_query_proxy_refresh_error="",
        )
        result.update(extra)
    except Exception as exc:
        extra["confirmed_query_proxy_refresh_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    save_result(task_id, extra=extra)


async def _query_doubao_task_once(
    task_id: str,
    meta: dict[str, Any],
    result: dict[str, Any],
    *,
    late_watch: bool = False,
) -> dict[str, Any]:
    if str(result.get("doubao_result_mode") or "") != "interface_poll":
        return {"code": "1", "text": "豆包正在浏览器中等待视频，请稍候...", "url": ""}
    now = datetime.now(timezone.utc)
    next_poll_at = parse_time(str(meta.get("next_result_poll_at") or ""))
    if next_poll_at and now < next_poll_at:
        return {"code": "1", "text": "豆包正在生成视频，请稍候...", "url": ""}
    # Reserve the next poll window before network I/O so repeated client status
    # refreshes cannot bypass the worker's global result-query limiter.
    update_meta(task_id, next_result_poll_at=(now + timedelta(seconds=15)).isoformat())
    conversation_id = str(result.get("doubao_conversation_id") or result.get("conversation_id") or "").strip()
    cookie_header = str(result.get("cookie_string") or "").strip()
    if not conversation_id or not cookie_header:
        save_result(task_id, extra={"doubao_query_error": "missing conversation id or cookie"})
        update_meta(task_id, next_result_poll_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat())
        return {"code": "1", "text": "豆包正在确认生成会话，请稍候...", "url": "", "retry_after": 30}

    from .doubao_automation import fetch_doubao_generation_result

    try:
        queried = await _run_task_query(
            task_id,
            result,
            lambda proxy_server: fetch_doubao_generation_result(
                cookie_header,
                conversation_id,
                web_id=str(result.get("doubao_web_id") or "111"),
                region=str(result.get("doubao_region") or "JP"),
                proxy_server=proxy_server,
            ),
        )
    except httpx.HTTPStatusError as exc:
        status = int(exc.response.status_code)
        save_result(
            task_id,
            extra={
                "doubao_query_error": f"HTTP {status}",
                "doubao_query_error_category": "login_invalid" if status == 401 else "rate_limited" if status in {403, 429} else "http_error",
            },
        )
        if status == 401:
            account_id = str(result.get("account_id") or "")
            if account_id:
                disable_account_for_login(account_id, "豆包接口查询确认登录状态失效")
                refund_account_quota_once(task_id, account_id, str(result.get("account_quota_charge_id") or ""))
                clear_account_current_task(account_id, task_id)
            mark_failed(task_id, "豆包登录状态失效")
            return {"code": "0", "text": "豆包登录状态失效", "url": ""}
        retry_after = 120 if status in {403, 429} else 30
        update_meta(task_id, next_result_poll_at=(datetime.now(timezone.utc) + timedelta(seconds=retry_after)).isoformat())
        return {"code": "1", "text": "豆包正在生成视频，请稍候...", "url": "", "retry_after": retry_after}
    except Exception as exc:
        save_result(
            task_id,
            extra={
                "doubao_query_error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "doubao_query_error_category": "transport_error",
            },
        )
        update_meta(task_id, next_result_poll_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat())
        return {"code": "1", "text": "豆包正在生成视频，请稍候...", "url": "", "retry_after": 30}

    state = str(queried.get("state") or "generating")
    text = str(queried.get("text") or "")[:1000]
    save_result(
        task_id,
        extra={
            "doubao_query_state": state,
            "doubao_query_text": sanitize_query_diagnostic(text),
            "doubao_query_error": "",
            "doubao_query_error_category": "",
            "doubao_last_interface_poll_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    account_id = str(result.get("account_id") or "")
    charge_id = str(result.get("account_quota_charge_id") or "")
    if state == "completed":
        candidate = queried.get("candidate") if isinstance(queried.get("candidate"), dict) else {}
        url = str(candidate.get("url") or "")
        if not url:
            update_meta(task_id, next_result_poll_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat())
            return {"code": "1", "text": "豆包正在生成视频，请稍候...", "url": "", "retry_after": 30}
        if account_id:
            if not bool(result.get("account_quota_refunded")):
                settle_account_quota(account_id, charge_id)
            clear_account_current_task(account_id, task_id)
        score = int(candidate.get("score") or 0)
        save_result(
            task_id,
            extra={
                "decoded_main_url": url,
                "doubao_video_detection_source": str(candidate.get("source") or "single_chain"),
                "doubao_selected_video_key": str(candidate.get("key") or "video_model.main_url"),
                "doubao_video_url_score": score,
                "doubao_watermark_status": "original" if score >= 240 else "fallback",
                "doubao_result_mode": "interface_poll_completed",
            },
            remove={"cookie_string", "conversation_id"},
        )
        mark_late_result_success(task_id) if late_watch else mark_success(task_id)
        return {"code": "2", "text": SUCCESS_TEXT, "url": url}
    if state in {"failed", "login_invalid", "verification"}:
        if account_id:
            if state == "login_invalid":
                disable_account_for_login(account_id, "豆包接口查询确认登录状态失效")
            elif state == "verification":
                mark_account_slider_verification(account_id)
            refund_account_quota_once(task_id, account_id, charge_id)
            clear_account_current_task(account_id, task_id)
        reason = text or "豆包视频生成失败"
        mark_failed(task_id, reason)
        return {"code": "0", "text": reason, "url": ""}
    if state == "rate_limited":
        update_meta(task_id, next_result_poll_at=(datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat())
        return {"code": "1", "text": "豆包正在生成视频，请稍候...", "url": "", "retry_after": 120}
    return {"code": "1", "text": text or "豆包正在生成视频，请稍候...", "url": ""}


async def _query_task_once(
    task_id: str,
    *,
    late_watch: bool = False,
    background_poll: bool = False,
) -> dict[str, Any]:
    expire_task_if_timeout(task_id)
    meta = get_meta(task_id)
    if str(meta.get("platform") or "dola") == "doubao" and (meta.get("status") == STATUS_SUBMITTED or late_watch):
        result = load_result(task_id)
        video_url = str(result.get("decoded_main_url") or "").strip()
        if video_url:
            mark_late_result_success(task_id) if late_watch else mark_success(task_id)
            return {"code": "2", "text": SUCCESS_TEXT, "url": video_url}
        if not background_poll:
            return {"code": "1", "text": "豆包正在生成视频，请稍候...", "url": ""}
        return await _query_doubao_task_once(task_id, meta, result, late_watch=late_watch)
    retry_limit = task_retry_limit()
    late_watch_active = late_watch and meta.get("status") == STATUS_FAILED and bool(
        (late_until := parse_time(str(meta.get("late_result_watch_until") or "")))
        and datetime.now(timezone.utc) < late_until
    )
    if meta.get("status") not in {STATUS_SUBMITTED, STATUS_SUCCESS} and not late_watch_active:
        if meta.get("status") == "running":
            return {"code": "1", "text": str(meta.get("status_reason") or "正在准备生成任务"), "url": ""}
        if meta.get("status") == STATUS_FAILED and str(meta.get("error") or "") in {"超时生成失败", "重试超过30分钟，生成失败"}:
            return {"code": "0", "text": str(meta.get("error") or "超时生成失败"), "url": ""}
        if meta.get("status") == "pending" and (
            str(meta.get("error") or "") == POLICY_RETRYING_TEXT
            or is_suspected_policy_false_positive(str(meta.get("error") or ""))
        ):
            return {"code": "1", "text": POLICY_RETRYING_TEXT, "url": ""}
        if meta.get("status") == STATUS_FAILED and is_suspected_policy_false_positive(str(meta.get("error") or "")):
            refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
            return {"code": "0", "text": POLICY_RETRY_TEXT, "url": ""}
        if int(meta.get("retry_count") or 0) > 0 and is_final_generation_failure(str(meta.get("error") or "")):
            return {"code": "1", "text": RETRY_GENERATING_TEXT, "url": ""}
        if meta.get("status") == STATUS_FAILED and str(meta.get("error") or "") == "任务超时未执行":
            return {"code": "0", "text": "任务超时未执行", "url": ""}
        if meta.get("status") == STATUS_FAILED and str(meta.get("error") or "") == "browser timeout":
            return {"code": "0", "text": "浏览器超时", "url": ""}
        if meta.get("status") == STATUS_FAILED and str(meta.get("error") or "") == "region restricted":
            return {"code": "0", "text": "Dola 当前地区不可用", "url": ""}
        if meta.get("status") == STATUS_FAILED and int(meta.get("retry_count") or 0) > retry_limit:
            return {"code": "0", "text": "多次生成失败", "url": ""}
        if meta.get("status") == STATUS_FAILED:
            return {"code": "0", "text": str(meta.get("error") or "失败"), "url": ""}
        if meta.get("status") == "pending" and (
            int(meta.get("retry_count") or 0) > 0 or int(meta.get("infrastructure_retry_count") or 0) > 0
        ):
            return {"code": "1", "text": "正在重试中，请稍等！", "url": ""}
        if meta.get("status") == "pending" and str(meta.get("status_reason") or meta.get("queue_reason") or ""):
            return {"code": "1", "text": str(meta.get("status_reason") or meta.get("queue_reason") or "正在排队"), "url": ""}
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
    submitted_at = parse_time(str(meta.get("submitted_at") or meta.get("updated_at") or ""))
    await _refresh_confirmed_query_proxy_if_due(task_id, result, submitted_at)
    if result.get("confirmed_query_proxy_refresh_attempted_at"):
        result = load_result(task_id)
    if not cookie:
        return {"code": "1", "text": "没有文本", "url": ""}

    sse_text = str(
        result.get("sse_response_text")
        or result.get("chat_response_text")
        or result.get("chat_response_preview")
        or result.get("submission_response_preview")
        or ""
    )
    conversation_id = extract_conversation_id_from_sse(sse_text)
    conversation_source = "submit_sse" if conversation_id else ""
    if not conversation_id:
        conversation_id = str(result.get("conversation_id") or "")
        conversation_source = "submit_result" if conversation_id else ""
    recovery_collection_id = str(result.get("submission_collection_id") or "")
    recovery_local_id = str(result.get("local_conversation_id") or "")
    recovery_unique_key = str(result.get("submission_unique_key") or "")
    if not conversation_id and any((recovery_collection_id, recovery_local_id, recovery_unique_key)):
        try:
            conversation_id = await _run_task_query(
                task_id,
                result,
                lambda proxy_server: fetch_matching_recent_conversation_id(
                    cookie,
                    collection_id=recovery_collection_id,
                    local_conversation_id=recovery_local_id,
                    unique_key=recovery_unique_key,
                    prompt=str(meta.get("prompt") or ""),
                    proxy_server=proxy_server,
                ),
            )
        except Exception as exc:
            diagnostic = query_error_diagnostic(exc)
            save_result(
                task_id,
                extra={
                    "conversation_recovery_error": diagnostic["last_query_error"],
                    "conversation_recovery_error_category": diagnostic["last_query_error_category"],
                },
            )
        if conversation_id:
            conversation_source = "matched_recent_submission"
    if conversation_id:
        save_result(
            task_id,
            conversation_id=conversation_id,
            extra={
                "conversation_source": conversation_source,
                "submission_ambiguous": False,
                "submission_ambiguous_at": "",
                "submit_confirmation_state": "confirmed",
                "conversation_recovery_error": "",
                "conversation_recovery_error_category": "",
            },
        )
        if any(
            (
                meta.get("preferred_account_id"),
                meta.get("ambiguous_proxy_retry_count"),
                meta.get("ambiguous_proxy_avoid_node_ids"),
            )
        ):
            update_meta(
                task_id,
                preferred_account_id="",
                ambiguous_proxy_retry_count=0,
                ambiguous_proxy_avoid_node_ids=[],
                proxy_retry_avoid_node_id="",
            )
    else:
        ambiguous_at = parse_time(str(result.get("submission_ambiguous_at") or ""))
        if bool(result.get("submission_ambiguous")) and ambiguous_at and (
            datetime.now(timezone.utc) - ambiguous_at
        ).total_seconds() >= AMBIGUOUS_SUBMISSION_RECOVERY_SECONDS:
            diagnostic = ambiguous_submission_diagnostic(result)
            save_result(task_id, extra={"ambiguous_submission_diagnostic": diagnostic})
            account_id = str(result.get("account_id") or "")
            if account_id:
                refund_account_quota_once(task_id, account_id, str(result.get("account_quota_charge_id") or ""))
            proxy_node_id = str(result.get("proxy_node_id") or "").strip()
            proxy_source = str(result.get("proxy_source") or "").strip().lower()
            proxy_retry_count = max(0, int(meta.get("ambiguous_proxy_retry_count") or 0))
            if account_id and proxy_source == "api" and proxy_retry_count < AMBIGUOUS_PROXY_RETRIES_PER_ACCOUNT:
                retry_ambiguous_proxy_task(
                    task_id,
                    ambiguous_retry_reason("提交后未取得有效会话，正在更换代理重试", result),
                    account_id,
                    proxy_node_id,
                    delay_seconds=3,
                )
                clear_transient_result(task_id)
                return {"code": "1", "text": "正在重试中，请稍等！", "url": ""}
            if account_id:
                clear_account_current_task(account_id, task_id)
                record_failed_account(task_id, account_id)
            update_meta(task_id, proxy_retry_avoid_node_id=proxy_node_id)
            retry_count = retry_ambiguous_submitted_task(
                task_id,
                ambiguous_retry_reason("提交后未取得有效会话，正在安全重试", result),
                max_retries=retry_limit,
                delay_seconds=3,
            )
            if retry_count <= retry_limit:
                clear_transient_result(task_id)
                return {"code": "1", "text": "正在重试中，请稍等！", "url": ""}
            meta = get_meta(task_id)
            refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
            return {"code": "0", "text": "生成失败，请重试！", "url": ""}
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
        main_url_encoded, tts_content = await _run_task_query(
            task_id,
            result,
            lambda proxy_server: fetch_single_chain(cookie, conversation_id, proxy_server=proxy_server),
        )
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
            mark_late_result_success(task_id) if late_watch_active else mark_success(task_id)
            return {"code": "2", "text": SUCCESS_TEXT, "url": decoded}

    text = tts_content or "没有文本"
    query_classification = "generating"
    if is_portrait_protection_rejection(text):
        query_classification = "portrait_protection"
    elif is_ten_second_generation_limit(text):
        query_classification = "ten_second_limit"
    elif is_missing_reference_image_request(text):
        query_classification = "missing_reference_image"
    elif is_account_quota_insufficient(text):
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
    if late_watch_active:
        return {"code": "1", "text": "late result observation", "url": ""}
    account_id = str(result.get("account_id") or "")
    if is_ten_second_generation_limit(text):
        if account_id:
            mark_account_ten_second_limit(account_id)
            clear_account_current_task(account_id, task_id)
            refund_account_quota_once(task_id, account_id, str(result.get("account_quota_charge_id") or ""))
            record_failed_account(task_id, account_id)
        retry_count = retry_submitted_task(task_id, TEN_SECOND_LIMIT_TEXT, max_retries=retry_limit, delay_seconds=10)
        if retry_count <= retry_limit:
            clear_transient_result(task_id)
            return {"code": "1", "text": TEN_SECOND_LIMIT_TEXT, "url": ""}
        meta = get_meta(task_id)
        mark_failed(task_id, TEN_SECOND_LIMIT_TEXT)
        refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
        return {"code": "0", "text": TEN_SECOND_LIMIT_TEXT, "url": ""}
    if is_portrait_protection_rejection(text):
        invalidate_reference_attachment_keys([str(item) for item in result.get("reference_image_cache_keys") or []])
        if not bool(meta.get("reference_is_real_person")):
            if account_id:
                clear_account_current_task(account_id, task_id)
                refund_account_quota_once(task_id, account_id, str(result.get("account_quota_charge_id") or ""))
            mark_failed(task_id, REFERENCE_REAL_PERSON_REQUIRED_TEXT)
            refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
            return {"code": "0", "text": REFERENCE_REAL_PERSON_REQUIRED_TEXT, "url": ""}
        update_meta(task_id, reference_upload_cache_bypass=True, reference_face_grid_retry=True, reference_force_grid=False)
        if account_id:
            clear_account_current_task(account_id, task_id)
            refund_account_quota_once(task_id, account_id, str(result.get("account_quota_charge_id") or ""))
            record_failed_account(task_id, account_id)
        portrait_retry_count = max(0, int(meta.get("portrait_protection_retry_count") or 0))
        if int(meta.get("image_count") or 0) > 0 and portrait_retry_count < 1:
            update_meta(task_id, portrait_protection_retry_count=portrait_retry_count + 1)
            retry_count = retry_submitted_task(
                task_id,
                PORTRAIT_PROTECTION_RETRY_TEXT,
                max_retries=retry_limit,
                delay_seconds=10,
            )
            if retry_count <= retry_limit:
                clear_transient_result(task_id)
                return {"code": "1", "text": "正在重试中，请稍等！", "url": ""}
        mark_failed(task_id, REFERENCE_IMAGE_INVALID_TEXT)
        meta = get_meta(task_id)
        refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
        return {"code": "0", "text": REFERENCE_IMAGE_INVALID_TEXT, "url": ""}
    if is_missing_reference_image_request(text):
        invalidate_reference_attachment_keys([str(item) for item in result.get("reference_image_cache_keys") or []])
        update_meta(task_id, reference_upload_cache_bypass=True)
        if account_id:
            clear_account_current_task(account_id, task_id)
            refund_account_quota_once(task_id, account_id, str(result.get("account_quota_charge_id") or ""))
        if int(meta.get("image_count") or 0) > 0:
            if account_id:
                record_failed_account(task_id, account_id)
            retry_count = retry_submitted_task(
                task_id,
                REFERENCE_IMAGE_RETRY_TEXT,
                max_retries=retry_limit,
                delay_seconds=10,
            )
            if retry_count <= retry_limit:
                clear_transient_result(task_id)
                return {"code": "1", "text": REFERENCE_IMAGE_RETRY_TEXT, "url": ""}
        mark_failed(task_id, REFERENCE_IMAGE_REQUIRED_TEXT)
        meta = get_meta(task_id)
        refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
        return {"code": "0", "text": REFERENCE_IMAGE_REQUIRED_TEXT, "url": ""}
    if is_account_quota_insufficient(text):
        if account_id:
            clear_account_current_task(account_id, task_id)
            if str(meta.get("platform") or "dola") == "dola":
                exhaust_timed_out_account(account_id, str(result.get("account_quota_charge_id") or ""))
            else:
                exhaust_account_quota(account_id, str(result.get("account_quota_charge_id") or ""))
            record_failed_account(task_id, account_id)
        retry_count = retry_submitted_task(task_id, ACCOUNT_QUOTA_RETRY_TEXT, max_retries=retry_limit, delay_seconds=10)
        if retry_count <= retry_limit:
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
                consume_failed_account_quota(task_id, meta, account_id, str(result.get("account_quota_charge_id") or ""))
            retry_count = retry_submitted_task(task_id, POLICY_RETRYING_TEXT, max_retries=1, delay_seconds=10)
            if retry_count <= 1:
                clear_transient_result(task_id)
                return {"code": "1", "text": POLICY_RETRYING_TEXT, "url": ""}
            mark_failed(task_id, POLICY_RETRY_TEXT)
            meta = get_meta(task_id)
            refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
            return {"code": "0", "text": POLICY_RETRY_TEXT, "url": ""}
        login_invalid = is_account_login_invalid(text)
        if account_id:
            clear_account_current_task(account_id, task_id)
            record_failed_account(task_id, account_id)
            if login_invalid:
                disable_account_for_login(account_id, "Dola 登录状态失效（游客模式）")
                refund_account_quota_once(task_id, account_id, str(result.get("account_quota_charge_id") or ""))
            else:
                consume_failed_account_quota(task_id, meta, account_id, str(result.get("account_quota_charge_id") or ""))
        retry_count = retry_submitted_task(task_id, text[:500], max_retries=retry_limit, delay_seconds=10)
        if retry_count > retry_limit:
            meta = get_meta(task_id)
            mark_failed(task_id, "多次生成失败")
            refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
            return {"code": "0", "text": "多次生成失败", "url": ""}
        clear_transient_result(task_id)
        return {"code": "1", "text": RETRY_GENERATING_TEXT, "url": ""}
    return {"code": "1", "text": GENERATING_TEXT, "url": ""}


async def query_task(
    task_id: str,
    *,
    late_watch: bool = False,
    background_poll: bool = False,
) -> dict[str, Any]:
    async with _query_lock(task_id):
        return await _query_task_once(
            task_id,
            late_watch=late_watch,
            background_poll=background_poll,
        )
