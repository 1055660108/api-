from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.doubao_automation import DoubaoVideoAutomation


class DoubaoAutomationTests(unittest.TestCase):
    @staticmethod
    def runner(proxy_session) -> DoubaoVideoAutomation:
        runner = DoubaoVideoAutomation.__new__(DoubaoVideoAutomation)
        runner.proxy_session = proxy_session
        runner.settings = SimpleNamespace(task_timeout_seconds=600)
        runner.task_id = "doubao-task"
        return runner

    def test_shared_proxy_is_passed_to_browser_and_released(self) -> None:
        proxy = {"server": "http://proxy.example:18080"}
        session = SimpleNamespace(
            acquire_browser_proxy=AsyncMock(return_value=proxy),
            release_browser_proxy=AsyncMock(),
        )
        runner = self.runner(session)
        runner._run_browser = AsyncMock(return_value={"success": True})

        outcome = asyncio.run(runner._run_profile())

        self.assertTrue(outcome["success"])
        runner._run_browser.assert_awaited_once_with(proxy)
        session.acquire_browser_proxy.assert_awaited_once()
        session.release_browser_proxy.assert_awaited_once()

    def test_shared_proxy_is_released_when_browser_fails(self) -> None:
        session = SimpleNamespace(
            acquire_browser_proxy=AsyncMock(return_value={"server": "http://proxy.example:18080"}),
            release_browser_proxy=AsyncMock(),
            mark_browser_proxy_unavailable=Mock(),
        )
        runner = self.runner(session)
        runner._run_browser = AsyncMock(side_effect=RuntimeError("browser failed"))

        with self.assertRaisesRegex(RuntimeError, "browser failed"):
            asyncio.run(runner._run_profile())

        session.release_browser_proxy.assert_awaited_once()
        session.mark_browser_proxy_unavailable.assert_called_once_with(reason="doubao_browser_failure")

    def test_proxy_refresh_defers_without_consuming_normal_retry(self) -> None:
        class ProxyRefreshError(RuntimeError):
            retry_after = 7
            queue_reason = "正在刷新API代理，任务已自动排队"
            queue_category = "proxy_refresh"

        session = SimpleNamespace(mark_browser_proxy_unavailable=lambda **_kwargs: None)
        runner = self.runner(session)
        runner._run_once = AsyncMock(side_effect=ProxyRefreshError("proxy refreshing"))

        outcome = asyncio.run(runner.run())

        self.assertTrue(outcome["retryable"])
        self.assertTrue(outcome["infrastructure_fault"])
        self.assertTrue(outcome["defer_only"])
        self.assertEqual(outcome["retry_after"], 7)
        self.assertEqual(outcome["defer_category"], "proxy_refresh")

    def test_region_restriction_recognizes_redirect_and_page_message(self) -> None:
        self.assertTrue(
            DoubaoVideoAutomation._is_region_restricted(
                "https://www.doubao.com/security/doubao-region-ban?source=1",
                "",
            )
        )
        self.assertTrue(DoubaoVideoAutomation._is_region_restricted(DOUBAO_CHAT_URL, "当前地区暂不支持豆包"))
        self.assertFalse(DoubaoVideoAutomation._is_region_restricted(DOUBAO_CHAT_URL, "开始视频生成"))


DOUBAO_CHAT_URL = "https://www.doubao.com/chat/"


if __name__ == "__main__":
    unittest.main()
