from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from pathlib import Path
from typing import Any, Coroutine

from playwright.async_api import Error as PlaywrightError

from .config import APP_ROOT


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
