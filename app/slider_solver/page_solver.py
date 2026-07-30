from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote_to_bytes

from .image_solver import solve_gap
from .motion import build_drag_path
from .types import Box, SliderSolveResult, SliderSolverSettings

Downloader = Callable[[str], Awaitable[bytes]]


class SliderChallengeSolver:
    def __init__(
        self,
        settings: SliderSolverSettings | None = None,
        *,
        downloader: Downloader | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings or SliderSolverSettings()
        self._downloader = downloader
        self._logger = logger or logging.getLogger(__name__)
        self._last_displacement: float | None = None
        self._last_confidence: float | None = None
        self._last_verify_response: dict[str, Any] | None = None

    async def solve(self, page: Any) -> SliderSolveResult:
        iframe_index = await self._visible_iframe_index(page)
        if iframe_index is None:
            return SliderSolveResult(status="not_present", attempts=0)

        last_error: str | None = None
        for attempt in range(1, self.settings.max_attempts + 1):
            self._last_displacement = None
            self._last_confidence = None
            self._last_verify_response = None
            try:
                if attempt > 1 or self.settings.refresh_before_solve:
                    await self._refresh(page, iframe_index)
                if await self._solve_once(page, iframe_index):
                    return SliderSolveResult(
                        status="success",
                        attempts=attempt,
                        displacement=self._last_displacement,
                        confidence=self._last_confidence,
                        verify_response=self._last_verify_response,
                    )
                last_error = "verification dialog remained visible"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self._logger.warning("slider attempt %s failed: %s", attempt, last_error)

            if attempt < self.settings.max_attempts:
                iframe_index = await self._visible_iframe_index(page)
                if iframe_index is None:
                    return SliderSolveResult(status="success", attempts=attempt)

        return SliderSolveResult(
            status="failed",
            attempts=self.settings.max_attempts,
            displacement=self._last_displacement,
            confidence=self._last_confidence,
            verify_response=self._last_verify_response,
            error=last_error,
        )

    async def _solve_once(self, page: Any, iframe_index: int) -> bool:
        frame = page.frame_locator(self.settings.iframe_selector).nth(iframe_index)
        background = frame.locator(self.settings.background_selector)
        piece = frame.locator(self.settings.piece_selector)
        handle = frame.locator(self.settings.handle_selector)

        background_url, piece_url = await asyncio.gather(
            background.evaluate("element => element.currentSrc || element.src || ''"),
            piece.evaluate("element => element.currentSrc || element.src || ''"),
        )
        if not background_url or not piece_url:
            raise ValueError("challenge image URL is missing")

        background_box_raw, handle_box_raw, background_bytes, piece_bytes = await asyncio.gather(
            background.bounding_box(),
            handle.bounding_box(),
            self._read_image(page, background, str(background_url)),
            self._read_image(page, piece, str(piece_url)),
        )
        if background_box_raw is None or handle_box_raw is None:
            raise ValueError("challenge element bounding box is missing")

        image_result = solve_gap(background_bytes, piece_bytes)
        if image_result.confidence < self.settings.minimum_confidence:
            raise ValueError(
                f"image confidence {image_result.confidence:.3f} is below "
                f"{self.settings.minimum_confidence:.3f}"
            )

        background_box = Box.from_mapping(background_box_raw)
        handle_box = Box.from_mapping(handle_box_raw)
        displacement = image_result.display_displacement(background_box.width)
        path = build_drag_path(
            handle_box,
            displacement=displacement,
            steps=self.settings.drag_steps,
            overshoot=self.settings.drag_overshoot,
        )

        self._last_displacement = displacement
        self._last_confidence = image_result.confidence
        loop = asyncio.get_running_loop()
        verify_future: asyncio.Future[dict[str, Any] | None] = loop.create_future()

        async def read_verify_response(response: Any) -> None:
            if self.settings.verify_url_fragment not in response.url or verify_future.done():
                return
            try:
                payload = await response.json()
            except Exception:
                payload = {"status": response.status, "text": await response.text()}
            if not verify_future.done():
                verify_future.set_result(payload if isinstance(payload, dict) else {"data": payload})

        def on_response(response: Any) -> None:
            asyncio.create_task(read_verify_response(response))

        page.on("response", on_response)
        try:
            await page.mouse.move(*path[0])
            await page.mouse.down()
            try:
                for point in path[1:]:
                    await page.mouse.move(*point)
                    await asyncio.sleep(self.settings.step_delay_seconds)
            finally:
                await page.mouse.up()

            deadline = loop.time() + self.settings.verify_timeout_seconds
            while loop.time() < deadline:
                if await self._visible_iframe_index(page) is None:
                    if verify_future.done():
                        self._last_verify_response = verify_future.result()
                    return True
                if verify_future.done():
                    self._last_verify_response = verify_future.result()
                await asyncio.sleep(0.05)
            return False
        finally:
            page.remove_listener("response", on_response)
            if not verify_future.done():
                verify_future.cancel()

    async def _refresh(self, page: Any, iframe_index: int) -> None:
        frame = page.frame_locator(self.settings.iframe_selector).nth(iframe_index)
        background = frame.locator(self.settings.background_selector)
        previous_url = await background.evaluate("element => element.currentSrc || element.src || ''")
        refresh = frame.get_by_text(self.settings.refresh_text, exact=True)
        if await refresh.count() != 1:
            raise ValueError("refresh control is missing or ambiguous")
        await refresh.click()

        deadline = asyncio.get_running_loop().time() + self.settings.refresh_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            current_url = await background.evaluate("element => element.currentSrc || element.src || ''")
            if current_url and current_url != previous_url:
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("challenge image did not refresh")

    async def _visible_iframe_index(self, page: Any) -> int | None:
        iframes = page.locator(self.settings.iframe_selector)
        for index in range(await iframes.count()):
            with contextlib.suppress(Exception):
                if await iframes.nth(index).is_visible():
                    return index
        return None

    async def _read_image(self, page: Any, locator: Any, url: str) -> bytes:
        if self._downloader is not None:
            return await self._downloader(url)
        if url.startswith("data:"):
            header, _, payload = url.partition(",")
            if not payload:
                raise ValueError("invalid data image URL")
            if ";base64" in header.lower():
                return base64.b64decode(payload, validate=True)
            return unquote_to_bytes(payload)
        if url.startswith("blob:"):
            return await self._read_image_in_frame(locator)

        request = getattr(getattr(page, "context", None), "request", None)
        if request is not None:
            try:
                headers = {"referer": str(getattr(page, "url", ""))} if getattr(page, "url", "") else None
                response = await request.get(url, headers=headers, timeout=10_000)
                if response.ok:
                    return await response.body()
            except Exception as exc:
                self._logger.debug("browser-context image download failed: %s", exc)
        return await self._read_image_in_frame(locator)

    @staticmethod
    async def _read_image_in_frame(locator: Any) -> bytes:
        values = await locator.evaluate(
            """
            async element => {
              const response = await fetch(element.currentSrc || element.src, {
                credentials: "include",
                cache: "no-store"
              });
              if (!response.ok) throw new Error(`image request failed ${response.status}`);
              return Array.from(new Uint8Array(await response.arrayBuffer()));
            }
            """
        )
        if not isinstance(values, list):
            raise ValueError("browser image response is invalid")
        return bytes(values)
