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

        with patch.object(proxy_manager.httpx, "AsyncClient", return_value=context), patch.object(
            proxy_manager,
            "_proxy_from_mihomo",
            AsyncMock(return_value={"server": "http://127.0.0.1:4567", "node_count": "managed"}),
        ) as managed:
            result = await proxy_manager.fetch_proxy_from_subscription("https://subscription.example/token", refresh_seconds=300)

        self.assertEqual(result["server"], "http://127.0.0.1:4567")
        managed.assert_awaited_once_with("https://subscription.example/token", 20, 300)

    async def test_mihomo_config_adds_missing_local_listener_settings(self) -> None:
        response = type(
            "Response",
            (),
            {"status_code": 200, "content": b"proxies:\n  - name: node\n    type: ss\n"},
        )()
        client = AsyncMock()
        client.get.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client

        with patch.object(proxy_manager.httpx, "AsyncClient", return_value=context):
            config = await proxy_manager._fetch_mihomo_config("https://subscription.example/token", 20, 4567)

        text = config.decode()
        self.assertIn("mixed-port: 4567", text)
        self.assertIn("allow-lan: false", text)
        self.assertIn("bind-address: 127.0.0.1", text)
        self.assertIn("external-controller: ''", text)


if __name__ == "__main__":
    unittest.main()
