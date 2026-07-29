from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .proxy_manager import dola_proxy_available, fetch_proxy_from_api


ProxyFetcher = Callable[..., Awaitable[dict[str, str]]]
ProxyProbe = Callable[[str, float], Awaitable[bool]]
DUPLICATE_REFRESH_BACKOFF_SECONDS = 1.0


@dataclass(eq=False)
class _ApiProxySlot:
    server: str
    host_port: str
    node_id: str
    active: int = 0
    invalid: bool = False


class ApiProxyLease:
    def __init__(self, pool: "ReusableApiProxyPool", slot: _ApiProxySlot):
        self.pool = pool
        self.slot = slot
        self.server = slot.server
        self.host_port = slot.host_port
        self.node_id = slot.node_id
        self._released = False

    def invalidate(self) -> None:
        self.slot.invalid = True

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self.pool.release(self.slot)


class ReusableApiProxyPool:
    def __init__(
        self,
        max_endpoints: int,
        contexts_per_endpoint: int,
        *,
        max_concurrent_refreshes: int = 2,
        fetcher: ProxyFetcher = fetch_proxy_from_api,
        probe: ProxyProbe = dola_proxy_available,
    ):
        self.max_endpoints = max(1, int(max_endpoints))
        self.contexts_per_endpoint = max(1, int(contexts_per_endpoint))
        self.capacity = self.max_endpoints * self.contexts_per_endpoint
        self.max_concurrent_refreshes = max(1, min(self.max_endpoints, int(max_concurrent_refreshes)))
        self.fetcher = fetcher
        self.probe = probe
        self._slots: list[_ApiProxySlot] = []
        self._launching = 0
        self._last_error = ""
        self._condition = asyncio.Condition()
        self._stopping = False

    async def acquire(
        self,
        api_url: str,
        *,
        timeout_seconds: int,
        scheme: str,
        excluded_node_ids: set[str] | None = None,
    ) -> ApiProxyLease:
        excluded = {str(item) for item in excluded_node_ids or set() if str(item)}
        while True:
            create_endpoint = False
            known_node_ids: set[str] = set()
            async with self._condition:
                if self._stopping:
                    raise RuntimeError("api proxy pool is stopping")
                self._slots = [slot for slot in self._slots if not (slot.invalid and slot.active == 0)]
                candidates = [
                    slot
                    for slot in self._slots
                    if not slot.invalid and slot.node_id not in excluded and slot.active < self.contexts_per_endpoint
                ]
                if candidates:
                    selected = min(candidates, key=lambda slot: (slot.active, slot.node_id))
                    selected.active += 1
                    return ApiProxyLease(self, selected)
                known_node_ids = {slot.node_id for slot in self._slots}
                if (
                    len(self._slots) + self._launching < self.max_endpoints
                    and self._launching < self.max_concurrent_refreshes
                ):
                    self._launching += 1
                    create_endpoint = True
                else:
                    await self._condition.wait()
                    continue
            if not create_endpoint:
                continue
            try:
                endpoint = await self._fetch_available_endpoint(
                    api_url,
                    timeout_seconds=timeout_seconds,
                    scheme=scheme,
                    excluded_node_ids=excluded | known_node_ids,
                )
                slot = _ApiProxySlot(**endpoint, active=1)
                duplicate_saturated = False
                async with self._condition:
                    self._launching = max(0, self._launching - 1)
                    if self._stopping:
                        self._condition.notify_all()
                        raise RuntimeError("api proxy pool is stopping")
                    duplicate = next((item for item in self._slots if item.node_id == slot.node_id and not item.invalid), None)
                    if duplicate is not None:
                        if duplicate.node_id not in excluded and duplicate.active < self.contexts_per_endpoint:
                            duplicate.active += 1
                            self._last_error = ""
                            self._condition.notify_all()
                            return ApiProxyLease(self, duplicate)
                        duplicate_saturated = True
                        self._condition.notify_all()
                    else:
                        self._slots.append(slot)
                        self._last_error = ""
                        self._condition.notify_all()
                        return ApiProxyLease(self, slot)
                if duplicate_saturated:
                    await asyncio.sleep(DUPLICATE_REFRESH_BACKOFF_SECONDS)
                    continue
            except Exception as exc:
                detail = str(exc).strip()
                self._last_error = f"{type(exc).__name__}: {detail or 'no detail'}"[:500]
                async with self._condition:
                    self._launching = max(0, self._launching - 1)
                    self._condition.notify_all()
                raise

    async def _fetch_available_endpoint(
        self,
        api_url: str,
        *,
        timeout_seconds: int,
        scheme: str,
        excluded_node_ids: set[str],
    ) -> dict[str, str]:
        seen: set[str] = set()
        errors: list[str] = []
        fetch_errors = 0
        fetch_count = 0
        while len(seen) < 3 and fetch_count < 6:
            fetch_count += 1
            try:
                proxy = await self.fetcher(api_url, timeout_seconds=timeout_seconds, scheme=scheme)
            except Exception as exc:
                fetch_errors += 1
                detail = str(exc).strip()
                errors.append(f"{type(exc).__name__}: {detail or 'no detail'}"[:160])
                if fetch_errors >= 3:
                    break
                continue
            host_port = str(proxy.get("host_port") or "").strip()
            server = str(proxy.get("server") or "").strip()
            if not host_port or not server:
                errors.append("proxy api returned an empty endpoint")
                continue
            node_id = f"api:{host_port}"
            if node_id in seen:
                continue
            seen.add(node_id)
            if node_id in excluded_node_ids:
                continue
            if not await self.probe(server, min(12.0, float(timeout_seconds))):
                errors.append(f"{host_port}: unavailable for Dola")
                continue
            return {"server": server, "host_port": host_port, "node_id": node_id}
        detail = "; ".join(errors[-6:]) or "no distinct API proxy endpoint was available"
        raise RuntimeError(f"api proxy pool could not refresh an endpoint ({detail})")

    async def release(self, slot: _ApiProxySlot) -> None:
        async with self._condition:
            slot.active = max(0, slot.active - 1)
            if slot.invalid and slot.active == 0 and slot in self._slots:
                self._slots.remove(slot)
            self._condition.notify_all()

    async def stop(self) -> None:
        async with self._condition:
            self._stopping = True
            self._slots.clear()
            self._condition.notify_all()

    def snapshot(self) -> dict[str, Any]:
        active = sum(slot.active for slot in self._slots)
        available = sum(
            max(0, self.contexts_per_endpoint - slot.active)
            for slot in self._slots
            if not slot.invalid
        )
        return {
            "endpoint_limit": self.max_endpoints,
            "contexts_per_endpoint": self.contexts_per_endpoint,
            "capacity": self.capacity,
            "refresh_concurrency_limit": self.max_concurrent_refreshes,
            "refreshing": self._launching,
            "endpoints": len(self._slots),
            "active": active,
            "available": available,
            "potential_available": max(0, self.capacity - active),
            "last_error": self._last_error,
        }
