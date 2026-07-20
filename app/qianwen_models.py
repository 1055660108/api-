from __future__ import annotations

import re
from typing import Any

from playwright.async_api import async_playwright

from .accounts import claim_account_for_maintenance, clear_account_current_task, update_account_cookies
from .browser_runtime import resolve_browser_executable
from .config import QIANWEN_PROFILES_DIR, load_settings
from .profile_lock import account_profile_lock


QIANWEN_URL = "https://www.qianwen.com/"
MODEL_RE = re.compile(r"^(?:万相\s*[\d.]+|HappyHorse\s*[\d.]+)$", re.IGNORECASE)


async def fetch_qianwen_video_models() -> list[str]:
    account = claim_account_for_maintenance("qianwen-model-sync", "qianwen")
    if not account:
        raise RuntimeError("no qianwen account available")
    settings = load_settings()
    lock = await account_profile_lock("qianwen", str(account["id"]))
    try:
        async with lock, async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(str(QIANWEN_PROFILES_DIR / str(account["id"])), headless=settings.headless, executable_path=resolve_browser_executable(settings.browser_executable_path), locale="zh-CN", viewport={"width": 1365, "height": 900}, args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-blink-features=AutomationControlled"])
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(QIANWEN_URL, wait_until="commit", timeout=90000)
                await page.wait_for_timeout(10000)
                body = await page.evaluate("document.body?.innerText || ''")
                if "登录" in body[:1200]:
                    cookies = []
                    for item in account.get("cookies") or []:
                        cookie = dict(item)
                        cookie["domain"] = ".qianwen.com"
                        cookies.append(cookie)
                    await context.add_cookies(cookies)
                    await page.reload(wait_until="commit", timeout=90000)
                    await page.wait_for_timeout(8000)
                await page.get_by_text("AI生视频", exact=True).first.click(force=True)
                await page.wait_for_timeout(1500)
                model_button = page.get_by_role("button", name=re.compile(r"万相|Wan|HappyHorse", re.IGNORECASE)).first
                if await model_button.count():
                    button_text = (await model_button.inner_text()).strip()
                    await model_button.click(force=True)
                    await page.wait_for_timeout(1000)
                else:
                    button_text = ""
                texts = await page.evaluate("document.body?.innerText || ''")
                models: list[str] = []
                if MODEL_RE.fullmatch(button_text):
                    models.append(button_text)
                for line in texts.splitlines():
                    value = line.strip()
                    if MODEL_RE.fullmatch(value) and value not in models:
                        models.append(value)
                update_account_cookies(str(account["id"]), await context.cookies([QIANWEN_URL]))
                return models
            finally:
                await context.close()
    finally:
        clear_account_current_task(str(account["id"]), "maintenance:qianwen-model-sync")
