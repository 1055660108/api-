from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from playwright.async_api import async_playwright

from .accounts import disable_account_for_login, update_account_cookies
from .browser_runtime import cancel_tracked_tasks, create_tracked_task, resolve_browser_executable, safe_close
from .config import QIANWEN_PROFILES_DIR, TASKS_DIR, ensure_dirs, load_settings
from .store import begin_task_submission, clear_transient_result, mark_pending, mark_submitted, mark_success, save_result, task_exists
from .profile_lock import account_profile_lock


QIANWEN_URL = "https://www.qianwen.com/"
VIDEO_URL_RE = re.compile(r'https?://[^"\\\s]+\.mp4(?:\?[^"\\\s]*)?', re.IGNORECASE)
MEDIA_URL_RE = re.compile(r'https?://[^"\\\s]+(?:\.mp4|mime_type=video|video_mp4|\.m3u8)(?:\?[^"\\\s]*)?', re.IGNORECASE)
TASK_KEY_RE = re.compile(r"(?:task|job|request|aigc|generation|message)[_-]?id", re.IGNORECASE)


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
    def __init__(self, task_id: str, prompt: str, ratio: str, model: str, task_type: str = "video", account: dict[str, Any] | None = None):
        self.task_id = task_id
        self.prompt = prompt
        self.ratio = ratio
        self.model = model
        self.task_type = "image" if task_type == "image" or model == "AI生图" else "video"
        self.account = account or {}
        self.settings = load_settings()
        ensure_dirs()
        self.profile_path = QIANWEN_PROFILES_DIR / str(self.account.get("id") or "unknown")
        self.network_events: list[dict[str, Any]] = []
        self.remote_task_ids: list[str] = []
        self.remote_video_urls: list[str] = []
        self.remote_video_scores: dict[str, int] = {}
        self.first_video_candidate_at = 0.0
        self.remote_error = ""

    async def _refresh_cookies(self, context) -> None:
        account_id = str(self.account.get("id") or "")
        if not account_id:
            return
        cookies = await context.cookies([QIANWEN_URL])
        if cookies:
            update_account_cookies(account_id, cookies)

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
        relevant = self.prompt in post_data or any(item in lowered_url for item in ("/chat", "video", "wanx", "aigc", "generate", "task", "completion"))
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
            return {"success": False, "retryable": True, "reason": reason}

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
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(self.profile_path),
                headless=self.settings.headless,
                executable_path=resolve_browser_executable(self.settings.browser_executable_path),
                locale="zh-CN",
                viewport={"width": 1365, "height": 900},
                args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            page = None
            response_handler = None
            response_tasks: set[asyncio.Task[Any]] = set()
            try:
                page = context.pages[0] if context.pages else await context.new_page()
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
                    cookies = []
                    for item in self.account.get("cookies") or []:
                        if isinstance(item, dict) and item.get("name"):
                            cookie = dict(item)
                            cookie["domain"] = ".qianwen.com"
                            cookies.append(cookie)
                    if cookies:
                        await context.add_cookies(cookies)
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
                if self.task_type == "video" and self.model and self.model != "万相 2.7":
                    model_button = page.get_by_role("button", name=re.compile(r"万相|Wan", re.IGNORECASE)).first
                    if await model_button.count():
                        await model_button.click()
                        option = page.get_by_text(self.model, exact=True)
                        if await option.count():
                            await option.last.click()
                        else:
                            await self._save_diagnostics(page, "model unavailable")
                            return self._failure("qianwen model unavailable", account_fault=False, retryable=False)
                self.network_events.clear()
                self.remote_task_ids.clear()
                self.remote_video_urls.clear()
                self.remote_video_scores.clear()
                self.first_video_candidate_at = 0.0
                self.remote_error = ""
                initial_image_urls: set[str] = set()
                if self.task_type == "image":
                    initial_image_urls = set(await page.locator("img").evaluate_all("items => items.map(item => item.src).filter(Boolean)"))
                await editor.fill(self.prompt)
                send_button = page.locator('button[aria-label="发送消息"]:visible').first
                await send_button.wait_for(state="visible", timeout=15000)
                if not begin_task_submission(self.task_id):
                    return {"success": False, "retryable": False, "reason": "task canceled before submission"}
                await send_button.click(force=True)
                mark_submitted(self.task_id)
                await page.wait_for_timeout(8000)
                await self._refresh_cookies(context)
                body = await page.locator("body").inner_text()
                prompt_still_visible = await page.get_by_text(self.prompt, exact=True).count()
                if self.remote_error:
                    reasons = {"login": "qianwen account not logged in", "rate_limit": "qianwen rate limited", "risk_control": "qianwen risk control", "model_unavailable": "qianwen model unavailable"}
                    reason = reasons[self.remote_error]
                    await self._save_diagnostics(page, reason)
                    return self._failure(reason, account_fault=self.remote_error == "login", retryable=self.remote_error != "model_unavailable")
                video_request = next((item for item in reversed(self.network_events) if self.prompt in str(item.get("post_data") or "")), None)
                if video_request:
                    try:
                        payload = json.loads(str(video_request.get("post_data") or "{}"))
                    except Exception:
                        payload = {}
                    biz_data = str(payload.get("biz_data") or "")
                    is_video_request = str(payload.get("ai_tool_scene") or "") == "zaodian_generate_video" or '"bizScene":"genVideo"' in biz_data or '"genMode":"vid_gen"' in biz_data
                    if not is_video_request:
                        await self._save_diagnostics(page, "video mode not active: ordinary chat request")
                        return self._failure("qianwen video mode not active", account_fault=False)
                request_confirmed = bool(self.remote_task_ids or video_request)
                if self.task_type == "video" and "正在为你生成视频" not in body and "正在排队中" not in body and not prompt_still_visible and not request_confirmed:
                    await self._save_diagnostics(page, "submit not confirmed")
                    return self._failure("qianwen submit not confirmed", account_fault=False)
                save_result(
                    self.task_id,
                    extra={
                        "platform": "qianwen",
                        "model": self.model,
                        "task_type": self.task_type,
                        "account_id": str(self.account.get("id") or ""),
                        "account_name": str(self.account.get("name") or ""),
                        "account_quota_charge_id": str(self.account.get("quota_charge_id") or ""),
                        "qianwen_page_url": page.url,
                        "qianwen_submit_confirmed": True,
                        "qianwen_remote_task_ids": self.remote_task_ids,
                        "qianwen_network_events": self.network_events[-10:],
                    },
                )
                deadline = asyncio.get_running_loop().time() + 1800
                while asyncio.get_running_loop().time() < deadline:
                    if self.remote_video_urls:
                        url = best_qianwen_video_url(self.remote_video_scores)
                        score = self.remote_video_scores.get(url, 0)
                        if score >= 200 or time.monotonic() - self.first_video_candidate_at >= 8:
                            await self._refresh_cookies(context)
                            save_result(self.task_id, extra={"decoded_main_url": url, "qianwen_remote_task_ids": self.remote_task_ids, "qianwen_page_url": page.url, "qianwen_video_url_score": score})
                            mark_success(self.task_id)
                            return {"success": True, "retryable": False, "reason": ""}
                    if self.task_type == "image":
                        current_image_urls = await page.locator("img").evaluate_all("items => items.map(item => item.src).filter(Boolean)")
                        candidates = [str(src) for src in current_image_urls if str(src).startswith("http") and str(src) not in initial_image_urls]
                        if candidates:
                            url = candidates[-1]
                            await self._refresh_cookies(context)
                            save_result(self.task_id, extra={"decoded_main_url": url, "image_urls": candidates[-4:], "qianwen_page_url": page.url})
                            mark_success(self.task_id)
                            return {"success": True, "retryable": False, "reason": ""}
                    videos = page.locator("video")
                    video_sources = [str(await videos.nth(index).get_attribute("src") or "") for index in range(await videos.count())]
                    video_sources = [src for src in video_sources if src.startswith("http")]
                    if video_sources:
                        src = best_qianwen_video_url(video_sources)
                        src_score = qianwen_video_url_score(src, "video_element")
                        if src_score >= 140:
                            await self._refresh_cookies(context)
                            save_result(self.task_id, extra={"decoded_main_url": src, "qianwen_page_url": page.url, "qianwen_video_url_score": src_score})
                            mark_success(self.task_id)
                            return {"success": True, "retryable": False, "reason": ""}
                    html = await page.content()
                    matches = VIDEO_URL_RE.findall(html.replace("\\u0026", "&").replace("\\/", "/"))
                    if matches:
                        url = best_qianwen_video_url(matches)
                        url_score = qianwen_video_url_score(url, "page_html")
                        if url_score >= 140:
                            await self._refresh_cookies(context)
                            save_result(self.task_id, extra={"decoded_main_url": url, "qianwen_page_url": page.url, "qianwen_video_url_score": url_score})
                            mark_success(self.task_id)
                            return {"success": True, "retryable": False, "reason": ""}
                    body = await page.locator("body").inner_text()
                    if any(marker in body[-1800:] for marker in ("生成失败", "生成遇到问题", "内容违规")):
                        await self._save_diagnostics(page, "generation failed")
                        return self._failure("qianwen generation failed", account_fault=False)
                    await page.wait_for_timeout(10000)
                await self._refresh_cookies(context)
                await self._save_diagnostics(page, f"{self.task_type} result timeout")
                return {"success": False, "retryable": True, "reason": f"qianwen {self.task_type} result timeout", "account_fault": False, "submitted": True}
            finally:
                if page is not None and response_handler is not None:
                    page.remove_listener("response", response_handler)
                await cancel_tracked_tasks(response_tasks)
                await safe_close(context)
