from __future__ import annotations

import asyncio
import unittest

from app.api_proxy_pool import ReusableApiProxyPool


class ApiProxyPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_concurrency_is_limited_to_two(self) -> None:
        fetching = 0
        maximum_fetching = 0
        fetched = 0

        async def fetcher(api_url: str, *, timeout_seconds: int, scheme: str) -> dict[str, str]:
            nonlocal fetching, maximum_fetching, fetched
            fetching += 1
            maximum_fetching = max(maximum_fetching, fetching)
            try:
                await asyncio.sleep(0.01)
                fetched += 1
                return {
                    "server": f"http://proxy-{fetched}.example:{18000 + fetched}",
                    "host_port": f"proxy-{fetched}.example:{18000 + fetched}",
                }
            finally:
                fetching -= 1

        async def probe(server: str, timeout: float) -> bool:
            await asyncio.sleep(0)
            return True

        pool = ReusableApiProxyPool(
            6,
            1,
            max_concurrent_refreshes=2,
            fetch_min_interval_seconds=0.001,
            fetcher=fetcher,
            probe=probe,
        )
        leases = await asyncio.wait_for(asyncio.gather(*(
            pool.acquire("https://proxy-api.example/get", timeout_seconds=20, scheme="http")
            for _ in range(6)
        )), timeout=2)
        self.assertEqual(maximum_fetching, 2)
        self.assertEqual(pool.snapshot()["refresh_concurrency_limit"], 2)
        self.assertEqual(pool.snapshot()["refreshing"], 0)
        self.assertEqual(pool.snapshot()["consecutive_refresh_failures"], 0)
        self.assertEqual(pool.snapshot()["available"], 0)
        await asyncio.gather(*(lease.release() for lease in leases))

    async def test_empty_timeout_error_includes_exception_type(self) -> None:
        async def fetcher(api_url: str, *, timeout_seconds: int, scheme: str) -> dict[str, str]:
            raise TimeoutError()

        async def probe(server: str, timeout: float) -> bool:
            return True

        pool = ReusableApiProxyPool(3, 4, fetch_min_interval_seconds=0, fetcher=fetcher, probe=probe)
        with self.assertRaisesRegex(RuntimeError, "TimeoutError: no detail"):
            await pool.acquire("https://proxy-api.example/get", timeout_seconds=20, scheme="http")
        snapshot = pool.snapshot()
        self.assertEqual(snapshot["refreshing"], 0)
        self.assertIn("TimeoutError: no detail", snapshot["last_error"])
        self.assertEqual(snapshot["consecutive_refresh_failures"], 1)
        self.assertGreater(snapshot["next_refresh_in_seconds"], 0)

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

        pool = ReusableApiProxyPool(3, 4, fetch_min_interval_seconds=0, fetcher=fetcher, probe=probe)
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

        pool = ReusableApiProxyPool(1, 4, fetch_min_interval_seconds=0, fetcher=fetcher, probe=probe)
        first = await pool.acquire("https://proxy-api.example/get", timeout_seconds=20, scheme="http")
        first_node = first.node_id
        first.invalidate()
        await first.release()
        second = await pool.acquire("https://proxy-api.example/get", timeout_seconds=20, scheme="http")
        self.assertNotEqual(second.node_id, first_node)
        self.assertEqual(fetched, 2)
        await second.release()

    async def test_parallel_refreshes_stagger_proxy_api_requests(self) -> None:
        started: list[float] = []

        async def fetcher(api_url: str, *, timeout_seconds: int, scheme: str) -> dict[str, str]:
            started.append(asyncio.get_running_loop().time())
            endpoint = len(started)
            return {
                "server": f"http://proxy-{endpoint}.example:{18000 + endpoint}",
                "host_port": f"proxy-{endpoint}.example:{18000 + endpoint}",
            }

        async def probe(server: str, timeout: float) -> bool:
            await asyncio.sleep(0.02)
            return True

        pool = ReusableApiProxyPool(
            2,
            1,
            max_concurrent_refreshes=2,
            fetch_min_interval_seconds=0.08,
            fetcher=fetcher,
            probe=probe,
        )
        leases = await asyncio.gather(*(
            pool.acquire("https://proxy-api.example/get", timeout_seconds=20, scheme="http")
            for _ in range(2)
        ))

        self.assertEqual(len(started), 2)
        self.assertGreaterEqual(started[1] - started[0], 0.05)
        self.assertEqual(len({lease.node_id for lease in leases}), 2)
        await asyncio.gather(*(lease.release() for lease in leases))


if __name__ == "__main__":
    unittest.main()
