from __future__ import annotations

import asyncio
import re
from typing import Any

from playwright.async_api import async_playwright

from .accounts import disable_account_for_login, set_account_cooldown, update_account_cookies
from .browser_runtime import cancel_tracked_tasks, create_tracked_task, resolve_browser_executable, safe_close
from .config import DOUBAO_PROFILES_DIR, DOUBAO_STATES_DIR, ensure_dirs, load_settings
from .store import begin_task_submission, clear_transient_result, is_task_canceled, mark_pending, mark_submitted, mark_success, release_task_submission, save_result, task_exists
from .profile_lock import account_profile_lock


DOUBAO_URL = "https://www.doubao.com/chat/"
VIDEO_URL_RE = re.compile(r'https?://[^"\\\s]+(?:mime_type=video_mp4|\.mp4(?:\?[^"\\\s]*)?)', re.IGNORECASE)
REGION_RESTRICTION_MARKERS = (
    "doubao-region-ban",
    "当前地区暂不支持",
    "所在地区暂不支持",
    "not available in your region",
    "region is not supported",
)
VIDEO_ENTRY_NAMES = ("视频生成", "生成视频", "AI 视频", "AI视频")


class DoubaoVideoAutomation:
    def __init__(
        self,
        task_id: str,
        prompt: str,
        ratio: str,
        model: str,
        account: dict[str, Any] | None = None,
        proxy_session: Any | None = None,
    ):
        self.task_id = task_id
        self.prompt = prompt
        self.ratio = ratio
        self.model = model
        self.account = account or {}
        self.proxy_session = proxy_session
        self.settings = load_settings()
        ensure_dirs()
        self.state_path = DOUBAO_STATES_DIR / f"{str(self.account.get('id') or 'unknown')}.json"
        self.profile_path = DOUBAO_PROFILES_DIR / str(self.account.get("id") or "unknown")

    async def _refresh_cookies(self, context) -> None:
        account_id = str(self.account.get("id") or "")
        if not account_id:
            return
        cookies = await context.cookies(["https://www.doubao.com"])
        if cookies:
            update_account_cookies(account_id, cookies)
        await context.storage_state(path=str(self.state_path))

    async def run(self) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(self._run_once(), timeout=max(self.settings.task_timeout_seconds, 600))
        except asyncio.TimeoutError:
            if task_exists(self.task_id):
                mark_pending(self.task_id, "doubao browser timeout")
            return {"success": False, "retryable": True, "reason": "doubao browser timeout"}
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
            return {"success": False, "retryable": True, "reason": "no doubao account available"}
        lock = await account_profile_lock("doubao", str(self.account.get("id") or ""))
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

    @staticmethod
    def _is_region_restricted(page_url: str, body: str) -> bool:
        haystack = f"{page_url}\n{body[:3000]}".lower()
        return any(marker.lower() in haystack for marker in REGION_RESTRICTION_MARKERS)

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

    @staticmethod
    async def _click_first_visible(locators: list[Any]) -> bool:
        for locator in locators:
            try:
                count = min(await locator.count(), 5)
                for index in range(count):
                    candidate = locator.nth(index)
                    if await candidate.is_visible():
                        await candidate.click(timeout=10000)
                        return True
            except Exception:
                continue
        return False

    async def _open_video_generation(self, page, body: str) -> bool:
        if "/video" in str(page.url).lower() and await page.locator('[contenteditable="true"][role="textbox"]').count():
            return True
        name_pattern = re.compile(r"^(?:视频生成|生成视频|AI\s*视频)$", re.IGNORECASE)
        entry_locators = [
            page.get_by_role("link", name=name_pattern),
            page.get_by_role("button", name=name_pattern),
            page.get_by_text(name_pattern),
            page.locator('a[href*="video"],button[data-testid*="video" i]'),
        ]
        if await self._click_first_visible(entry_locators):
            await page.wait_for_timeout(2000)
            return True
        creation_entry = page.get_by_text(re.compile(r"^(?:AI\s*创作|创作中心)$", re.IGNORECASE))
        if await self._click_first_visible([creation_entry]):
            await page.wait_for_timeout(1500)
            if await self._click_first_visible(entry_locators):
                await page.wait_for_timeout(2000)
                return True
        editor_exists = bool(await page.locator('[contenteditable="true"][role="textbox"]').count())
        return editor_exists and any(marker in body for marker in (*VIDEO_ENTRY_NAMES, "Seedance"))

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
            context = await playwright.chromium.launch_persistent_context(
                str(self.profile_path),
                **launch_options,
            )
            page = None
            response_handler = None
            response_tasks: set[asyncio.Task[Any]] = set()
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                cookies = [dict(item) for item in self.account.get("cookies") or [] if isinstance(item, dict) and item.get("name")]
                if cookies:
                    await context.add_cookies(cookies)
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
                        "infrastructure_fault": True,
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
                if not await self._open_video_generation(page, body):
                    await self._record_diagnostic(page, "doubao_video_entry_missing", body)
                    return {
                        "success": False,
                        "retryable": True,
                        "reason": f"doubao video entry unavailable at {str(page.url)[:300]}",
                        "infrastructure_fault": True,
                    }
                if self.model != "Seedance 2.0 Mini":
                    model_button = page.get_by_role("button", name=re.compile(r"Mini|Fast|Pro|Seedance|\d+\.\d+", re.IGNORECASE)).first
                    if await model_button.count():
                        await model_button.click(force=True)
                        option = page.get_by_text(self.model, exact=True)
                        if not await option.count():
                            option = page.get_by_text(self.model.removeprefix("Seedance "), exact=True)
                        if await option.count():
                            await option.last.click(force=True)
                        else:
                            return {"success": False, "retryable": False, "reason": "doubao model unavailable"}
                completion_result: dict[str, Any] = {
                    "done": False,
                    "error": "",
                    "error_category": "",
                    "video_url": "",
                    "accepted": False,
                    "response_preview": "",
                }

                async def capture_completion(response) -> None:
                    if "/chat/completion" not in response.url:
                        return
                    try:
                        text = await response.text()
                    except Exception:
                        return
                    completion_result["done"] = True
                    completion_result["response_preview"] = re.sub(r"\s+", " ", text)[:1200]
                    if "710022002" in text:
                        completion_result["error"] = "doubao service frequent"
                        completion_result["error_category"] = "service_frequent"
                        set_account_cooldown(str(self.account.get("id") or ""), 1800, "豆包当前服务访问频繁")
                        return
                    if "710022004" in text or '"type":"verify"' in text or '"verify_scene":"doubao_message_web"' in text:
                        completion_result["error"] = "doubao verification required"
                        completion_result["error_category"] = "slider_verification"
                        set_account_cooldown(str(self.account.get("id") or ""), 86400, "豆包触发网页人机验证，请在固定 Profile 中人工验证")
                        return
                    if "STREAM_ERROR" in text:
                        completion_result["error"] = "doubao submit rejected"
                        completion_result["error_category"] = "submit_rejected"
                        set_account_cooldown(str(self.account.get("id") or ""), 1800, "豆包提交被拒绝")
                        return
                    if "SSE_REPLY_END" in text and "STREAM_ERROR" not in text:
                        completion_result["accepted"] = True
                    match = VIDEO_URL_RE.search(text.replace("\\u0026", "&").replace("\\/", "/"))
                    if match:
                        completion_result["video_url"] = match.group(0)

                response_handler = lambda response: create_tracked_task(response_tasks, capture_completion(response))
                page.on("response", response_handler)
                editor = page.locator('[contenteditable="true"][role="textbox"]').first
                await editor.click()
                await editor.fill(self.prompt)
                if self.ratio:
                    ratio_button = page.get_by_role("button", name="比例")
                    if await ratio_button.count():
                        await ratio_button.click()
                        option = page.get_by_text(self.ratio, exact=True)
                        if await option.count():
                            await option.last.click()
                if not begin_task_submission(self.task_id):
                    canceled = is_task_canceled(self.task_id)
                    return {"success": False, "retryable": not canceled, "reason": "用户取消生成" if canceled else "任务提交状态已变化，正在重试"}
                await editor.press("Enter")
                submit_deadline = asyncio.get_running_loop().time() + 30
                while not completion_result["done"] and asyncio.get_running_loop().time() < submit_deadline:
                    await page.wait_for_timeout(500)
                await self._refresh_cookies(context)
                if completion_result["error"]:
                    release_task_submission(self.task_id)
                    category = str(completion_result["error_category"])
                    save_result(self.task_id, extra={
                        "doubao_submit_error_category": category,
                        "doubao_submission_response_preview": str(completion_result["response_preview"]),
                    })
                    if category == "service_frequent":
                        if self.proxy_session is not None:
                            self.proxy_session.mark_browser_proxy_unavailable(reason="doubao_service_frequent")
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": str(completion_result["error"]),
                            "infrastructure_fault": True,
                        }
                    if category == "slider_verification":
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": str(completion_result["error"]),
                            "account_fault": True,
                            "account_slider_verification": True,
                            "switch_account": True,
                        }
                    return {"success": False, "retryable": True, "reason": str(completion_result["error"])}
                if not completion_result["done"]:
                    release_task_submission(self.task_id)
                    save_result(self.task_id, extra={"doubao_submit_error_category": "confirmation_timeout"})
                    return {"success": False, "retryable": True, "reason": "doubao submit not confirmed"}
                if not completion_result["accepted"] and not completion_result["video_url"]:
                    release_task_submission(self.task_id)
                    return {"success": False, "retryable": True, "reason": "doubao submit not accepted"}
                mark_submitted(self.task_id)
                save_result(
                    self.task_id,
                    extra={
                        "platform": "doubao",
                        "model": self.model,
                        "account_id": str(self.account.get("id") or ""),
                        "account_name": str(self.account.get("name") or ""),
                        "account_quota_charge_id": str(self.account.get("quota_charge_id") or ""),
                        "doubao_page_url": page.url,
                        "doubao_submit_confirmed": bool(completion_result["accepted"]),
                    },
                )
                deadline = asyncio.get_running_loop().time() + 240
                while asyncio.get_running_loop().time() < deadline:
                    if completion_result["error"]:
                        await self._refresh_cookies(context)
                        return {"success": False, "retryable": True, "reason": str(completion_result["error"])}
                    if completion_result["video_url"]:
                        url = str(completion_result["video_url"])
                        await self._refresh_cookies(context)
                        save_result(self.task_id, extra={"decoded_main_url": url, "doubao_page_url": page.url})
                        mark_success(self.task_id)
                        return {"success": True, "retryable": False, "reason": ""}
                    videos = page.locator("video")
                    count = await videos.count()
                    for index in range(count):
                        src = str(await videos.nth(index).get_attribute("src") or "")
                        if src.startswith("http"):
                            await self._refresh_cookies(context)
                            save_result(self.task_id, extra={"decoded_main_url": src, "doubao_page_url": page.url})
                            mark_success(self.task_id)
                            return {"success": True, "retryable": False, "reason": ""}
                    links = await page.locator('a[href*="video"],a[href$=".mp4"],a[download]').evaluate_all("els => els.map(e => e.href).filter(Boolean)")
                    for url in links:
                        if str(url).startswith("http"):
                            await self._refresh_cookies(context)
                            save_result(self.task_id, extra={"decoded_main_url": str(url), "doubao_page_url": page.url})
                            mark_success(self.task_id)
                            return {"success": True, "retryable": False, "reason": ""}
                    text = await page.locator("body").inner_text()
                    if any(marker in text[-1500:] for marker in ("生成失败", "无法生成", "内容违规")):
                        return {"success": False, "retryable": True, "reason": "doubao generation failed"}
                    await page.wait_for_timeout(10000)
                await self._refresh_cookies(context)
                return {"success": False, "retryable": True, "reason": "doubao video result timeout"}
            finally:
                if page is not None and response_handler is not None:
                    page.remove_listener("response", response_handler)
                await cancel_tracked_tasks(response_tasks)
                await safe_close(context)
