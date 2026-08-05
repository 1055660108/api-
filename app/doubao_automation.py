from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any, AsyncContextManager, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .accounts import disable_account_for_login, set_account_cooldown, set_account_pinned_proxy_node, update_account_cookies
from .automation import DolaFetchAutomation, PREPARE_UPLOAD_SCRIPT, PREPARE_UPLOAD_TIMEOUT_SECONDS, ReferenceUploadCapacityError, is_reference_upload_failure, is_reference_upload_phase
from .browser_runtime import BROWSER_EXTRA_HTTP_HEADERS, BROWSER_INIT_SCRIPT, BROWSER_USER_AGENT, BrowserContextLease, ReusableBrowserPool, bounded_cleanup, cancel_tracked_tasks, create_tracked_task, resolve_browser_executable, safe_close
from .config import DOUBAO_STATES_DIR, ensure_dirs, load_settings
from .query import decode_main_url, extract_main_url, extract_tts_content
from .reference_images import prepare_task_reference_images
from .slider_solver import SliderChallengeSolver, SliderSolveResult, SliderSolverSettings, find_slider_page
from .store import STATUS_SUBMITTED, begin_task_submission, clear_transient_result, get_meta, is_task_canceled, mark_pending, mark_submitted, mark_success, release_task_submission, save_result, set_execution_phase, task_exists, update_meta
from .profile_lock import account_profile_lock


DOUBAO_URL = "https://www.doubao.com/chat/"
DOUBAO_VIDEO_CREATION_URL = "https://www.doubao.com/chat/create-image"
DOUBAO_SINGLE_CHAIN_URL = "https://www.doubao.com/im/chain/single"
VIDEO_URL_RE = re.compile(r'https?://[^"\\\s]+(?:mime_type=video_mp4|\.mp4(?:\?[^"\\\s]*)?)', re.IGNORECASE)
REGION_RESTRICTION_MARKERS = (
    "doubao-region-ban",
    "受区域限制，请先登录再使用豆包",
    "当前地区暂不支持",
    "所在地区暂不支持",
    "not available in your region",
    "region is not supported",
)
DOUBAO_MODEL_CODES = {
    "Seedance 2.0 Mini": "seedance_v2.0_mini",
    "Seedance 2.0 Fast": "seedance_v2.0",
}
DOUBAO_VIDEO_RATIOS = {"auto", "3:4", "4:3", "9:16", "16:9", "1:1", "21:9"}
DOUBAO_RESULT_WAIT_SECONDS = 10 * 60
DOUBAO_RESULT_POLL_MILLISECONDS = 5000
DOUBAO_ORIGINAL_VIDEO_SCORE = 240
DOUBAO_UNWATERMARKED_RESULT_SOURCES = {
    "fallback_unwatermarked",
    "single_chain_explicit_unwatermarked",
    "network_explicit_unwatermarked",
}
DOUBAO_WEB_DIRECT_RESULT_SOURCES = {
    "video_current_src",
    "source_src",
    "download_link",
    "media_response",
}
DOUBAO_PREPARE_UPLOAD_BODY = {"tenant_id": "5", "scene_id": "5", "resource_type": 2}
DOUBAO_SUBMISSION_MARKER = "视频生成已提交"
DOUBAO_VIDEO_CREATION_SUBMIT_WAIT_SECONDS = 75
DOUBAO_VERIFICATION_RENDER_WAIT_SECONDS = 15
DOUBAO_VIDEO_CACHE_MAX_BYTES = 512 * 1024 * 1024
DOUBAO_REFERENCE_UPLOAD_STABLE_CHECKS = 2
DOUBAO_CONVERSATION_ID_RE = re.compile(
    r'(?:"|\\")?(?:conversation_id|conversationId|conversationID|conv_id|convId)(?:"|\\")?\s*[:=]\s*(?:"|\\")?(\d{15,24})',
    re.IGNORECASE,
)
QAAB_SALT = bytes.fromhex(
    "4dd4c2e6b83162090e52b3c7a6733ba41cb2462b829ab58a196b39db57177524"
    "f49baf7f08e8d68d26a72e37c1a95a2f1f05a51892aef2949732b62a38aadd58"
)


def _doubao_single_chain_url(web_id: str, region: str) -> str:
    normalized_web_id = str(web_id or "111").strip() or "111"
    normalized_region = str(region or "JP").strip().upper() or "JP"
    query = urlencode({
        "version_code": "20800",
        "language": "zh",
        "device_platform": "web",
        "aid": "497858",
        "real_aid": "497858",
        "pkg_type": "release_version",
        "device_id": normalized_web_id,
        "pc_version": "3.29.10",
        "web_id": normalized_web_id,
        "tea_uuid": normalized_web_id,
        "region": normalized_region,
        "sys_region": normalized_region,
        "samantha_web": "1",
        "web_platform": "browser",
        "use-olympus-account": "1",
        "web_tab_id": normalized_web_id,
    })
    return f"{DOUBAO_SINGLE_CHAIN_URL}?{query}"


def extract_doubao_conversation_id(value: Any) -> str:
    if isinstance(value, (dict, list)):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            value = str(value or "")
    text = str(value or "")
    match = DOUBAO_CONVERSATION_ID_RE.search(text)
    if match:
        return match.group(1)
    route = re.search(r"/chat/(\d{15,24})(?:[/?#]|$)", text)
    return route.group(1) if route else ""


def _doubao_single_chain_payload(conversation_id: str) -> dict[str, Any]:
    return {
        "cmd": 3100,
        "uplink_body": {
            "pull_singe_chain_uplink_body": {
                "conversation_id": str(conversation_id or ""),
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


async def fetch_doubao_generation_result(
    cookie_header: str,
    conversation_id: str,
    *,
    web_id: str = "111",
    region: str = "JP",
    proxy_server: str = "",
    video_cache_path: str | Path | None = None,
) -> dict[str, Any]:
    timeout = httpx.Timeout(30.0, connect=15.0)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json; encoding=utf-8",
        "Cookie": str(cookie_header or ""),
        "User-Agent": BROWSER_USER_AGENT,
        "agw-js-conv": "str",
    }
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        proxy=str(proxy_server or "") or None,
    ) as client:
        parsed, payload = await fetch_doubao_single_chain(
            client,
            headers,
            conversation_id,
            web_id=web_id,
            region=region,
        )
        try:
            unwatermarked = await fetch_doubao_unwatermarked_result(client, cookie_header, payload)
        except Exception as exc:
            unwatermarked = {
                "url": "",
                "source": "",
                "key": "",
                "status": "unwatermarked_query_error",
                "attempts": 0,
                "errors": [type(exc).__name__],
            }
        unwatermarked_url = str(unwatermarked.get("url") or "")
        if unwatermarked_url:
            source = str(unwatermarked.get("source") or "fallback_unwatermarked")
            key = str(unwatermarked.get("key") or "video_model.fallback_api")
            cache_bytes = 0
            if video_cache_path is not None:
                try:
                    cache_bytes = await cache_doubao_video(
                        client,
                        cookie_header,
                        unwatermarked_url,
                        Path(video_cache_path),
                    )
                except Exception as exc:
                    return {
                        "state": "awaiting_unwatermarked",
                        "text": str(parsed.get("text") or "视频已生成，正在准备下载地址"),
                        "watermarked_candidate": dict(parsed.get("candidate") or {}),
                        "unwatermarked_status": "video_cache_unavailable",
                        "unwatermarked_attempts": int(unwatermarked.get("attempts") or 0),
                        "unwatermarked_errors": [
                            *list(unwatermarked.get("errors") or [])[:9],
                            f"video_cache_{type(exc).__name__}",
                        ],
                        "video_cache_error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    }
            return {
                "state": "completed",
                "text": str(parsed.get("text") or ""),
                "candidate": {
                    "url": unwatermarked_url,
                    "key": key,
                    "source": source,
                    "score": doubao_video_url_score(
                        unwatermarked_url,
                        key,
                        source,
                    ),
                    "watermark_status": "original",
                },
                "unwatermarked_status": "completed",
                "unwatermarked_attempts": int(unwatermarked.get("attempts") or 0),
                "unwatermarked_errors": list(unwatermarked.get("errors") or [])[:10],
                "video_cache_bytes": cache_bytes,
                "video_cache_error": "",
            }
        if parsed.get("state") == "completed":
            return {
                "state": "awaiting_unwatermarked",
                "text": str(parsed.get("text") or "视频已生成，正在获取无水印地址"),
                "watermarked_candidate": dict(parsed.get("candidate") or {}),
                "unwatermarked_status": str(unwatermarked.get("status") or "fallback_unavailable"),
                "unwatermarked_attempts": int(unwatermarked.get("attempts") or 0),
                "unwatermarked_errors": list(unwatermarked.get("errors") or [])[:10],
            }
        parsed["unwatermarked_status"] = str(unwatermarked.get("status") or "")
        parsed["unwatermarked_attempts"] = int(unwatermarked.get("attempts") or 0)
        parsed["unwatermarked_errors"] = list(unwatermarked.get("errors") or [])[:10]
        return parsed


async def cache_doubao_video(
    client: httpx.AsyncClient,
    cookie_header: str,
    url: str,
    target: Path,
) -> int:
    if target.is_file() and target.stat().st_size > 0:
        return target.stat().st_size
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    headers = {
        "Accept": "video/*,*/*;q=0.8",
        "Accept-Encoding": "identity",
        "Cookie": str(cookie_header or ""),
        "Origin": "https://www.doubao.com",
        "Referer": DOUBAO_URL,
        "User-Agent": BROWSER_USER_AGENT,
    }
    total = 0
    try:
        async with client.stream("GET", str(url or ""), headers=headers, follow_redirects=True) as response:
            response.raise_for_status()
            content_length = int(response.headers.get("content-length") or 0)
            if content_length > DOUBAO_VIDEO_CACHE_MAX_BYTES:
                raise RuntimeError("doubao video exceeds cache size limit")
            with temporary.open("wb") as output:
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > DOUBAO_VIDEO_CACHE_MAX_BYTES:
                        raise RuntimeError("doubao video exceeds cache size limit")
                    output.write(chunk)
        if total <= 0:
            raise RuntimeError("doubao video cache response was empty")
        temporary.replace(target)
        return total
    finally:
        temporary.unlink(missing_ok=True)


async def fetch_doubao_single_chain(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    conversation_id: str,
    *,
    web_id: str = "111",
    region: str = "JP",
) -> tuple[dict[str, Any], Any]:
    response = await client.post(
        _doubao_single_chain_url(web_id, region),
        headers=headers,
        json=_doubao_single_chain_payload(conversation_id),
    )
    response.raise_for_status()
    body = response.content.decode("utf-8-sig", errors="replace")
    parsed = parse_doubao_generation_result(body)
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        payload = {}
    return parsed, payload


def unwatermarked_fallback_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params.update(channel="no", codec_type="8", logo_type="unwatermarked")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), parsed.fragment))


def _decode_loose_base64(value: str) -> bytes:
    text = str(value or "").strip()
    variants = (
        text,
        text.translate(str.maketrans({"$": "_", "@": "/", "#": "."})),
        text.translate(str.maketrans({"$": "+", "@": "/", "#": "="})),
    )
    for variant in dict.fromkeys(variants):
        padded = variant + "=" * (-len(variant) % 4)
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                return decoder(padded.encode("ascii"))
            except Exception:
                continue
    return b""


def _aes_cbc_url(payload: bytes, key: bytes, iv: bytes) -> str:
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(payload) + decryptor.finalize()
        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
        decoded = unpadder.update(padded) + unpadder.finalize()
    except (TypeError, ValueError):
        return ""
    text = decoded.decode("utf-8", errors="ignore").strip("\x00\r\n\t ")
    return text if text.startswith(("http://", "https://")) else ""


def decode_qaab_url(token: str, key_seed: str) -> str:
    data = _decode_loose_base64(token)
    seed = _decode_loose_base64(key_seed)
    if not data or not seed:
        return ""
    digest1 = hashlib.sha512(seed[:32]).digest()
    digest2 = hashlib.sha512(digest1 + QAAB_SALT).digest()
    key, iv = digest2[:16], digest2[16:32]
    attempts = [(data, key, iv)]
    if data.startswith(b"\xa8\x00\x01\x00"):
        attempts = [(data[4:], key, iv), (data[4:], iv, key)]
        if len(data) > 36:
            attempts.extend(((data[36:], key, data[20:36]), (data[36:], key, iv)))
    for encrypted, attempt_key, attempt_iv in attempts:
        decoded = _aes_cbc_url(encrypted, attempt_key, attempt_iv)
        if decoded:
            return decoded
    return ""


def _latest_doubao_message(data: Any) -> dict[str, Any]:
    body = data.get("downlink_body", {}) if isinstance(data, dict) else {}
    chain = body.get("pull_singe_chain_downlink_body", {}) if isinstance(body, dict) else {}
    messages = chain.get("messages", []) if isinstance(chain, dict) else []
    messages = [item for item in messages if isinstance(item, dict)]

    def order_key(item: dict[str, Any], position: int) -> tuple[int, int]:
        for key in ("message_index", "index", "create_time_ms", "create_time", "update_time", "message_id"):
            try:
                return int(str(item.get(key))), position
            except (TypeError, ValueError):
                continue
        return 0, position

    return max(
        enumerate(messages),
        key=lambda pair: order_key(pair[1], pair[0]),
        default=(-1, {}),
    )[1]


def extract_doubao_fallback_apis(data: Any) -> list[str]:
    found: list[str] = []
    latest_message = _latest_doubao_message(data)

    def add_fallback(value: Any) -> None:
        if isinstance(value, str):
            normalized = value.strip()
            if normalized.startswith(("http://", "https://")) and normalized not in found:
                found.append(normalized)
            return
        if isinstance(value, dict):
            for child in value.values():
                add_fallback(child)
        elif isinstance(value, list):
            for child in value:
                add_fallback(child)

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(value, dict):
            add_fallback(value.get("fallback_api"))
            for child in value.values():
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)
        elif isinstance(value, str) and value.strip().startswith(("{", "[")):
            try:
                walk(json.loads(value), depth + 1)
            except (TypeError, ValueError):
                pass

    walk(latest_message)
    return found


def _explicit_unwatermarked_field(path: str) -> bool:
    normalized = str(path or "").lower()
    if any(marker in normalized for marker in ("cover", "poster", "thumbnail", "image", "avatar")):
        return False
    return any(
        marker in normalized
        for marker in (
            "unwatermarked",
            "no_watermark",
            "without_watermark",
            "watermark_free",
            "original_url",
            "original_download_url",
            "origin_url",
        )
    )


def _url_has_watermark_marker(url: str) -> bool:
    value = str(url or "").lower()
    return any(
        marker in value
        for marker in (
            "watermark=1",
            "watermark%3d1",
            "wm=1",
            "video_gen_watermark",
            "logo_type=watermark",
        )
    )


def extract_doubao_explicit_unwatermarked_urls(data: Any) -> list[dict[str, str]]:
    latest_message = _latest_doubao_message(data)
    key_seed = _find_deep_string(latest_message, "key_seed") or _find_deep_string(data, "key_seed")
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    def walk(value: Any, path: str = "", depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                walk(child, child_path, depth + 1)
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]", depth + 1)
            return
        if not isinstance(value, str):
            return
        if value.strip().startswith(("{", "[")):
            try:
                walk(json.loads(value), path, depth + 1)
            except (TypeError, ValueError):
                pass
            return
        if not _explicit_unwatermarked_field(path):
            return
        decoded = value.strip() if value.strip().startswith(("http://", "https://")) else (
            decode_qaab_url(value, key_seed) or decode_main_url(value)
        )
        if not decoded or decoded in seen or _url_has_watermark_marker(decoded):
            return
        seen.add(decoded)
        found.append({"url": decoded, "key": path})

    walk(latest_message)
    return found


