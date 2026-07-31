from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from playwright.async_api import Error as PlaywrightError

from app import browser_runtime


class BrowserRuntimeTests(unittest.TestCase):
    def test_bounded_cleanup_contains_cleanup_failures(self) -> None:
        async def fail() -> None:
            raise RuntimeError("cleanup failed")

        asyncio.run(browser_runtime.bounded_cleanup(fail(), timeout_seconds=0.1))

    def test_reusable_pool_limits_processes_and_contexts(self) -> None:
        class FakeContext:
            def __init__(self):
                self.close = AsyncMock()

        class FakeBrowser:
            def __init__(self):
                self.connected = True
                self.contexts: list[FakeContext] = []
                self.close = AsyncMock(side_effect=self._close)

            def is_connected(self) -> bool:
                return self.connected

            async def _close(self) -> None:
                self.connected = False

            async def new_context(self, **_options):
                context = FakeContext()
                self.contexts.append(context)
                return context

        browsers: list[FakeBrowser] = []

        async def launch(**_options):
            browser = FakeBrowser()
            browsers.append(browser)
            return browser

        playwright = unittest.mock.Mock()
        playwright.chromium.launch = AsyncMock(side_effect=launch)
        playwright.stop = AsyncMock()
        runtime = unittest.mock.Mock()
        runtime.start = AsyncMock(return_value=playwright)

        async def exercise() -> None:
            pool = browser_runtime.ReusableBrowserPool(max_processes=2, contexts_per_process=4)
            options = {
                "executable_path": None,
                "headless": True,
                "proxy": {"server": "http://proxy.example:8080"},
                "browser_args": ["--no-sandbox"],
                "context_options": {"locale": "zh-CN"},
            }
            leases = [await pool.acquire_context(**options) for _ in range(5)]
            self.assertEqual(len(browsers), 2)
            self.assertEqual(pool.snapshot()["active_contexts"], 5)
            self.assertEqual(pool.snapshot()["submission_capacity"], 8)
            self.assertTrue(all(lease.context is not leases[0].context for lease in leases[1:]))
            await asyncio.gather(*(lease.release() for lease in leases))
            self.assertEqual(pool.snapshot()["active_contexts"], 0)
            await pool.stop()
            self.assertTrue(all(not browser.connected for browser in browsers))
            playwright.stop.assert_awaited_once()

        with patch.object(browser_runtime, "async_playwright", return_value=runtime):
            asyncio.run(exercise())

    def test_context_close_timeout_releases_capacity_and_retires_browser(self) -> None:
        close_started = asyncio.Event()

        class HangingContext:
            async def close(self):
                close_started.set()
                await asyncio.Event().wait()

        class FakeBrowser:
            def __init__(self):
                self.connected = True
                self.close = AsyncMock(side_effect=self._close)

            def is_connected(self) -> bool:
                return self.connected

            async def _close(self) -> None:
                self.connected = False

            async def new_context(self, **_options):
                return HangingContext()

        browser = FakeBrowser()
        playwright = unittest.mock.Mock()
        playwright.chromium.launch = AsyncMock(return_value=browser)
        playwright.stop = AsyncMock()
        runtime = unittest.mock.Mock()
        runtime.start = AsyncMock(return_value=playwright)

        async def exercise() -> None:
            pool = browser_runtime.ReusableBrowserPool(max_processes=1, contexts_per_process=4)
            options = {
                "executable_path": None,
                "headless": True,
                "proxy": {"server": "http://proxy.example:8080"},
                "browser_args": [],
                "context_options": {},
            }
            lease = await pool.acquire_context(**options)
            self.assertEqual(pool.snapshot()["active_contexts"], 1)
            await lease.release()
            self.assertTrue(close_started.is_set())
            self.assertEqual(pool.snapshot()["active_contexts"], 0)
            self.assertEqual(pool.snapshot()["closing_contexts"], 0)
            self.assertEqual(pool.snapshot()["processes"], 0)
            browser.close.assert_awaited_once()
            await pool.stop()

        with patch.object(browser_runtime, "async_playwright", return_value=runtime), patch.object(
            browser_runtime,
            "BROWSER_CONTEXT_CLOSE_TIMEOUT_SECONDS",
            0.01,
        ):
            asyncio.run(exercise())

    def test_canceled_context_release_still_releases_capacity(self) -> None:
        close_started = asyncio.Event()

        class HangingContext:
            async def close(self):
                close_started.set()
                await asyncio.Event().wait()

        class FakeBrowser:
            def __init__(self):
                self.connected = True

            def is_connected(self) -> bool:
                return self.connected

            async def close(self) -> None:
                self.connected = False

            async def new_context(self, **_options):
                return HangingContext()

        browser = FakeBrowser()
        playwright = unittest.mock.Mock()
        playwright.chromium.launch = AsyncMock(return_value=browser)
        playwright.stop = AsyncMock()
        runtime = unittest.mock.Mock()
        runtime.start = AsyncMock(return_value=playwright)

        async def exercise() -> None:
            pool = browser_runtime.ReusableBrowserPool(max_processes=1, contexts_per_process=4)
            lease = await pool.acquire_context(
                executable_path=None,
                headless=True,
                proxy=None,
                browser_args=[],
                context_options={},
            )
            release_task = asyncio.create_task(lease.release())
            await close_started.wait()
            release_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await release_task
            self.assertEqual(pool.snapshot()["active_contexts"], 0)
            self.assertEqual(pool.snapshot()["processes"], 0)
            await pool.stop()

        with patch.object(browser_runtime, "async_playwright", return_value=runtime):
            asyncio.run(exercise())

    def test_configured_executable_has_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "browser.exe"
            executable.touch()
            self.assertEqual(browser_runtime.resolve_browser_executable(str(executable)), str(executable.resolve()))

    def test_distinct_proxy_configs_use_distinct_browser_processes(self) -> None:
        launches: list[dict] = []

        class FakeContext:
            def __init__(self):
                self.close = AsyncMock()

        class FakeProxyBrowser:
            def __init__(self):
                self.connected = True
                self.close = AsyncMock(side_effect=self._close)

            def is_connected(self) -> bool:
                return self.connected

            async def _close(self) -> None:
                self.connected = False

            async def new_context(self, **_options):
                return FakeContext()

        async def exercise() -> None:
            playwright = unittest.mock.Mock()

            async def launch(**options):
                launches.append(options)
                return FakeProxyBrowser()

            playwright.chromium.launch = AsyncMock(side_effect=launch)
            playwright.stop = AsyncMock()
            runtime = unittest.mock.Mock()
            runtime.start = AsyncMock(return_value=playwright)
            pool = browser_runtime.ReusableBrowserPool(max_processes=2, contexts_per_process=4)
            common = {"executable_path": None, "headless": True, "browser_args": [], "context_options": {"locale": "zh-CN"}}
            with patch.object(browser_runtime, "async_playwright", return_value=runtime):
                first = await pool.acquire_context(**common, proxy={"server": "http://127.0.0.1:4101"})
                second = await pool.acquire_context(**common, proxy={"server": "http://127.0.0.1:4102"})
                await first.release()
                await second.release()
                await pool.stop()

        asyncio.run(exercise())
        self.assertEqual(len(launches), 2)
        self.assertNotEqual(launches[0]["proxy"], launches[1]["proxy"])

    def test_concurrent_proxy_groups_launch_once_and_fill_all_contexts(self) -> None:
        class FakeContext:
            def __init__(self):
                self.close = AsyncMock()

        class FakeBrowser:
            def __init__(self):
                self.connected = True
                self.contexts: list[FakeContext] = []
                self.close = AsyncMock(side_effect=self._close)

            def is_connected(self) -> bool:
                return self.connected

            async def _close(self) -> None:
                self.connected = False

            async def new_context(self, **_options):
                context = FakeContext()
                self.contexts.append(context)
                await asyncio.sleep(0)
                return context

        browsers: list[FakeBrowser] = []

        async def launch(**_options):
            await asyncio.sleep(0.01)
            browser = FakeBrowser()
            browsers.append(browser)
            return browser

        async def exercise() -> None:
            playwright = unittest.mock.Mock()
            playwright.chromium.launch = AsyncMock(side_effect=launch)
            playwright.stop = AsyncMock()
            runtime = unittest.mock.Mock()
            runtime.start = AsyncMock(return_value=playwright)
            pool = browser_runtime.ReusableBrowserPool(max_processes=12, contexts_per_process=4)
            common = {"executable_path": None, "headless": True, "browser_args": [], "context_options": {"locale": "zh-CN"}}
            with patch.object(browser_runtime, "async_playwright", return_value=runtime):
                pending = [
                    asyncio.create_task(pool.acquire_context(**common, proxy={"server": f"http://127.0.0.1:{4100 + index // 4}"}))
                    for index in range(48)
                ]
                leases = await asyncio.wait_for(asyncio.gather(*pending), timeout=3)
                self.assertEqual(len(browsers), 12)
                self.assertEqual(pool.snapshot()["active_contexts"], 48)
                self.assertTrue(all(len(browser.contexts) == 4 for browser in browsers))
                await asyncio.gather(*(lease.release() for lease in leases))
                await pool.stop()

        asyncio.run(exercise())

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

        self.assertLess(
            dola.index("await _bounded_cleanup(safe_unroute_all(page))"),
            dola.index("await _bounded_cleanup(safe_close(context))"),
        )
        self.assertIn("await _bounded_cleanup(lease.release())", dola)
        self.assertNotIn('context_options["proxy"] = proxy_config', dola)
        self.assertIn("proxy=proxy_config", dola)
        self.assertGreaterEqual(dola.count("self._mark_active_proxy_unavailable("), 5)
        self.assertIn('if not self.proxy_node_id and self.active_proxy_source != "account":', dola)
        self.assertIn("await _bounded_cleanup(release_dola_subscription_proxy(self.subscription_proxy))", dola)
        self.assertIn("await dola_proxy_available", dola)
        self.assertIn("browser_pool=self._dola_browser_pool", (root / "worker.py").read_text(encoding="utf-8"))
        self.assertIn("submission_pacer=self._wait_for_dola_submit_slot", (root / "worker.py").read_text(encoding="utf-8"))
        self.assertIn("proxy_session=proxy_session", (root / "worker.py").read_text(encoding="utf-8"))
        self.assertIn("proxy=proxy_config", doubao)
        self.assertIn("await bounded_cleanup(lease.release())", doubao)
        self.assertIn("await self.browser_pool.acquire_context(", doubao)
        self.assertIn('context_options["storage_state"] = storage_state', doubao)
        self.assertNotIn("await context.add_cookies(cookies)", doubao)
        self.assertIn("await context.add_init_script(BROWSER_INIT_SCRIPT)", doubao)
        self.assertNotIn("launch_persistent_context", doubao)
        self.assertNotIn("find_slider_page", doubao)
        self.assertNotIn("slider_solver", doubao)
        self.assertIn("await page.evaluate(", doubao)
        self.assertIn("DOUBAO_SUBMIT_SCRIPT", doubao)
        self.assertNotIn("_open_video_generation", doubao)
        self.assertNotIn('editor.press("Enter")', doubao)
        self.assertNotIn('get_by_role("button", name="比例")', doubao)
        self.assertNotIn('name=re.compile(r"Mini|Fast|Pro|Seedance', doubao)
        self.assertGreaterEqual((root / "worker.py").read_text(encoding="utf-8").count("browser_pool=self._dola_browser_pool"), 2)
        self.assertIn("await self.proxy_session.release_browser_proxy()", doubao)
        self.assertLess(qianwen.index('page.remove_listener("response", response_handler)'), qianwen.index("await cancel_tracked_tasks(response_tasks)"))
        self.assertNotIn('asyncio.create_task(capture_completion(response))', qianwen)
        self.assertLess(qianwen.index("await cancel_tracked_tasks(response_tasks)"), qianwen.index("await safe_close(context)"))

    def test_submission_barrier_only_reports_real_user_cancellation(self) -> None:
        root = Path(__file__).parents[1] / "app"
        for filename in ("automation.py", "doubao_automation.py", "qianwen_automation.py"):
            source = (root / filename).read_text(encoding="utf-8")
            self.assertNotIn("task canceled before submission", source)
            self.assertIn("is_task_canceled(self.task_id)", source)
            self.assertIn("任务提交状态已变化，正在重试", source)

    def test_qianwen_prefers_original_unwatermarked_video_urls(self) -> None:
        from app.qianwen_automation import best_qianwen_video_url, qianwen_video_url_score

        preview = "https://cdn.example/preview-watermark.mp4?watermark=1"
        play = "https://cdn.example/result.m3u8"
        original = "https://cdn.example/original.mp4?lr=unwatermarked"
        scores = {
            preview: qianwen_video_url_score(preview, "preview_video_url"),
            play: qianwen_video_url_score(play, "play_url"),
            original: qianwen_video_url_score(original, "download_url_without_watermark"),
        }
        self.assertEqual(best_qianwen_video_url(scores), original)
        self.assertGreater(scores[original], scores[play])
        self.assertGreater(scores[play], scores[preview])


if __name__ == "__main__":
    unittest.main()
