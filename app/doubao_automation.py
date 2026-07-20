from __future__ import annotations

import asyncio
import re
from typing import Any

from playwright.async_api import async_playwright

from .accounts import disable_account_for_login, set_account_cooldown, update_account_cookies
from .browser_runtime import cancel_tracked_tasks, create_tracked_task, resolve_browser_executable, safe_close
from .config import DOUBAO_PROFILES_DIR, DOUBAO_STATES_DIR, ensure_dirs, load_settings
from .store import begin_task_submission, clear_transient_result, mark_pending, mark_submitted, mark_success, save_result, task_exists
from .profile_lock import account_profile_lock


DOUBAO_URL = "https://www.doubao.com/chat/"
VIDEO_URL_RE = re.compile(r'https?://[^"\\\s]+(?:mime_type=video_mp4|\.mp4(?:\?[^"\\\s]*)?)', re.IGNORECASE)


class DoubaoVideoAutomation:
    def __init__(self, task_id: str, prompt: str, ratio: str, model: str, account: dict[str, Any] | None = None):
        self.task_id = task_id
        self.prompt = prompt
        self.ratio = ratio
        self.model = model
        self.account = account or {}
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
            reason = str(exc)[:500]
            if task_exists(self.task_id):
                mark_pending(self.task_id, reason)
            return {"success": False, "retryable": True, "reason": reason}

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
                await page.goto(DOUBAO_URL, wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(5000)
                body = await page.locator("body").inner_text()
                user_visible = await page.get_by_text(re.compile(r"^用户\d+$")).count()
                if "登录" in body[:1500] and not user_visible:
                    cookies = [dict(item) for item in self.account.get("cookies") or [] if isinstance(item, dict) and item.get("name")]
                    if cookies:
                        await context.add_cookies(cookies)
                        await page.reload(wait_until="domcontentloaded", timeout=90000)
                        await page.wait_for_timeout(5000)
                        body = await page.locator("body").inner_text()
                        user_visible = await page.get_by_text(re.compile(r"^用户\d+$")).count()
                    if "登录" in body[:1500] and not user_visible:
                        disable_account_for_login(str(self.account.get("id") or ""), "豆包登录状态失效，请重新导入 Cookie")
                        return {"success": False, "retryable": True, "reason": "doubao account not logged in"}
                await self._refresh_cookies(context)
                await page.get_by_text("视频生成", exact=True).click(timeout=15000)
                await page.wait_for_timeout(2000)
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
                completion_result: dict[str, Any] = {"done": False, "error": "", "video_url": "", "accepted": False}

                async def capture_completion(response) -> None:
                    if "/chat/completion" not in response.url:
                        return
                    try:
                        text = await response.text()
                    except Exception:
                        return
                    completion_result["done"] = True
                    if "710022002" in text:
                        completion_result["error"] = "doubao service frequent"
                        set_account_cooldown(str(self.account.get("id") or ""), 1800, "豆包当前服务访问频繁")
                        return
                    if "710022004" in text or '"type":"verify"' in text or '"verify_scene":"doubao_message_web"' in text:
                        completion_result["error"] = "doubao verification required"
                        set_account_cooldown(str(self.account.get("id") or ""), 86400, "豆包触发网页人机验证，请在固定 Profile 中人工验证")
                        return
                    if "STREAM_ERROR" in text:
                        completion_result["error"] = "doubao submit rejected"
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
                    return {"success": False, "retryable": False, "reason": "task canceled before submission"}
                await editor.press("Enter")
                mark_submitted(self.task_id)
                submit_deadline = asyncio.get_running_loop().time() + 30
                while not completion_result["done"] and asyncio.get_running_loop().time() < submit_deadline:
                    await page.wait_for_timeout(500)
                await self._refresh_cookies(context)
                if completion_result["error"]:
                    return {"success": False, "retryable": True, "submitted": True, "reason": str(completion_result["error"])}
                if not completion_result["done"]:
                    return {"success": False, "retryable": True, "submitted": True, "reason": "doubao submit not confirmed"}
                if not completion_result["accepted"] and not completion_result["video_url"]:
                    return {"success": False, "retryable": True, "submitted": True, "reason": "doubao submit not accepted"}
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
