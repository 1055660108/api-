from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import automation, config, proxy_manager


def proxy_settings(primary: str) -> SimpleNamespace:
    return SimpleNamespace(
        proxy_enabled=True,
        proxy_source=primary,
        proxy_subscription_url="https://subscription.example/token",
        proxy_subscription_scheme="http",
        proxy_subscription_refresh_seconds=900,
        proxy_auto_select=True,
        proxy_selected_node="",
        proxy_auto_countries=[],
        proxy_account_scheme="socks5",
        proxy_account_host="proxy.example.com",
        proxy_account_port=3010,
        proxy_account_username="fake-region-JP",
        proxy_account_password="fake-password",
        proxy_api_url="",
        proxy_api_scheme="http",
        proxy_api_timeout_seconds=20,
    )


def automation_instance() -> automation.DolaFetchAutomation:
    instance = object.__new__(automation.DolaFetchAutomation)
    instance.task_id = "fake-task"
    instance.proxy_node_id = ""
    instance.active_proxy_source = ""
    instance.subscription_proxy = None
    instance._save_result = lambda **kwargs: None
    return instance


class ProxyFailoverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        proxy_manager._PROXY_SOURCE_FAILURES.clear()

    async def test_subscription_failure_falls_back_to_authenticated_proxy(self) -> None:
        instance = automation_instance()
        settings = proxy_settings("subscription")
        with patch.object(automation, "load_settings", return_value=settings), patch.object(
            automation, "acquire_dola_subscription_proxy", new=AsyncMock(side_effect=RuntimeError("subscription unavailable"))
        ) as subscription, patch.object(automation, "dola_proxy_available", new=AsyncMock(return_value=True)) as probe:
            result = await instance._browser_proxy_config()

        subscription.assert_awaited_once()
        self.assertIn("fake-region-JP:fake-password@proxy.example.com:3010", probe.await_args.args[0])
        self.assertEqual(result, {
            "server": "socks5://proxy.example.com:3010",
            "username": "fake-region-JP",
            "password": "fake-password",
        })
        self.assertEqual(instance.active_proxy_source, "account")

    async def test_authenticated_proxy_failure_falls_back_to_subscription(self) -> None:
        instance = automation_instance()
        settings = proxy_settings("account")
        subscription_proxy = {
            "server": "http://127.0.0.1:7890",
            "node_id": "node-1",
            "node_name": "备用节点",
            "node_count": "2",
        }
        with patch.object(automation, "load_settings", return_value=settings), patch.object(
            automation, "dola_proxy_available", new=AsyncMock(return_value=False)
        ), patch.object(
            automation, "acquire_dola_subscription_proxy", new=AsyncMock(return_value=subscription_proxy)
        ) as subscription:
            result = await instance._browser_proxy_config()

        subscription.assert_awaited_once()
        self.assertEqual(result, {"server": "http://127.0.0.1:7890"})
        self.assertEqual(instance.active_proxy_source, "subscription")
        self.assertEqual(instance.proxy_node_id, "node-1")

    def test_authenticated_proxy_helpers_keep_browser_credentials_separate(self) -> None:
        probe_url = config.account_proxy_url_for("socks5", "proxy.example.com", 3010, "fake user", "p@ss:word")
        browser = config.account_browser_proxy_config_for("socks5", "proxy.example.com", 3010, "fake user", "p@ss:word")
        self.assertEqual(probe_url, "socks5://fake%20user:p%40ss%3Aword@proxy.example.com:3010")
        self.assertEqual(browser, {
            "server": "socks5://proxy.example.com:3010",
            "username": "fake user",
            "password": "p@ss:word",
        })

    def test_failed_proxy_source_enters_temporary_cooldown(self) -> None:
        proxy_manager.mark_proxy_source_unavailable("account")
        self.assertFalse(proxy_manager.proxy_source_available("account"))
        proxy_manager.mark_proxy_source_available("account")
        self.assertTrue(proxy_manager.proxy_source_available("account"))


if __name__ == "__main__":
    unittest.main()
