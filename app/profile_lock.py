from __future__ import annotations

import asyncio


_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
_GUARD = asyncio.Lock()


async def account_profile_lock(platform: str, account_id: str) -> asyncio.Lock:
    key = (str(platform or "").strip().lower(), str(account_id or "").strip().lower())
    async with _GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS[key] = lock
        return lock
