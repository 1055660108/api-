from __future__ import annotations

import re

from playwright.async_api import async_playwright

from .accounts import claim_account_for_maintenance, clear_account_current_task, update_account_cookies
from .browser_runtime import resolve_browser_executable
from .config import DOUBAO_PROFILES_DIR, TARGET_URL, load_settings
from .profile_lock import account_profile_lock


async def fetch_platform_video_models(platform: str) -> list[str]:
    account = claim_account_for_maintenance(f"{platform}-model-sync", platform)
    if not account:
        raise RuntimeError(f"no {platform} account available")
    maintenance_id = f"maintenance:{platform}-model-sync"
    try:
        if platform == "doubao":
            return await _fetch_doubao(account)
        if platform == "dola":
            return await _fetch_dola(account)
        raise RuntimeError("unsupported platform")
    finally:
        clear_account_current_task(str(account["id"]), maintenance_id)


async def _fetch_doubao(account: dict) -> list[str]:
    settings = load_settings()
    lock = await account_profile_lock("doubao", str(account["id"]))
    async with lock, async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(str(DOUBAO_PROFILES_DIR / str(account["id"])), headless=settings.headless, executable_path=resolve_browser_executable(settings.browser_executable_path), locale="zh-CN", viewport={"width": 1365, "height": 900}, args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-blink-features=AutomationControlled"])
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.doubao.com/chat/", wait_until="commit", timeout=90000)
            await page.wait_for_timeout(10000)
            body = await page.evaluate("document.body?.innerText || ''")
            if "登录" in body[:1500] and not await page.get_by_text(re.compile(r"^用户\d+$")).count():
                await context.add_cookies([dict(item) for item in account.get("cookies") or []])
                await page.reload(wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(5000)
            await page.get_by_text("视频生成", exact=True).click(force=True)
            await page.wait_for_timeout(1500)
            selectors = page.get_by_role("button", name=re.compile(r"Mini|Fast|Pro|Seedance|\d+\.\d+", re.IGNORECASE))
            for index in range(await selectors.count()):
                try:
                    await selectors.nth(index).click(force=True)
                    await page.wait_for_timeout(500)
                    break
                except Exception:
                    continue
            text = await page.evaluate("document.body?.innerText || ''")
            models = []
            for value in re.findall(r"(?:Seedance\s*)?\d+\.\d+(?:\s*(?:Mini|Fast|Pro))?", text, re.IGNORECASE):
                name = value.strip()
                if name not in models:
                    models.append(name)
            update_account_cookies(str(account["id"]), await context.cookies(["https://www.doubao.com"]))
            models = [name if name.lower().startswith("seedance") else f"Seedance {name}" for name in models]
            if "视频生成" in text:
                for fallback in ("Seedance 2.0 Mini", "Seedance 2.0 Fast"):
                    if fallback not in models:
                        models.append(fallback)
            return models
        finally:
            await context.close()


async def _fetch_dola(account: dict) -> list[str]:
    settings = load_settings()
    lock = await account_profile_lock("dola", str(account["id"]))
    async with lock, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.headless, executable_path=resolve_browser_executable(settings.browser_executable_path), args=["--disable-dev-shm-usage", "--no-sandbox"])
        context = await browser.new_context(locale="zh-CN", viewport={"width": 1365, "height": 900})
        try:
            await context.add_cookies([dict(item) for item in account.get("cookies") or []])
            page = await context.new_page()
            await page.goto(TARGET_URL, wait_until="commit", timeout=90000)
            await page.wait_for_timeout(12000)
            text = await page.evaluate("document.body?.innerText || ''")
            models = []
            for value in re.findall(r"Seedance\s*\d+(?:\.\d+)+(?:\s*(?:Mini|Pro))?", text, re.IGNORECASE):
                name = re.sub(r"\s+", " ", value).strip()
                if name not in models:
                    models.append(name)
            update_account_cookies(str(account["id"]), await context.cookies())
            return models or (["Seedance 2.0"] if "视频" in text or "Seedance" in text else [])
        finally:
            await context.close()
            await browser.close()