def _find_deep_string(value: Any, key: str, depth: int = 0) -> str:
    if depth > 12:
        return ""
    if isinstance(value, dict):
        direct = value.get(key)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        for child in value.values():
            found = _find_deep_string(child, key, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_deep_string(child, key, depth + 1)
            if found:
                return found
    elif isinstance(value, str) and value.strip().startswith(("{", "[")):
        try:
            return _find_deep_string(json.loads(value), key, depth + 1)
        except (TypeError, ValueError):
            return ""
    return ""


def _quality_number(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def fallback_payload_video_url(payload: Any) -> str:
    root = payload if isinstance(payload, dict) else {}
    nested_data = root.get("data") if isinstance(root.get("data"), dict) else {}
    video_info = root.get("video_info") or nested_data.get("video_info") or root
    if not isinstance(video_info, dict):
        return ""
    data = video_info.get("data") if isinstance(video_info.get("data"), dict) else video_info
    video_list = data.get("video_list") if isinstance(data, dict) else None
    entries = list(video_list.values()) if isinstance(video_list, dict) else [data]
    candidates: list[tuple[int, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        token = str(entry.get("main_url") or entry.get("play_url") or "").strip()
        if not token:
            continue
        score = _quality_number(entry.get("bitrate") or entry.get("real_bitrate")) + (
            _quality_number(entry.get("vwidth") or entry.get("width"))
            * _quality_number(entry.get("vheight") or entry.get("height"))
        )
        candidates.append((score, token))
    key_seed = _find_deep_string(payload, "key_seed")
    for _, token in sorted(candidates, key=lambda item: item[0], reverse=True):
        decoded = decode_qaab_url(token, key_seed) or decode_main_url(token)
        if decoded and not _url_has_watermark_marker(decoded):
            return decoded
    return ""


async def fetch_doubao_unwatermarked_result(
    client: httpx.AsyncClient,
    cookie_header: str,
    data: Any,
) -> dict[str, Any]:
    fallback_apis = extract_doubao_fallback_apis(data)
    errors: list[str] = []
    attempts = 0
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Cookie": str(cookie_header or ""),
        "Origin": "https://www.doubao.com",
        "Referer": DOUBAO_URL,
        "User-Agent": BROWSER_USER_AGENT,
    }
    for fallback_api in fallback_apis:
        attempts += 1
        try:
            response = await client.get(
                unwatermarked_fallback_url(fallback_api),
                headers=headers,
                follow_redirects=True,
            )
            response.raise_for_status()
            payload = json.loads(response.content.decode("utf-8-sig"))
            url = fallback_payload_video_url(payload)
            if url:
                return {
                    "url": url,
                    "source": "fallback_unwatermarked",
                    "key": "video_model.fallback_api",
                    "status": "completed",
                    "attempts": attempts,
                    "errors": errors,
                }
            errors.append("fallback_decode_failed")
        except httpx.HTTPStatusError as exc:
            errors.append(f"fallback_http_{int(exc.response.status_code)}")
        except httpx.HTTPError:
            errors.append("fallback_transport_error")
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            errors.append("fallback_invalid_response")

    explicit_candidates = extract_doubao_explicit_unwatermarked_urls(data)
    if explicit_candidates:
        selected = explicit_candidates[0]
        return {
            "url": selected["url"],
            "source": "single_chain_explicit_unwatermarked",
            "key": selected["key"],
            "status": "completed",
            "attempts": attempts,
            "errors": errors,
        }
    status = "fallback_api_missing" if not fallback_apis else "fallback_unavailable"
    return {"url": "", "source": "", "key": "", "status": status, "attempts": attempts, "errors": errors}


async def fetch_doubao_unwatermarked_url(
    client: httpx.AsyncClient,
    cookie_header: str,
    data: Any,
) -> str:
    return str((await fetch_doubao_unwatermarked_result(client, cookie_header, data)).get("url") or "")


def is_doubao_account_quota_insufficient(text: str) -> bool:
    value = re.sub(r"\s+", "", str(text or ""))
    direct_markers = (
        "今日视频生成免费次数用完了",
        "视频生成免费次数用完了",
        "免费次数用完了",
        "次数用完了",
        "额度不足",
        "额度已用完",
        "额度用完了",
        "额度已耗尽",
        "额度耗尽了",
        "次数不足",
        "次数已用完",
        "次数已耗尽",
        "余额不足",
    )
    if any(marker in value for marker in direct_markers):
        return True
    if re.search(r"(?:额度|次数|余额)(?:为|剩余)?0(?:个|次|点)?", value):
        return True
    if re.search(r"(?:剩余|还有)0(?:个)?(?:次|额度|视频生成额度)", value):
        return True
    return "视频生成额度" in value and "剩余" in value and "无法生成" in value


def doubao_ui_generation_acknowledged(text: str, model: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or ""))
    model_name = re.sub(r"\s+", "", str(model or ""))
    return bool(
        DOUBAO_SUBMISSION_MARKER in normalized
        and model_name
        and f"本次使用{model_name}生成" in normalized
    )


def parse_doubao_generation_result(body: str) -> dict[str, Any]:
    body = str(body or "")
    if "710022002" in body or "当前服务访问频繁" in body or "服务访问频繁" in body:
        return {"state": "rate_limited", "text": "豆包当前服务访问频繁"}
    if "710022004" in body or '"type":"verify"' in body or '"verify_scene":"doubao_message_web"' in body:
        return {"state": "verification", "text": "豆包需要网页人机验证"}
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("doubao single chain returned invalid JSON") from exc
    main_url = decode_main_url(extract_main_url(payload))
    text = extract_tts_content(payload)
    if main_url:
        candidates: dict[str, dict[str, Any]] = {}
        add_doubao_video_candidate(
            candidates,
            main_url,
            key="video_model.main_url",
            source="single_chain",
        )
        candidate = best_doubao_video_candidate(candidates)
        return {"state": "completed", "text": text, "candidate": candidate}
    if is_doubao_account_quota_insufficient(text):
        return {"state": "quota_insufficient", "text": text or "豆包账号额度不足或已耗尽"}
    if any(
        marker in text
        for marker in (
            "生成失败",
            "无法生成",
            "内容违规",
            "疑似包含侵权/违规",
            "无法返回该内容",
            "生成额度未扣除",
        )
    ):
        return {"state": "failed", "text": text or "豆包视频生成失败"}
    if any(marker in text for marker in ("扫码登录", "手机号登录", "登录豆包")):
        return {"state": "login_invalid", "text": text or "豆包登录状态失效"}
    if doubao_payload_has_submission_marker(payload):
        return {"state": "generating", "text": text or DOUBAO_SUBMISSION_MARKER}
    return {"state": "submission_unconfirmed", "text": text or "豆包未返回视频生成提交回执"}


def doubao_video_url_score(url: str, key: str = "", source: str = "") -> int:
    value = str(url or "").lower()
    field = str(key or "").lower()
    candidate_source = str(source or "").lower()
    score = 0
    clean_markers = (
        "no_watermark",
        "without_watermark",
        "watermark_free",
        "unwatermarked",
        "watermark=0",
        "watermark%3d0",
        "wm=0",
    )
    explicitly_clean = any(marker in field or marker in value for marker in clean_markers)
    if explicitly_clean:
        score += 500
    if any(marker in field for marker in ("original", "origin", "source_url", "raw_url")):
        score += 380
    if "download" in field:
        score += 120
    if "main_url" in field:
        score += 240
    elif "video_url" in field:
        score += 100
    elif "play_url" in field or "play_addr" in field:
        score += 60
    if candidate_source == "submission_response":
        score += 30
    elif candidate_source == "fallback_unwatermarked":
        score += 700
    elif candidate_source == "single_chain":
        score += 100
    elif candidate_source == "media_response":
        score += 20
    elif candidate_source in {"video_current_src", "source_src", "download_link"}:
        score += 10
    if ".mp4" in value or "video_mp4" in value:
        score += 20
    if ".m3u8" in value:
        score -= 20
    if not explicitly_clean and ("watermark" in field or "watermark=1" in value or "wm=1" in value):
        score -= 600
    if any(marker in field or marker in value for marker in ("preview", "thumbnail", "poster", "cover", "sample")):
        score -= 180
    return score


def doubao_video_candidate_is_acceptable(candidate: dict[str, Any] | None) -> bool:
    if not candidate:
        return False
    source = str(candidate.get("source") or "").strip().lower()
    key = str(candidate.get("key") or "").strip().lower()
    url = str(candidate.get("url") or "").strip()
    watermark_status = str(candidate.get("watermark_status") or "").strip().lower()
    if not url or _url_has_watermark_marker(url) or (watermark_status and watermark_status != "original"):
        return False
    if source in DOUBAO_UNWATERMARKED_RESULT_SOURCES:
        return True
    return _explicit_unwatermarked_field(key)


def add_doubao_video_candidate(
    candidates: dict[str, dict[str, Any]],
    url: str,
    *,
    key: str = "",
    source: str = "network_json",
) -> None:
    normalized = html.unescape(str(url or "").strip()).replace("\\u0026", "&").replace("\\/", "/")
    normalized = normalized.rstrip('"\' ,')
    if not normalized.startswith(("http://", "https://")):
        return
    path = normalized.split("?", 1)[0].lower()
    if path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")):
        return
    score = doubao_video_url_score(normalized, key, source)
    current = candidates.get(normalized)
    if current is None or score > int(current.get("score") or -1000):
        candidates[normalized] = {"url": normalized, "key": str(key or ""), "source": str(source or ""), "score": score}


def collect_doubao_video_candidates(
    value: Any,
    candidates: dict[str, dict[str, Any]],
    key: str = "",
    *,
    source: str = "network_json",
) -> None:
    if isinstance(value, dict):
        for child_key, child in value.items():
            path = f"{key}.{child_key}" if key else str(child_key)
            collect_doubao_video_candidates(child, candidates, path, source=source)
        return
    if isinstance(value, list):
        for child in value:
            collect_doubao_video_candidates(child, candidates, key, source=source)
        return
    if not isinstance(value, str):
        return
    text = html.unescape(value).replace("\\u0026", "&").replace("\\/", "/")
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            nested = json.loads(stripped)
        except (TypeError, ValueError):
            nested = None
        if isinstance(nested, (dict, list)):
            collect_doubao_video_candidates(nested, candidates, key, source=source)
            return
    matches = VIDEO_URL_RE.findall(text)
    video_field = re.search(r"video|play|download|origin|original|source|watermark|main_url", key, re.IGNORECASE)
    image_field = re.search(r"cover|poster|thumbnail|image|avatar", key, re.IGNORECASE)
    if video_field and not image_field and stripped.startswith(("http://", "https://")) and not re.search(r"\s", stripped):
        matches.insert(0, stripped)
    for match in dict.fromkeys(matches):
        add_doubao_video_candidate(candidates, match, key=key, source=source)


def collect_doubao_response_candidates(body: str, candidates: dict[str, dict[str, Any]]) -> None:
    text = str(body or "")
    parsed = False
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, (dict, list)):
        collect_doubao_video_candidates(payload, candidates, "response", source="network_json")
        parsed = True
    for line in text.splitlines():
        value = line.strip()
        if value.startswith("data:"):
            value = value[5:].strip()
        if not value or value == "[DONE]" or not value.startswith(("{", "[")):
            continue
        try:
            payload = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, (dict, list)):
            collect_doubao_video_candidates(payload, candidates, "response", source="network_json")
            parsed = True
    if not parsed:
        collect_doubao_video_candidates(text, candidates, "response", source="network_json")


