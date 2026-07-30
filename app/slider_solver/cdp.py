from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any


async def connect_over_cdp_with_retry(
    chromium: Any,
    endpoint: str,
    *,
    max_attempts: int = 40,
    delay_seconds: float = 0.5,
    sleep: Any = asyncio.sleep,
) -> Any:
    for attempt in range(max_attempts):
        try:
            return await chromium.connect_over_cdp(endpoint)
        except Exception:
            if attempt == max_attempts - 1:
                raise
            await sleep(delay_seconds)
    raise RuntimeError("unreachable")


async def find_slider_page(
    contexts: Iterable[Any],
    iframe_selector: str,
    *,
    url_contains: str = "",
) -> Any | None:
    """Return the first page that currently has a visible slider iframe."""
    for context in contexts:
        for page in context.pages:
            if url_contains and url_contains not in page.url:
                continue
            try:
                iframes = page.locator(iframe_selector)
                for index in range(await iframes.count()):
                    if await iframes.nth(index).is_visible():
                        return page
            except Exception:
                continue
    return None
