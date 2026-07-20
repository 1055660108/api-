from __future__ import annotations

import base64
import unittest
from unittest.mock import AsyncMock, patch

from app import proxy_manager


class ProxyManagerTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(set(payload), {"id", "name", "country", "protocol", "server", "port", "latency_ms", "latency_measured", "selected"})
        self.assertNotIn("secret", str(payload))

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

    async def test_mihomo_config_adds_missing_local_listener_settings(self) -> None:
        config = await proxy_manager._fetch_mihomo_config("https://subscription.example/token", 20, 4567, 9090)

        text = config.decode()
        self.assertIn("mixed-port: 4567", text)
        self.assertIn("allow-lan: false", text)
        self.assertIn("bind-address: 127.0.0.1", text)
        self.assertIn("external-controller: 127.0.0.1:9090", text)
        self.assertIn("url: 'https://subscription.example/token'", text)

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

    async def test_auto_selection_uses_lowest_measured_latency(self) -> None:
        nodes = proxy_manager.subscription_node_list("http://us.example.com:8080#US\nhttp://jp.example.com:8080#Japan")
        delays = {nodes[0].id: (180, 1.0), nodes[1].id: (35, 1.0)}
        with patch.object(proxy_manager, "fetch_subscription_node_list", AsyncMock(return_value=nodes)), patch.dict(
            proxy_manager._NODE_DELAYS, delays, clear=True
        ):
            result = await proxy_manager.resolve_subscription_proxy("https://subscription.example/token")

        self.assertEqual(result["node_id"], nodes[1].id)


if __name__ == "__main__":
    unittest.main()
