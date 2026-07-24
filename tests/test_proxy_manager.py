from __future__ import annotations

import base64
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app import proxy_manager


class ProxyManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        proxy_manager._MIHOMO_SELECTED_NODE_ID = ""
        proxy_manager._SUBSCRIPTION_RESOLVE_LOCK = None

    def test_parses_native_proxy_subscription(self) -> None:
        parsed = proxy_manager.parse_subscription_nodes("http://proxy.example:8080\nsocks5://127.0.0.1:1080")

        self.assertEqual(parsed.native_proxies, ("http://proxy.example:8080", "socks5://127.0.0.1:1080"))
        self.assertEqual(parsed.tunnel_nodes, ())

    def test_base64_tunnel_subscription_is_not_treated_as_http_proxy(self) -> None:
        content = "vless://user@example.com:443?security=reality#node\nhysteria2://secret@example.net:8443#node2"
        encoded = base64.b64encode(content.encode()).decode()

        parsed = proxy_manager.parse_subscription_nodes(encoded)

        self.assertEqual(parsed.native_proxies, ())
        self.assertEqual(len(parsed.tunnel_nodes), 2)
        with self.assertRaisesRegex(RuntimeError, "require mihomo"):
            proxy_manager.parse_proxy_subscription(encoded)

    def test_urlsafe_base64_subscription_without_padding_is_parsed(self) -> None:
        content = "vless://user@hk.example.com:443#Hong-Kong\ntrojan://secret@sg.example.com:443#Singapore"
        encoded = base64.urlsafe_b64encode(content.encode()).decode().rstrip("=")

        nodes = proxy_manager.subscription_node_list(encoded)

        self.assertEqual([node.country for node in nodes], ["香港", "新加坡"])
        self.assertEqual([node.protocol for node in nodes], ["vless", "trojan"])

    def test_subscription_nodes_include_country_and_stable_id(self) -> None:
        nodes = proxy_manager.subscription_node_list(
            "vless://user@hk.example.com:443#%F0%9F%87%AD%F0%9F%87%B0%20Hong%20Kong\n"
            "trojan://secret@tokyo.example.com:443#Japan%20Tokyo"
        )

        self.assertEqual([node.country for node in nodes], ["香港", "日本"])
        self.assertEqual([node.protocol for node in nodes], ["vless", "trojan"])
        self.assertTrue(all(len(node.id) == 16 for node in nodes))

    def test_clash_yaml_nodes_are_listed_without_exposing_credentials(self) -> None:
        nodes = proxy_manager.subscription_node_list(
            "proxies:\n  - name: Singapore 01\n    type: ss\n    server: sg.example.com\n    port: 443\n    password: secret\n"
        )

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].country, "新加坡")
        payload = proxy_manager.node_payload(nodes[0])
        self.assertEqual(set(payload), {"id", "name", "country", "protocol", "server", "port", "latency_ms", "latency_measured", "latency_status", "selected"})
        self.assertNotIn("secret", str(payload))

    def test_base64_clash_yaml_nodes_are_listed(self) -> None:
        content = "proxies:\n  - {name: Japan 01, type: trojan, server: jp.example.com, port: 443, password: secret}\n"
        nodes = proxy_manager.subscription_node_list(base64.b64encode(content.encode()).decode())

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].name, "Japan 01")
        self.assertEqual(nodes[0].protocol, "trojan")

    async def test_subscription_download_allows_limited_redirects(self) -> None:
        response = type("Response", (), {"status_code": 200, "content": b"vless://user@example.com:443#node"})()
        client = AsyncMock()
        client.get.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client
        proxy_manager._SUBSCRIPTION_CACHE.update(url="", nodes=(), refreshed_at=0.0)

        with patch.object(proxy_manager.httpx, "AsyncClient", return_value=context) as client_factory:
            nodes = await proxy_manager.fetch_subscription_node_list("https://subscription.example/token", force=True)

        self.assertEqual(len(nodes), 1)
        self.assertTrue(client_factory.call_args.kwargs["follow_redirects"])
        self.assertEqual(client_factory.call_args.kwargs["max_redirects"], 5)

    async def test_subscription_redirect_limit_has_distinct_error(self) -> None:
        context = AsyncMock()
        context.__aenter__.return_value.get.side_effect = proxy_manager.httpx.TooManyRedirects("too many redirects")
        proxy_manager._SUBSCRIPTION_CACHE.update(url="", nodes=(), refreshed_at=0.0)

        with patch.object(proxy_manager.httpx, "AsyncClient", return_value=context):
            with self.assertRaisesRegex(RuntimeError, "redirect limit"):
                await proxy_manager.fetch_subscription_node_list("https://subscription.example/token", force=True)

    async def test_tunnel_subscription_uses_local_mihomo_proxy(self) -> None:
        response = type(
            "Response",
            (),
            {
                "status_code": 200,
                "content": base64.b64encode(b"vless://user@example.com:443#node"),
            },
        )()
        client = AsyncMock()
        client.get.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client

        proxy_manager._SUBSCRIPTION_CACHE.update(url="", nodes=(), refreshed_at=0.0)
        with patch.object(proxy_manager.httpx, "AsyncClient", return_value=context), patch.object(
            proxy_manager,
            "_proxy_from_mihomo",
            AsyncMock(return_value={"server": "http://127.0.0.1:4567", "node_count": "managed"}),
        ) as managed, patch.object(proxy_manager, "_select_mihomo_node", AsyncMock()) as selected:
            result = await proxy_manager.fetch_proxy_from_subscription("https://subscription.example/token", refresh_seconds=300)

        self.assertEqual(result["server"], "http://127.0.0.1:4567")
        self.assertEqual(managed.await_args_list[-1].args, ("https://subscription.example/token", 20, 300))
        selected.assert_awaited_once()

    async def test_mihomo_selection_failure_rebuilds_once(self) -> None:
        nodes = proxy_manager.subscription_node_list("vless://user@example.com:443#node")
        proxy_manager._NODE_DELAYS[nodes[0].id] = (20, proxy_manager.time.monotonic())
        managed_proxy = {"server": "http://127.0.0.1:4567", "node_count": "managed"}
        with patch.object(proxy_manager, "fetch_subscription_node_list", AsyncMock(return_value=nodes)), patch.object(
            proxy_manager, "_proxy_from_mihomo", AsyncMock(return_value=managed_proxy)
        ) as managed, patch.object(
            proxy_manager, "_select_mihomo_node", AsyncMock(side_effect=[RuntimeError("mihomo controller is not available"), None])
        ) as selected:
            result = await proxy_manager.resolve_subscription_proxy("https://subscription.example/token")

        self.assertEqual(result["server"], managed_proxy["server"])
        self.assertEqual(selected.await_count, 2)
        self.assertTrue(managed.await_args_list[-1].kwargs["force_rebuild"])

    async def test_mihomo_selected_node_is_reused_without_controller_write(self) -> None:
        nodes = proxy_manager.subscription_node_list("vless://user@example.com:443#node")
        proxy_manager._NODE_DELAYS[nodes[0].id] = (20, proxy_manager.time.monotonic())
        proxy_manager._MIHOMO_SELECTED_NODE_ID = nodes[0].id
        with patch.object(proxy_manager, "fetch_subscription_node_list", AsyncMock(return_value=nodes)), patch.object(
            proxy_manager, "_proxy_from_mihomo", AsyncMock(return_value={"server": "http://127.0.0.1:4567", "node_count": "managed"})
        ), patch.object(proxy_manager, "_select_mihomo_node", AsyncMock()) as selected:
            await proxy_manager.resolve_subscription_proxy("https://subscription.example/token")

        selected.assert_not_awaited()

    async def test_concurrent_proxy_resolution_selects_controller_once(self) -> None:
        nodes = proxy_manager.subscription_node_list("vless://user@example.com:443#node")
        proxy_manager._NODE_DELAYS[nodes[0].id] = (20, proxy_manager.time.monotonic())

        async def select_once(node: proxy_manager.ProxyNode) -> None:
            await asyncio.sleep(0)
            proxy_manager._MIHOMO_SELECTED_NODE_ID = node.id

        with patch.object(proxy_manager, "fetch_subscription_node_list", AsyncMock(return_value=nodes)), patch.object(
            proxy_manager, "_proxy_from_mihomo", AsyncMock(return_value={"server": "http://127.0.0.1:4567", "node_count": "managed"})
        ), patch.object(proxy_manager, "_select_mihomo_node", AsyncMock(side_effect=select_once)) as selected:
            results = await asyncio.gather(*(
                proxy_manager.resolve_subscription_proxy("https://subscription.example/token") for _ in range(20)
            ))

        self.assertEqual(len(results), 20)
        self.assertEqual(selected.await_count, 1)

    async def test_mihomo_config_adds_missing_local_listener_settings(self) -> None:
        provider = b"proxies:\n  - name: secured-node\n    type: trojan\n    server: example.com\n    port: 443\n    password: preserved-secret\n"
        with patch.dict(proxy_manager._SUBSCRIPTION_CACHE, {"provider": provider}):
            config = await proxy_manager._fetch_mihomo_config("https://subscription.example/token", 20, 4567, 9090)

        text = config.decode()
        self.assertIn("mixed-port: 4567", text)
        self.assertIn("allow-lan: false", text)
        self.assertIn("bind-address: 127.0.0.1", text)
        self.assertIn("external-controller: 127.0.0.1:9090", text)
        self.assertIn("name: secured-node", text)
        self.assertIn("password: preserved-secret", text)
        self.assertNotIn("subscription.example", text)

    async def test_manual_native_node_selection_is_used(self) -> None:
        nodes = proxy_manager.subscription_node_list("http://us.example.com:8080#US\nhttp://jp.example.com:8080#Japan")
        with patch.object(proxy_manager, "fetch_subscription_node_list", AsyncMock(return_value=nodes)):
            result = await proxy_manager.resolve_subscription_proxy(
                "https://subscription.example/token",
                auto_select=False,
                selected_node=nodes[1].id,
            )

        self.assertEqual(result["node_id"], nodes[1].id)
        self.assertEqual(result["server"], nodes[1].uri.split("#", 1)[0])

    async def test_manual_selection_is_not_restricted_by_auto_country_filters(self) -> None:
        nodes = proxy_manager.subscription_node_list("http://us.example.com:8080#US\nhttp://jp.example.com:8080#Japan")
        with patch.object(proxy_manager, "fetch_subscription_node_list", AsyncMock(return_value=nodes)):
            result = await proxy_manager.resolve_subscription_proxy(
                "https://subscription.example/token",
                auto_select=False,
                selected_node=nodes[0].id,
                selected_countries=["日本"],
            )

        self.assertEqual(result["node_id"], nodes[0].id)

    async def test_auto_selection_uses_lowest_measured_latency(self) -> None:
        nodes = proxy_manager.subscription_node_list("http://us.example.com:8080#US\nhttp://jp.example.com:8080#Japan")
        delays = {nodes[0].id: (180, proxy_manager.time.monotonic()), nodes[1].id: (35, proxy_manager.time.monotonic())}
        with patch.object(proxy_manager, "fetch_subscription_node_list", AsyncMock(return_value=nodes)), patch.dict(
            proxy_manager._NODE_DELAYS, delays, clear=True
        ):
            result = await proxy_manager.resolve_subscription_proxy("https://subscription.example/token")

        self.assertEqual(result["node_id"], nodes[1].id)

    async def test_auto_selection_only_uses_checked_countries(self) -> None:
        nodes = proxy_manager.subscription_node_list("http://us.example.com:8080#US\nhttp://jp.example.com:8080#Japan")
        delays = {nodes[0].id: (10, proxy_manager.time.monotonic()), nodes[1].id: (35, proxy_manager.time.monotonic())}
        with patch.object(proxy_manager, "fetch_subscription_node_list", AsyncMock(return_value=nodes)), patch.dict(
            proxy_manager._NODE_DELAYS, delays, clear=True
        ):
            result = await proxy_manager.resolve_subscription_proxy(
                "https://subscription.example/token",
                selected_countries=["日本"],
            )

        self.assertEqual(result["node_id"], nodes[1].id)

    def test_delay_payload_reports_pending_unavailable_and_expired_states(self) -> None:
        node = proxy_manager.subscription_node_list("http://jp.example.com:8080#Japan")[0]
        with patch.dict(proxy_manager._NODE_DELAYS, {}, clear=True):
            self.assertEqual(proxy_manager.node_payload(node)["latency_status"], "pending")
        with patch.dict(proxy_manager._NODE_DELAYS, {node.id: (None, proxy_manager.time.monotonic())}, clear=True):
            self.assertEqual(proxy_manager.node_payload(node)["latency_status"], "unavailable")
        with patch.dict(proxy_manager._NODE_DELAYS, {node.id: (25, proxy_manager.time.monotonic() - 301)}, clear=True):
            payload = proxy_manager.node_payload(node)
            self.assertEqual(payload["latency_status"], "expired")
            self.assertIsNone(payload["latency_ms"])

    def test_runtime_failure_marks_selected_node_unavailable(self) -> None:
        node = proxy_manager.subscription_node_list("http://jp.example.com:8080#Japan")[0]
        with patch.dict(proxy_manager._NODE_DELAYS, {node.id: (25, proxy_manager.time.monotonic())}, clear=True):
            proxy_manager.mark_node_unavailable(node.id)
            self.assertEqual(proxy_manager.node_payload(node)["latency_status"], "unavailable")

    async def test_refresh_clears_delay_cache_before_atomic_rebuild(self) -> None:
        nodes = proxy_manager.subscription_node_list("vless://user@example.com:443#node")
        proxy_manager._NODE_DELAYS[nodes[0].id] = (20, proxy_manager.time.monotonic())
        with patch.object(proxy_manager, "_proxy_from_mihomo", AsyncMock()) as rebuild:
            await proxy_manager.rebuild_mihomo_from_snapshot("https://subscription.example/token", nodes)
        self.assertEqual(proxy_manager._NODE_DELAYS, {})
        rebuild.assert_awaited_once()

    async def test_mihomo_readiness_requires_process_both_ports_and_dola_group(self) -> None:
        process = unittest.mock.Mock()
        process.poll.return_value = None
        with patch.object(proxy_manager, "_port_is_open", side_effect=lambda port: port in {4567, 9090}), patch.object(
            proxy_manager, "_mihomo_group_ready", AsyncMock(return_value=True)
        ):
            self.assertTrue(await proxy_manager._mihomo_ready(process, 4567, 9090))
        with patch.object(proxy_manager, "_port_is_open", return_value=True), patch.object(
            proxy_manager, "_mihomo_group_ready", AsyncMock(return_value=False)
        ):
            self.assertFalse(await proxy_manager._mihomo_ready(process, 4567, 9090))


if __name__ == "__main__":
    unittest.main()
