from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from .accounts import (
    claim_account_for_maintenance,
    clear_account_current_task,
    disable_account_for_login,
    list_accounts,
    merge_account_cookies,
)
from .browser_runtime import resolve_browser_executable
from .config import QIANWEN_PROFILES_DIR, load_settings
from .profile_lock import account_profile_lock
from .qianwen_ai_studio import QianwenAIStudioAutomation, acquire_qianwen_ai_studio_credit_proxy
from .qianwen_automation import QIANWEN_URL, QianwenVideoAutomation
from .proxy_manager import release_dola_subscription_proxy


LOCAL_TZ = timezone(timedelta(hours=8))
_START_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _now() -> datetime:
    return datetime.now(LOCAL_TZ)


class QianwenProbeManager:
    """Low-priority account health and optional AI Studio credit checker."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._run_task: asyncio.Task | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._last_scheduled_date = ""
        self._last_run_at = 0.0
        self._status: dict[str, Any] = {
            "running": False,
            "total": 0,
            "completed": 0,
            "success": 0,
            "failed": 0,
            "credit_failed": 0,
            "skipped": 0,
            "current_account_id": "",
            "current_account_name": "",
            "current_state": "",
            "last_reason": "",
            "last_run_at": "",
            "last_completed_at": "",
            "started_at": "",
        }

    def start_scheduler(self) -> None:
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def stop_scheduler(self) -> None:
        for task in (self._run_task, self._scheduler_task):
            if task and not task.done():
                task.cancel()
        for task in (self._run_task, self._scheduler_task):
            if task:
                await asyncio.gather(task, return_exceptions=True)
        self._run_task = None
        self._scheduler_task = None

    def snapshot(self) -> dict[str, Any]:
        settings = load_settings()
        return {
            **self._status,
            "enabled": settings.qianwen_probe_enabled,
            "collect_credit": settings.qianwen_probe_collect_credit,
            "interval_minutes": settings.qianwen_probe_interval_minutes,
            "daily_start": settings.qianwen_probe_daily_start,
            "active": bool(self._run_task and not self._run_task.done()),
        }

    async def start_now(self, *, collect_credit: bool | None = None) -> bool:
        async with self._lock:
            if self._run_task and not self._run_task.done():
                return False
            self._run_task = asyncio.create_task(
                self._run_once(load_settings().qianwen_probe_collect_credit if collect_credit is None else bool(collect_credit))
            )
            return True

    async def stop_now(self) -> None:
        task = self._run_task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._run_task = None

    async def _scheduler_loop(self) -> None:
        while True:
            settings = load_settings()
            if settings.qianwen_probe_enabled and not self._run_task_active():
                now = _now()
                start = self._parse_start(settings.qianwen_probe_daily_start)
                due_daily = now.hour > start[0] or (now.hour == start[0] and now.minute >= start[1])
                due_interval = self._last_run_at <= 0 or time.monotonic() - self._last_run_at >= settings.qianwen_probe_interval_minutes * 60
                if due_daily and (self._last_scheduled_date != now.date().isoformat() or due_interval):
                    self._last_scheduled_date = now.date().isoformat()
                    await self.start_now(collect_credit=settings.qianwen_probe_collect_credit)
            await asyncio.sleep(15)

    def _run_task_active(self) -> bool:
        return bool(self._run_task and not self._run_task.done())

    @staticmethod
    def _parse_start(value: str) -> tuple[int, int]:
        match = _START_PATTERN.match(str(value or "00:00"))
        return (int(match.group(1)), int(match.group(2))) if match else (0, 0)

    async def _run_once(self, collect_credit: bool) -> None:
        started = _now().isoformat()
        account_rows = await asyncio.to_thread(list_accounts, platform="qianwen")
        accounts = [
            item for item in account_rows
            if item.get("enabled", True)
            and str(item.get("account_status") or "normal") == "normal"
        ]
        self._status.update(
            running=True,
            total=len(accounts),
            completed=0,
            success=0,
            failed=0,
            credit_failed=0,
            skipped=0,
            current_account_id="",
            current_account_name="",
            current_state="准备测活",
            last_reason="",
            started_at=started,
            last_run_at=started,
        )
        self._last_run_at = time.monotonic()
        processed: set[str] = set()
        try:
            for item in accounts:
                account_id = str(item.get("id") or "")
                if not account_id:
                    continue
                self._status.update(
                    current_account_id=account_id,
                    current_account_name=str(item.get("name") or account_id),
                    current_state="检查登录状态",
                )
                account = await asyncio.to_thread(
                    claim_account_for_maintenance,
                    "qianwen-probe",
                    "qianwen",
                    preferred_id=account_id,
                    exclude_ids=processed,
                )
                if not account:
                    processed.add(account_id)
                    self._status["skipped"] += 1
                    self._status["completed"] += 1
                    continue
                maintenance_id = "maintenance:qianwen-probe"
                processed.add(account_id)
                try:
                    result = await self._probe_account(account, collect_credit)
                    if result.get("ok"):
                        self._status["success"] += 1
                        if result.get("credit_ok") is False:
                            self._status["credit_failed"] += 1
                            self._status["last_reason"] = str(result.get("reason") or "积分同步失败")[:240]
                    else:
                        self._status["failed"] += 1
                        self._status["last_reason"] = str(result.get("reason") or "测活失败")[:240]
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._status["failed"] += 1
                    self._status["last_reason"] = str(exc)[:240]
                finally:
                    await asyncio.to_thread(clear_account_current_task, account_id, maintenance_id)
                self._status["completed"] += 1
        except asyncio.CancelledError:
            self._status["last_reason"] = "管理员停止测活"
            raise
        finally:
            self._status.update(
                running=False,
                current_account_id="",
                current_account_name="",
                current_state="已完成" if self._status["completed"] >= self._status["total"] else "已停止",
                last_completed_at=_now().isoformat(),
            )

    async def _probe_account(self, account: dict[str, Any], collect_credit: bool) -> dict[str, Any]:
        account_id = str(account.get("id") or "")
        profile_path = QIANWEN_PROFILES_DIR / account_id
        lock = await account_profile_lock("qianwen", account_id)
        async with lock:
            login_ok, login_reason, cookies = await self._check_login(account, profile_path)
            if not login_ok:
                if login_reason == "login_invalid":
                    await asyncio.to_thread(disable_account_for_login, account_id, "千问测活确认登录失效")
                return {"ok": False, "reason": "千问登录失效" if login_reason == "login_invalid" else "千问测活网络异常"}
            if cookies:
                await asyncio.to_thread(merge_account_cookies, account_id, cookies)
            if not collect_credit:
                return {"ok": True, "credit_ok": None, "reason": "登录正常"}
            self._status["current_state"] = "领取 AI Studio 积分"
            runner = QianwenAIStudioAutomation(
                f"probe:{account_id}",
                "",
                "16:9",
                "HappyHorse 1.1",
                10,
                account=account,
            )
            credit_cookies = cookies or [dict(item) for item in account.get("cookies") or []]
            credit = await runner._sync_daily_credit(credit_cookies, None, force=True)
            if not credit.get("ok"):
                return {"ok": True, "credit_ok": False, "reason": str(credit.get("reason") or "积分同步失败")}
            return {"ok": True, "credit_ok": True, "reason": f"登录正常，积分 {credit.get('total_amount', 0)}"}

    async def _check_login(self, account: dict[str, Any], profile_path: Path) -> tuple[bool, str, list[dict[str, Any]]]:
        settings = load_settings()
        launch_options: dict[str, Any] = {
            "headless": settings.headless,
            "executable_path": resolve_browser_executable(settings.browser_executable_path),
            "locale": "zh-CN",
            "viewport": {"width": 1365, "height": 900},
            "args": ["--disable-dev-shm-usage", "--no-sandbox", "--disable-blink-features=AutomationControlled"],
        }
        automation = QianwenVideoAutomation(
            f"probe:{account.get('id')}",
            "",
            "16:9",
            "万相 2.7",
            account=account,
        )
        proxy: dict[str, str] | None = None
        try:
            try:
                proxy = await acquire_qianwen_ai_studio_credit_proxy(settings)
                if proxy.get("server"):
                    launch_options["proxy"] = proxy
            except Exception:
                proxy = None
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(str(profile_path), **launch_options)
                try:
                    cookies = automation._account_cookies()
                    if cookies:
                        await context.add_cookies(cookies)
                    page = context.pages[0] if context.pages else await context.new_page()
                    await page.goto("https://www.qianwen.com/", wait_until="commit", timeout=45000)
                    await page.wait_for_timeout(2500)
                    logged_in, login_visible = await automation._login_state(page, context)
                    refreshed = await context.cookies(["https://www.qianwen.com/", QIANWEN_URL])
                    if logged_in:
                        return True, "", refreshed
                    if login_visible:
                        return False, "login_invalid", refreshed
                    return False, "network", refreshed
                finally:
                    await context.close()
        except Exception:
            return False, "network", []
        finally:
            if proxy is not None:
                try:
                    await release_dola_subscription_proxy(proxy)
                except Exception:
                    pass
