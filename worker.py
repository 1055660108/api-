from __future__ import annotations

import asyncio
import json
import signal
import time

from app.config import DATA_DIR, ensure_config
from app.postgres import enabled as postgres_enabled
from app.postgres import ensure_schema as ensure_postgres_schema
from app.worker import manager


async def write_health() -> None:
    health_path = DATA_DIR / ".worker-health.json"
    while True:
        payload = {**manager.health_snapshot(), "updated_at": time.time()}
        temporary = health_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(health_path)
        await asyncio.sleep(10)


async def run_worker() -> None:
    ensure_config()
    if postgres_enabled():
        ensure_postgres_schema()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stopped.set)
        except NotImplementedError:
            pass
    await manager.start()
    health_task = asyncio.create_task(write_health())
    try:
        await stopped.wait()
    finally:
        health_task.cancel()
        await asyncio.gather(health_task, return_exceptions=True)
        (DATA_DIR / ".worker-health.json").unlink(missing_ok=True)
        await manager.stop()


def main() -> None:
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
