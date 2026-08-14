from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from playwright.async_api import async_playwright

from .accounts import disable_account_for_login, update_account_cookies
from .browser_runtime import cancel_tracked_tasks, create_tracked_task, resolve_browser_executable, safe_close
from .browser_live_view import TaskBrowserLiveView
from .config import QIANWEN_PROFILES_DIR, TASKS_DIR, ensure_dirs, load_settings
from .store import begin_task_submission, clear_transient_result, get_meta, is_task_canceled, mark_pending, save_result, task_exists, task_image_paths, update_meta
from .profile_lock import account_profile_lock


QIANWEN_URL = "https://www.qianwen.com/"
QIANWEN_AI_STUDIO_HOSTS = {"create.qianwen.com", "ai-studio-create.qianwen.com"}
QIANWEN_CHAT_API_URL = "https://chat2.qianwen.com/api/v2/chat"
QIANWEN_CHAT_SNAP_API_URL = "https://chat2.qianwen.com/api/v1/chat/snap"
QIANWEN_DETAIL_API_URL = "https://chat2-api.qianwen.com/api/v1/session/req/detail"
VIDEO_URL_RE = re.compile(r'https?://[^"\\\s]+\.mp4(?:\?[^"\\\s]*)?', re.IGNORECASE)
MEDIA_URL_RE = re.compile(r'https?://[^"\\\s]+(?:\.mp4|mime_type=video|video_mp4|\.m3u8)(?:\?[^"\\\s]*)?', re.IGNORECASE)
TASK_KEY_RE = re.compile(r"(?:task|job|request|aigc|generation|message)[_-]?id", re.IGNORECASE)
QIANWEN_QUERY_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
QIANWEN_REFERENCE_REQUIRED_MODELS = {"万相 2.7", "万相 2.6", "HappyHorse 1.0"}
QIANWEN_CHAT_ROUTE_PATTERNS = ("**/api/v2/chat*", "**/api/v1/chat/snap*")


def qianwen_interface_response_confirmed(status: int, body: str) -> bool:
    text = str(body or "")
    lowered = text.lower()
    if int(status or 0) != 200 or "stream_error" in lowered:
        return False
    if is_qianwen_user_validation_error(text):
        return False
    return bool(re.search(r'"(?:sessionid|session_id)"\s*:\s*"[^" ]+"', text) and re.search(r'"(?:reqid|req_id)"\s*:\s*"[^" ]+"', text))


def qianwen_httpx_proxy_config(proxy_config: dict[str, str] | None) -> str:
    if not proxy_config or not proxy_config.get("server"):
        return ""
    server = str(proxy_config.get("server") or "")
    username = str(proxy_config.get("username") or "")
    password = str(proxy_config.get("password") or "")
    if username:
        from urllib.parse import quote

        scheme, separator, remainder = server.partition("://")
        if separator:
            server = f"{scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{remainder}"
    return server


def is_qianwen_ai_studio_redirect(url: str) -> bool:
    host = str(urlsplit(str(url or "").strip()).hostname or "").lower().rstrip(".")
    return host in QIANWEN_AI_STUDIO_HOSTS


def is_qianwen_user_validation_error(body: str, url: str = "") -> bool:
    text = f"{body}\n{url}".lower()
    return any(marker in text for marker in ("fail_sys_user_validate", "rgv587_error", "action=captcha"))


def is_qianwen_text_only_response(body: str) -> bool:
    text = str(body or "").lower()
    return any(marker in text for marker in (
        "抱歉，我无法回答这个问题",
        "我无法回答这个问题",
        "我们聊聊别的吧",
        "-zs11403-",
        "input query audit rejected",
        "安审拒绝",
    ))


def qianwen_model_requires_reference(model: str) -> bool:
    return str(model or "").strip() in QIANWEN_REFERENCE_REQUIRED_MODELS


def is_qianwen_account_quota_insufficient(text: str) -> bool:
    value = re.sub(r"\s+", "", str(text or "")).lower()
    return any(
        marker in value
        for marker in (
            "额度不足",
            "当前剩余",
            "不足以完成本次视频生成",
            "购买获得更多额度",
            "creditnotenough",
            "insufficientcredit",
        )
    )


def is_qianwen_content_rejection(text: str) -> bool:
    value = str(text or "").lower()
    return any(marker in value for marker in (
        "当前内容无法生成", "内容无法生成", "内容违规", "审核未通过",
    ))


def is_qianwen_chat_api_url(url: str) -> bool:
    value = str(url or "").strip().lower()
    return value.startswith((QIANWEN_CHAT_API_URL.lower(), QIANWEN_CHAT_SNAP_API_URL.lower()))


