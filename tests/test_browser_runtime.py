from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from playwright.async_api import Error as PlaywrightError

from app import browser_runtime


class BrowserRuntimeTests(unittest.TestCase):
    def test_configured_executable_has_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "browser.exe"
            executable.touch()
            self.assertEqual(browser_runtime.resolve_browser_executable(str(executable)), str(executable.resolve()))

    def test_invalid_configured_executable_fails_clearly(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "configured browser executable not found"):
            browser_runtime.resolve_browser_executable("missing-browser.exe")

    def test_project_playwright_browser_is_discovered_without_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / ".pw-browsers" / "chromium-1228" / "chrome-win64" / "chrome.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch.object(browser_runtime, "APP_ROOT", root), patch.dict("os.environ", {}, clear=True):
                self.assertEqual(browser_runtime.resolve_browser_executable(), str(executable.resolve()))

    def test_safe_playwright_cleanup_absorbs_closed_target_errors(self) -> None:
        page = unittest.mock.Mock()
        page.unroute_all = AsyncMock(side_effect=PlaywrightError("target closed"))
        context = unittest.mock.Mock()
        context.close = AsyncMock(side_effect=PlaywrightError("target closed"))

        async def cleanup() -> None:
            await browser_runtime.safe_unroute_all(page)
            await browser_runtime.safe_close(context)

        asyncio.run(cleanup())
        page.unroute_all.assert_awaited_once_with(behavior="ignoreErrors")
        context.close.assert_awaited_once_with()

    def test_tracked_tasks_are_cancelled_and_exceptions_are_retrieved(self) -> None:
        async def exercise() -> tuple[list[dict], bool]:
            loop = asyncio.get_running_loop()
            unhandled: list[dict] = []
            loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
            tasks: set[asyncio.Task] = set()
            blocker_started = asyncio.Event()

            async def fail() -> None:
                raise PlaywrightError("target closed")

            async def block() -> None:
                blocker_started.set()
                await asyncio.Event().wait()

            browser_runtime.create_tracked_task(tasks, fail())
            blocker = browser_runtime.create_tracked_task(tasks, block())
            await blocker_started.wait()
            await asyncio.sleep(0)
            await browser_runtime.cancel_tracked_tasks(tasks)
            await asyncio.sleep(0)
            return unhandled, blocker.cancelled()

        unhandled, blocker_cancelled = asyncio.run(exercise())
        self.assertEqual(unhandled, [])
        self.assertTrue(blocker_cancelled)

    def test_three_platforms_cleanup_before_closing_playwright(self) -> None:
        root = Path(__file__).parents[1] / "app"
        dola = (root / "automation.py").read_text(encoding="utf-8")
        doubao = (root / "doubao_automation.py").read_text(encoding="utf-8")
        qianwen = (root / "qianwen_automation.py").read_text(encoding="utf-8")

        self.assertLess(dola.index("await safe_unroute_all(page)"), dola.index("await safe_close(context)"))
        for source in (doubao, qianwen):
            self.assertLess(source.index('page.remove_listener("response", response_handler)'), source.index("await cancel_tracked_tasks(response_tasks)"))
            self.assertLess(source.index("await cancel_tracked_tasks(response_tasks)"), source.index("await safe_close(context)"))
            self.assertNotIn('asyncio.create_task(capture_completion(response))', source)


if __name__ == "__main__":
    unittest.main()