def best_doubao_video_candidate(candidates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return max(
        candidates.values(),
        key=lambda item: (int(item.get("score") or 0), ".mp4" in str(item.get("url") or "").lower()),
        default={},
    )


def is_doubao_text_only_video_response(value: str) -> bool:
    text = re.sub(r"\s+", "", str(value or "")).lower()
    if not text or DOUBAO_SUBMISSION_MARKER in text:
        return False
    direct_refusals = (
        "没办法直接生成视频文件",
        "无法直接生成视频文件",
        "不能直接生成视频文件",
        "没法直接生成视频文件",
        "无法为你直接生成视频文件",
        "不能为你直接生成视频文件",
    )
    if any(marker in text for marker in direct_refusals):
        return True
    prompt_export = any(marker in text for marker in (
        "直接用于ai视频生成工具的提示词",
        "用于ai视频生成工具的提示词",
        "把提示词导入",
        "将提示词导入",
        "复制到视频生成工具",
        "复制到ai视频生成工具",
        "复制到视频创作工具",
    ))
    tool_recommendation = "视频生成工具" in text or any(
        marker in text for marker in ("剪映ai", "可灵ai", "即梦ai", "海螺ai")
    )
    versioned_prompts = (
        ("版本1" in text or "版本一" in text)
        and ("版本2" in text or "版本二" in text)
        and ("提示词" in text or "风格" in text)
    )
    explicit_prompt_rewrite = any(marker in text for marker in (
        "ai视频提示词",
        "视频生成提示词",
        "视频创作提示词",
    )) and any(marker in text for marker in (
        "提示词如下",
        "版本1",
        "版本一",
        "分镜",
        "镜头",
        "可直接使用",
    ))
    return explicit_prompt_rewrite or (prompt_export and (tool_recommendation or versioned_prompts))


def doubao_reference_upload_progress_visible(value: str) -> bool:
    text = re.sub(r"\s+", "", str(value or ""))
    if any(marker in text for marker in ("正在上传", "上传中", "处理中")):
        return True
    return any(0 <= int(match) <= 100 for match in re.findall(r"(?<!\d)(\d{1,3})%(?!\d)", text))


def classify_doubao_submission(result: dict[str, Any]) -> tuple[str, str]:
    if result.get("region_restricted"):
        return "doubao region restricted", "region_restricted"
    if result.get("login_invalid"):
        return "doubao account not logged in", "login_invalid"
    if result.get("service_frequent"):
        return "doubao service frequent", "service_frequent"
    if result.get("slider_verification"):
        return "doubao verification required", "slider_verification"
    if result.get("quota_insufficient"):
        return "豆包账号额度不足或已耗尽", "quota_insufficient"
    if result.get("text_only_response"):
        return "豆包仅返回文本，未提交视频生成", "text_only_response"
    if result.get("video_creation_page_refusal_repeated"):
        return "豆包要求改用视频创作页面", "generation_ack_missing"
    if result.get("video_creation_ui_error"):
        return str(result.get("video_creation_ui_error") or "doubao video creation page unavailable"), "video_creation_ui_error"
    if result.get("stream_error"):
        return "doubao submit rejected", "submit_rejected"
    if not result.get("ok"):
        return f"doubao submit http {int(result.get('status') or 0)}", "http_error"
    if not result.get("accepted"):
        return "doubao generation acknowledgement missing", "generation_ack_missing"
    return "", ""


def extract_doubao_assistant_response_text(value: str) -> str:
    chunks: list[str] = []
    reply_briefs: list[str] = []
    for block in re.split(r"\r?\n\r?\n", str(value or "")):
        event = ""
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if event not in {"CHUNK_DELTA", "SSE_REPLY_END"} or not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except (TypeError, ValueError):
            continue
        if event == "CHUNK_DELTA" and isinstance(payload, dict) and isinstance(payload.get("text"), str):
            chunks.append(payload["text"])
        elif event == "SSE_REPLY_END" and isinstance(payload, dict):
            finish = payload.get("msg_finish_attr")
            if isinstance(finish, dict) and isinstance(finish.get("brief"), str):
                reply_briefs.append(finish["brief"])
    parts = ["".join(chunks).strip(), *[item.strip() for item in reply_briefs]]
    return "\n".join(item for item in parts if item)


def doubao_payload_has_submission_marker(value: Any) -> bool:
    if isinstance(value, dict):
        if str(value.get("role") or "").lower() == "user" or str(value.get("user_type") or "") == "1":
            return False
        return any(doubao_payload_has_submission_marker(child) for child in value.values())
    if isinstance(value, list):
        return any(doubao_payload_has_submission_marker(child) for child in value)
    if isinstance(value, str):
        if DOUBAO_SUBMISSION_MARKER in value:
            return True
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return doubao_payload_has_submission_marker(json.loads(stripped))
            except (TypeError, ValueError):
                return False
    return False


def detect_doubao_generation_acknowledgement(value: str) -> str:
    raw = str(value or "")
    try:
        if doubao_payload_has_submission_marker(json.loads(raw)):
            return DOUBAO_SUBMISSION_MARKER
    except (TypeError, ValueError):
        pass
    for line in raw.splitlines():
        data = line[5:].strip() if line.startswith("data:") else ""
        if not data or not data.startswith(("{", "[")):
            continue
        try:
            if doubao_payload_has_submission_marker(json.loads(data)):
                return DOUBAO_SUBMISSION_MARKER
        except (TypeError, ValueError):
            continue
    if not re.search(r"(?m)^event:", raw) and DOUBAO_SUBMISSION_MARKER in raw:
        return DOUBAO_SUBMISSION_MARKER
    return ""


def detect_doubao_video_creation_page_refusal(value: str) -> str:
    raw = str(value or "")
    assistant_text = extract_doubao_assistant_response_text(raw)
    searchable = assistant_text or ("" if re.search(r"(?m)^event:", raw) else raw)
    match = re.search(
        r"(?:无法|不能|不支持)[^\n\r]{0,50}(?:直接)?[^\n\r]{0,30}(?:生成|触发)"
        r"[^\n\r]{0,120}(?:(?:进入|前往|打开)[^\n\r]{0,40}(?:视频)?(?:创作|生成)?(?:页面|入口)|"
        r"(?:豆包)?视频(?:AI)?创作(?:功能页|页面|入口)[^\n\r]{0,30}(?:提交|生成)?)",
        searchable,
        re.IGNORECASE,
    )
    return match.group(0) if match else ""


def should_use_doubao_video_creation_page(result: dict[str, Any]) -> bool:
    if not isinstance(result, dict) or result.get("accepted") or not result.get("ok"):
        return False
    terminal_keys = (
        "service_frequent",
        "slider_verification",
        "quota_insufficient",
        "stream_error",
        "region_restricted",
        "login_invalid",
    )
    return not any(bool(result.get(key)) for key in terminal_keys)


def normalize_doubao_submission_acknowledgement(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict) or result.get("accepted") or not result.get("ok"):
        return result
    response_text = "\n".join(
        str(result.get(key) or "")
        for key in ("initial_response_preview", "response_preview", "direct_submission_response_preview")
    )
    if "710022004" in response_text:
        result["slider_verification"] = True
        result["upstream_error_code"] = "710022004"
        result["upstream_error_message"] = "rate limited"
        return result
    if "710022002" in response_text:
        result["service_frequent"] = True
        result["upstream_error_code"] = "710022002"
        return result
    if "event: STREAM_ERROR" in response_text:
        result["stream_error"] = True
        return result
    acknowledgement = detect_doubao_generation_acknowledgement(
        response_text
    )
    if not acknowledgement:
        return result
    result["accepted"] = True
    result["generation_wait_message_detected"] = True
    result["generation_wait_message"] = acknowledgement
    result["generation_ack_source"] = "python_response_fallback"
    return result


DOUBAO_SINGLE_CHAIN_SCRIPT = r"""
async ({conversationId}) => {
  function cookieValue(name) {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = document.cookie.match(new RegExp(`(?:^|; )${escaped}=([^;]*)`));
    return match ? decodeURIComponent(match[1]) : "";
  }
  function storageDigits(regex) {
    for (const store of [localStorage, sessionStorage]) {
      for (let index = 0; index < store.length; index += 1) {
        const key = store.key(index);
        const value = store.getItem(key) || "";
        const match = regex.test(key) ? value.match(/\d{15,24}/) : null;
        if (match) return match[0];
      }
    }
    return "";
  }
  const webId = storageDigits(/web_id|tea_uuid|device_id/i) || "111";
  const region = cookieValue("flow_user_country") || "JP";
  const params = new URLSearchParams({
    version_code: "20800",
    language: "zh",
    device_platform: "web",
    aid: "497858",
    real_aid: "497858",
    pkg_type: "release_version",
    device_id: webId,
    pc_version: "3.29.10",
    web_id: webId,
    tea_uuid: webId,
    region,
    sys_region: region,
    samantha_web: "1",
    web_platform: "browser",
    "use-olympus-account": "1",
    web_tab_id: crypto.randomUUID ? crypto.randomUUID() : String(Date.now())
  });
  const response = await fetch(`${location.origin}/im/chain/single?${params.toString()}`, {
    method: "POST",
    credentials: "include",
    headers: {
      accept: "application/json, text/plain, */*",
      "agw-js-conv": "str",
      "content-type": "application/json; encoding=utf-8"
    },
    body: JSON.stringify({
      cmd: 3100,
      uplink_body: {
        pull_singe_chain_uplink_body: {
          conversation_id: String(conversationId || ""),
          anchor_index: 111,
          conversation_type: 3,
          direction: 1,
          limit: 20,
          ext: {},
          filter: {index_list: []},
          evaluate_ab_params: "",
          evaluate_common_params: ""
        }
      },
      sequence_id: "111",
      channel: 2,
      version: "1"
    })
  });
  return {
    ok: response.ok,
    status: response.status,
    body: (await response.text()).slice(0, 2097152)
  };
}
"""


DOUBAO_PREHANDLE_ATTACHMENTS_SCRIPT = r"""
async ({attachments}) => {
  function uuid() {
    return crypto.randomUUID ? crypto.randomUUID() :
      "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {
        const r = Math.random() * 16 | 0;
        const v = c === "x" ? r : (r & 0x3 | 0x8);
        return v.toString(16);
      });
  }
  function cookieValue(name) {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = document.cookie.match(new RegExp(`(?:^|; )${escaped}=([^;]*)`));
    return match ? decodeURIComponent(match[1]) : "";
  }
  function storageFind(regex) {
    for (const store of [localStorage, sessionStorage]) {
      for (let index = 0; index < store.length; index += 1) {
        const key = store.key(index);
        const value = store.getItem(key) || "";
        if (regex.test(key) && value && value.length < 100) return value;
      }
    }
    return "";
  }
  function trySign(url) {
    const signers = [window.byted_acrawler, window.bytedAcrawler, window.__acrawler, window.ABogus].filter(Boolean);
    for (const signer of signers) {
      try {
        if (typeof signer.sign !== "function") continue;
        const signed = signer.sign({url});
        if (typeof signed === "string" && signed) return signed;
        if (signed && typeof signed === "object") {
          if (typeof signed.a_bogus === "string") return signed.a_bogus;
          if (typeof signed.aBogus === "string") return signed.aBogus;
        }
      } catch (_) {}
    }
    return "";
  }
  const webId = (storageFind(/web_id|tea_uuid|device_id/i).match(/\d{15,24}/) || [])[0]
    || `${Date.now()}${String(Math.random()).slice(2, 8)}`;
  const fp = cookieValue("s_v_web_id") || storageFind(/s_v_web_id|fp|verify/i) || `verify_${Date.now()}`;
  const region = cookieValue("flow_user_country") || "JP";
  const pcVersion = storageFind(/pc_version/i) || "3.30.1";
  const localMessageId = uuid();
  const normalized = (attachments || []).map(item => ({
    ...item,
    identifier: item.identifier || uuid(),
    local_message_id: item.local_message_id || localMessageId
  }));
  const results = [];
  for (const item of normalized) {
    const params = new URLSearchParams({
      version_code: "20800",
      language: "zh",
      device_platform: "web",
      doubao_device_platform: "web",
      aid: "497858",
      real_aid: "497858",
      pkg_type: "release_version",
      device_id: webId,
      pc_version: pcVersion,
      doubao_pc_version: pcVersion,
      web_id: webId,
      tea_uuid: webId,
      region,
      sys_region: region,
      samantha_web: "1",
      web_platform: "browser",
      "use-olympus-account": "1",
      web_tab_id: uuid(),
      fp
    });
    const msToken = cookieValue("msToken") || storageFind(/mstoken/i);
    if (msToken) params.set("msToken", msToken);
    let requestUrl = `${location.origin}/alice/message/pre_handle_v2_without_conv?${params.toString()}`;
    const aBogus = trySign(requestUrl);
    if (aBogus) {
      params.set("a_bogus", aBogus);
      requestUrl = `${location.origin}/alice/message/pre_handle_v2_without_conv?${params.toString()}`;
    }
    const response = await fetch(requestUrl, {
      method: "POST",
      credentials: "include",
      headers: {
        accept: "application/json, text/plain, */*",
        "agw-js-conv": "str",
        "content-type": "application/json"
      },
      body: JSON.stringify({
        uplink_entity: {
          entity_type: 2,
          entity_content: {image: {key: item.uri}},
          identifier: item.identifier
        },
        bot_id: "7338286299411103781",
        local_message_id: item.local_message_id
      })
    });
    const text = await response.text();
    let payload = null;
    try { payload = JSON.parse(text); } catch (_) {}
    results.push({ok: response.ok && payload && Number(payload.code) === 0, status: response.status, text: text.slice(0, 1000)});
    if (!results[results.length - 1].ok) {
      return {ok: false, status: response.status, error: text.slice(0, 1000), attachments: normalized, results};
    }
  }
  return {ok: true, attachments: normalized, results};
}
"""


DOUBAO_SUBMIT_SCRIPT = r"""
async ({prompt, ratio, model, duration, attachments, retryLimit, retryDelayMs, freshConversationRetryUsed}) => {
  function uuid() {
    return crypto.randomUUID ? crypto.randomUUID() :
      "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {
        const r = Math.random() * 16 | 0;
        const v = c === "x" ? r : (r & 0x3 | 0x8);
        return v.toString(16);
      });
  }
  function randomDigits(len) {
    let out = "";
    for (let i = 0; i < len; i += 1) out += String(Math.floor(Math.random() * 10));
    return out.replace(/^0/, "1");
  }
  function cookieValue(name) {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = document.cookie.match(new RegExp(`(?:^|; )${escaped}=([^;]*)`));
    return match ? decodeURIComponent(match[1]) : "";
  }
  function storageFind(regex) {
    for (const store of [localStorage, sessionStorage]) {
      for (let index = 0; index < store.length; index += 1) {
        const key = store.key(index);
        const value = store.getItem(key) || "";
        if (regex.test(key) && value && value.length < 100) return value;
      }
    }
    return "";
  }
  function trySign(url) {
    const signers = [window.byted_acrawler, window.bytedAcrawler, window.__acrawler, window.ABogus].filter(Boolean);
    for (const signer of signers) {
      try {
        if (typeof signer.sign !== "function") continue;
        const signed = signer.sign({url});
        if (typeof signed === "string" && signed) return signed;
        if (signed && typeof signed === "object") {
          if (typeof signed.a_bogus === "string") return signed.a_bogus;
          if (typeof signed.aBogus === "string") return signed.aBogus;
          if (typeof signed.url === "string") {
            const value = new URL(signed.url, location.origin).searchParams.get("a_bogus");
            if (value) return value;
          }
        }
      } catch (_) {}
    }
    return "";
  }
  function extract(patterns, text) {
    for (const pattern of patterns) {
      const match = text.match(pattern);
      if (match) return match[1];
    }
    return "";
  }
  function searchableText(value) {
    return String(value || "")
      .replace(/\\u([0-9a-fA-F]{4})/g, (_, code) => String.fromCharCode(parseInt(code, 16)))
      .replace(/\\n/g, "\n")
      .replace(/\\"/g, '"')
      .replace(/\\\//g, "/");
  }
  function asksForVideoConfirmation(value) {
    const text = searchableText(value);
    const mentionsVideo = /视频|video/i.test(text);
    const asksInChinese = /(?:是否|请问).{0,40}(?:需要|生成|创建)|(?:需要|要).{0,20}(?:我|为您|帮您).{0,30}(?:生成|创建)/i.test(text);
    const asksInEnglish = /(?:would you like|do you want|shall i|should i).{0,80}(?:create|generate|proceed)/i.test(text);
    return mentionsVideo && (asksInChinese || asksInEnglish);
  }
  function assistantResponseText(value) {
    const chunks = [];
    const briefs = [];
    for (const block of String(value || "").split(/\r?\n\r?\n/)) {
      let event = "";
      const dataLines = [];
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length || !["CHUNK_DELTA", "SSE_REPLY_END"].includes(event)) continue;
      try {
        const payload = JSON.parse(dataLines.join("\n"));
        if (event === "CHUNK_DELTA" && typeof payload.text === "string") chunks.push(payload.text);
        if (event === "SSE_REPLY_END" && typeof payload.msg_finish_attr?.brief === "string") {
          briefs.push(payload.msg_finish_attr.brief);
        }
      } catch (_) {}
    }
    return [chunks.join("").trim(), ...briefs.map(item => item.trim())].filter(Boolean).join("\n");
  }
  function generationWaitMessage(value) {
    const rawText = searchableText(value);
    return rawText.includes("视频生成已提交") ? "视频生成已提交" : "";
  }
  function textOnlySubmissionClaim(value) {
    const rawText = searchableText(value);
    const assistantText = assistantResponseText(rawText);
    const text = assistantText || (/^event:/m.test(rawText) ? "" : rawText);
    if (!text || text.includes("视频生成已提交")) return "";
    const match = text.match(/(?:已[^\n\r]{0,20}提交|正在[^\n\r]{0,20}(?:生成|渲染)|(?:生成|渲染)(?:中|处理(?:中)?)|预计等待)/i);
    return match ? match[0] : "";
  }
  function videoCreationPageRefusal(value) {
    const rawText = searchableText(value);
    const assistantText = assistantResponseText(rawText);
    const text = assistantText || (/^event:/m.test(rawText) ? "" : rawText);
    const match = text.match(/(?:无法|不能|不支持)[^\n\r]{0,50}(?:直接)?[^\n\r]{0,30}(?:生成|触发)[^\n\r]{0,120}(?:(?:进入|前往|打开)[^\n\r]{0,40}(?:视频)?(?:创作|生成)?(?:页面|入口)|(?:豆包)?视频(?:AI)?创作(?:功能页|页面|入口)[^\n\r]{0,30}(?:提交|生成)?)/i);
    return match ? match[0] : "";
  }
  function quotaInsufficient(value) {
    const text = searchableText(value).replace(/\s+/g, "");
    return /(?:视频生成)?额度(?:不足|已用完|用完了|已耗尽|耗尽了|为0|剩余0)/.test(text)
      || /(?:次数|余额)(?:不足|已用完|用完了|已耗尽|耗尽了|为0|剩余0)/.test(text)
      || /(?:剩余|还有)0(?:个)?(?:次|额度|视频生成额度)/.test(text)
      || /视频生成额度.*剩余.*无法生成/.test(text);
  }
  function terminalSubmissionSignal(value) {
    const text = searchableText(value);
    return quotaInsufficient(text)
      || text.includes("710022002")
      || text.includes("710022004")
      || text.includes("当前服务访问频繁")
      || text.includes("服务访问频繁")
      || text.includes("STREAM_ERROR")
      || text.includes('"type":"verify"')
      || text.includes('"verify_scene":"doubao_message_web"');
  }
  function modelDisplayName(value) {
    if (value === "seedance_v2.0_mini") return "Seedance 2.0 Mini";
    if (value === "seedance_v2.0") return "Seedance 2.0 Fast";
    return String(value || "Seedance");
  }
  function generationInstruction(promptText, followUp = false) {
    const ratioText = ratio && ratio !== "auto" ? ratio : "自动";
    return `生成视频：${promptText}，${ratioText}，${seconds}s`;
  }
  function lastMessageIndex(value) {
    const matches = [...String(value || "").matchAll(/"(?:message_index|messageIndex)"\s*:\s*(\d+)/g)];
    if (!matches.length) return null;
    return Math.max(...matches.map(item => Number(item[1])).filter(Number.isFinite));
  }

  let localConversationId = `local_${randomDigits(16)}`;
  const uniqueKey = uuid();
  const webId = (storageFind(/web_id|tea_uuid|device_id/i).match(/\d{15,24}/) || [])[0]
    || `${Date.now()}${randomDigits(6)}`;
  const fp = cookieValue("s_v_web_id") || storageFind(/s_v_web_id|fp|verify/i) || `verify_${randomDigits(12)}`;
  const region = cookieValue("flow_user_country") || "JP";
  const pcVersion = storageFind(/pc_version/i) || "3.30.1";
  const params = new URLSearchParams({
    aid: "497858",
    device_id: webId,
    device_platform: "web",
    doubao_device_platform: "web",
    doubao_pc_version: pcVersion,
    fp,
    language: "zh",
    pc_version: pcVersion,
    pkg_type: "release_version",
    real_aid: "497858",
    region,
    samantha_web: "1",
    sys_region: region,
    tea_uuid: webId,
    tz_name: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
    "use-olympus-account": "1",
    version_code: "20800",
    web_id: webId,
    web_platform: "browser",
    web_tab_id: uuid()
  });
  const msToken = cookieValue("msToken") || storageFind(/mstoken/i);
  if (msToken) params.set("msToken", msToken);
  let requestUrl = `${location.origin}/chat/completion?${params.toString()}`;
  const aBogus = trySign(requestUrl);
  if (aBogus) {
    params.set("a_bogus", aBogus);
    requestUrl = `${location.origin}/chat/completion?${params.toString()}`;
  }

  const seconds = Math.max(1, Number(duration) || 10);
  const messages = [];
  if (attachments && attachments.length) {
    const attachmentMessageId = attachments[0].local_message_id || uuid();
    messages.push({
      local_message_id: attachmentMessageId,
      content_block: [{
        block_type: 10052,
        content: {
          attachment_block: {
            attachments: attachments.map(item => ({
              type: 1,
              identifier: item.identifier || uuid(),
              image: {
                name: item.name || "image.png",
                uri: item.uri,
                image_ori: {
                  url: "",
                  width: Number(item.width || 0),
                  height: Number(item.height || 0),
                  format: "",
                  url_formats: {}
                }
              },
              parse_state: 0,
              review_state: 1,
              upload_status: 1,
              progress: 100,
              src: ""
            }))
          },
          pc_event_block: ""
        },
        block_id: uuid(),
        parent_id: "",
        meta_info: [],
        append_fields: []
      }],
      message_status: 0
    });
  }
  messages.push({
    local_message_id: uuid(),
    content_block: [{
      block_type: 10000,
      content: {
        text_block: {text: generationInstruction(prompt), icon_url: "", icon_url_dark: "", summary: ""},
        pc_event_block: ""
      },
      block_id: uuid(),
      parent_id: "",
      meta_info: [],
      append_fields: []
    }],
    message_status: 0
  });
  const payload = {
    client_meta: {
      local_conversation_id: localConversationId,
      conversation_id: "",
      bot_id: "7338286299411103781",
      last_section_id: "",
      last_message_index: null
    },
    messages,
    option: {
      send_message_scene: "",
      create_time_ms: Date.now(),
      collect_id: "",
      is_audio: false,
      answer_with_suggest: false,
      tts_switch: false,
      need_deep_think: 0,
      click_clear_context: false,
      from_suggest: false,
      is_regen: false,
      is_replace: false,
      is_from_click_option: false,
      is_from_click_softlink: false,
      disable_sse_cache: false,
      select_text_action: "",
      is_select_text: false,
      resend_for_regen: false,
      scene_type: 0,
      unique_key: uniqueKey,
      start_seq: 0,
      need_create_conversation: true,
      conversation_init_option: {need_ack_conversation: true},
      regen_query_id: [],
      edit_query_id: [],
      regen_instruction: "",
      no_replace_for_regen: false,
      message_from: 0,
      shared_app_name: "",
      shared_app_id: "",
      sse_recv_event_options: {support_chunk_delta: true},
      is_ai_playground: false,
      is_old_user: false,
      recovery_option: {
        is_recovery: false,
        req_create_time_sec: Math.floor(Date.now() / 1000),
        append_sse_event_scene: 0
      },
      message_storage_type: 0,
      related_deleted_message_ids: {}
    },
    chat_ability: {
      ability_type: 17,
      ability_param: JSON.stringify({ratio: ratio || "auto", model, duration: seconds})
    },
    user_context: [],
    ext: {
      answer_with_suggest: "0",
      sub_conv_firstmet_type: "1",
      collection_id: "",
      conversation_init_option: JSON.stringify({need_ack_conversation: true}),
      commerce_credit_config_enable: "0"
    }
  };
  history.pushState({}, "", `/chat/${localConversationId}`);
  async function submitPayload(body) {
    const response = await fetch(requestUrl, {
      method: "POST",
      credentials: "include",
      headers: {
        accept: "*/*",
        "agw-js-conv": "str, str",
        "content-type": "application/json",
        "last-event-id": "undefined"
      },
      body: JSON.stringify(body)
    });
    let text = "";
    let timedOut = false;
    const reader = response.body && response.body.getReader ? response.body.getReader() : null;
    if (reader) {
      const decoder = new TextDecoder("utf-8");
      const deadline = Date.now() + 90000;
      for (;;) {
        const remain = Math.max(1, deadline - Date.now());
        const item = await Promise.race([
          reader.read(),
          new Promise(resolve => setTimeout(() => resolve({timeout: true}), remain))
        ]);
        if (item.timeout) {
          timedOut = true;
          break;
        }
        const {done, value} = item;
        if (done) break;
        text += decoder.decode(value, {stream: true});
        if (text.includes("710022002") || text.includes("710022004") || text.includes("STREAM_ERROR") || text.includes("SSE_REPLY_END")) break;
      }
      try { await reader.cancel(); } catch (_) {}
      try { text += decoder.decode(); } catch (_) {}
    } else {
      text = await response.text();
    }
    return {response, text, timedOut};
  }
  function findConversationId(value) {
    return extract([
      /"(?:conversation_id|conversationId|conversationID|conv_id|convId)"\s*:\s*"?(\d{15,24})"?/,
      /(?:conversation_id|conversationId|conversationID|conv_id|convId)(?:\\?"|)\s*[:=]\s*(?:\\?")?(\d{15,24})/
    ], value);
  }
  function findVideoUrl(value) {
    return extract([
      /(https?:\/\/[^"\\\s]+(?:mime_type=video_mp4|\.mp4(?:\?[^"\\\s]*)?))/i
    ], searchableText(value));
  }

  function conversationPayload(source, conversationId, responseText, messageText) {
    const body = JSON.parse(JSON.stringify(source));
    body.client_meta.local_conversation_id = localConversationId;
    body.client_meta.conversation_id = conversationId || "";
    body.client_meta.last_section_id = extract([/"(?:section_id|sectionId)"\s*:\s*"?(\d{15,24})"?/], responseText);
    body.client_meta.last_message_index = lastMessageIndex(responseText);
    const textMessage = body.messages.find(message =>
      (message.content_block || []).some(block => Number(block.block_type) === 10000)
    );
    if (!textMessage) throw new Error("doubao text message block missing");
    textMessage.local_message_id = uuid();
    textMessage.content_block[0].block_id = uuid();
    textMessage.content_block[0].content.text_block.text = messageText;
    if (conversationId) body.messages = [textMessage];
    body.option.create_time_ms = Date.now();
    body.option.unique_key = uuid();
    body.option.need_create_conversation = !conversationId;
    body.option.conversation_init_option = {need_ack_conversation: !conversationId};
    body.ext.conversation_init_option = JSON.stringify({need_ack_conversation: !conversationId});
    return body;
  }
  async function performAttempt(body, fallbackConversationId) {
    let submission = await submitPayload(body);
    let response = submission.response;
    let text = submission.text;
    let timedOut = submission.timedOut;
    let conversationId = findConversationId(text) || fallbackConversationId || "";
    let videoUrl = findVideoUrl(text);
    const initialText = text;
    const confirmationPromptDetected = asksForVideoConfirmation(text);
    let autoConfirmationSent = false;
    if (response.ok && conversationId && confirmationPromptDetected) {
      const confirmationPayload = conversationPayload(body, conversationId, text, generationInstruction(prompt, true));
      submission = await submitPayload(confirmationPayload);
      response = submission.response;
      text = `${text}\n${submission.text}`;
      timedOut = timedOut || submission.timedOut;
      conversationId = findConversationId(submission.text) || conversationId;
      videoUrl = findVideoUrl(submission.text) || videoUrl;
      autoConfirmationSent = true;
    }
    return {
      response,
      text,
      timedOut,
      conversationId,
      videoUrl,
      confirmationPromptDetected,
      autoConfirmationSent,
      initialText
    };
  }

  const maxResends = Math.max(0, Math.min(10, Number.parseInt(retryLimit, 10) || 0));
  const resendDelayMs = Math.max(5000, Number(retryDelayMs) || 15000);
  const retryInstruction = generationInstruction(prompt, true);
  let attempt = await performAttempt(payload, "");
  let response = attempt.response;
  let text = attempt.text;
  let timedOut = attempt.timedOut;
  let conversationId = attempt.conversationId;
  let videoUrl = attempt.videoUrl;
  let confirmationPromptDetected = attempt.confirmationPromptDetected;
  let autoConfirmationSent = attempt.autoConfirmationSent;
  const initialResponsePreview = attempt.initialText.length <= 6000
    ? attempt.initialText
    : `${attempt.initialText.slice(0, 3000)}\n...[truncated]...\n${attempt.initialText.slice(-3000)}`;
  let detectedWaitMessage = generationWaitMessage(text);
  let sameAccountResendCount = 0;
  let sameAccountAttemptCount = 1;
  let videoCreationPageRefusalCount = videoCreationPageRefusal(attempt.text) ? 1 : 0;
  let videoCreationPageNewConversationResendCount = freshConversationRetryUsed ? 1 : 0;
  let videoCreationPageRefusalRepeated = false;
  let latestAttemptText = attempt.text;
  while (
    response.ok
    && !detectedWaitMessage
    && !videoUrl
    && !terminalSubmissionSignal(latestAttemptText)
  ) {
    const creationPageRefusal = videoCreationPageRefusal(latestAttemptText);
    const unverifiedTextClaim = textOnlySubmissionClaim(latestAttemptText);
    if (creationPageRefusal || unverifiedTextClaim) {
      videoCreationPageRefusalRepeated = creationPageRefusal;
      break;
    }
    if (sameAccountResendCount >= maxResends) break;
    await new Promise(resolve => setTimeout(resolve, resendDelayMs));
    let resendPayload;
    let fallbackConversationId = conversationId;
    resendPayload = conversationPayload(payload, conversationId, latestAttemptText, retryInstruction);
    attempt = await performAttempt(resendPayload, fallbackConversationId);
    response = attempt.response;
    text = `${text}\n${attempt.text}`;
    latestAttemptText = attempt.text;
    timedOut = timedOut || attempt.timedOut;
    conversationId = attempt.conversationId || fallbackConversationId;
    videoUrl = attempt.videoUrl || videoUrl;
    confirmationPromptDetected = confirmationPromptDetected || attempt.confirmationPromptDetected;
    autoConfirmationSent = autoConfirmationSent || attempt.autoConfirmationSent;
    detectedWaitMessage = generationWaitMessage(attempt.text) || generationWaitMessage(text);
    if (videoCreationPageRefusal(attempt.text)) videoCreationPageRefusalCount += 1;
    sameAccountResendCount += 1;
    sameAccountAttemptCount += 1;
  }
  const preview = text.length <= 6000 ? text : `${text.slice(0, 3000)}\n...[truncated]...\n${text.slice(-3000)}`;
  return {
    ok: response.ok,
    status: response.status,
    response_preview: preview,
    conversation_id: conversationId,
    local_conversation_id: localConversationId,
    web_id: webId,
    region,
    video_url: videoUrl,
    confirmation_prompt_detected: confirmationPromptDetected,
    auto_confirmation_sent: autoConfirmationSent,
    initial_response_preview: initialResponsePreview,
    generation_wait_message_detected: Boolean(detectedWaitMessage),
    generation_wait_message: detectedWaitMessage,
    same_account_resend_count: sameAccountResendCount,
    same_account_attempt_count: sameAccountAttemptCount,
    same_account_retry_limit: maxResends,
    resend_delay_ms: resendDelayMs,
    video_creation_page_refusal_count: videoCreationPageRefusalCount,
    video_creation_page_new_conversation_resend_count: videoCreationPageNewConversationResendCount,
    video_creation_page_refusal_repeated: videoCreationPageRefusalRepeated,
    submitted_with_images: Boolean(attachments && attachments.length),
    accepted: Boolean(detectedWaitMessage || videoUrl),
    quota_insufficient: quotaInsufficient(text),
    service_frequent: text.includes("710022002") || text.includes("当前服务访问频繁") || text.includes("服务访问频繁"),
    slider_verification: text.includes("710022004") || text.includes('"type":"verify"') || text.includes('"verify_scene":"doubao_message_web"'),
    stream_error: text.includes("STREAM_ERROR"),
    sse_timed_out: timedOut
  };
}
"""


class DoubaoReferenceImageUploader(DolaFetchAutomation):
    async def _prepare_image_upload(self, page: Page) -> dict[str, Any]:
        try:
            result = await asyncio.wait_for(
                page.evaluate(PREPARE_UPLOAD_SCRIPT, {"body": DOUBAO_PREPARE_UPLOAD_BODY}),
                timeout=PREPARE_UPLOAD_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("prepare_upload timed out") from exc
        if not isinstance(result, dict):
            raise RuntimeError("prepare_upload returned invalid response")
        if not result.get("ok"):
            raise RuntimeError(f"prepare_upload failed with HTTP {result.get('status')}: {str(result.get('text') or '')[:500]}")
        data = result.get("json")
        if not isinstance(data, dict) or data.get("code") != 0:
            raise RuntimeError(f"prepare_upload returned unexpected body: {str(result.get('text') or data)[:500]}")
        upload_config = data.get("data")
        if not isinstance(upload_config, dict):
            raise RuntimeError("prepare_upload did not return upload config")
        return upload_config


class DoubaoVideoAutomation:
    def __init__(
        self,
        task_id: str,
        prompt: str,
        ratio: str,
        model: str,
        duration: int = 10,
        account: dict[str, Any] | None = None,
        proxy_session: Any | None = None,
        browser_pool: ReusableBrowserPool | None = None,
        submission_pacer: Callable[[], Awaitable[None]] | None = None,
        image_upload_slot: Callable[[], AsyncContextManager[None]] | None = None,
        image_preparation_done: Callable[[], None] | None = None,
    ):
        self.task_id = task_id
        self.prompt = prompt
        self.ratio = ratio
        self.model = model
        self.duration = max(1, int(duration or 10))
        self.account = account or {}
        self.proxy_session = proxy_session
        self.browser_pool = browser_pool
        self.submission_pacer = submission_pacer
        self.image_upload_slot = image_upload_slot
        self.image_preparation_done = image_preparation_done
        self.settings = load_settings()
        self.verification_solver = SliderChallengeSolver(
            SliderSolverSettings(max_attempts=3, verify_timeout_seconds=8.0)
        )
        ensure_dirs()
        self.state_path = DOUBAO_STATES_DIR / f"{str(self.account.get('id') or 'unknown')}.json"

    def _finish_image_preparation(self) -> None:
        callback = getattr(self, "image_preparation_done", None)
        self.image_preparation_done = None
        if callback is not None:
            callback()

    def _context_storage_state(self) -> dict[str, Any] | None:
        saved: dict[str, Any] = {}
        if self.state_path.is_file():
            try:
                value = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    saved = value
            except (OSError, ValueError):
                saved = {}

        merged: dict[tuple[str, str, str], dict[str, Any]] = {}
        for source in (saved.get("cookies") or [], self.account.get("cookies") or []):
            for item in source:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                cookie = dict(item)
                key = (
                    str(cookie.get("name") or ""),
                    str(cookie.get("domain") or cookie.get("url") or ""),
                    str(cookie.get("path") or "/"),
                )
                merged[key] = cookie

        origins = [dict(item) for item in saved.get("origins") or [] if isinstance(item, dict)]
        if not merged and not origins:
            return None
        return {"cookies": list(merged.values()), "origins": origins}

    async def _refresh_cookies(self, context) -> list[dict[str, Any]]:
        account_id = str(self.account.get("id") or "")
        if not account_id:
            return []
        cookies = await context.cookies(["https://www.doubao.com"])
        if cookies:
            update_account_cookies(account_id, cookies)
        try:
            await context.storage_state(path=str(self.state_path), indexed_db=True)
        except Exception:
            await context.storage_state(path=str(self.state_path))
        return [dict(item) for item in cookies if isinstance(item, dict)]

    @staticmethod
    def _cookie_header(cookies: list[dict[str, Any]]) -> str:
        return "; ".join(
            f"{str(item.get('name') or '')}={str(item.get('value') or '')}"
            for item in cookies
            if str(item.get("name") or "")
        )

    async def run(self) -> dict[str, Any]:
        try:
            submit_timeout = 120 * (max(0, min(10, int(self.settings.doubao_submit_retry_limit))) + 1) + 90
            return await asyncio.wait_for(
                self._run_once(),
                timeout=max(self.settings.task_timeout_seconds, 900, submit_timeout),
            )
        except asyncio.TimeoutError:
            submitted = task_exists(self.task_id) and str(get_meta(self.task_id).get("status") or "") == STATUS_SUBMITTED
            if task_exists(self.task_id) and not submitted:
                mark_pending(self.task_id, "doubao browser timeout")
            return {"success": False, "retryable": not submitted, "reason": "doubao browser timeout"}
        except ReferenceUploadCapacityError as exc:
            return {
                "success": False,
                "retryable": True,
                "reason": str(exc),
                "infrastructure_fault": True,
                "defer_only": True,
                "retry_after": exc.retry_after,
                "defer_reason": "参考图上传繁忙，任务已自动排队",
                "defer_category": "image_upload_limit",
            }
        except Exception as exc:
            if all(hasattr(exc, name) for name in ("retry_after", "queue_reason", "queue_category")):
                return {
                    "success": False,
                    "retryable": True,
                    "reason": str(exc),
                    "infrastructure_fault": True,
                    "defer_only": True,
                    "retry_after": max(1, int(exc.retry_after)),
                    "defer_reason": str(exc.queue_reason),
                    "defer_category": str(exc.queue_category),
                }
            reason = str(exc)[:500]
            execution_phase = ""
            if task_exists(self.task_id):
                try:
                    execution_phase = str(get_meta(self.task_id).get("execution_phase") or "")
                except FileNotFoundError:
                    pass
            reference_upload_failure = is_reference_upload_failure(reason) or is_reference_upload_phase(execution_phase)
            if reference_upload_failure and task_exists(self.task_id):
                save_result(self.task_id, extra={
                    "doubao_submit_error_category": "reference_upload",
                    "doubao_reference_upload_error": reason,
                })
            if task_exists(self.task_id):
                mark_pending(self.task_id, reason)
            return {
                "success": False,
                "retryable": True,
                "reason": reason,
                "infrastructure_fault": self.proxy_session is not None or reference_upload_failure,
            }

    async def _run_once(self) -> dict[str, Any]:
        if not task_exists(self.task_id):
            return {"success": True, "retryable": False, "reason": ""}
        clear_transient_result(self.task_id)
        if not self.account:
            return {"success": False, "retryable": True, "reason": "no doubao account available"}
        lock = await account_profile_lock("doubao", str(self.account.get("id") or ""))
        async with lock:
            return await self._run_profile()

    async def _run_profile(self) -> dict[str, Any]:
        proxy_config = None
        proxy_acquired = False
        try:
            self._set_phase("connecting_node", "正在连接豆包生成节点")
            if self.proxy_session is not None:
                proxy_config = await self.proxy_session.acquire_browser_proxy()
                proxy_acquired = True
            if task_exists(self.task_id) and is_task_canceled(self.task_id):
                return {"success": False, "retryable": False, "reason": "用户取消生成"}
            return await self._run_browser(proxy_config)
        except Exception as exc:
            reference_upload_failure = False
            if task_exists(self.task_id):
                try:
                    reference_upload_failure = is_reference_upload_failure(str(exc)) or is_reference_upload_phase(
                        str(get_meta(self.task_id).get("execution_phase") or "")
                    )
                except FileNotFoundError:
                    pass
            if proxy_acquired and not reference_upload_failure and not isinstance(exc, ReferenceUploadCapacityError):
                self.proxy_session.mark_browser_proxy_unavailable(reason="doubao_browser_failure")
            raise
        finally:
            if self.proxy_session is not None:
                await self.proxy_session.release_browser_proxy()

    async def _record_diagnostic(self, page, category: str, body: str = "") -> None:
        if not task_exists(self.task_id):
            return
        try:
            title = str(await page.title())[:300]
        except Exception:
            title = ""
        if not body:
            try:
                body = str(await page.locator("body").inner_text())
            except Exception:
                body = ""
        excerpt = re.sub(r"\s+", " ", body).strip()[:1200]
        save_result(
            self.task_id,
            extra={
                "doubao_diagnostic_category": category,
                "doubao_page_url": str(page.url or "")[:1000],
                "doubao_page_title": title,
                "doubao_page_excerpt": excerpt,
            },
        )

    def _set_phase(self, phase: str, status_reason: str) -> None:
        if task_exists(self.task_id):
            set_execution_phase(self.task_id, phase, status_reason)

    @staticmethod
    def _response_video_url(response) -> str:
        try:
            url = str(response.url or "")
            content_type = str(response.headers.get("content-type") or "").lower()
        except Exception:
            return ""
        if url.startswith("http") and (
            "video/" in content_type
            or "mime_type=video_mp4" in url.lower()
            or ".mp4" in url.lower()
        ):
            return url
        return ""

    async def _capture_video_candidates(self, response, candidates: dict[str, dict[str, Any]]) -> None:
        try:
            request = response.request
            if request.resource_type not in {"xhr", "fetch"} or int(response.status or 0) >= 400:
                return
            response_url = str(response.url or "")
            lowered_url = response_url.lower()
            if not any(marker in lowered_url for marker in ("/chat", "/im/chain/", "conversation", "message", "video", "aigc", "generate", "task", "completion")):
                return
            content_length = int(response.headers.get("content-length") or 0)
            if content_length > 2 * 1024 * 1024:
                return
            body = await response.text()
        except Exception:
            return
        body = str(body or "")[:2 * 1024 * 1024]
        collect_doubao_response_candidates(body, candidates)

    @staticmethod
    async def _fetch_single_chain_candidates(
        page: Page,
        conversation_id: str,
        candidates: dict[str, dict[str, Any]],
    ) -> str:
        try:
            result = await page.evaluate(
                DOUBAO_SINGLE_CHAIN_SCRIPT,
                {"conversationId": conversation_id},
            )
        except Exception as exc:
            return f"single chain request failed: {str(exc)[:300]}"
        if not isinstance(result, dict):
            return "single chain returned an invalid response"
        if not result.get("ok"):
            return f"single chain http {int(result.get('status') or 0)}"
        body = str(result.get("body") or "")
        try:
            payload = json.loads(body.lstrip("\ufeff"))
        except (TypeError, ValueError):
            return "single chain returned invalid JSON"
        original_url = decode_main_url(extract_main_url(payload))
        if original_url:
            add_doubao_video_candidate(
                candidates,
                original_url,
                key="video_model.main_url",
                source="single_chain",
            )
        return ""

    @staticmethod
    async def _page_video_url(page, captured_urls: list[str]) -> tuple[str, str]:
        for url in reversed(captured_urls):
            if str(url).startswith("http"):
                return str(url), "media_response"
        video_urls = await page.locator("video").evaluate_all(
            """elements => elements.flatMap(element => [
                element.currentSrc || "",
                element.getAttribute("src") || "",
                ...Array.from(element.querySelectorAll("source")).map(source => source.src || source.getAttribute("src") || "")
            ]).filter(Boolean)"""
        )
        for url in video_urls:
            if str(url).startswith("http"):
                return str(url), "video_current_src"
        source_urls = await page.locator("source").evaluate_all(
            "elements => elements.map(element => element.src || element.getAttribute('src') || '').filter(Boolean)"
        )
        for url in source_urls:
            if str(url).startswith("http"):
                return str(url), "source_src"
        links = await page.locator('a[href*="video"],a[href$=".mp4"],a[download]').evaluate_all(
            "elements => elements.map(element => element.href).filter(Boolean)"
        )
        for url in links:
            if str(url).startswith("http"):
                return str(url), "download_link"
        return "", ""

    @staticmethod
    async def _activate_completed_video(page) -> bool:
        posters = page.locator('img[src*="video_dsz_watermark"]')
        if not await posters.count():
            return False
        wrappers = page.locator('div[class*="video-player-wrapper"]')
        if await wrappers.count():
            await wrappers.last.click(force=True, timeout=5000)
        else:
            await posters.last.click(force=True, timeout=5000)
        await page.wait_for_timeout(1500)
        return True

    async def _save_video_success(
        self,
        context,
        page,
        url: str,
        source: str,
        *,
        score: int = 0,
        candidate_key: str = "",
        candidate_count: int = 1,
    ) -> dict[str, Any]:
        candidate = {"url": url, "source": source, "key": candidate_key, "score": int(score)}
        if not doubao_video_candidate_is_acceptable(candidate):
            save_result(
                self.task_id,
                extra={
                    "doubao_rejected_fallback_source": source,
                    "doubao_rejected_fallback_key": candidate_key,
                    "doubao_rejected_fallback_score": int(score),
                },
            )
            return {"success": False, "retryable": False, "reason": "doubao unwatermarked result unavailable"}
        result_source = source if source in DOUBAO_UNWATERMARKED_RESULT_SOURCES else "network_explicit_unwatermarked"
        await self._refresh_cookies(context)
        save_result(
            self.task_id,
            extra={
                "decoded_main_url": url,
                "doubao_page_url": page.url,
                "doubao_video_detection_source": result_source,
                "doubao_result_source": result_source,
                "doubao_selected_video_key": candidate_key,
                "doubao_video_url_score": int(score),
                "doubao_video_candidate_count": max(1, int(candidate_count)),
                "doubao_watermark_status": "original",
            },
            remove={"cookie_string", "conversation_id"},
        )
        mark_success(self.task_id)
        return {"success": True, "retryable": False, "reason": ""}

    async def _observe_service_frequent(
        self,
        page: Page,
        *,
        seconds: float = 15.0,
    ) -> str:
        self._set_phase("checking_account_risk", "正在确认豆包账号登录状态")
        deadline = asyncio.get_running_loop().time() + max(0.0, seconds)
        while True:
            try:
                body = await page.locator("body").inner_text()
                if await self._login_required(page, body):
                    save_result(self.task_id, extra={"doubao_service_frequent_state": "login_invalid"})
                    return "login_invalid"
            except Exception as exc:
                save_result(self.task_id, extra={"doubao_service_frequent_check_error": str(exc)[:300]})

            if asyncio.get_running_loop().time() >= deadline:
                save_result(self.task_id, extra={"doubao_service_frequent_state": "service_frequent"})
                return "service_frequent"
            await page.wait_for_timeout(500)

    @staticmethod
    def _is_region_restricted(page_url: str, body: str) -> bool:
        lowered_url = str(page_url or "").lower()
        if "doubao-region-ban" in lowered_url:
            return True
        excerpt = str(body or "")[:3000]
        compact_excerpt = re.sub(r"\s+", "", excerpt)
        if all(marker in compact_excerpt for marker in ("受区域限制", "请先登录", "使用豆包")):
            return True
        normal_shell_markers = ("新对话", "AI 创作", "历史对话", "视频生成")
        if any(marker in excerpt for marker in normal_shell_markers):
            return False
        return any(marker.lower() in excerpt.lower() for marker in REGION_RESTRICTION_MARKERS[1:])

    @staticmethod
    async def _login_required(page, body: str) -> bool:
        user_visible = await page.get_by_text(re.compile(r"^用户\d+$")).count()
        if user_visible:
            return False
        login_button = page.get_by_role("button", name=re.compile(r"^登录(?:豆包)?$"))
        login_link = page.get_by_role("link", name=re.compile(r"^登录(?:豆包)?$"))
        if await login_button.count() or await login_link.count():
            return True
        return any(marker in body[:2000] for marker in ("扫码登录", "手机号登录", "登录豆包"))

    async def _submit_via_video_creation_internal_request(self, page: Page, *, image_count: int = 0) -> dict[str, Any]:
        self._set_phase("opening_video_creation_page", "正在建立豆包视频生成会话")
        await page.goto(DOUBAO_VIDEO_CREATION_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(3000)
        body = await page.locator("body").inner_text()
        if self._is_region_restricted(str(page.url), body):
            return {"ok": False, "status": 0, "accepted": False, "region_restricted": True}
        if await self._login_required(page, body):
            return {"ok": False, "status": 0, "accepted": False, "login_invalid": True}
        if self.model not in DOUBAO_MODEL_CODES:
            return {
                "ok": False,
                "status": 0,
                "accepted": False,
                "video_creation_ui_error": f"doubao video model unavailable: {self.model}",
            }
        ratio = re.sub(r"\s+", "", str(self.ratio or "auto")).replace("：", ":").lower()
        if ratio not in DOUBAO_VIDEO_RATIOS:
            return {
                "ok": False,
                "status": 0,
                "accepted": False,
                "video_creation_ui_error": f"doubao video ratio unavailable: {ratio}",
            }

        attachments: list[dict[str, Any]] = []
        if image_count > 0:
            self._set_phase("uploading_video_creation_references", "正在通过豆包视频接口上传参考图")
            uploader = DoubaoReferenceImageUploader(
                self.task_id,
                self.prompt,
                ratio,
                self.duration,
                account=self.account,
                image_upload_slot=self.image_upload_slot,
                proxy_platform="doubao",
            )
            proxy_session = getattr(self, "proxy_session", None)
            if proxy_session is not None:
                uploader.active_proxy_source = str(getattr(proxy_session, "active_proxy_source", "") or "")
                uploader.active_proxy_server = str(getattr(proxy_session, "active_proxy_server", "") or "")
                uploader.proxy_node_id = str(getattr(proxy_session, "proxy_node_id", "") or "")
            attachments = await uploader._upload_images_if_needed(page)
            if len(attachments) < image_count:
                raise RuntimeError("doubao video reference upload did not return every attachment")
            prehandled = await page.evaluate(DOUBAO_PREHANDLE_ATTACHMENTS_SCRIPT, {"attachments": attachments})
            if not isinstance(prehandled, dict) or not prehandled.get("ok"):
                raise RuntimeError(
                    f"doubao video reference pre-handle failed: {str((prehandled or {}).get('error') if isinstance(prehandled, dict) else prehandled)[:500]}"
                )
            attachments = [dict(item) for item in prehandled.get("attachments") or [] if isinstance(item, dict)]
            if len(attachments) < image_count:
                raise RuntimeError("doubao video reference pre-handle returned incomplete attachments")

        if self.submission_pacer is not None:
            await self.submission_pacer()
        self._set_phase("submitting_video_creation_request", "正在通过豆包视频接口提交任务")
        result = await page.evaluate(
            DOUBAO_SUBMIT_SCRIPT,
            {
                "prompt": self.prompt,
                "ratio": ratio,
                "model": DOUBAO_MODEL_CODES[self.model],
                "duration": self.duration,
                "attachments": attachments,
                "retryLimit": max(0, min(10, int(self.settings.doubao_submit_retry_limit))),
                "retryDelayMs": 15000,
                "freshConversationRetryUsed": False,
            },
        )
        if not isinstance(result, dict):
            return {"ok": False, "status": 0, "accepted": False, "video_creation_ui_error": "doubao video request returned invalid response"}
        result = normalize_doubao_submission_acknowledgement(result)
        if not result.get("accepted") and is_doubao_text_only_video_response(
            "\n".join(str(result.get(key) or "") for key in ("initial_response_preview", "response_preview"))
        ):
            result["text_only_response"] = True
        result.update(
            video_creation_page_used=True,
            video_creation_selected_model=self.model,
            video_creation_selected_ratio=ratio,
            video_creation_selected_duration=self.duration,
            submitted_with_images=bool(attachments),
            generation_ack_source=str(result.get("generation_ack_source") or "video_creation_internal_request"),
        )
        return result

    async def _submit_via_video_creation_page_ui(self, page: Page, *, image_count: int = 0) -> dict[str, Any]:
        self._set_phase("opening_video_creation_page", "正在进入豆包新对话")
        await page.goto(DOUBAO_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(3000)
        body = await page.locator("body").inner_text()
        if self._is_region_restricted(str(page.url), body):
            return {"ok": False, "status": 0, "accepted": False, "region_restricted": True}
        if await self._login_required(page, body):
            return {"ok": False, "status": 0, "accepted": False, "login_invalid": True}

        async def click_visible(candidates: tuple[Any, ...], *, timeout: int = 0) -> bool:
            deadline = asyncio.get_running_loop().time() + max(0, timeout) / 1000
            while True:
                for candidate in candidates:
                    try:
                        count = await candidate.count()
                        for index in range(count - 1, -1, -1):
                            item = candidate.nth(index)
                            if await item.is_visible():
                                await item.click(force=True)
                                return True
                    except Exception:
                        continue
                if asyncio.get_running_loop().time() >= deadline:
                    return False
                await page.wait_for_timeout(250)

        async def click_exact_visible_text(label: str) -> bool:
            return bool(await page.evaluate(
                r"""label => {
                    const normalize = value => String(value || "")
                        .replace(/\s+/g, "")
                        .replace(/：/g, ":");
                    const expected = normalize(label);
                    const candidates = Array.from(document.querySelectorAll("body *"))
                        .filter(element => normalize(element.textContent) === expected)
                        .filter(element => {
                            const style = getComputedStyle(element);
                            const box = element.getBoundingClientRect();
                            return style.visibility !== "hidden" && style.display !== "none"
                                && box.width > 0 && box.height > 0;
                        })
                        .sort((left, right) => {
                            const a = left.getBoundingClientRect();
                            const b = right.getBoundingClientRect();
                            return a.width * a.height - b.width * b.height;
                        });
                    const element = candidates[0];
                    if (!element) return false;
                    const target = element.closest("button,[role=button],[role=tab],[role=option],[role=menuitem],a") || element;
                    target.click();
                    return true;
                }""",
                label,
            ))

        await click_visible(
            (
                page.get_by_role("button", name="新对话", exact=True),
                page.get_by_role("link", name="新对话", exact=True),
                page.get_by_text("新对话", exact=True),
            ),
            timeout=5000,
        )
        await page.wait_for_timeout(1200)

        self._set_phase("opening_video_creation_page", "正在打开豆包视频生成")
        direct_video_entry = await click_visible(
            (
                page.get_by_role("button", name="视频生成", exact=True),
                page.get_by_role("tab", name="视频生成", exact=True),
                page.get_by_text("视频生成", exact=True),
            ),
            timeout=8000,
        )
        if not direct_video_entry:
            direct_video_entry = await click_exact_visible_text("视频生成")
        if direct_video_entry:
            await page.wait_for_timeout(1500)

        async def visible_model_button(timeout: int):
            selector = page.locator("button:visible").filter(
                has_text=re.compile(r"Seedance\s+\d", re.IGNORECASE)
            ).last
            try:
                await selector.wait_for(state="visible", timeout=timeout)
                return selector
            except Exception:
                return None

        model_button = await visible_model_button(15000) if direct_video_entry else None
        if model_button is None:
            ai_creation_opened = await click_visible(
                (
                    page.get_by_role("button", name="AI 创作", exact=True),
                    page.get_by_role("link", name="AI 创作", exact=True),
                    page.get_by_text("AI 创作", exact=True),
                ),
                timeout=5000,
            )
            if ai_creation_opened:
                await page.wait_for_timeout(1500)
                video_tab_opened = await click_visible(
                    (
                        page.get_by_role("tab", name="视频", exact=True),
                        page.get_by_role("button", name="视频", exact=True),
                        page.get_by_text("视频", exact=True),
                    ),
                    timeout=8000,
                )
                if video_tab_opened:
                    await page.wait_for_timeout(1500)
                    model_button = await visible_model_button(20000)
        if model_button is None:
            await page.goto(DOUBAO_VIDEO_CREATION_URL, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(3000)
            video_tab_opened = await click_visible(
                (
                    page.get_by_role("tab", name="视频", exact=True),
                    page.get_by_role("button", name="视频", exact=True),
                    page.get_by_text("视频", exact=True),
                ),
                timeout=10000,
            )
            if not video_tab_opened:
                video_tab_opened = await click_exact_visible_text("视频")
            if video_tab_opened:
                await page.wait_for_timeout(1500)
                model_button = await visible_model_button(30000)
        if model_button is None:
            return {
                "ok": False,
                "status": 0,
                "accepted": False,
                "video_creation_ui_error": "doubao video creation entry unavailable",
            }
        current_model_text = re.sub(r"\s+", " ", (await model_button.inner_text()).strip())
        selected_model = self.model if self.model in current_model_text else ""
        if not selected_model:
            await model_button.click(force=True)
            await page.wait_for_timeout(500)
            model_options = (
                page.get_by_role("menuitem", name=self.model, exact=True),
                page.get_by_role("option", name=self.model, exact=True),
                page.get_by_role("button", name=self.model, exact=True),
                page.get_by_text(self.model, exact=True),
            )
            option_clicked = False
            for options_locator in model_options:
                for index in range(await options_locator.count() - 1, -1, -1):
                    item = options_locator.nth(index)
                    try:
                        if not await item.is_visible():
                            continue
                        label = re.sub(r"\s+", " ", (await item.inner_text()).strip())
                        if label != self.model and not label.startswith(f"{self.model} "):
                            continue
                        await item.click(force=True)
                        option_clicked = True
                        break
                    except Exception:
                        continue
                if option_clicked:
                    break
            if not option_clicked:
                await page.keyboard.press("Escape")
                return {
                    "ok": False,
                    "status": 0,
                    "accepted": False,
                    "video_creation_ui_error": f"doubao video model unavailable: {self.model}",
                }
            confirmation_deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < confirmation_deadline:
                await page.wait_for_timeout(250)
                confirmed_button = await visible_model_button(1000)
                if confirmed_button is None:
                    continue
                confirmed_text = re.sub(r"\s+", " ", (await confirmed_button.inner_text()).strip())
                if self.model in confirmed_text:
                    selected_model = self.model
                    model_button = confirmed_button
                    break
            if not selected_model:
                return {
                    "ok": False,
                    "status": 0,
                    "accepted": False,
                    "video_creation_ui_error": f"doubao video model selection mismatch: {self.model}",
                }

        ratio_label = "自动" if not self.ratio or self.ratio == "auto" else self.ratio
        duration_label = f"{self.duration}s"
        active_editor = None

        async def video_settings_button():
            selector = page.locator("button:visible").filter(
                has_text=re.compile(r"(?:自动|\d+:\d+)\s*[·•]\s*\d+s", re.IGNORECASE)
            )
            try:
                await selector.last.wait_for(state="visible", timeout=10000)
            except Exception as exc:
                raise RuntimeError("doubao video ratio and duration selector unavailable") from exc
            editor_box = None
            if active_editor is not None:
                try:
                    if await active_editor.is_visible():
                        editor_box = await active_editor.bounding_box()
                except Exception:
                    editor_box = None
            if editor_box is None:
                editors = page.locator('[contenteditable="true"][role="textbox"]:visible,textarea:visible')
                for index in range(await editors.count() - 1, -1, -1):
                    candidate = editors.nth(index)
                    try:
                        if not await candidate.is_visible():
                            continue
                        editor_box = await candidate.bounding_box()
                    except Exception:
                        continue
                    if editor_box:
                        break
            if editor_box:
                positioned: list[tuple[float, Any]] = []
                editor_center_x = editor_box["x"] + editor_box["width"] / 2
                editor_center_y = editor_box["y"] + editor_box["height"] / 2
                for index in range(await selector.count() - 1, -1, -1):
                    candidate = selector.nth(index)
                    try:
                        if not await candidate.is_visible():
                            continue
                        box = await candidate.bounding_box()
                    except Exception:
                        continue
                    if not box:
                        continue
                    center_x = box["x"] + box["width"] / 2
                    center_y = box["y"] + box["height"] / 2
                    near_editor = (
                        editor_box["x"] - 40 <= center_x <= editor_box["x"] + editor_box["width"] + 40
                        and editor_box["y"] - 100 <= center_y <= editor_box["y"] + editor_box["height"] + 40
                    )
                    if near_editor:
                        distance = abs(center_x - editor_center_x) + abs(center_y - editor_center_y)
                        positioned.append((distance, candidate))
                if positioned:
                    return min(positioned, key=lambda item: item[0])[1]
            return selector.last

        async def select_video_setting(label: str) -> None:
            selector = await video_settings_button()
            await selector.scroll_into_view_if_needed()
            selector_box = await selector.bounding_box()
            current_setting = re.sub(r"\s+", "", str(await selector.inner_text() or "")).replace("：", ":").lower()
            expected_setting = re.sub(r"\s+", "", label).replace("：", ":").lower()
            if expected_setting and expected_setting in current_setting:
                return

            async def settings_panel_open() -> bool:
                option_pattern = re.compile(r"(?:^|\D)(?:3\s*[:：]\s*4|4\s*[:：]\s*3|9\s*[:：]\s*16|16\s*[:：]\s*9|1\s*[:：]\s*1|21\s*[:：]\s*9)(?:\D|$)")
                options = page.locator(
                    '[role="dialog"]:visible button, [role="dialog"]:visible [role="option"], '
                    '[role="menu"]:visible button, [role="menu"]:visible [role="menuitem"], '
                    '[data-radix-popper-content-wrapper]:visible button, '
                    'button:visible, [role="option"]:visible, [role="menuitem"]:visible'
                ).filter(has_text=option_pattern)
                return await options.count() >= 2

            panel_open = False
            try:
                await selector.click(timeout=5000)
            except Exception:
                await selector.click(force=True)
            await page.wait_for_timeout(500)
            panel_open = await settings_panel_open()
            if not panel_open:
                await selector.focus()
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(500)
                panel_open = await settings_panel_open()
            if not panel_open:
                await selector.evaluate("element => element.click()")
                await page.wait_for_timeout(500)
                panel_open = await settings_panel_open()
            selected = False
            option_deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < option_deadline and not selected:
                ratio_match = re.fullmatch(r"(\d+)\s*[:：]\s*(\d+)", label)
                option_pattern = re.compile(
                    rf"(?:^|\D){ratio_match.group(1)}\s*[:：]\s*{ratio_match.group(2)}(?:\D|$)"
                    if ratio_match else rf"(?:^|\D){re.escape(label)}(?:\D|$)",
                    re.IGNORECASE,
                )
                options = (
                    page.locator(
                        '[role="dialog"]:visible button, [role="dialog"]:visible [role="option"], '
                        '[role="menu"]:visible button, [role="menu"]:visible [role="menuitem"], '
                        '[data-radix-popper-content-wrapper]:visible button'
                    ).filter(has_text=option_pattern),
                    page.get_by_role("button", name=option_pattern),
                    page.get_by_role("option", name=option_pattern),
                    page.get_by_role("menuitem", name=option_pattern),
                    page.get_by_text(option_pattern),
                )
                for options_locator in options:
                    for index in range(await options_locator.count() - 1, -1, -1):
                        option = options_locator.nth(index)
                        try:
                            if not await option.is_visible():
                                continue
                            option_box = await option.bounding_box()
                            if selector_box and option_box:
                                option_center_x = option_box["x"] + option_box["width"] / 2
                                option_center_y = option_box["y"] + option_box["height"] / 2
                                in_current_panel = (
                                    selector_box["x"] - 300 <= option_center_x <= selector_box["x"] + selector_box["width"] + 300
                                    and selector_box["y"] - 350 <= option_center_y <= selector_box["y"] + selector_box["height"] + 200
                                )
                                if not in_current_panel:
                                    continue
                            interactive = option.locator(
                                "xpath=ancestor-or-self::*[self::button or @role='button' or @role='option' or @role='menuitem' or self::div][1]"
                            )
                            target = interactive.first if await interactive.count() else option
                            await target.click(force=True)
                            selected = True
                            break
                        except Exception:
                            continue
                    if selected:
                        break
                if not selected:
                    await page.wait_for_timeout(250)
            if not selected and selector_box is None:
                selected = await click_exact_visible_text(label)
            if not selected:
                reason = "doubao video settings panel unavailable" if not panel_open else f"doubao video setting unavailable: {label}"
                raise RuntimeError(reason)
            await page.wait_for_timeout(500)

        try:
            settings_button = await video_settings_button()
        except RuntimeError as exc:
            return {
                "ok": False,
                "status": 0,
                "accepted": False,
                "video_creation_ui_error": str(exc),
            }

        submitted_with_images = False
        if image_count > 0:
            paths = await asyncio.to_thread(prepare_task_reference_images, self.task_id)
            file_input = page.locator('input[type="file"]')
            if paths and not await file_input.count():
                await click_visible(
                    (
                        page.get_by_role("button", name=re.compile(r"^(?:添加|上传|参考图|\+)$")),
                        page.locator('button:visible[aria-label*="上传"]'),
                        page.locator('button:visible[aria-label*="添加"]'),
                    ),
                    timeout=3000,
                )
                await page.wait_for_timeout(500)
                file_input = page.locator('input[type="file"]')
            if not paths or not await file_input.count():
                return {
                    "ok": False,
                    "status": 0,
                    "accepted": False,
                    "video_creation_ui_error": "doubao video reference input unavailable",
                }
            self._set_phase("uploading_video_creation_references", "正在向豆包视频创作页添加参考图")
            async def upload_references() -> str:
                visible_images_before = await page.locator("img:visible").count()
                await file_input.last.set_input_files([str(path) for path in paths])
                upload_deadline = asyncio.get_running_loop().time() + 90
                stable_checks = 0
                while asyncio.get_running_loop().time() < upload_deadline:
                    await page.wait_for_timeout(500)
                    upload_body = await page.locator("body").inner_text()
                    upload_tail = re.sub(r"\s+", "", upload_body[-3000:])
                    if any(marker in upload_tail for marker in ("上传失败", "图片上传失败", "参考图上传失败")):
                        return "failed"
                    visible_images = await page.locator("img:visible").count()
                    progress_visible = doubao_reference_upload_progress_visible(upload_tail)
                    preview_ready = visible_images >= visible_images_before + min(len(paths), image_count)
                    if not progress_visible and preview_ready:
                        stable_checks += 1
                        if stable_checks >= DOUBAO_REFERENCE_UPLOAD_STABLE_CHECKS:
                            return "completed"
                    else:
                        stable_checks = 0
                return "timeout"

            image_upload_slot = getattr(self, "image_upload_slot", None)
            if image_upload_slot is not None:
                async with image_upload_slot():
                    upload_state = await upload_references()
            else:
                upload_state = await upload_references()
            if upload_state == "failed":
                return {
                    "ok": False,
                    "status": 0,
                    "accepted": False,
                    "video_creation_ui_error": "doubao video reference upload failed",
                }
            if upload_state != "completed":
                return {
                    "ok": False,
                    "status": 0,
                    "accepted": False,
                    "video_creation_ui_error": "doubao video reference upload completion missing",
                }
            submitted_with_images = True

        evidence: dict[str, Any] = {"conversation_id": "", "response_preview": "", "network_marker": False}
        submission_request_seen = asyncio.Event()
        capture_tasks: set[asyncio.Task[Any]] = set()
        capture_enabled = False

        def remember_evidence(value: Any, *, force_preview: bool = False) -> None:
            text = str(value or "")
            conversation_id = extract_doubao_conversation_id(text)
            if conversation_id:
                evidence["conversation_id"] = conversation_id
            if DOUBAO_SUBMISSION_MARKER in text:
                evidence["network_marker"] = True
            if text and (force_preview or any(marker in text for marker in (DOUBAO_SUBMISSION_MARKER, "conversation_id", "conversationId"))):
                evidence["response_preview"] = text[-6000:]

        async def capture_submission_response(response) -> None:
            if not capture_enabled:
                return
            try:
                request = response.request
                if request.resource_type not in {"xhr", "fetch"}:
                    return
                if urlsplit(str(response.url or "")).path != "/chat/completion":
                    return
                remember_evidence(request.post_data or "")
                remember_evidence(response.url)
                content_length = int(response.headers.get("content-length") or 0)
                if content_length > 2 * 1024 * 1024:
                    return
                response_body = await response.text()
            except Exception:
                return
            remember_evidence(response_body[:2 * 1024 * 1024], force_preview=True)

        def capture_submission(response) -> None:
            create_tracked_task(capture_tasks, capture_submission_response(response))

        def capture_submission_request(request) -> None:
            if not capture_enabled or str(request.method or "").upper() != "POST":
                return
            if urlsplit(str(request.url or "")).path != "/chat/completion":
                return
            remember_evidence(request.post_data or "")
            submission_request_seen.set()

        page.on("response", capture_submission)
        page.on("request", capture_submission_request)
        try:
            editors = page.locator('[contenteditable="true"][role="textbox"]:visible,textarea:visible')
            editor = None
            fallback_editors: list[tuple[float, Any]] = []
            settings_box = await settings_button.bounding_box()
            for index in range(await editors.count() - 1, -1, -1):
                candidate = editors.nth(index)
                if not await candidate.is_visible():
                    continue
                attributes_list: list[str] = []
                for name in ("placeholder", "data-placeholder", "aria-label"):
                    attributes_list.append(str(await candidate.get_attribute(name) or ""))
                attributes = " ".join(attributes_list)
                if "视频" in attributes:
                    editor = candidate
                    break
                box = await candidate.bounding_box()
                if box and settings_box:
                    distance = abs((box["y"] + box["height"]) - settings_box["y"])
                    fallback_editors.append((distance, candidate))
            if editor is None and fallback_editors:
                editor = min(fallback_editors, key=lambda item: item[0])[1]
            if editor is None:
                return {
                    "ok": False,
                    "status": 0,
                    "accepted": False,
                    "video_creation_ui_error": "doubao video prompt input unavailable",
                }
            active_editor = editor
            self._set_phase("submitting_video_creation_page", "正在通过豆包视频创作页提交任务")
            await editor.fill(self.prompt)
            await page.wait_for_timeout(300)

            try:
                await select_video_setting(ratio_label)
                settings_button = await video_settings_button()
                selected_settings = re.sub(r"\s+", "", (await settings_button.inner_text()).strip()).replace("：", ":")
                if duration_label not in selected_settings:
                    await select_video_setting(duration_label)
                    settings_button = await video_settings_button()
                    selected_settings = re.sub(r"\s+", "", (await settings_button.inner_text()).strip()).replace("：", ":")
            except RuntimeError as exc:
                return {
                    "ok": False,
                    "status": 0,
                    "accepted": False,
                    "video_creation_ui_error": str(exc),
                }
            if ratio_label not in selected_settings or duration_label not in selected_settings:
                return {
                    "ok": False,
                    "status": 0,
                    "accepted": False,
                    "video_creation_ui_error": f"doubao video settings mismatch: {selected_settings}",
                }

            async def find_send_button():
                named = (
                    page.get_by_role("button", name=re.compile(r"^(?:发送|提交|生成)$")),
                    page.locator('button:visible[aria-label*="发送"]'),
                    page.locator('button:visible[title*="发送"]'),
                )
                for candidate in named:
                    count = await candidate.count()
                    for index in range(count - 1, -1, -1):
                        item = candidate.nth(index)
                        if await item.is_visible() and await item.is_enabled():
                            return item
                editor_box = await editor.bounding_box()
                if not editor_box:
                    return None
                positioned: list[tuple[float, Any]] = []
                buttons = page.locator("button:visible")
                for index in range(await buttons.count() - 1, -1, -1):
                    item = buttons.nth(index)
                    try:
                        if not await item.is_enabled():
                            continue
                        box = await item.bounding_box()
                    except Exception:
                        continue
                    if not box:
                        continue
                    center_x = box["x"] + box["width"] / 2
                    center_y = box["y"] + box["height"] / 2
                    if (
                        center_x >= editor_box["x"] + editor_box["width"] * 0.75
                        and editor_box["y"] - 20 <= center_y <= editor_box["y"] + editor_box["height"] + 80
                    ):
                        positioned.append((-center_x, item))
                return min(positioned, key=lambda item: item[0])[1] if positioned else None

            send_button = await find_send_button()
            if send_button is None:
                return {
                    "ok": False,
                    "status": 0,
                    "accepted": False,
                    "video_creation_ui_error": "doubao video send button unavailable",
                }
            baseline_body = await page.locator("body").inner_text()
            baseline_ack_count = baseline_body.count(DOUBAO_SUBMISSION_MARKER)
            if self.submission_pacer is not None:
                await self.submission_pacer()
            capture_enabled = True
            await send_button.click(force=True)
            try:
                await asyncio.wait_for(submission_request_seen.wait(), timeout=3)
            except asyncio.TimeoutError:
                await editor.press("Enter")
                try:
                    await asyncio.wait_for(submission_request_seen.wait(), timeout=3)
                except asyncio.TimeoutError:
                    return {
                        "ok": True,
                        "status": 200,
                        "accepted": False,
                        "response_preview": str(evidence.get("response_preview") or ""),
                        "video_creation_page_used": True,
                        "submitted_with_images": submitted_with_images,
                        "native_submission_request_seen": False,
                    }

            deadline = asyncio.get_running_loop().time() + DOUBAO_VIDEO_CREATION_SUBMIT_WAIT_SECONDS
            marker_visible = False
            acknowledgement_visible = False
            last_body = ""
            while asyncio.get_running_loop().time() < deadline:
                await page.wait_for_timeout(500)
                last_body = await page.locator("body").inner_text()
                if self._is_region_restricted(str(page.url), last_body):
                    return {"ok": False, "status": 0, "accepted": False, "region_restricted": True}
                if await self._login_required(page, last_body):
                    return {"ok": False, "status": 0, "accepted": False, "login_invalid": True}
                if "710022002" in last_body or "当前服务访问频繁" in last_body or "服务访问频繁" in last_body:
                    return {"ok": True, "status": 200, "accepted": False, "service_frequent": True}
                if is_doubao_account_quota_insufficient(last_body[-2500:]):
                    return {"ok": True, "status": 200, "accepted": False, "quota_insufficient": True}
                network_preview = str(evidence.get("response_preview") or "")
                if "710022004" in network_preview:
                    return {
                        "ok": True,
                        "status": 200,
                        "accepted": False,
                        "slider_verification": True,
                        "response_preview": network_preview,
                        "native_submission_request_seen": submission_request_seen.is_set(),
                    }
                if "710022002" in network_preview:
                    return {
                        "ok": True,
                        "status": 200,
                        "accepted": False,
                        "service_frequent": True,
                        "response_preview": network_preview,
                        "native_submission_request_seen": submission_request_seen.is_set(),
                    }
                if "event: STREAM_ERROR" in network_preview:
                    return {
                        "ok": True,
                        "status": 200,
                        "accepted": False,
                        "stream_error": True,
                        "response_preview": network_preview,
                        "native_submission_request_seen": submission_request_seen.is_set(),
                    }
                marker_visible = last_body.count(DOUBAO_SUBMISSION_MARKER) > baseline_ack_count
                acknowledgement_visible = marker_visible and doubao_ui_generation_acknowledged(last_body, self.model)
                remember_evidence(page.url)
                if acknowledgement_visible and evidence.get("conversation_id"):
                    break

            conversation_id = str(evidence.get("conversation_id") or "")
            if not acknowledgement_visible:
                response_preview = last_body[-6000:]
                return {
                    "ok": True,
                    "status": 200,
                    "accepted": False,
                    "response_preview": response_preview,
                    "text_only_response": is_doubao_text_only_video_response(response_preview),
                    "video_creation_page_used": True,
                    "submitted_with_images": submitted_with_images,
                    "generation_wait_message_detected": marker_visible,
                    "generation_wait_message": DOUBAO_SUBMISSION_MARKER if marker_visible else "",
                    "native_submission_request_seen": submission_request_seen.is_set(),
                }
            if not conversation_id:
                return {
                    "ok": False,
                    "status": 0,
                    "accepted": False,
                    "video_creation_ui_error": "doubao video creation conversation id missing",
                    "generation_wait_message_detected": True,
                    "generation_wait_message": DOUBAO_SUBMISSION_MARKER,
                    "native_submission_request_seen": submission_request_seen.is_set(),
                }
            return {
                "ok": True,
                "status": 200,
                "accepted": True,
                "conversation_id": conversation_id,
                "generation_wait_message_detected": True,
                "generation_wait_message": DOUBAO_SUBMISSION_MARKER,
                "generation_ack_source": "video_creation_page_dom",
                "response_preview": str(evidence.get("response_preview") or last_body[-6000:]),
                "video_creation_page_used": True,
                "video_creation_selected_model": selected_model,
                "video_creation_selected_ratio": ratio_label,
                "video_creation_selected_duration": self.duration,
                "submitted_with_images": submitted_with_images,
                "native_submission_request_seen": submission_request_seen.is_set(),
            }
        finally:
            try:
                page.remove_listener("response", capture_submission)
            except Exception:
                pass
            try:
                page.remove_listener("request", capture_submission_request)
            except Exception:
                pass
            await cancel_tracked_tasks(capture_tasks)

    async def _resolve_submission_verification(self, page: Page) -> SliderSolveResult:
        self._set_phase("waiting_doubao_verification", "正在等待豆包人机验证")
        deadline = asyncio.get_running_loop().time() + DOUBAO_VERIFICATION_RENDER_WAIT_SECONDS
        target_page: Page | None = None
        while asyncio.get_running_loop().time() < deadline:
            target_page = await find_slider_page(
                [page.context],
                self.verification_solver.settings.iframe_selector,
            )
            if target_page is not None:
                break
            await page.wait_for_timeout(250)

        if target_page is None:
            iframe_sources: list[str] = []
            for candidate_page in page.context.pages:
                try:
                    frames = candidate_page.locator("iframe")
                    for index in range(min(await frames.count(), 20)):
                        source = str(await frames.nth(index).get_attribute("src") or "").strip()
                        if source:
                            iframe_sources.append(source[:300])
                except Exception:
                    continue
            if task_exists(self.task_id):
                save_result(self.task_id, extra={
                    "doubao_verification_solver_status": "not_present",
                    "doubao_verification_solver_attempts": 0,
                    "doubao_verification_iframe_sources": iframe_sources[:10],
                })
            return SliderSolveResult(status="not_present", attempts=0)

        self._set_phase("resolving_doubao_verification", "正在完成豆包人机验证")
        result = await self.verification_solver.solve(target_page)
        if task_exists(self.task_id):
            save_result(self.task_id, extra={
                "doubao_verification_solver_status": result.status,
                "doubao_verification_solver_attempts": result.attempts,
                "doubao_verification_solver_confidence": (
                    round(result.confidence, 4) if result.confidence is not None else None
                ),
                "doubao_verification_solver_error": str(result.error or "")[:300],
            })
        if result.status == "success":
            await page.wait_for_timeout(1200)
        return result

    async def _submit_via_video_creation_page(self, page: Page, *, image_count: int = 0) -> dict[str, Any]:
        result = await self._submit_via_video_creation_page_ui(page, image_count=image_count)
        if result.get("slider_verification"):
            verification_result = await self._resolve_submission_verification(page)
            result["verification_solver_status"] = verification_result.status
            result["verification_solver_attempts"] = verification_result.attempts
            if verification_result.status == "success":
                await self._refresh_cookies(page.context)
                retried = await self._submit_via_video_creation_page_ui(page, image_count=image_count)
                retried["verification_recovery_attempted"] = True
                retried["verification_solver_status"] = verification_result.status
                retried["verification_solver_attempts"] = verification_result.attempts
                return retried
        ui_error = str(result.get("video_creation_ui_error") or "")
        settings_ui_error = any(marker in ui_error for marker in (
            "video ratio and duration selector unavailable",
            "video settings panel unavailable",
            "video setting unavailable",
            "video settings mismatch",
        ))
        if settings_ui_error:
            fallback = await self._submit_via_video_creation_internal_request(page, image_count=image_count)
            fallback["native_submission_fallback_used"] = True
            fallback["native_video_creation_ui_error"] = ui_error[:500]
            return fallback
        terminal_keys = (
            "video_creation_ui_error",
            "text_only_response",
            "service_frequent",
            "slider_verification",
            "quota_insufficient",
            "region_restricted",
            "login_invalid",
        )
        if (
            result.get("ok")
            and not result.get("accepted")
            and not result.get("native_submission_request_seen")
            and not any(result.get(key) for key in terminal_keys)
        ):
            fallback = await self._submit_via_video_creation_internal_request(page, image_count=image_count)
            fallback["native_submission_fallback_used"] = True
            return fallback
        return result

    async def _run_browser(self, proxy_config: dict[str, str] | None) -> dict[str, Any]:
        runtime = self.browser_pool.playwright_context() if self.browser_pool is not None else async_playwright()
        async with runtime as playwright:
            browser: Browser | None = None
            context: BrowserContext | None = None
            lease: BrowserContextLease | None = None
            page = None
            capture_video_response = None
            capture_tasks: set[asyncio.Task[Any]] = set()
            try:
                self._set_phase("starting_browser", "正在启动豆包生成环境")
                executable_path = resolve_browser_executable(self.settings.browser_executable_path)
                browser_args = ["--disable-dev-shm-usage", "--no-sandbox", "--disable-blink-features=AutomationControlled"]
                context_options: dict[str, Any] = {
                    "locale": "zh-CN",
                    "viewport": {"width": 1365, "height": 900},
                    "user_agent": BROWSER_USER_AGENT,
                    "extra_http_headers": BROWSER_EXTRA_HTTP_HEADERS,
                    "accept_downloads": False,
                }
                proxy_timezone_id = str(
                    getattr(getattr(self, "proxy_session", None), "proxy_timezone_id", "") or ""
                ).strip()
                if proxy_timezone_id:
                    context_options["timezone_id"] = proxy_timezone_id
                storage_state = self._context_storage_state()
                if storage_state is not None:
                    context_options["storage_state"] = storage_state
                if self.browser_pool is not None:
                    self._set_phase("allocating_browser", "正在分配豆包浏览器资源")
                    lease = await self.browser_pool.acquire_context(
                        executable_path=executable_path,
                        headless=self.settings.headless,
                        proxy=proxy_config,
                        browser_args=browser_args,
                        context_options=context_options,
                    )
                    browser = lease.browser
                    context = lease.context
                else:
                    self._set_phase("launching_browser", "正在启动豆包浏览器")
                    browser = await playwright.chromium.launch(
                        headless=self.settings.headless,
                        executable_path=executable_path,
                        proxy=proxy_config,
                        args=browser_args,
                    )
                    context = await browser.new_context(**context_options)
                await context.add_init_script(BROWSER_INIT_SCRIPT)
                page = context.pages[0] if context.pages else await context.new_page()
                self._set_phase("opening_generation_page", "正在打开豆包生成页面")
                await page.goto(DOUBAO_URL, wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(5000)
                body = await page.locator("body").inner_text()
                if self._is_region_restricted(str(page.url), body):
                    await self._record_diagnostic(page, "doubao_region_restricted", body)
                    if self.proxy_session is not None:
                        self.proxy_session.mark_browser_proxy_unavailable(reason="doubao_region_restricted")
                    return {
                        "success": False,
                        "retryable": True,
                        "reason": "doubao region restricted",
                        "account_fault": True,
                        "account_region_restricted": True,
                        "switch_account": True,
                    }
                if await self._login_required(page, body):
                    await self._record_diagnostic(page, "doubao_login_invalid", body)
                    disable_account_for_login(str(self.account.get("id") or ""), "豆包登录状态失效，请重新导入 Cookie")
                    return {
                        "success": False,
                        "retryable": True,
                        "reason": "doubao account not logged in",
                        "account_fault": True,
                        "account_login_invalid": True,
                        "switch_account": True,
                    }
                await self._refresh_cookies(context)
                if self.model not in DOUBAO_MODEL_CODES:
                    return {"success": False, "retryable": False, "reason": "doubao model unavailable"}
                image_count = int(get_meta(self.task_id).get("image_count") or 0) if task_exists(self.task_id) else 0
                self._set_phase(
                    "preparing_references",
                    "正在准备豆包参考图" if image_count > 0 else "正在准备豆包生成请求",
                )
                if not begin_task_submission(self.task_id):
                    canceled = is_task_canceled(self.task_id)
                    return {"success": False, "retryable": not canceled, "reason": "用户取消生成" if canceled else "任务提交状态已变化，正在重试"}
                self._set_phase("submitting_request", "正在通过豆包网页提交生成请求")
                try:
                    completion_result = await self._submit_via_video_creation_page(page, image_count=image_count)
                except Exception as exc:
                    completion_result = {
                        "ok": False,
                        "status": 0,
                        "accepted": False,
                        "video_creation_ui_error": f"doubao video creation page failed: {str(exc)[:300]}",
                    }
                finally:
                    self._finish_image_preparation()
                completion_result["video_creation_page_used"] = True
                error, category = classify_doubao_submission(completion_result)
                if category == "video_creation_ui_error":
                    await self._record_diagnostic(page, "doubao_video_creation_ui_error")
                if category == "service_frequent":
                    set_account_cooldown(str(self.account.get("id") or ""), 1800, "豆包当前服务访问频繁")
                elif category == "submit_rejected":
                    set_account_cooldown(str(self.account.get("id") or ""), 1800, "豆包提交被拒绝")

                if category == "service_frequent":
                    risk_state = await self._observe_service_frequent(page)
                    if risk_state == "login_invalid":
                        error = "doubao account not logged in"
                        category = "login_invalid"
                await self._refresh_cookies(context)
                self._set_phase("submission_received", "豆包生成请求已接收，正在确认状态")
                if error:
                    release_task_submission(self.task_id)
                    save_result(self.task_id, extra={
                        "doubao_submit_error_category": category,
                        "doubao_submission_response_preview": str(completion_result.get("response_preview") or "")[:6000],
                        "doubao_confirmation_prompt_detected": bool(completion_result.get("confirmation_prompt_detected")),
                        "doubao_auto_confirmation_sent": bool(completion_result.get("auto_confirmation_sent")),
                        "doubao_initial_response_preview": str(completion_result.get("initial_response_preview") or "")[:6000],
                        "doubao_generation_wait_message_detected": bool(completion_result.get("generation_wait_message_detected")),
                        "doubao_generation_wait_message": str(completion_result.get("generation_wait_message") or "")[:1000],
                        "doubao_generation_ack_source": str(completion_result.get("generation_ack_source") or "javascript_response")[:100],
                        "doubao_same_account_resend_count": int(completion_result.get("same_account_resend_count") or 0),
                        "doubao_same_account_attempt_count": int(completion_result.get("same_account_attempt_count") or 1),
                        "doubao_same_account_retry_limit": int(completion_result.get("same_account_retry_limit") or 0),
                        "doubao_resend_delay_ms": int(completion_result.get("resend_delay_ms") or 0),
                        "doubao_video_creation_page_refusal_count": int(completion_result.get("video_creation_page_refusal_count") or 0),
                        "doubao_video_creation_page_new_conversation_resend_count": int(completion_result.get("video_creation_page_new_conversation_resend_count") or 0),
                        "doubao_video_creation_page_refusal_repeated": bool(completion_result.get("video_creation_page_refusal_repeated")),
                        "doubao_video_creation_page_used": bool(completion_result.get("video_creation_page_used")),
                        "doubao_video_creation_ui_error": str(completion_result.get("video_creation_ui_error") or "")[:500],
                        "doubao_native_video_creation_ui_error": str(completion_result.get("native_video_creation_ui_error") or "")[:500],
                        "doubao_native_submission_request_seen": bool(completion_result.get("native_submission_request_seen")),
                        "doubao_native_submission_fallback_used": bool(completion_result.get("native_submission_fallback_used")),
                        "doubao_direct_submission_response_preview": str(completion_result.get("direct_submission_response_preview") or "")[:6000],
                    })
                    if category == "service_frequent":
                        if self.proxy_session is not None:
                            self.proxy_session.mark_browser_proxy_unavailable(reason="doubao_service_frequent")
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": error,
                            "infrastructure_fault": True,
                        }
                    if category == "slider_verification":
                        set_account_cooldown(str(self.account.get("id") or ""), 86400, "豆包触发网页人机验证")
                        proxy_cooldown_seconds = 0
                        if self.proxy_session is not None:
                            proxy_cooldown_seconds = self.proxy_session.mark_doubao_verification_proxy()
                        save_result(self.task_id, extra={
                            "doubao_verification_proxy_cooldown_seconds": proxy_cooldown_seconds,
                            "doubao_verification_proxy_node_id": str(getattr(self.proxy_session, "proxy_node_id", "") or ""),
                        })
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": error,
                            "account_fault": True,
                            "account_slider_verification": True,
                            "switch_account": True,
                        }
                    if category == "login_invalid":
                        disable_account_for_login(str(self.account.get("id") or ""), "豆包登录状态失效，请重新导入 Cookie")
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": error,
                            "account_fault": True,
                            "account_login_invalid": True,
                            "switch_account": True,
                        }
                    if category == "region_restricted":
                        if self.proxy_session is not None:
                            self.proxy_session.mark_browser_proxy_unavailable(reason="doubao_region_restricted")
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": error,
                            "account_fault": True,
                            "account_region_restricted": True,
                            "switch_account": True,
                        }
                    if category == "quota_insufficient":
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": error,
                            "account_fault": True,
                            "account_quota_insufficient": True,
                            "switch_account": True,
                        }
                    if category == "text_only_response":
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": error,
                            "account_fault": True,
                            "account_text_only": True,
                            "switch_account": True,
                        }
                    if category == "generation_ack_missing":
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": error,
                            "account_fault": True,
                            "switch_account": True,
                        }
                    if category == "generation_ack_missing_after_new_conversation":
                        update_meta(self.task_id, doubao_fresh_conversation_retry_used=False)
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": error,
                            "account_fault": True,
                            "switch_account": True,
                        }
                    if category == "video_creation_ui_error":
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": error,
                            "infrastructure_fault": True,
                        }
                    return {"success": False, "retryable": True, "reason": error}
                conversation_id = str(completion_result.get("conversation_id") or "")
                proxy_session = getattr(self, "proxy_session", None)
                if proxy_session is not None:
                    proxy_session.record_doubao_proxy_success()
                    active_node_id = str(getattr(proxy_session, "proxy_node_id", "") or "").strip()
                    active_source = str(getattr(proxy_session, "active_proxy_source", "") or "").strip()
                    if active_source == "subscription" and active_node_id:
                        set_account_pinned_proxy_node(str(self.account.get("id") or ""), active_node_id)
                        self.account["pinned_proxy_node_id"] = active_node_id
                refreshed_cookies = await self._refresh_cookies(context)
                save_result(
                    self.task_id,
                    extra={
                        "platform": "doubao",
                        "model": self.model,
                        "account_id": str(self.account.get("id") or ""),
                        "account_name": str(self.account.get("name") or ""),
                        "account_quota_charge_id": str(self.account.get("quota_charge_id") or ""),
                        "doubao_page_url": page.url,
                        "doubao_submit_confirmed": bool(completion_result.get("accepted")),
                        "doubao_submitted_with_images": bool(completion_result.get("submitted_with_images")),
                        "conversation_id": conversation_id,
                        "cookie_string": self._cookie_header(refreshed_cookies),
                        "doubao_conversation_id": conversation_id,
                        "doubao_web_id": str(completion_result.get("web_id") or "111")[:64],
                        "doubao_region": str(completion_result.get("region") or "JP")[:16],
                        "doubao_confirmation_prompt_detected": bool(completion_result.get("confirmation_prompt_detected")),
                        "doubao_auto_confirmation_sent": bool(completion_result.get("auto_confirmation_sent")),
                        "doubao_generation_wait_message_detected": bool(completion_result.get("generation_wait_message_detected")),
                        "doubao_generation_wait_message": str(completion_result.get("generation_wait_message") or "")[:1000],
                        "doubao_generation_ack_source": str(completion_result.get("generation_ack_source") or "javascript_response")[:100],
                        "doubao_same_account_resend_count": int(completion_result.get("same_account_resend_count") or 0),
                        "doubao_same_account_attempt_count": int(completion_result.get("same_account_attempt_count") or 1),
                        "doubao_same_account_retry_limit": int(completion_result.get("same_account_retry_limit") or 0),
                        "doubao_resend_delay_ms": int(completion_result.get("resend_delay_ms") or 0),
                        "doubao_video_creation_page_refusal_count": int(completion_result.get("video_creation_page_refusal_count") or 0),
                        "doubao_video_creation_page_new_conversation_resend_count": int(completion_result.get("video_creation_page_new_conversation_resend_count") or 0),
                        "doubao_video_creation_page_refusal_repeated": bool(completion_result.get("video_creation_page_refusal_repeated")),
                        "doubao_video_creation_page_used": bool(completion_result.get("video_creation_page_used")),
                        "doubao_video_creation_selected_model": str(completion_result.get("video_creation_selected_model") or "")[:100],
                        "doubao_video_creation_selected_ratio": str(completion_result.get("video_creation_selected_ratio") or "")[:20],
                        "doubao_video_creation_selected_duration": int(completion_result.get("video_creation_selected_duration") or 0),
                        "doubao_native_video_creation_ui_error": str(completion_result.get("native_video_creation_ui_error") or "")[:500],
                        "doubao_native_submission_request_seen": bool(completion_result.get("native_submission_request_seen")),
                        "doubao_native_submission_fallback_used": bool(completion_result.get("native_submission_fallback_used")),
                        "doubao_direct_submission_response_preview": str(completion_result.get("direct_submission_response_preview") or "")[:6000],
                        "doubao_result_mode": "interface_poll" if conversation_id else "browser_fallback",
                    },
                )
                video_candidates: dict[str, dict[str, Any]] = {}
                collect_doubao_video_candidates(completion_result, video_candidates, "submission", source="submission_response")
                if conversation_id:
                    mark_submitted(self.task_id, result_poll_delay_seconds=20)
                    self._set_phase("waiting_result", "豆包已释放浏览器，正在通过接口查询视频")
                    return {
                        "success": True,
                        "retryable": False,
                        "reason": "",
                        "confirmation_pending": True,
                        "keep_account_claimed": True,
                    }
                immediate_candidate = best_doubao_video_candidate(video_candidates)
                if doubao_video_candidate_is_acceptable(immediate_candidate):
                    return await self._save_video_success(
                        context,
                        page,
                        str(immediate_candidate.get("url") or ""),
                        str(immediate_candidate.get("source") or "submission_response"),
                        score=int(immediate_candidate.get("score") or 0),
                        candidate_key=str(immediate_candidate.get("key") or ""),
                        candidate_count=len(video_candidates),
                    )
                mark_submitted(self.task_id, result_poll_delay_seconds=20)

                def capture_video_response(response) -> None:
                    url = self._response_video_url(response)
                    if url:
                        add_doubao_video_candidate(video_candidates, url, key="response.url", source="media_response")
                    create_tracked_task(capture_tasks, self._capture_video_candidates(response, video_candidates))

                page.on("response", capture_video_response)
                deadline = asyncio.get_running_loop().time() + DOUBAO_RESULT_WAIT_SECONDS
                playback_triggered = False
                single_chain_error = ""
                self._set_phase("waiting_result", "豆包正在生成视频")
                while asyncio.get_running_loop().time() < deadline:
                    if conversation_id:
                        query_error = await self._fetch_single_chain_candidates(page, conversation_id, video_candidates)
                        if query_error:
                            single_chain_error = query_error
                    url, source = await self._page_video_url(page, [])
                    if url:
                        add_doubao_video_candidate(video_candidates, url, key=f"dom.{source}", source=source)
                    text = await page.locator("body").inner_text()
                    if not playback_triggered:
                        try:
                            playback_triggered = await self._activate_completed_video(page)
                        except Exception as exc:
                            save_result(self.task_id, extra={"doubao_video_activation_error": str(exc)[:500]})
                        if playback_triggered:
                            url, source = await self._page_video_url(page, [])
                            if url:
                                add_doubao_video_candidate(video_candidates, url, key=f"dom.{source}", source=source)
                    candidate = best_doubao_video_candidate(video_candidates)
                    candidate_score = int(candidate.get("score") or 0)
                    if doubao_video_candidate_is_acceptable(candidate):
                        return await self._save_video_success(
                            context,
                            page,
                            str(candidate.get("url") or ""),
                            str(candidate.get("source") or ""),
                            score=candidate_score,
                            candidate_key=str(candidate.get("key") or ""),
                            candidate_count=len(video_candidates),
                        )
                    if is_doubao_account_quota_insufficient(text[-1500:]):
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": "豆包账号额度不足或已耗尽",
                            "account_fault": True,
                            "account_quota_insufficient": True,
                            "switch_account": True,
                        }
                    if any(marker in text[-1500:] for marker in ("生成失败", "无法生成", "内容违规")):
                        return {"success": False, "retryable": False, "reason": "doubao generation failed"}
                    await page.wait_for_timeout(DOUBAO_RESULT_POLL_MILLISECONDS)
                candidate = best_doubao_video_candidate(video_candidates)
                if doubao_video_candidate_is_acceptable(candidate):
                    return await self._save_video_success(
                        context,
                        page,
                        str(candidate.get("url") or ""),
                        str(candidate.get("source") or ""),
                        score=int(candidate.get("score") or 0),
                        candidate_key=str(candidate.get("key") or ""),
                        candidate_count=len(video_candidates),
                    )
                await self._refresh_cookies(context)
                save_result(
                    self.task_id,
                    extra={
                        "doubao_video_result_timeout": True,
                        "doubao_page_url": page.url,
                        "doubao_single_chain_error": single_chain_error,
                        "doubao_rejected_fallback_source": str(candidate.get("source") or ""),
                        "doubao_rejected_fallback_key": str(candidate.get("key") or ""),
                        "doubao_rejected_fallback_score": int(candidate.get("score") or 0),
                    },
                )
                return {"success": False, "retryable": False, "reason": "doubao original video result timeout"}
            finally:
                self._finish_image_preparation()
                if page is not None and capture_video_response is not None:
                    try:
                        page.remove_listener("response", capture_video_response)
                    except Exception:
                        pass
                await cancel_tracked_tasks(capture_tasks)
                if lease is not None:
                    await bounded_cleanup(lease.release())
                else:
                    await bounded_cleanup(safe_close(context))
                    await bounded_cleanup(safe_close(browser))
