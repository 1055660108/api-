from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Coroutine

from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, Playwright, async_playwright

from .config import APP_ROOT


BROWSER_POOL_PROCESSES = 12
BROWSER_CONTEXTS_PER_PROCESS = 3
BROWSER_SUBMISSION_CONCURRENCY = BROWSER_POOL_PROCESSES * BROWSER_CONTEXTS_PER_PROCESS
BROWSER_RECYCLE_TASKS = 80
BROWSER_RECYCLE_SECONDS = 30 * 60
BROWSER_CONTEXT_CLOSE_TIMEOUT_SECONDS = 8.0
BROWSER_PROCESS_CLOSE_TIMEOUT_SECONDS = 5.0


@dataclass(eq=False)
class _BrowserSlot:
    key: str
    browser: Browser | None = None
    active: int = 0
    completed: int = 0
    created_at: float = 0.0
    launching: bool = True
    retiring: bool = False
    retire_requested: bool = False
    closing: int = 0


class BrowserContextLease:
    def __init__(self, pool: "ReusableBrowserPool", slot: _BrowserSlot, context: BrowserContext):
        self.pool = pool
        self.slot = slot
        self.browser = slot.browser
        self.context = context
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self.pool.release(self.slot, self.context)


class ReusableBrowserPool:
    def __init__(self, max_processes: int = BROWSER_POOL_PROCESSES, contexts_per_process: int = BROWSER_CONTEXTS_PER_PROCESS):
        self.max_processes = max(1, int(max_processes))
        self.contexts_per_process = max(1, int(contexts_per_process))
        self.capacity = self.max_processes * self.contexts_per_process
        self._playwright: Playwright | None = None
        self._slots: list[_BrowserSlot] = []
        self._condition = asyncio.Condition()
        self._start_lock = asyncio.Lock()
        self._stopping = False

    async def start(self) -> None:
        async with self._start_lock:
            if self._playwright is not None:
                return
            self._stopping = False
            self._playwright = await async_playwright().start()

    @asynccontextmanager
    async def playwright_context(self) -> AsyncIterator[Playwright]:
        await self.start()
        if self._playwright is None:
            raise RuntimeError("browser pool is not available")
        yield self._playwright

    @staticmethod
    def _browser_connected(slot: _BrowserSlot) -> bool:
        try:
            return bool(slot.browser and slot.browser.is_connected())
        except Exception:
            return False

    @staticmethod
    def _slot_key(executable_path: str | None, headless: bool, proxy: dict[str, str] | None) -> str:
        return json.dumps(
            {"executable_path": str(executable_path or ""), "headless": bool(headless), "proxy": proxy or {}},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    async def acquire_context(
        self,
        *,
        executable_path: str | None,
        headless: bool,
        proxy: dict[str, str] | None,
        browser_args: list[str],
        context_options: dict[str, Any],
    ) -> BrowserContextLease:
        await self.start()
        key = self._slot_key(executable_path, headless, proxy)
        while True:
            retired: _BrowserSlot | None = None
            launch_slot: _BrowserSlot | None = None
            selected: _BrowserSlot | None = None
            async with self._condition:
                if self._stopping:
                    raise RuntimeError("browser pool is stopping")
                for slot in list(self._slots):
                    if not slot.launching and slot.active == 0 and not self._browser_connected(slot):
                        self._slots.remove(slot)
                        retired = slot
                        break
                for slot in self._slots:
                    if slot.key == key and not slot.launching and not slot.retiring and self._browser_connected(slot) and slot.active < self.contexts_per_process:
                        slot.active += 1
                        selected = slot
                        break
                if selected is None:
                    if any(slot.key == key and slot.launching and not slot.retiring for slot in self._slots):
                        await self._condition.wait()
                        continue
                    if len(self._slots) >= self.max_processes:
                        idle = next((slot for slot in self._slots if not slot.launching and slot.active == 0), None)
                        if idle is not None:
                            self._slots.remove(idle)
                            retired = idle
                    if len(self._slots) < self.max_processes:
                        launch_slot = _BrowserSlot(key=key, active=1, created_at=time.monotonic())
                        self._slots.append(launch_slot)
                    else:
                        await self._condition.wait()
                        continue
            if retired and retired.browser:
                await safe_close(retired.browser)
            if selected is not None:
                try:
                    context = await selected.browser.new_context(**context_options)  # type: ignore[union-attr]
                    return BrowserContextLease(self, selected, context)
                except Exception:
                    await self._release_slot(selected, context=None, failed=True)
                    raise
            if launch_slot is None:
                continue
            try:
                if self._playwright is None:
                    raise RuntimeError("browser pool is not available")
                browser = await self._playwright.chromium.launch(
                    headless=headless,
                    executable_path=executable_path,
                    proxy=proxy,
                    args=browser_args,
                )
                async with self._condition:
                    launch_slot.browser = browser
                    launch_slot.launching = False
                    self._condition.notify_all()
                context = await browser.new_context(**context_options)
                return BrowserContextLease(self, launch_slot, context)
            except Exception:
                await self._release_slot(launch_slot, context=None, failed=True)
                raise

    async def _finish_release(self, slot: _BrowserSlot, *, close_failed: bool) -> Browser | None:
        retired: Browser | None = None
        async with self._condition:
            slot.closing = max(0, slot.closing - 1)
            if close_failed:
                slot.retire_requested = True
            slot.retiring = slot.retire_requested or slot.closing > 0
            if slot in self._slots and slot.active == 0 and slot.closing == 0 and slot.retire_requested:
                self._slots.remove(slot)
                retired = slot.browser
            self._condition.notify_all()
        return retired

    @staticmethod
    async def _close_retired_browser(browser: Browser | None) -> None:
        if browser is None:
            return
        try:
            await asyncio.wait_for(safe_close(browser), timeout=BROWSER_PROCESS_CLOSE_TIMEOUT_SECONDS)
        except Exception:
            pass

    async def _release_slot(self, slot: _BrowserSlot, context: BrowserContext | None, failed: bool = False) -> None:
        retired: Browser | None = None
        async with self._condition:
            slot.active = max(0, slot.active - 1)
            slot.completed += int(context is not None)
            expired = time.monotonic() - slot.created_at >= BROWSER_RECYCLE_SECONDS
            if failed or not self._browser_connected(slot) or slot.completed >= BROWSER_RECYCLE_TASKS or expired:
                slot.retire_requested = True
            if context is not None:
                slot.closing += 1
            slot.retiring = slot.retire_requested or slot.closing > 0
            if slot in self._slots and slot.active == 0 and slot.closing == 0 and slot.retire_requested:
                self._slots.remove(slot)
                retired = slot.browser
            self._condition.notify_all()

        if context is None:
            await self._close_retired_browser(retired)
            return

        close_failed = False
        try:
            await asyncio.wait_for(safe_close(context), timeout=BROWSER_CONTEXT_CLOSE_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            close_failed = True
            retired = await self._finish_release(slot, close_failed=True)
            await self._close_retired_browser(retired)
            raise
        except Exception:
            close_failed = True

        retired = await self._finish_release(slot, close_failed=close_failed)
        await self._close_retired_browser(retired)

    async def release(self, slot: _BrowserSlot, context: BrowserContext) -> None:
        await self._release_slot(slot, context=context)

    async def stop(self) -> None:
        async with self._condition:
            self._stopping = True
            slots = list(self._slots)
            self._slots.clear()
            self._condition.notify_all()
        await asyncio.gather(*(safe_close(slot.browser) for slot in slots if slot.browser is not None), return_exceptions=True)
        async with self._start_lock:
            if self._playwright is not None:
                with suppress(Exception):
                    await self._playwright.stop()
                self._playwright = None

    def snapshot(self) -> dict[str, int]:
        active = sum(slot.active for slot in self._slots)
        return {
            "process_limit": self.max_processes,
            "contexts_per_process": self.contexts_per_process,
            "submission_capacity": self.capacity,
            "processes": sum(1 for slot in self._slots if slot.browser is not None),
            "active_contexts": active,
            "closing_contexts": sum(slot.closing for slot in self._slots),
            "retiring_processes": sum(bool(slot.retire_requested) for slot in self._slots),
            "available_contexts": max(0, self.capacity - active),
        }


def _playwright_candidates(root: Path) -> list[Path]:
    if not root.exists():
        return []
    patterns = (
        "chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe",
        "chromium-*/chrome-win64/chrome.exe",
        "chromium-*/chrome-linux*/chrome",
        "chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell",
    )
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(sorted(root.glob(pattern), reverse=True))
    return candidates


def resolve_browser_executable(configured_path: str = "") -> str | None:
    configured = Path(str(configured_path or "").strip()).expanduser() if str(configured_path or "").strip() else None
    if configured:
        if configured.is_file():
            return str(configured.resolve())
        raise RuntimeError(f"configured browser executable not found: {configured}")
    roots: list[Path] = []
    environment_root = str(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if environment_root and environment_root != "0":
        roots.append(Path(environment_root).expanduser())
    roots.append(APP_ROOT / ".pw-browsers")
    for root in roots:
        for candidate in _playwright_candidates(root):
            if candidate.is_file():
                return str(candidate.resolve())
    candidates = (
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/google-chrome-stable"),
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def create_tracked_task(tasks: set[asyncio.Task[Any]], coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    task = asyncio.create_task(coroutine)
    tasks.add(task)

    def consume_result(completed: asyncio.Task[Any]) -> None:
        tasks.discard(completed)
        with suppress(asyncio.CancelledError, Exception):
            completed.exception()

    task.add_done_callback(consume_result)
    return task


async def cancel_tracked_tasks(tasks: set[asyncio.Task[Any]]) -> None:
    pending = list(tasks)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    tasks.clear()


async def safe_unroute_all(page: Any) -> None:
    if page is None:
        return
    try:
        await page.unroute_all(behavior="ignoreErrors")
    except PlaywrightError:
        pass


async def safe_close(target: Any) -> None:
    if target is None:
        return
    try:
        await target.close()
    except PlaywrightError:
        pass
