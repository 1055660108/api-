from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import browser_live_view, config


class FakePage:
    url = "https://example.test/video"

    def is_closed(self) -> bool:
        return False

    async def screenshot(self, **_kwargs) -> bytes:
        return b"fake-jpeg-frame"

    async def title(self) -> str:
        return "Video creation"


class BrowserLiveViewTests(unittest.TestCase):
    def test_monitor_storage_failure_does_not_fail_browser_task(self) -> None:
        async def run() -> None:
            monitor = browser_live_view.TaskBrowserLiveView("e" * 32, "doubao")
            await monitor.start(FakePage())
            await monitor.stop()

        with tempfile.TemporaryDirectory() as directory, patch.object(config, "DATA_DIR", Path(directory)), patch.object(
            browser_live_view, "_atomic_write", side_effect=OSError("disk full")
        ):
            asyncio.run(run())

    def test_capture_is_idle_until_admin_requests_view(self) -> None:
        async def run() -> dict:
            monitor = browser_live_view.TaskBrowserLiveView("d" * 32, "dola")
            await monitor.start(FakePage())
            await asyncio.sleep(0.05)
            await monitor.stop()
            return browser_live_view.read_live_view("d" * 32)

        with tempfile.TemporaryDirectory() as directory, patch.object(config, "DATA_DIR", Path(directory)):
            state = asyncio.run(run())
            self.assertFalse(state["frame_available"])
            self.assertEqual(int(state.get("sequence") or 0), 0)

    def test_capture_writes_latest_frame_and_closes_session(self) -> None:
        async def run() -> dict:
            monitor = browser_live_view.TaskBrowserLiveView("a" * 32, "doubao")
            browser_live_view.request_live_view("a" * 32)
            await monitor.start(FakePage())
            for _ in range(50):
                state = browser_live_view.read_live_view("a" * 32)
                if state.get("frame_available") and int(state.get("sequence") or 0) > 0:
                    break
                await asyncio.sleep(0.01)
            await monitor.stop()
            return browser_live_view.read_live_view("a" * 32)

        with tempfile.TemporaryDirectory() as directory, patch.object(config, "DATA_DIR", Path(directory)):
            state = asyncio.run(run())
            frame = browser_live_view.browser_live_frame_path("a" * 32)
            self.assertFalse(state["active"])
            self.assertTrue(state["frame_available"])
            self.assertEqual(state["platform"], "doubao")
            self.assertEqual(state["url"], FakePage.url)
            self.assertEqual(frame.read_bytes(), b"fake-jpeg-frame")

    def test_cleanup_removes_only_expired_view_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "DATA_DIR", Path(directory)):
            root = browser_live_view.live_view_dir()
            root.mkdir(parents=True)
            expired = root / "expired.jpg"
            current = root / "current.jpg"
            expired.write_bytes(b"old")
            current.write_bytes(b"new")
            old = time.time() - 7200
            os.utime(expired, (old, old))
            removed = browser_live_view.cleanup_stale_live_views(3600)
            self.assertEqual(removed, 1)
            self.assertFalse(expired.exists())
            self.assertTrue(current.exists())


if __name__ == "__main__":
    unittest.main()
