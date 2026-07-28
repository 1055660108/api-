from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import account_proxies, automation, config, proxy_manager


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
        proxy_latency_threshold_ms=800,
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
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(config, "DATA_DIR", Path(self.temporary_directory.name))
        self.data_patch.start()
        account_proxies._ROTATION_CURSOR = 0
        proxy_manager._PROXY_SOURCE_FAILURES.clear()

    def tearDown(self) -> None:
        self.data_patch.stop()
        self.temporary_directory.cleanup()

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

    async def test_authenticated_pool_tries_next_selected_proxy_before_other_mode(self) -> None:
        settings = proxy_settings("account")
        settings.proxy_account_host = ""
        settings.proxy_account_port = 0
        settings.proxy_account_username = ""
        settings.proxy_account_password = ""
        imported = account_proxies.import_account_proxies(
            "\n".join([
                "socks5://fake-region-JP:first@jp.example.com:3010",
                "socks5://fake-region-US:second@us.example.com:3020",
            ]),
            settings,
        )
        instance = automation_instance()
        with patch.object(automation, "load_settings", return_value=settings), patch.object(
            automation, "dola_proxy_available", new=AsyncMock(side_effect=[False, True])
        ) as probe, patch.object(
            automation, "acquire_dola_subscription_proxy", new=AsyncMock()
        ) as subscription:
            result = await instance._browser_proxy_config()

        self.assertEqual(probe.await_count, 2)
        subscription.assert_not_awaited()
        self.assertEqual(result["server"], "socks5://us.example.com:3020")
        self.assertEqual(instance.proxy_node_id, imported["selected_ids"][1])

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
        self.assertGreater(proxy_manager.proxy_source_retry_after("account"), 0)
        proxy_manager.mark_proxy_source_available("account")
        self.assertTrue(proxy_manager.proxy_source_available("account"))
        self.assertEqual(proxy_manager.proxy_source_retry_after("account"), 0)

    async def test_all_cooling_proxy_sources_defer_without_consuming_a_retry(self) -> None:
        instance = automation_instance()
        instance.settings = SimpleNamespace(task_timeout_seconds=180)
        settings = proxy_settings("subscription")
        settings.proxy_account_host = ""
        settings.proxy_account_port = 0
        settings.proxy_account_username = ""
        settings.proxy_account_password = ""
        proxy_manager.mark_proxy_source_unavailable("subscription")

        with patch.object(automation, "load_settings", return_value=settings), patch.object(
            instance, "_run_once", new=AsyncMock(side_effect=automation.ProxyCoolingDownError(45))
        ):
            outcome = await instance.run()

        self.assertTrue(outcome["retryable"])
        self.assertTrue(outcome["infrastructure_fault"])
        self.assertTrue(outcome["defer_only"])
        self.assertEqual(outcome["retry_after"], 45)

    async def test_proxy_configuration_reports_the_soonest_cooling_deadline(self) -> None:
        instance = automation_instance()
        settings = proxy_settings("subscription")
        settings.proxy_account_host = ""
        settings.proxy_account_port = 0
        settings.proxy_account_username = ""
        settings.proxy_account_password = ""
        proxy_manager.mark_proxy_source_unavailable("subscription")

        with patch.object(automation, "load_settings", return_value=settings):
            with self.assertRaises(automation.ProxyCoolingDownError) as raised:
                await instance._browser_proxy_config()

        self.assertGreater(raised.exception.retry_after, 0)
        self.assertLessEqual(raised.exception.retry_after, proxy_manager.PROXY_SOURCE_FAILURE_COOLDOWN_SECONDS)

    def test_proxy_mode_unavailable_is_an_infrastructure_failure(self) -> None:
        self.assertTrue(automation.is_infrastructure_failure("all configured proxy modes are unavailable (subscription: cooling down)"))


if __name__ == "__main__":
    unittest.main()
