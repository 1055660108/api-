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
        platform_proxy_sources={"dola": primary, "doubao": primary, "qianwen": primary},
        platform_proxy_random={"dola": False, "doubao": False, "qianwen": False},
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
        proxy_manager._NODE_COOLDOWNS.clear()
        proxy_manager._NODE_GATEWAY_FAILURES.clear()
        proxy_manager._NODE_DELAYS.clear()
        proxy_manager._NODE_DOLA_HEALTH.clear()

    def tearDown(self) -> None:
        self.data_patch.stop()
        self.temporary_directory.cleanup()

    async def test_platform_proxy_source_overrides_legacy_global_source(self) -> None:
        instance = automation_instance()
        instance.proxy_platform = "doubao"
        settings = proxy_settings("subscription")
        settings.platform_proxy_sources["doubao"] = "direct"
        with patch.object(automation, "load_settings", return_value=settings), patch.object(
            automation, "acquire_dola_subscription_proxy", new=AsyncMock()
        ) as subscription:
            result = await instance._browser_proxy_config()

        self.assertIsNone(result)
        subscription.assert_not_awaited()

    async def test_platform_random_mode_is_forwarded_to_subscription_pool(self) -> None:
        instance = automation_instance()
        instance.proxy_platform = "doubao"
        settings = proxy_settings("subscription")
        settings.platform_proxy_random["doubao"] = True
        proxy = {"server": "http://127.0.0.1:7890", "node_id": "node-random", "node_name": "随机节点", "node_count": "2"}
        with patch.object(automation, "load_settings", return_value=settings), patch.object(
            automation, "acquire_dola_subscription_proxy", new=AsyncMock(return_value=proxy)
        ) as subscription:
            await instance._browser_proxy_config()

        self.assertTrue(subscription.await_args.kwargs["random_select"])

    async def test_random_subscription_selection_ignores_fixed_node_without_auto_select(self) -> None:
        nodes = (
            proxy_manager.ProxyNode(id="fixed", name="fixed", country="JP", protocol="http", server="fixed.example", port=8001, uri="http://fixed.example:8001"),
            proxy_manager.ProxyNode(id="other", name="other", country="US", protocol="http", server="other.example", port=8002, uri="http://other.example:8002"),
        )
        delay_state = {"fixed": (20, 1.0), "other": (30, 1.0)}
        with patch.object(proxy_manager, "_NODE_DELAYS", delay_state), patch.object(proxy_manager.time, "monotonic", return_value=2.0), patch.object(
            proxy_manager.secrets, "choice", return_value=(30, nodes[1])
        ):
            chosen = await proxy_manager._choose_subscription_node(
                nodes,
                "https://subscription.example/token",
                timeout_seconds=10,
                auto_select=False,
                selected_node="fixed",
                selected_countries=(),
                latency_threshold_ms=5000,
                excluded_node_ids=(),
                random_select=True,
            )

        self.assertEqual(chosen.id, "other")

    async def test_subscription_mode_does_not_fall_back_to_authenticated_proxy(self) -> None:
        instance = automation_instance()
        settings = proxy_settings("subscription")
        with patch.object(automation, "load_settings", return_value=settings), patch.object(
            automation, "acquire_dola_subscription_proxy", new=AsyncMock(side_effect=RuntimeError("subscription unavailable"))
        ) as subscription, patch.object(automation, "dola_proxy_available", new=AsyncMock(return_value=True)) as probe:
            with self.assertRaisesRegex(RuntimeError, "all configured proxy modes are unavailable"):
                await instance._browser_proxy_config()

        subscription.assert_awaited_once()
        probe.assert_not_awaited()
        self.assertEqual(instance.active_proxy_source, "")

    async def test_authenticated_proxy_mode_does_not_fall_back_to_subscription(self) -> None:
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
            with self.assertRaisesRegex(RuntimeError, "all configured proxy modes are unavailable"):
                await instance._browser_proxy_config()

        subscription.assert_not_awaited()
        self.assertEqual(instance.active_proxy_source, "")
        self.assertEqual(instance.proxy_node_id, "")

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
        ) as subscription, patch.object(
            automation,
            "acquire_authenticated_socks_proxy",
            new=AsyncMock(return_value={"server": "http://127.0.0.1:19090", "exit_id": "ip:203.0.113.20"}),
        ) as bridge:
            result = await instance._browser_proxy_config()

        self.assertEqual(probe.await_count, 2)
        subscription.assert_not_awaited()
        bridge.assert_awaited_once()
        self.assertEqual(result["server"], "http://127.0.0.1:19090")
        self.assertEqual(instance.proxy_node_id, imported["selected_ids"][1])
        self.assertEqual(instance.proxy_exit_id, "ip:203.0.113.20")

    async def test_api_mode_tries_three_distinct_candidates_until_one_is_available(self) -> None:
        settings = proxy_settings("api")
        settings.proxy_api_url = "https://proxy-api.example/get"
        proxies = [
            {"server": "http://proxy-a.example:18001", "host_port": "proxy-a.example:18001"},
            {"server": "http://proxy-b.example:18002", "host_port": "proxy-b.example:18002"},
            {"server": "http://proxy-c.example:18003", "host_port": "proxy-c.example:18003"},
        ]
        instance = automation_instance()
        with patch.object(automation, "load_settings", return_value=settings), patch.object(
            automation, "fetch_proxy_from_api", new=AsyncMock(side_effect=proxies)
        ) as fetch, patch.object(
            automation, "dola_proxy_available", new=AsyncMock(side_effect=[False, False, True])
        ) as probe, patch.object(
            automation, "proxy_exit_identity", new=AsyncMock(return_value="ip:203.0.113.30")
        ) as exit_identity, patch.object(
            automation, "acquire_dola_subscription_proxy", new=AsyncMock()
        ) as subscription:
            result = await instance._browser_proxy_config()

        self.assertEqual(fetch.await_count, 3)
        self.assertEqual(probe.await_count, 3)
        exit_identity.assert_awaited_once_with("http://proxy-c.example:18003", "api:proxy-c.example:18003")
        subscription.assert_not_awaited()
        self.assertEqual(result, {"server": "http://proxy-c.example:18003"})
        self.assertEqual(instance.proxy_node_id, "api:proxy-c.example:18003")
        self.assertEqual(instance.proxy_exit_id, "ip:203.0.113.30")
        self.assertTrue(proxy_manager.proxy_source_available("api"))
        self.assertEqual(proxy_manager.node_retry_after("api:proxy-a.example:18001"), 0)
        self.assertEqual(proxy_manager.node_retry_after("api:proxy-b.example:18002"), 0)

    async def test_api_mode_uses_shared_endpoint_pool_for_real_context_capacity(self) -> None:
        settings = proxy_settings("api")
        settings.proxy_api_url = "https://proxy-api.example/get"
        lease = SimpleNamespace(
            server="http://proxy-pool.example:18010",
            host_port="proxy-pool.example:18010",
            node_id="api:proxy-pool.example:18010",
            invalidate=lambda: None,
            release=AsyncMock(),
        )
        proxy_pool = SimpleNamespace(acquire=AsyncMock(return_value=lease))
        instance = automation_instance()
        instance.api_proxy_pool = proxy_pool
        instance.api_proxy_lease = None
        with patch.object(automation, "load_settings", return_value=settings), patch.object(
            automation, "proxy_exit_identity", new=AsyncMock(return_value="ip:203.0.113.40")
        ):
            result = await instance._browser_proxy_config()

        proxy_pool.acquire.assert_awaited_once_with(
            settings.proxy_api_url,
            timeout_seconds=settings.proxy_api_timeout_seconds,
            scheme=settings.proxy_api_scheme,
            excluded_node_ids=set(),
        )
        self.assertIs(instance.api_proxy_lease, lease)
        self.assertEqual(instance.proxy_node_id, lease.node_id)
        self.assertEqual(instance.proxy_exit_id, "ip:203.0.113.40")
        self.assertEqual(result, {"server": "http://proxy-pool.example:18010"})

    async def test_api_mode_ignores_duplicate_endpoints_while_seeking_candidates(self) -> None:
        settings = proxy_settings("api")
        settings.proxy_api_url = "https://proxy-api.example/get"
        duplicate = {"server": "http://proxy-a.example:18001", "host_port": "proxy-a.example:18001"}
        replacement = {"server": "http://proxy-b.example:18002", "host_port": "proxy-b.example:18002"}
        instance = automation_instance()
        with patch.object(automation, "load_settings", return_value=settings), patch.object(
            automation, "fetch_proxy_from_api", new=AsyncMock(side_effect=[duplicate, duplicate, replacement])
        ) as fetch, patch.object(
            automation, "dola_proxy_available", new=AsyncMock(side_effect=[False, True])
        ) as probe, patch.object(
            automation, "proxy_exit_identity", new=AsyncMock(return_value="ip:203.0.113.31")
        ):
            result = await instance._browser_proxy_config()

        self.assertEqual(fetch.await_count, 3)
        self.assertEqual(probe.await_count, 2)
        self.assertEqual(result["server"], "http://proxy-b.example:18002")

    async def test_api_mode_avoids_all_proxies_that_missed_a_conversation(self) -> None:
        settings = proxy_settings("api")
        settings.proxy_api_url = "https://proxy-api.example/get"
        excluded = {"api:proxy-a.example:18001", "api:proxy-b.example:18002"}
        proxies = [
            {"server": "http://proxy-a.example:18001", "host_port": "proxy-a.example:18001"},
            {"server": "http://proxy-b.example:18002", "host_port": "proxy-b.example:18002"},
            {"server": "http://proxy-c.example:18003", "host_port": "proxy-c.example:18003"},
        ]
        instance = automation_instance()
        with patch.object(instance, "_task_exists", return_value=True), patch.object(
            automation, "load_settings", return_value=settings
        ), patch.object(
            automation,
            "get_meta",
            return_value={"ambiguous_proxy_avoid_node_ids": sorted(excluded)},
        ), patch.object(
            automation, "fetch_proxy_from_api", new=AsyncMock(side_effect=proxies)
        ) as fetch, patch.object(
            automation, "dola_proxy_available", new=AsyncMock(return_value=True)
        ) as probe, patch.object(
            automation, "proxy_exit_identity", new=AsyncMock(return_value="ip:203.0.113.32")
        ), patch.object(automation, "update_meta"):
            result = await instance._browser_proxy_config()

        self.assertEqual(fetch.await_count, 3)
        probe.assert_awaited_once_with("http://proxy-c.example:18003", 12.0)
        self.assertEqual(result["server"], "http://proxy-c.example:18003")

    async def test_api_mode_queues_briefly_only_after_three_candidates_fail(self) -> None:
        settings = proxy_settings("api")
        settings.proxy_api_url = "https://proxy-api.example/get"
        proxies = [
            {"server": f"http://proxy-{index}.example:{18000 + index}", "host_port": f"proxy-{index}.example:{18000 + index}"}
            for index in range(1, 4)
        ]
        instance = automation_instance()
        with patch.object(automation, "load_settings", return_value=settings), patch.object(
            automation, "fetch_proxy_from_api", new=AsyncMock(side_effect=proxies)
        ) as fetch, patch.object(
            automation, "dola_proxy_available", new=AsyncMock(return_value=False)
        ) as probe, patch.object(
            automation, "acquire_dola_subscription_proxy", new=AsyncMock()
        ) as subscription:
            with self.assertRaises(automation.ProxyCoolingDownError) as raised:
                await instance._browser_proxy_config()

        self.assertEqual(raised.exception.retry_after, automation.API_PROXY_RETRY_AFTER_SECONDS)
        self.assertEqual(fetch.await_count, 3)
        self.assertEqual(probe.await_count, 3)
        subscription.assert_not_awaited()
        self.assertTrue(proxy_manager.proxy_source_available("api"))
        self.assertTrue(all(proxy_manager.node_retry_after(f"api:proxy-{index}.example:{18000 + index}") == 0 for index in range(1, 4)))

    async def test_api_mode_stops_after_three_extraction_errors(self) -> None:
        settings = proxy_settings("api")
        settings.proxy_api_url = "https://proxy-api.example/get"
        instance = automation_instance()
        with patch.object(automation, "load_settings", return_value=settings), patch.object(
            automation,
            "fetch_proxy_from_api",
            new=AsyncMock(side_effect=RuntimeError("proxy api temporarily unavailable")),
        ) as fetch, patch.object(
            automation, "dola_proxy_available", new=AsyncMock()
        ) as probe:
            with self.assertRaises(automation.ProxyCoolingDownError) as raised:
                await instance._browser_proxy_config()

        self.assertEqual(raised.exception.retry_after, automation.API_PROXY_RETRY_AFTER_SECONDS)
        self.assertEqual(fetch.await_count, automation.API_PROXY_FETCH_ERROR_LIMIT)
        probe.assert_not_awaited()
        self.assertTrue(proxy_manager.proxy_source_available("api"))

    def test_authenticated_proxy_helpers_keep_browser_credentials_separate(self) -> None:
        probe_url = config.account_proxy_url_for("socks5", "proxy.example.com", 3010, "fake user", "p@ss:word")
        browser = config.account_browser_proxy_config_for("socks5", "proxy.example.com", 3010, "fake user", "p@ss:word")
        self.assertEqual(probe_url, "socks5://fake%20user:p%40ss%3Aword@proxy.example.com:3010")
        self.assertEqual(browser, {
            "server": "socks5://proxy.example.com:3010",
            "username": "fake user",
            "password": "p@ss:word",
        })
        self.assertEqual(
            config.browser_proxy_config_for("socks5h://proxy.example.com:3010"),
            {"server": "socks5://proxy.example.com:3010"},
        )

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

    def test_runtime_failure_remembers_node_for_the_next_task_attempt(self) -> None:
        instance = automation_instance()
        instance.proxy_node_id = "failed-node"
        instance._task_exists = lambda: True
        with patch.object(automation, "get_meta", return_value={}), patch.object(automation, "update_meta") as update, patch.object(
            automation, "mark_node_unavailable"
        ) as unavailable:
            instance._mark_active_proxy_unavailable(cooldown_seconds=600, reason="service_frequent")

        unavailable.assert_called_once_with("failed-node", reason="service_frequent", cooldown_seconds=600)
        update.assert_called_once_with(
            "fake-task",
            proxy_retry_avoid_node_id="failed-node",
            failed_proxy_node_ids=["failed-node"],
        )

    def test_gateway_failure_tracks_node_without_immediate_first_hit_cooldown(self) -> None:
        instance = automation_instance()
        instance.proxy_node_id = "gateway-node"
        instance._remember_failed_proxy_node = unittest.mock.Mock()
        with patch.object(automation, "record_node_gateway_failure", return_value=False) as record:
            instance._record_active_gateway_failure(504)

        instance._remember_failed_proxy_node.assert_called_once_with()
        record.assert_called_once_with("gateway-node", 504)

    def test_active_limited_node_defers_before_submission_and_is_avoided(self) -> None:
        instance = automation_instance()
        instance.proxy_node_id = "limited-node"
        instance._remember_failed_proxy_node = unittest.mock.Mock()
        with patch.object(automation, "node_retry_after", return_value=45):
            outcome = instance._active_proxy_cooldown_outcome()

        self.assertTrue(outcome["defer_only"])
        self.assertEqual(outcome["retry_after"], 45)
        instance._remember_failed_proxy_node.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
