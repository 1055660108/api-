from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from . import config as app_config


CAPTURE_INTERVAL_SECONDS = 1.5
CAPTURE_TIMEOUT_MS = 2500
CAPTURE_JPEG_QUALITY = 55
VIEW_RETENTION_SECONDS = 60 * 60
VIEW_REQUEST_SECONDS = 8.0


def live_view_dir() -> Path:
    return app_config.DATA_DIR / "browser_live_views"


def live_view_paths(task_id: str) -> tuple[Path, Path]:
    base = live_view_dir()
    return base / f"{task_id}.json", base / f"{task_id}.jpg"


def _live_view_request_path(task_id: str) -> Path:
    return live_view_dir() / f"{task_id}.watch"


def request_live_view(task_id: str, duration_seconds: float = VIEW_REQUEST_SECONDS) -> None:
    path = _live_view_request_path(task_id)
    expires_at = time.time() + max(2.0, float(duration_seconds))
    try:
        _atomic_write(path, str(expires_at).encode("ascii"))
    except OSError:
        pass


def live_view_requested(task_id: str) -> bool:
    path = _live_view_request_path(task_id)
    try:
        expires_at = float(path.read_text(encoding="ascii"))
    except (FileNotFoundError, OSError, ValueError):
        return False
    if expires_at > time.time():
        return True
    path.unlink(missing_ok=True)
    return False


def read_live_view(task_id: str) -> dict[str, Any]:
    metadata_path, frame_path = live_view_paths(task_id)
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        payload["frame_available"] = frame_path.is_file() and frame_path.stat().st_size > 0
    except OSError:
        payload["frame_available"] = False
    return payload


def browser_live_frame_path(task_id: str) -> Path | None:
    _metadata_path, frame_path = live_view_paths(task_id)
    try:
        available = frame_path.is_file() and frame_path.stat().st_size > 0
    except OSError:
        available = False
    if not available:
        return None
    return frame_path


def cleanup_stale_live_views(max_age_seconds: float = VIEW_RETENTION_SECONDS) -> int:
    root = live_view_dir()
    if not root.is_dir():
        return 0
    cutoff = time.time() - max(60.0, float(max_age_seconds))
    removed = 0
    for path in root.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class TaskBrowserLiveView:
    def __init__(self, task_id: str, platform: str):
        self.task_id = str(task_id or "").strip()
        self.platform = str(platform or "").strip().lower()
        self.session_id = secrets.token_hex(8)
        self.page: Page | None = None
        self._capture_task: asyncio.Task[None] | None = None
        self._started_at = time.time()
        self._sequence = 0

    async def start(self, page: Page) -> None:
        if not self.task_id or self._capture_task is not None:
            return
        self.page = page
        try:
            metadata_path, frame_path = live_view_paths(self.task_id)
            await asyncio.to_thread(metadata_path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(frame_path.unlink, missing_ok=True)
            await self._write_metadata(active=True, frame_available=False)
            self._capture_task = asyncio.create_task(self._capture_loop())
        except Exception:
            self.page = None

    async def stop(self) -> None:
        capture_task = self._capture_task
        self._capture_task = None
        if capture_task is not None:
            capture_task.cancel()
            await asyncio.gather(capture_task, return_exceptions=True)
        try:
            current = await asyncio.to_thread(read_live_view, self.task_id)
            if current.get("session_id") == self.session_id:
                await self._write_metadata(
                    active=False,
                    frame_available=bool(current.get("frame_available")),
                    closed_at=time.time(),
                    last_error=str(current.get("last_error") or ""),
                )
        except Exception:
            pass
        self.page = None

    async def _capture_loop(self) -> None:
        while True:
            try:
                requested = await asyncio.to_thread(live_view_requested, self.task_id)
                if requested:
                    await self._capture_once()
                else:
                    current = await asyncio.to_thread(read_live_view, self.task_id)
                    await self._write_metadata(
                        active=True,
                        frame_available=bool(current.get("frame_available")),
                        url=str(self.page.url or "")[:1500] if self.page is not None else "",
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                requested = False
            await asyncio.sleep(CAPTURE_INTERVAL_SECONDS if requested else 3.0)

    async def _capture_once(self) -> None:
        page = self.page
        if page is None or page.is_closed():
            await self._write_metadata(active=False, frame_available=False, last_error="browser page closed")
            return
        metadata_path, frame_path = live_view_paths(self.task_id)
        try:
            frame = await page.screenshot(
                type="jpeg",
                quality=CAPTURE_JPEG_QUALITY,
                full_page=False,
                timeout=CAPTURE_TIMEOUT_MS,
            )
            self._sequence += 1
            await asyncio.to_thread(_atomic_write, frame_path, frame)
            title = ""
            with suppress(Exception):
                title = await page.title()
            await self._write_metadata(
                active=True,
                frame_available=True,
                title=title[:300],
                url=str(page.url or "")[:1500],
                sequence=self._sequence,
                frame_bytes=len(frame),
                last_error="",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            current = await asyncio.to_thread(read_live_view, self.task_id)
            await self._write_metadata(
                active=True,
                frame_available=bool(current.get("frame_available")),
                last_error=f"{type(exc).__name__}: {str(exc)[:300]}",
            )

    async def _write_metadata(self, *, active: bool, frame_available: bool, **extra: Any) -> None:
        metadata_path, _frame_path = live_view_paths(self.task_id)
        now = time.time()
        current = await asyncio.to_thread(read_live_view, self.task_id)
        if current.get("session_id") != self.session_id:
            current = {}
        payload = {
            **current,
            "task_id": self.task_id,
            "platform": self.platform,
            "session_id": self.session_id,
            "active": bool(active),
            "frame_available": bool(frame_available),
            "started_at": self._started_at,
            "updated_at": now,
            **extra,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        await asyncio.to_thread(_atomic_write, metadata_path, encoded)
