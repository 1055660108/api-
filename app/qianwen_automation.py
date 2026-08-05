from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx
from playwright.async_api import async_playwright

from .accounts import disable_account_for_login, update_account_cookies
from .browser_runtime import cancel_tracked_tasks, create_tracked_task, resolve_browser_executable, safe_close
from .config import QIANWEN_PROFILES_DIR, TASKS_DIR, ensure_dirs, load_settings
from .store import begin_task_submission, clear_transient_result, is_task_canceled, mark_pending, save_result, task_exists, task_image_paths
from .profile_lock import account_profile_lock


QIANWEN_URL = "https://www.qianwen.com/"
QIANWEN_CHAT_API_URL = "https://chat2.qianwen.com/api/v2/chat"
QIANWEN_DETAIL_API_URL = "https://chat2-api.qianwen.com/api/v1/session/req/detail"
VIDEO_URL_RE = re.compile(r'https?://[^"\\\s]+\.mp4(?:\?[^"\\\s]*)?', re.IGNORECASE)
MEDIA_URL_RE = re.compile(r'https?://[^"\\\s]+(?:\.mp4|mime_type=video|video_mp4|\.m3u8)(?:\?[^"\\\s]*)?', re.IGNORECASE)
TASK_KEY_RE = re.compile(r"(?:task|job|request|aigc|generation|message)[_-]?id", re.IGNORECASE)
QIANWEN_QUERY_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"


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
        "gen_mode": str(request_data.get("genMode") or params.get("gen_mode") or ""),
        "attachment_ids": [
            str(item.get("materialId") or "")
            for item in attachments
            if isinstance(item, dict) and item.get("materialId")
        ],
    }


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
    ):
        self.task_id = task_id
        self.prompt = prompt
        self.ratio = ratio
        self.model = model
        self.task_type = "image" if task_type == "image" or model == "AI生图" else "video"
        self.duration = max(1, int(duration or 10))
        self.account = account or {}
        self.proxy_session = proxy_session
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

    async def _select_video_setting(self, page, pattern: re.Pattern[str]) -> bool:
        control = self._video_settings_control(page)
        if not await control.count():
            return False
        await control.click(force=True)
        await page.wait_for_timeout(500)
        options = page.get_by_text(pattern)
        for index in range(await options.count() - 1, -1, -1):
            option = options.nth(index)
            if not await option.is_visible():
                continue
            await option.click(force=True)
            await page.wait_for_timeout(500)
            return True
        return False

    async def _ensure_video_duration(self, page) -> bool:
        if self.duration != 10:
            return False
        control = self._video_settings_control(page)
        if await control.count() and re.search(r"10\s*s", str(await control.inner_text() or ""), re.IGNORECASE):
            return True
        return await self._select_video_setting(page, re.compile(r"^10\s*(?:秒|s)$", re.IGNORECASE))

    async def _ensure_video_ratio(self, page) -> bool:
        ratio = str(self.ratio or "").strip()
        if ratio not in {"16:9", "9:16", "1:1", "4:3", "3:4"}:
            return False
        return await self._select_video_setting(page, re.compile(rf"^{re.escape(ratio)}$"))

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

    async def _open_image_file_chooser(self, page):
        reference_buttons = page.get_by_role("button", name="参考", exact=True)
        for index in range(await reference_buttons.count()):
            button = reference_buttons.nth(index)
            if not await button.is_visible() or not await button.is_enabled():
                continue
            try:
                async with page.expect_file_chooser(timeout=3000) as chooser_info:
                    await button.click()
                return await chooser_info.value
            except Exception:
                continue
        triggers = page.locator(
            'button[aria-label*="添加"]:visible, button[aria-label*="附件"]:visible, '
            'button[aria-label*="上传"]:visible, button[title*="添加"]:visible, '
            'button[title*="附件"]:visible, [data-testid*="attachment"]:visible'
        )
        for index in range(await triggers.count()):
            trigger = triggers.nth(index)
            try:
                async with page.expect_file_chooser(timeout=1500) as chooser_info:
                    await trigger.click()
                return await chooser_info.value
            except Exception:
                pass
            upload_options = page.locator(
                '[role="listbox"]:visible [role="option"], [role="menu"]:visible [role="menuitem"], '
                '[role="dialog"]:visible button:visible'
            )
            for option_index in range(await upload_options.count()):
                option = upload_options.nth(option_index)
                label = str(await option.inner_text() or "").strip()
                if "上传图片" not in label and "本地图片" not in label:
                    continue
                try:
                    async with page.expect_file_chooser(timeout=3000) as chooser_info:
                        await option.click()
                    return await chooser_info.value
                except Exception:
                    continue
        image_inputs = page.locator('input[type="file"][accept*="image"]')
        if await image_inputs.count():
            return image_inputs.last
        return None

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
        for path in paths:
            baseline_preview_count = await self._reference_preview_count(page)
            chooser = await self._open_image_file_chooser(page)
            if chooser is None:
                return False
            if hasattr(chooser, "set_files"):
                await chooser.set_files(str(path))
            else:
                await chooser.set_input_files(str(path))
            await page.wait_for_timeout(2500)
            if not await self._wait_for_reference_upload(page, baseline_preview_count=baseline_preview_count):
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
        post_data = str(request.post_data or "")
        url = str(response.url)
        lowered_url = url.lower()
        submission = parse_qianwen_submission(post_data) if request.method == "POST" and lowered_url.startswith(QIANWEN_CHAT_API_URL) else {}
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
        if response.status in {401, 403} or any(marker in lowered for marker in ("not login", "unauthorized", "登录失效")):
            self.remote_error = "login"
        elif response.status == 429 or (response.status >= 400 and any(marker in lowered for marker in ("rate limit", "too many requests", "访问频繁", "限流"))):
            self.remote_error = "rate_limit"
        elif response.status in {403, 412} and any(marker in lowered for marker in ("risk", "verify", "captcha", "风控", "验证")):
            self.remote_error = "risk_control"
        elif any(marker in lowered for marker in ("model not", "unsupported model", "模型不可用")):
            self.remote_error = "model_unavailable"
        if submission:
            self.remote_submission = submission
            self.remote_session_id = str(submission.get("session_id") or "")
            self.remote_req_id = str(submission.get("req_id") or "")
            has_stream_error = "stream_error" in lowered and bool(re.search(r'"error_code"\s*:\s*[1-9]\d*', lowered))
            if response.status == 200 and self.remote_session_id and self.remote_req_id and not has_stream_error:
                self.submission_event.set()

    def _capture_request(self, request) -> None:
        if request.method != "POST" or not str(request.url).lower().startswith(QIANWEN_CHAT_API_URL):
            return
        submission = parse_qianwen_submission(str(request.post_data or ""))
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

    def _failure(self, reason: str, *, account_fault: bool = False, retryable: bool = True) -> dict[str, Any]:
        return {"success": False, "retryable": retryable, "reason": reason, "account_fault": account_fault}

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
            response_tasks: set[asyncio.Task[Any]] = set()
            try:
                imported_cookies = self._account_cookies()
                if imported_cookies:
                    await context.add_cookies(imported_cookies)
                page = context.pages[0] if context.pages else await context.new_page()
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
                video_entry = page.locator('button[aria-label="AI生视频"]:visible').first
                if not await video_entry.count():
                    video_entry = page.get_by_text("AI生视频", exact=True).first
                await video_entry.evaluate("element => element.click()")
                await page.wait_for_timeout(3000)
                if await video_entry.get_attribute("aria-pressed") != "true":
                    await video_entry.evaluate("element => element.click()")
                    await page.wait_for_timeout(2000)
                if await video_entry.get_attribute("aria-pressed") != "true":
                    await self._save_diagnostics(page, "video mode did not activate")
                    return self._failure("qianwen video mode not active", account_fault=False)
                editor = page.locator('[contenteditable="true"][role="textbox"]:visible').first
                await editor.wait_for(state="visible", timeout=15000)
                reference_paths = task_image_paths(self.task_id)
                if self.task_type == "video" and self.model == "HappyHorse 1.0" and not reference_paths:
                    await self._save_diagnostics(page, "HappyHorse requires a reference image")
                    return self._failure("qianwen HappyHorse requires a reference image", account_fault=False, retryable=False)
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
                        await self._save_diagnostics(page, "reference image upload unavailable")
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
                await send_button.click(force=True)
                try:
                    await asyncio.wait_for(self.submission_request_event.wait(), timeout=3)
                except asyncio.TimeoutError:
                    await editor.press("Enter")
                try:
                    await asyncio.wait_for(self.submission_event.wait(), timeout=25)
                except asyncio.TimeoutError:
                    pass
                await page.wait_for_timeout(2000)
                if self.remote_error:
                    reasons = {"login": "qianwen account not logged in", "rate_limit": "qianwen rate limited", "risk_control": "qianwen risk control", "model_unavailable": "qianwen model unavailable"}
                    reason = reasons[self.remote_error]
                    await self._save_diagnostics(page, reason)
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
                        "qianwen_attachment_ids": list(self.remote_submission.get("attachment_ids") or []),
                        "qianwen_setting_mismatch": setting_mismatch,
                        "qianwen_remote_task_ids": self.remote_task_ids,
                        "qianwen_network_events": self.network_events[-10:],
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
                if page is not None and request_handler is not None:
                    page.remove_listener("request", request_handler)
                if page is not None and response_handler is not None:
                    page.remove_listener("response", response_handler)
                await cancel_tracked_tasks(response_tasks)
                await safe_close(context)