def _try_parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _walk_qianwen(value: Any, path: str = ""):
    value = _try_parse_json(value)
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _walk_qianwen(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_qianwen(child, f"{path}[{index}]")


def qianwen_cookie_header(cookies: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{item['name']}={item['value']}"
        for item in cookies
        if isinstance(item, dict) and item.get("name") and item.get("value") is not None
    )


def qianwen_cookie_value(cookie_header: str, name: str) -> str:
    expected = str(name or "").strip()
    for part in str(cookie_header or "").split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key.replace("\\_", "_") == expected:
            return value.strip()
    return ""


def parse_qianwen_submission(post_data: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(post_data or "{}"))
    except json.JSONDecodeError:
        return {}
    biz_data = _try_parse_json(payload.get("biz_data"))
    if not isinstance(biz_data, dict):
        return {}
    request_data = biz_data.get("req") if isinstance(biz_data.get("req"), dict) else {}
    params = request_data.get("params") if isinstance(request_data.get("params"), dict) else {}
    scene = str(payload.get("ai_tool_scene") or "")
    if scene != "zaodian_generate_video" and str(biz_data.get("bizScene") or "") != "genVideo":
        return {}
    report = biz_data.get("videoReportParams") if isinstance(biz_data.get("videoReportParams"), dict) else {}
    attachments = params.get("attachments") if isinstance(params.get("attachments"), list) else []
    return {
        "session_id": str(payload.get("session_id") or ""),
        "req_id": str(payload.get("req_id") or ""),
        "root_model": str(request_data.get("rootModel") or ""),
        "model": str(report.get("model") or request_data.get("rootModel") or ""),
        "duration": int(params.get("duration") or 0),
        "ratio": str(params.get("size") or ""),
        "resolution": str(params.get("resolution") or ""),
        "audio": params.get("audio") if isinstance(params.get("audio"), bool) else None,
        "gen_mode": str(request_data.get("genMode") or params.get("gen_mode") or ""),
        "attachment_ids": [
            str(item.get("materialId") or "")
            for item in attachments
            if isinstance(item, dict) and item.get("materialId")
        ],
    }


def enable_qianwen_wan27_audio(post_data: str) -> tuple[str, bool]:
    original = str(post_data or "")
    try:
        payload = json.loads(original)
    except (TypeError, json.JSONDecodeError):
        return original, False
    if not isinstance(payload, dict):
        return original, False
    raw_biz_data = payload.get("biz_data")
    biz_data = _try_parse_json(raw_biz_data)
    if not isinstance(biz_data, dict):
        return original, False
    request_data = biz_data.get("req") if isinstance(biz_data.get("req"), dict) else {}
    if str(request_data.get("rootModel") or "").strip().lower() != "wan27":
        return original, False
    params = request_data.get("params") if isinstance(request_data.get("params"), dict) else {}
    if params.get("audio") is True:
        return original, False
    params["audio"] = True
    request_data["params"] = params
    biz_data["req"] = request_data
    payload["biz_data"] = (
        json.dumps(biz_data, ensure_ascii=False, separators=(",", ":"))
        if isinstance(raw_biz_data, str)
        else biz_data
    )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")), True


def qianwen_request_post_data(request: Any) -> str:
    try:
        return str(request.post_data or "")
    except Exception:
        return ""


def parse_qianwen_generation_result(payload: Any) -> dict[str, Any]:
    unwatermarked_candidates: list[tuple[int, str, str]] = []
    watermarked_download_candidates: list[tuple[str, str]] = []
    status_values: list[tuple[str, Any]] = []
    error_codes: list[int] = []
    messages: list[str] = []
    task_ids: list[str] = []
    for path, value in _walk_qianwen(payload):
        key = path.rsplit(".", 1)[-1].split("[", 1)[0].lower()
        if key in {"status", "state", "progress"} and isinstance(value, (str, int, float, bool)):
            status_values.append((path, value))
        if key == "error_code":
            try:
                error_codes.append(int(value or 0))
            except (TypeError, ValueError):
                pass
        if key in {"error_msg", "message", "text"} and isinstance(value, str) and value.strip():
            messages.append(value.strip())
        if key == "task_id" and isinstance(value, str) and value and value not in task_ids:
            task_ids.append(value)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            lowered = path.lower()
            score = 0
            if any(marker in lowered for marker in ("without_watermark", "no_watermark", "unwatermarked", "original")):
                score += 3000
            if ".display_list[" in lowered and re.search(r"\.video\[\d+\]\.url$", lowered):
                score += 2000
            if "download_video" in lowered:
                watermarked_download_candidates.append((path, value))
                score = 0
            if score:
                unwatermarked_candidates.append((score, path, value))
    unwatermarked_candidates.sort(reverse=True)
    video_url = unwatermarked_candidates[0][2] if unwatermarked_candidates else ""
    text = "\n".join(dict.fromkeys(messages))[:2000]
    lowered_text = text.lower()
    quota_insufficient = is_qianwen_account_quota_insufficient(text)
    failed = any(code not in {0} for code in error_codes)
    failed = failed or any(
        marker in lowered_text
        for marker in ("无法生成", "生成失败", "内容无法生成", "违规", "审核未通过", "generation failed")
    )
    numeric_statuses = {
        int(value)
        for path, value in status_values
        if path.endswith((".content.status", ".extra_info.content.status")) and isinstance(value, (int, float))
    }
    failed = failed or 3 in numeric_statuses
    if video_url:
        state = "succeeded"
    elif quota_insufficient:
        state = "quota_insufficient"
    elif failed:
        state = "failed"
    else:
        state = "generating"
    return {
        "state": state,
        "video_url": video_url,
        "video_source": unwatermarked_candidates[0][1] if video_url else "",
        "watermarked_download_available": bool(watermarked_download_candidates),
        "watermarked_download_sources": [path for path, _url in watermarked_download_candidates[-5:]],
        "text": text,
        "task_ids": task_ids,
        "statuses": status_values[-20:],
        "error_codes": error_codes[-10:],
        "quota_insufficient": quota_insufficient,
    }


async def fetch_qianwen_generation_result(
    cookie_header: str,
    session_id: str,
    req_id: str,
    *,
    proxy_server: str = "",
) -> dict[str, Any]:
    user_id = qianwen_cookie_value(cookie_header, "b-user-id")
    params = {
        "biz_id": "ai_qwen",
        "chat_client": "h5",
        "device": "pc",
        "fr": "pc",
        "pr": "qwen",
        "ut": user_id,
        "la": "zh-CN",
        "tz": "UTC",
        "wv": "4.0.14",
        "ve": "4.0.14",
        "session_id": str(session_id or ""),
        "req_id": str(req_id or ""),
    }
    headers = {
        "accept": "application/json, text/plain, */*",
        "cookie": str(cookie_header or ""),
        "referer": f"https://www.qianwen.com/chat/{session_id}",
        "user-agent": QIANWEN_QUERY_UA,
    }
    client_options: dict[str, Any] = {
        "headers": headers,
        "follow_redirects": True,
        "timeout": httpx.Timeout(45.0, connect=15.0),
        "trust_env": False,
    }
    if proxy_server:
        client_options["proxy"] = proxy_server
    async with httpx.AsyncClient(**client_options) as client:
        response = await client.get(QIANWEN_DETAIL_API_URL, params=params)
        response.raise_for_status()
        parsed = parse_qianwen_generation_result(response.json())
        parsed.update({"http_status": response.status_code, "session_id": session_id, "req_id": req_id})
        return parsed


def qianwen_video_url_score(url: str, key: str = "") -> int:
    value = str(url or "").lower()
    field = str(key or "").lower()
    score = 0
    clean_markers = ("no_watermark", "without_watermark", "watermark_free", "unwatermarked", "watermark=0", "watermark%3d0", "wm=0")
    original_markers = ("original", "origin", "source", "download", "raw")
    if any(marker in field or marker in value for marker in clean_markers):
        score += 300
    if any(marker in field for marker in original_markers):
        score += 140
    if "main_url" in field:
        score += 100
    elif "video_url" in field:
        score += 70
    elif "play_url" in field:
        score += 30
    if ".mp4" in value or "video_mp4" in value:
        score += 20
    if ".m3u8" in value:
        score -= 10
    explicitly_clean = any(marker in field or marker in value for marker in clean_markers)
    if not explicitly_clean and ("watermark" in field or "watermark=1" in value or "wm=1" in value):
        score -= 240
    if any(marker in field or marker in value for marker in ("preview", "thumbnail", "poster", "sample")):
        score -= 120
    return score


def best_qianwen_video_url(candidates: dict[str, int] | list[str]) -> str:
    if isinstance(candidates, dict):
        rows = candidates.items()
    else:
        rows = ((url, qianwen_video_url_score(url)) for url in candidates)
    return max(rows, key=lambda item: (item[1], ".mp4" in item[0].lower()), default=("", 0))[0]


class QianwenVideoAutomation:
    def __init__(
        self,
        task_id: str,
        prompt: str,
        ratio: str,
        model: str,
        task_type: str = "video",
        duration: int = 10,
        account: dict[str, Any] | None = None,
        proxy_session: Any | None = None,
        submission_pacer: Any | None = None,
    ):
        self.task_id = task_id
        self.prompt = prompt
        self.ratio = ratio
        self.model = model
        self.task_type = "image" if task_type == "image" or model == "AI生图" else "video"
        self.duration = max(1, int(duration or 10))
        self.account = account or {}
        self.proxy_session = proxy_session
        self.submission_pacer = submission_pacer
        self.settings = load_settings()
        ensure_dirs()
        self.profile_path = QIANWEN_PROFILES_DIR / str(self.account.get("id") or "unknown")
        self.network_events: list[dict[str, Any]] = []
        self.remote_task_ids: list[str] = []
        self.remote_video_urls: list[str] = []
        self.remote_video_scores: dict[str, int] = {}
        self.first_video_candidate_at = 0.0
        self.remote_error = ""
        self.remote_session_id = ""
        self.remote_req_id = ""
        self.remote_submission: dict[str, Any] = {}
        self.audio_request_patch_count = 0
        self.qianwen_interface_submit_attempts = 0
        self.qianwen_interface_submit_successes = 0
        self.qianwen_interface_submit_fallbacks = 0
        self.reference_upload_failure_detail = ""
        self.submission_request_event = asyncio.Event()
        self.submission_event = asyncio.Event()

    def _account_cookies(self) -> list[dict[str, Any]]:
        cookies: list[dict[str, Any]] = []
        for item in self.account.get("cookies") or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            cookie = dict(item)
            cookie["domain"] = ".qianwen.com"
            cookie.setdefault("path", "/")
            cookies.append(cookie)
        return cookies

    async def _refresh_cookies(self, context) -> list[dict[str, Any]]:
        account_id = str(self.account.get("id") or "")
        cookies = await context.cookies([QIANWEN_URL])
        if account_id and cookies:
            update_account_cookies(account_id, cookies)
        return cookies

    def _video_settings_control(self, page):
        pattern = re.compile(r"(?:480|720|1080)P\s*[·•.\-/]\s*(?:5|10|15)\s*s", re.IGNORECASE)
        return page.locator("button:visible").filter(has_text=pattern).last

    def _video_setting_options(self, page, pattern: re.Pattern[str]):
        return page.locator("button:visible:not([disabled])").filter(has_text=pattern)

    async def _visible_video_setting_option(self, page, pattern: re.Pattern[str]):
        options = self._video_setting_options(page, pattern)
        for index in range(await options.count() - 1, -1, -1):
            option = options.nth(index)
            if not await option.is_visible() or not await option.is_enabled():
                continue
            text = str(await option.inner_text() or "").strip()
            if pattern.fullmatch(text):
                return option
        return None

    async def _select_video_setting(self, page, pattern: re.Pattern[str]) -> bool:
        # Ratio selection leaves this panel open on some Qianwen builds. Reusing
        # an already visible option avoids toggling the panel closed before the
        # duration is selected.
        option = await self._visible_video_setting_option(page, pattern)
        if option is None:
            control = self._video_settings_control(page)
            if not await control.count():
                return False
            await control.click(force=True)
            await page.wait_for_timeout(500)
            option = await self._visible_video_setting_option(page, pattern)
        if option is None:
            return False
        await option.click(force=True)
        await page.wait_for_timeout(500)
        return True

    async def _video_setting_is_selected(self, page, pattern: re.Pattern[str]) -> bool:
        option = await self._visible_video_setting_option(page, pattern)
        if option is None:
            control = self._video_settings_control(page)
            if not await control.count():
                return False
            await control.click(force=True)
            await page.wait_for_timeout(300)
            option = await self._visible_video_setting_option(page, pattern)
        if option is None:
            return False
        class_name = str(await option.get_attribute("class") or "")
        state_values: list[str] = []
        for name in ("aria-checked", "aria-pressed", "aria-selected", "data-state"):
            state_values.append(str(await option.get_attribute(name) or ""))
        state = " ".join(state_values)
        return bool(re.search(r"active|selected|checked", class_name, re.IGNORECASE) or re.search(r"\b(?:true|on|checked)\b", state, re.IGNORECASE))

    async def _ensure_video_duration(self, page) -> bool:
        if self.duration not in {5, 10, 15}:
            return False
        duration_pattern = re.compile(rf"^{self.duration}\s*(?:秒|s)$", re.IGNORECASE)
        control = self._video_settings_control(page)
        if await control.count() and re.search(rf"{self.duration}\s*s", str(await control.inner_text() or ""), re.IGNORECASE):
            return True
        if not await self._select_video_setting(page, duration_pattern):
            return False
        for _ in range(4):
            control = self._video_settings_control(page)
            if await control.count() and re.search(rf"{self.duration}\s*s", str(await control.inner_text() or ""), re.IGNORECASE):
                return True
            await page.wait_for_timeout(250)
        return False

    async def _ensure_video_ratio(self, page) -> bool:
        ratio = str(self.ratio or "").strip()
        if ratio not in {"16:9", "9:16", "1:1", "4:3", "3:4"}:
            return False
        ratio_pattern = re.compile(rf"^{re.escape(ratio)}$")
        if not await self._select_video_setting(page, ratio_pattern):
            return False
        return await self._video_setting_is_selected(page, ratio_pattern)

    async def _ensure_video_model(self, page) -> bool:
        if self.task_type != "video" or not self.model:
            return True
        model_buttons = page.locator("button:visible").filter(has_text=re.compile(r"万相|Wan|HappyHorse", re.IGNORECASE))
        if not await model_buttons.count():
            return False
        model_button = model_buttons.last
        current_model = str(await model_button.inner_text() or "")
        if self.model in current_model:
            return True
        await model_button.click(force=True)
        options = page.get_by_text(self.model, exact=True)
        for index in range(await options.count() - 1, -1, -1):
            option = options.nth(index)
            if not await option.is_visible():
                continue
            await option.click(force=True)
            await page.wait_for_timeout(800)
            return True
        return False

    def _video_mode_entry(self, page):
        return page.locator('button[aria-label="AI生视频"]:visible').first

    async def _video_mode_active(self, page, entry=None) -> bool:
        current_entry = entry or self._video_mode_entry(page)
        if await current_entry.count() and str(await current_entry.get_attribute("aria-pressed") or "").lower() == "true":
            return True
        model_buttons = page.locator("button:visible").filter(has_text=re.compile(r"万相|Wan|HappyHorse", re.IGNORECASE))
        return bool(await model_buttons.count() and await self._video_settings_control(page).count())

    async def _activate_video_mode(self, page) -> bool:
        for _attempt in range(3):
            entry = self._video_mode_entry(page)
            if not await entry.count():
                await page.wait_for_timeout(1000)
                continue
            if await self._video_mode_active(page, entry):
                return True
            try:
                await entry.click(force=True)
            except Exception:
                await entry.evaluate("element => element.click()")
            for _poll in range(12):
                await page.wait_for_timeout(500)
                if await self._video_mode_active(page):
                    return True
            await page.keyboard.press("Escape")
        return False

    async def _route_chat_submission(self, route, request) -> None:
        patched_data, changed = enable_qianwen_wan27_audio(qianwen_request_post_data(request))
        if changed:
            self.audio_request_patch_count += 1
        submission = parse_qianwen_submission(patched_data)
        if (
            self.settings.qianwen_reference_interface_submit_enabled
            and submission.get("attachment_ids")
            and is_qianwen_chat_api_url(str(request.url))
        ):
            self.qianwen_interface_submit_attempts += 1
            try:
                headers = await request.all_headers()
                headers.pop("content-length", None)
                options = {
                    "follow_redirects": True,
                    "timeout": httpx.Timeout(90.0, connect=20.0),
                    "trust_env": False,
                }
                if self._interface_proxy_server:
                    options["proxy"] = self._interface_proxy_server
                async with httpx.AsyncClient(**options) as client:
                    response = await client.post(
                        str(request.url),
                        headers=headers,
                        content=patched_data.encode("utf-8"),
                    )
                body = response.text
                if qianwen_interface_response_confirmed(response.status_code, body):
                    self.qianwen_interface_submit_successes += 1
                    await route.fulfill(
                        status=response.status_code,
                        headers={"content-type": response.headers.get("content-type", "text/event-stream;charset=UTF-8")},
                        body=body,
                    )
                    return
            except Exception as exc:
                self.network_events.append({"url": str(request.url), "method": "POST", "status": 0, "interface_submit_error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            self.qianwen_interface_submit_fallbacks += 1
        await route.continue_(post_data=patched_data if changed else None)

    @staticmethod
    async def _available_image_file_input(page):
        selectors = (
            '[data-chat-input-top-content="true"] input[type="file"]',
            '[data-chat-input-bottom-bar="true"] input[type="file"]',
            '[role="dialog"]:visible input[type="file"]',
            '[role="menu"]:visible input[type="file"]',
            'input[type="file"][accept*="image"]',
            'input[type="file"][accept*=".png"]',
            'input[type="file"]',
        )
        for selector in selectors:
            inputs = page.locator(selector)
            count = await inputs.count()
            for index in range(count - 1, -1, -1):
                candidate = inputs.nth(index)
                try:
                    accept = str(await candidate.get_attribute("accept") or "").lower()
                except Exception:
                    accept = ""
                if accept and not any(marker in accept for marker in ("image", ".png", ".jpg", ".jpeg", ".webp")):
                    continue
                return candidate
        return None

    async def _wait_for_image_file_input(self, page, timeout_seconds: float = 1.5):
        deadline = asyncio.get_running_loop().time() + max(0.1, float(timeout_seconds))
        while asyncio.get_running_loop().time() < deadline:
            candidate = await self._available_image_file_input(page)
            if candidate is not None:
                return candidate
            await page.wait_for_timeout(150)
        return None

    async def _open_visible_upload_option(self, page):
        upload_options = page.locator(
            '[role="listbox"]:visible [role="option"], [role="menu"]:visible [role="menuitem"], '
            '[role="menu"]:visible button:visible, [role="dialog"]:visible button:visible, '
            '[data-radix-popper-content-wrapper] button:visible, [data-radix-popper-content-wrapper] [role="menuitem"]'
        )
        for option_index in range(await upload_options.count()):
            option = upload_options.nth(option_index)
            try:
                if not await option.is_visible() or not await option.is_enabled():
                    continue
                label = re.sub(r"\s+", "", str(await option.inner_text() or ""))
            except Exception:
                continue
            media_label = any(marker in label for marker in ("图片", "图像", "照片", "文件", "素材"))
            if not media_label or not any(marker in label for marker in ("上传", "本地", "选择", "添加")):
                continue
            try:
                async with page.expect_file_chooser(timeout=3000) as chooser_info:
                    await option.click(force=True)
                return await chooser_info.value
            except Exception:
                candidate = await self._wait_for_image_file_input(page)
                if candidate is not None:
                    return candidate
        return None

    async def _open_image_file_chooser(self, page):
        existing_option = await self._open_visible_upload_option(page)
        if existing_option is not None:
            return existing_option
        reference_buttons = page.get_by_role("button", name="参考", exact=True)
        if not await reference_buttons.count():
            reference_buttons = page.locator('button:visible:has-text("参考")')
        for index in range(await reference_buttons.count()):
            button = reference_buttons.nth(index)
            if not await button.is_visible() or not await button.is_enabled():
                continue
            try:
                async with page.expect_file_chooser(timeout=3000) as chooser_info:
                    await button.click(force=True)
                return await chooser_info.value
            except Exception:
                candidate = await self._wait_for_image_file_input(page)
                if candidate is not None:
                    return candidate
                option = await self._open_visible_upload_option(page)
                if option is not None:
                    return option
        triggers = page.locator(
            'button[aria-label*="添加"]:visible, button[aria-label*="附件"]:visible, '
            'button[aria-label*="上传"]:visible, button[title*="添加"]:visible, '
            'button[title*="附件"]:visible, [data-testid*="attachment"]:visible'
        )
        for index in range(await triggers.count()):
            trigger = triggers.nth(index)
            try:
                async with page.expect_file_chooser(timeout=1500) as chooser_info:
                    await trigger.click(force=True)
                return await chooser_info.value
            except Exception:
                candidate = await self._wait_for_image_file_input(page)
                if candidate is not None:
                    return candidate
            option = await self._open_visible_upload_option(page)
            if option is not None:
                return option
        return await self._wait_for_image_file_input(page)

    @staticmethod
    async def _reference_preview_count(page) -> int:
        return int(await page.evaluate(
            r"""() => {
                const host = document.querySelector('[data-chat-input-top-content="true"]');
                if (!host) return 0;
                const candidates = host.querySelectorAll('img,canvas,video,[style*="background-image"]');
                return Array.from(candidates).filter(element => {
                    const style = getComputedStyle(element);
                    const box = element.getBoundingClientRect();
                    const button = element.closest('button');
                    if (button && button.innerText.trim() === '参考') return false;
                    return style.display !== 'none' && style.visibility !== 'hidden'
                        && box.width >= 24 && box.height >= 24;
                }).length;
            }"""
        ))

    async def _wait_for_reference_upload(self, page, *, baseline_preview_count: int, timeout_seconds: int = 90) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        stable_checks = 0
        while asyncio.get_running_loop().time() < deadline:
            body_tail = (await page.locator("body").inner_text())[-2500:]
            busy_text = any(marker in body_tail for marker in ("上传中", "正在上传", "处理中"))
            progress_visible = await page.locator('[role="progressbar"]:visible').count() > 0
            preview_count = await self._reference_preview_count(page)
            if not busy_text and not progress_visible and preview_count > baseline_preview_count:
                stable_checks += 1
                if stable_checks >= 2:
                    return True
            else:
                stable_checks = 0
            await page.wait_for_timeout(1000)
        return False

    async def _upload_reference_images(self, page, paths: list[Any]) -> bool:
        self.reference_upload_failure_detail = ""
        for image_index, path in enumerate(paths, start=1):
            baseline_preview_count = await self._reference_preview_count(page)
            chooser = await self._open_image_file_chooser(page)
            if chooser is None:
                self.reference_upload_failure_detail = f"reference image {image_index} upload control unavailable"
                return False
            try:
                if hasattr(chooser, "set_files"):
                    await chooser.set_files(str(path))
                else:
                    await chooser.set_input_files(str(path))
            except Exception as exc:
                self.reference_upload_failure_detail = f"reference image {image_index} file selection failed: {type(exc).__name__}"
                return False
            await page.wait_for_timeout(2500)
            if not await self._wait_for_reference_upload(page, baseline_preview_count=baseline_preview_count):
                self.reference_upload_failure_detail = f"reference image {image_index} preview did not become stable"
                return False
        return True

    async def _login_state(self, page, context) -> tuple[bool, bool]:
        body = await page.locator("body").inner_text(timeout=90000)
        cookies = await context.cookies([QIANWEN_URL])
        has_sso = any(item.get("name") in {"tongyi_sso_ticket", "tongyi_sso_ticket_hash"} for item in cookies)
        has_user = bool(re.search(r"Qwen\d{4,}", body))
        return has_sso and has_user, "登录" in body[:1200]

    def _collect_network_values(self, value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                self._collect_network_values(child, str(child_key))
            return
        if isinstance(value, list):
            for child in value:
                self._collect_network_values(child, key)
            return
        text = str(value or "")
        if TASK_KEY_RE.search(key) and 6 <= len(text) <= 200 and text not in self.remote_task_ids:
            self.remote_task_ids.append(text)
        if isinstance(value, str):
            for match in MEDIA_URL_RE.findall(text.replace("\\u0026", "&").replace("\\/", "/")):
                if match not in self.remote_video_urls:
                    self.remote_video_urls.append(match)
                self.remote_video_scores[match] = max(self.remote_video_scores.get(match, -1000), qianwen_video_url_score(match, key))
                if not self.first_video_candidate_at:
                    self.first_video_candidate_at = time.monotonic()

    async def _capture_response(self, response) -> None:
        request = response.request
        if request.resource_type not in {"xhr", "fetch"}:
            return
        url = str(response.url)
        lowered_url = url.lower()
        post_data, _changed = enable_qianwen_wan27_audio(qianwen_request_post_data(request))
        submission = parse_qianwen_submission(post_data) if request.method == "POST" and is_qianwen_chat_api_url(lowered_url) else {}
        relevant = bool(submission) or self.prompt in post_data or any(item in lowered_url for item in ("/chat", "video", "wanx", "aigc", "generate", "task", "completion"))
        if not relevant:
            return
        try:
            body = await response.text()
        except Exception:
            body = ""
        event = {"url": url, "method": request.method, "status": response.status, "post_data": post_data[:3000], "body": body[:12000]}
        self.network_events.append(event)
        try:
            self._collect_network_values(json.loads(body))
        except Exception:
            self._collect_network_values(body)
        lowered = body.lower()
        user_validation_error = is_qianwen_user_validation_error(body, url)
        text_only_response = is_qianwen_text_only_response(body)
        content_rejection = is_qianwen_content_rejection(body)
        if user_validation_error:
            self.remote_error = "user_validate"
        if response.status in {401, 403} or any(marker in lowered for marker in ("not login", "unauthorized", "登录失效")):
            self.remote_error = "login"
        elif response.status == 429 or (response.status >= 400 and any(marker in lowered for marker in ("rate limit", "too many requests", "访问频繁", "限流"))):
            self.remote_error = "rate_limit"
        elif response.status in {403, 412} and any(marker in lowered for marker in ("risk", "verify", "captcha", "风控", "验证")):
            self.remote_error = "risk_control"
        elif any(marker in lowered for marker in ("model not", "unsupported model", "模型不可用")):
            self.remote_error = "model_unavailable"
        if text_only_response and not user_validation_error:
            self.remote_error = "text_only"
        if content_rejection and not user_validation_error:
            self.remote_error = "content_rejected"
        if submission:
            self.remote_submission = submission
            self.remote_session_id = str(submission.get("session_id") or "")
            self.remote_req_id = str(submission.get("req_id") or "")
            has_stream_error = "stream_error" in lowered and bool(re.search(r'"error_code"\s*:\s*[1-9]\d*', lowered))
            if response.status == 200 and self.remote_session_id and self.remote_req_id and not has_stream_error and not user_validation_error:
                self.submission_event.set()

    def _capture_request(self, request) -> None:
        if request.method != "POST" or not is_qianwen_chat_api_url(str(request.url)):
            return
        post_data, _changed = enable_qianwen_wan27_audio(qianwen_request_post_data(request))
        submission = parse_qianwen_submission(post_data)
        if not submission:
            return
        self.remote_submission = submission
        self.remote_session_id = str(submission.get("session_id") or "")
        self.remote_req_id = str(submission.get("req_id") or "")
        if self.remote_session_id and self.remote_req_id:
            self.submission_request_event.set()

    async def _save_diagnostics(self, page, reason: str) -> None:
        task_dir = TASKS_DIR / self.task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=str(task_dir / "qianwen_failure.png"), full_page=True)
        except Exception:
            pass
        try:
            html = await page.content()
            html = re.sub(r'(?i)(cookie|authorization|token|ticket)(["\s:=]+)[^"\s<]+', r'\1\2[REDACTED]', html)
            html = re.sub(r'(?i)(value=["\'])[^"]+(["\'])', r'\1[REDACTED]\2', html)
            (task_dir / "qianwen_failure.html").write_text(html, encoding="utf-8")
        except Exception:
            pass
        diagnostic = {"reason": reason, "remote_task_ids": self.remote_task_ids, "remote_error": self.remote_error, "events": self.network_events[-20:]}
        (task_dir / "qianwen_network.json").write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")

    async def _wait_for_submission_or_quota(self, page, timeout_seconds: int = 25) -> bool:
        quota_title = page.get_by_text(re.compile(r"^额度不足$"))
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        while time.monotonic() < deadline:
            if self.remote_error:
                return False
            if self.submission_event.is_set():
                return False
            for index in range(await quota_title.count() - 1, -1, -1):
                if await quota_title.nth(index).is_visible():
                    return True
            await page.wait_for_timeout(500)
        return False

    def _failure(self, reason: str, *, account_fault: bool = False, retryable: bool = True, **extra: Any) -> dict[str, Any]:
        return {"success": False, "retryable": retryable, "reason": reason, "account_fault": account_fault, **extra}

    async def run(self) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(self._run_once(), timeout=max(self.settings.task_timeout_seconds, 720))
        except asyncio.TimeoutError:
            if task_exists(self.task_id):
                mark_pending(self.task_id, "qianwen browser timeout")
            return {"success": False, "retryable": True, "reason": "qianwen browser timeout"}
        except Exception as exc:
            reason = str(exc)[:500]
            if task_exists(self.task_id):
                mark_pending(self.task_id, reason)
            return {
                "success": False,
                "retryable": True,
                "reason": reason,
                "infrastructure_fault": self.proxy_session is not None,
            }

    async def _run_once(self) -> dict[str, Any]:
        if not task_exists(self.task_id):
            return {"success": True, "retryable": False, "reason": ""}
        clear_transient_result(self.task_id)
        if not self.account:
            return {"success": False, "retryable": True, "reason": "no qianwen account available"}
        lock = await account_profile_lock("qianwen", str(self.account.get("id") or ""))
        async with lock:
            return await self._run_profile()

    async def _run_profile(self) -> dict[str, Any]:
        proxy_config = None
        proxy_acquired = False
        try:
            if self.proxy_session is not None:
                proxy_config = await self.proxy_session.acquire_browser_proxy()
                proxy_acquired = True
            return await self._run_browser(proxy_config)
        except Exception:
            if proxy_acquired:
                self.proxy_session.mark_browser_proxy_unavailable(reason="qianwen_browser_failure")
            raise
        finally:
            if self.proxy_session is not None:
                await self.proxy_session.release_browser_proxy()

    async def _run_browser(self, proxy_config: dict[str, str] | None) -> dict[str, Any]:
        self._interface_proxy_server = qianwen_httpx_proxy_config(proxy_config)
        async with async_playwright() as playwright:
            launch_options: dict[str, Any] = {
                "headless": self.settings.headless,
                "executable_path": resolve_browser_executable(self.settings.browser_executable_path),
                "locale": "zh-CN",
                "viewport": {"width": 1365, "height": 900},
                "args": ["--disable-dev-shm-usage", "--no-sandbox", "--disable-blink-features=AutomationControlled"],
            }
            if proxy_config:
                launch_options["proxy"] = proxy_config
            context = await playwright.chromium.launch_persistent_context(str(self.profile_path), **launch_options)
            page = None
            request_handler = None
            response_handler = None
            route_handler = None
            response_tasks: set[asyncio.Task[Any]] = set()
            live_view = TaskBrowserLiveView(self.task_id, "qianwen")
            try:
                imported_cookies = self._account_cookies()
                if imported_cookies:
                    await context.add_cookies(imported_cookies)
                page = context.pages[0] if context.pages else await context.new_page()
                await live_view.start(page)
                route_handler = self._route_chat_submission
                for pattern in QIANWEN_CHAT_ROUTE_PATTERNS:
                    await page.route(pattern, route_handler)
                request_handler = self._capture_request
                page.on("request", request_handler)
                response_handler = lambda response: create_tracked_task(response_tasks, self._capture_response(response))
                page.on("response", response_handler)
                try:
                    await page.goto(QIANWEN_URL, wait_until="commit", timeout=45000)
                    await page.wait_for_function("document.body && document.body.children.length > 0", timeout=60000)
                    await page.wait_for_timeout(8000)
                except Exception as exc:
                    await self._save_diagnostics(page, f"navigation timeout: {exc}")
                    return self._failure("qianwen network timeout", account_fault=False)
                if is_qianwen_ai_studio_redirect(page.url):
                    await self._save_diagnostics(page, "qianwen redirected to AI Studio")
                    return self._failure(
                        "qianwen redirected to AI Studio; switching account",
                        account_fault=False,
                        switch_account=True,
                    )
                logged_in, login_visible = await self._login_state(page, context)
                if not logged_in and login_visible:
                    if imported_cookies:
                        await context.add_cookies(imported_cookies)
                        await page.reload(wait_until="domcontentloaded", timeout=90000)
                        await page.wait_for_timeout(12000)
                        logged_in, login_visible = await self._login_state(page, context)
                    if not logged_in:
                        imported_has_sso = any(str(item.get("name") or "") in {"tongyi_sso_ticket", "tongyi_sso_ticket_hash"} for item in self.account.get("cookies") or [])
                        if login_visible and not imported_has_sso:
                            disable_account_for_login(str(self.account.get("id") or ""), "千问登录凭证已失效，请重新导入 Cookie")
                            await self._save_diagnostics(page, "login invalid")
                            return self._failure("qianwen account not logged in", account_fault=True)
                        await self._save_diagnostics(page, "login check pending")
                        return self._failure("qianwen login check pending", account_fault=False)
                await self._refresh_cookies(context)
                if not await self._activate_video_mode(page):
                    await page.reload(wait_until="domcontentloaded", timeout=90000)
                    await page.wait_for_timeout(8000)
                    if is_qianwen_ai_studio_redirect(page.url):
                        await self._save_diagnostics(page, "qianwen redirected to AI Studio after reload")
                        return self._failure(
                            "qianwen redirected to AI Studio; switching account",
                            account_fault=False,
                            switch_account=True,
                        )
                    if not await self._activate_video_mode(page):
                        await self._save_diagnostics(page, "video mode did not activate after same-account reload")
                        return self._failure("qianwen video mode not active", account_fault=False)
                editor = page.locator('[contenteditable="true"][role="textbox"]:visible').first
                await editor.wait_for(state="visible", timeout=15000)
                reference_paths = task_image_paths(self.task_id)
                if self.task_type == "video" and qianwen_model_requires_reference(self.model) and not reference_paths:
                    reason = f"qianwen {self.model} requires a reference image"
                    await self._save_diagnostics(page, reason)
                    return self._failure(reason, account_fault=False, retryable=False)
                if not await self._ensure_video_model(page):
                    await self._save_diagnostics(page, "model unavailable")
                    return self._failure("qianwen model unavailable", account_fault=False, retryable=False)
                if self.task_type == "video" and not await self._ensure_video_ratio(page):
                    await self._save_diagnostics(page, "ratio unavailable")
                    return self._failure(f"qianwen ratio unavailable: {self.ratio}", account_fault=False, retryable=False)
                if self.task_type == "video" and not await self._ensure_video_duration(page):
                    await self._save_diagnostics(page, "duration unavailable")
                    return self._failure("qianwen duration unavailable", account_fault=False, retryable=False)
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
                uploaded_reference_count = 0
                if reference_paths:
                    if not await self._upload_reference_images(page, reference_paths):
                        upload_detail = self.reference_upload_failure_detail or "reference image upload unavailable"
                        update_meta(self.task_id, qianwen_reference_upload_detail=upload_detail)
                        await self._save_diagnostics(page, upload_detail)
                        return self._failure("qianwen reference image upload failed", account_fault=False)
                    uploaded_reference_count = await self._reference_preview_count(page)
                    if uploaded_reference_count < len(reference_paths):
                        await self._save_diagnostics(page, "reference image preview missing after upload")
                        return self._failure("qianwen reference image upload failed", account_fault=False)
                self.network_events.clear()
                self.remote_task_ids.clear()
                self.remote_video_urls.clear()
                self.remote_video_scores.clear()
                self.first_video_candidate_at = 0.0
                self.remote_error = ""
                self.remote_session_id = ""
                self.remote_req_id = ""
                self.remote_submission = {}
                self.submission_request_event.clear()
                self.submission_event.clear()
                await editor.fill(self.prompt)
                if reference_paths and await self._reference_preview_count(page) < uploaded_reference_count:
                    await self._save_diagnostics(page, "reference image preview lost before submit")
                    return self._failure("qianwen reference image lost before submit", account_fault=False)
                send_button = page.locator('button[aria-label="发送消息"]:visible').first
                await send_button.wait_for(state="visible", timeout=15000)
                if await send_button.is_disabled():
                    await self._save_diagnostics(page, "send button disabled")
                    return self._failure("qianwen send button disabled", account_fault=False)
                if not begin_task_submission(self.task_id):
                    canceled = is_task_canceled(self.task_id)
                    return {"success": False, "retryable": not canceled, "reason": "用户取消生成" if canceled else "任务提交状态已变化，正在重试"}
                if self.submission_pacer is not None:
                    await self.submission_pacer()
                if task_exists(self.task_id) and is_task_canceled(self.task_id):
                    return {"success": False, "retryable": False, "reason": "用户取消生成"}
                await send_button.click(force=True)
                try:
                    await asyncio.wait_for(self.submission_request_event.wait(), timeout=3)
                except asyncio.TimeoutError:
                    await editor.press("Enter")
                quota_dialog_visible = await self._wait_for_submission_or_quota(page, 25)
                await page.wait_for_timeout(2000)
                try:
                    page_text = await page.locator("body").inner_text(timeout=5000)
                except Exception:
                    page_text = ""
                if quota_dialog_visible or is_qianwen_account_quota_insufficient(page_text):
                    await self._save_diagnostics(page, "account quota insufficient")
                    return self._failure(
                        "千问账号额度不足",
                        account_fault=True,
                        account_quota_insufficient=True,
                        switch_account=True,
                    )
                if self.remote_error:
                    reasons = {"login": "qianwen account not logged in", "rate_limit": "qianwen rate limited", "risk_control": "qianwen risk control", "user_validate": "qianwen user validation required", "model_unavailable": "qianwen model unavailable", "text_only": "qianwen returned text only; video was not submitted", "content_rejected": "千问返回：当前内容无法生成，请修改后重试"}
                    reason = reasons[self.remote_error]
                    await self._save_diagnostics(page, reason)
                    if self.remote_error == "user_validate":
                        return self._failure(
                            reason,
                            account_fault=False,
                            retryable=True,
                            switch_account=True,
                            account_cooldown_seconds=1800,
                        )
                    if self.remote_error == "text_only":
                        account_id = str(self.account.get("id") or "")
                        used = bool(get_meta(self.task_id).get("qianwen_text_only_retry_used"))
                        if not used:
                            update_meta(self.task_id, preferred_account_id=account_id, qianwen_text_only_retry_used=True)
                            return self._failure(reason, account_fault=False, retryable=True, qianwen_text_only=True)
                        update_meta(self.task_id, preferred_account_id="", qianwen_text_only_retry_used=False)
                        return self._failure(reason, account_fault=False, retryable=True, switch_account=True, qianwen_text_only=True)
                    if self.remote_error == "content_rejected":
                        client_reason = "参考图内容违规" if reference_paths else "提示词输入违规"
                        update_meta(self.task_id, client_error=client_reason, qianwen_failure_category="content_rejected")
                        return self._failure(reason, account_fault=False, retryable=False, content_rejected=True)
                    return self._failure(reason, account_fault=self.remote_error == "login", retryable=self.remote_error != "model_unavailable")
                if not self.submission_event.is_set() or not self.remote_session_id or not self.remote_req_id:
                    await self._save_diagnostics(page, "submit not confirmed")
                    return self._failure("qianwen submit not confirmed", account_fault=False)
                refreshed_cookies = await self._refresh_cookies(context)
                setting_mismatch: list[str] = []
                actual_duration = int(self.remote_submission.get("duration") or 0)
                actual_ratio = str(self.remote_submission.get("ratio") or "")
                actual_model = str(self.remote_submission.get("model") or "")
                if actual_duration != self.duration:
                    setting_mismatch.append(f"duration={actual_duration}")
                if actual_ratio != self.ratio:
                    setting_mismatch.append(f"ratio={actual_ratio}")
                if self.model and self.model.lower() not in actual_model.lower():
                    setting_mismatch.append(f"model={actual_model}")
                if reference_paths and not self.remote_submission.get("attachment_ids"):
                    setting_mismatch.append("attachments=missing")
                save_result(
                    self.task_id,
                    cookie_string=qianwen_cookie_header(refreshed_cookies),
                    cookies=refreshed_cookies,
                    extra={
                        "platform": "qianwen",
                        "model": self.model,
                        "task_type": self.task_type,
                        "account_id": str(self.account.get("id") or ""),
                        "account_name": str(self.account.get("name") or ""),
                        "account_quota_charge_id": str(self.account.get("quota_charge_id") or ""),
                        "qianwen_page_url": page.url,
                        "qianwen_submit_confirmed": True,
                        "qianwen_session_id": self.remote_session_id,
                        "qianwen_req_id": self.remote_req_id,
                        "qianwen_actual_model": actual_model,
                        "qianwen_actual_duration": actual_duration,
                        "qianwen_actual_ratio": actual_ratio,
                        "qianwen_actual_audio": self.remote_submission.get("audio"),
                        "qianwen_audio_request_patch_count": self.audio_request_patch_count,
                        "qianwen_attachment_ids": list(self.remote_submission.get("attachment_ids") or []),
                        "qianwen_setting_mismatch": setting_mismatch,
                        "qianwen_remote_task_ids": self.remote_task_ids,
                        "qianwen_network_events": self.network_events[-10:],
                        "qianwen_interface_submit_attempts": self.qianwen_interface_submit_attempts,
                        "qianwen_interface_submit_successes": self.qianwen_interface_submit_successes,
                        "qianwen_interface_submit_fallbacks": self.qianwen_interface_submit_fallbacks,
                    },
                )
                return {
                    "success": False,
                    "retryable": True,
                    "reason": "qianwen generation submitted",
                    "account_fault": False,
                    "submitted": True,
                    "keep_account_claimed": True,
                }
            finally:
                await live_view.stop()
                if page is not None and request_handler is not None:
                    page.remove_listener("request", request_handler)
                if page is not None and response_handler is not None:
                    page.remove_listener("response", response_handler)
                if page is not None and route_handler is not None:
                    for pattern in QIANWEN_CHAT_ROUTE_PATTERNS:
                        try:
                            await page.unroute(pattern, route_handler)
                        except Exception:
                            pass
                await cancel_tracked_tasks(response_tasks)
                await safe_close(context)
