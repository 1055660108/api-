from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import worker


class WorkerEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_writer_publishes_fresh_manager_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            with patch.object(worker, "DATA_DIR", data_dir), patch.object(worker.manager, "health_snapshot", return_value={"ok": True, "worker_alive": 1}):
                task = asyncio.create_task(worker.write_health())
                await asyncio.sleep(0.05)
                payload = json.loads((data_dir / ".worker-health.json").read_text(encoding="utf-8"))
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["worker_alive"], 1)
        self.assertLess(time.time() - payload["updated_at"], 2)


if __name__ == "__main__":
    unittest.main()
