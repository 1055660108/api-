from __future__ import annotations

import asyncio
import unittest

from app.api_proxy_pool import ReusableApiProxyPool


class ApiProxyPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_pool_provides_real_context_capacity(self) -> None:
        fetched = 0

        async def fetcher(api_url: str, *, timeout_seconds: int, scheme: str) -> dict[str, str]:
            nonlocal fetched
            fetched += 1
            return {
                "server": f"http://proxy-{fetched}.example:{18000 + fetched}",
                "host_port": f"proxy-{fetched}.example:{18000 + fetched}",
            }

        async def probe(server: str, timeout: float) -> bool:
            return True

        pool = ReusableApiProxyPool(3, 4, fetcher=fetcher, probe=probe)
        leases = await asyncio.gather(*(
            pool.acquire("https://proxy-api.example/get", timeout_seconds=20, scheme="http")
            for _ in range(12)
        ))
        snapshot = pool.snapshot()
        self.assertEqual(snapshot["capacity"], 12)
        self.assertEqual(snapshot["endpoints"], 3)
        self.assertEqual(snapshot["active"], 12)
        self.assertEqual(len({lease.node_id for lease in leases}), 3)
        await asyncio.gather(*(lease.release() for lease in leases))
        self.assertEqual(pool.snapshot()["active"], 0)

    async def test_invalid_endpoint_is_removed_and_replaced(self) -> None:
        fetched = 0

        async def fetcher(api_url: str, *, timeout_seconds: int, scheme: str) -> dict[str, str]:
            nonlocal fetched
            fetched += 1
            return {
                "server": f"http://proxy-{fetched}.example:{18000 + fetched}",
                "host_port": f"proxy-{fetched}.example:{18000 + fetched}",
            }

        async def probe(server: str, timeout: float) -> bool:
            return True

        pool = ReusableApiProxyPool(1, 4, fetcher=fetcher, probe=probe)
        first = await pool.acquire("https://proxy-api.example/get", timeout_seconds=20, scheme="http")
        first_node = first.node_id
        first.invalidate()
        await first.release()
        second = await pool.acquire("https://proxy-api.example/get", timeout_seconds=20, scheme="http")
        self.assertNotEqual(second.node_id, first_node)
        self.assertEqual(fetched, 2)
        await second.release()


if __name__ == "__main__":
    unittest.main()
