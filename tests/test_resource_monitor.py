from __future__ import annotations

import unittest
from collections import namedtuple
from unittest.mock import Mock, patch

from app import resource_monitor


DiskUsage = namedtuple("DiskUsage", "total used free")


class ResourceMonitorTests(unittest.TestCase):
    def test_disk_and_container_memory_thresholds_create_alerts(self) -> None:
        queue = Mock(backend="file", client=None)
        with patch.object(resource_monitor.shutil, "disk_usage", return_value=DiskUsage(100, 90, 10)), patch.object(
            resource_monitor, "memory_pressure", return_value=(0.82, 82, 100)
        ), patch.object(
            resource_monitor, "_worker_memory", return_value={"ok": True, "ratio": 0.2, "used_bytes": 20, "limit_bytes": 100, "level": "normal"}
        ), patch.object(resource_monitor, "get_task_queue", return_value=queue), patch.object(resource_monitor.postgres, "enabled", return_value=False):
            snapshot = resource_monitor.collect_resource_snapshot()
        self.assertEqual(snapshot["level"], "critical")
        self.assertEqual(snapshot["disk"]["level"], "critical")
        self.assertEqual(snapshot["memory"]["api"]["level"], "warning")
        self.assertEqual({item["id"] for item in snapshot["alerts"]}, {"disk", "api_memory"})

    def test_redis_and_postgres_are_sampled_without_affecting_request_path(self) -> None:
        redis_client = Mock()
        redis_client.info.side_effect = [
            {"used_memory": 64 * 1024 * 1024, "maxmemory": 512 * 1024 * 1024},
            {"connected_clients": 8},
        ]
        queue = Mock(backend="redis", client=redis_client)
        postgres_health = {"ok": True, "connections": 12, "max_connections": 100, "database_size_bytes": 1024, "latency_ms": 2.0, "pool": {}}
        with patch.object(resource_monitor.shutil, "disk_usage", return_value=DiskUsage(100, 20, 80)), patch.object(
            resource_monitor, "memory_pressure", return_value=(0.2, 20, 100)
        ), patch.object(
            resource_monitor, "_worker_memory", return_value={"ok": True, "ratio": 0.2, "used_bytes": 20, "limit_bytes": 100, "level": "normal"}
        ), patch.object(resource_monitor, "get_task_queue", return_value=queue), patch.object(
            resource_monitor.postgres, "enabled", return_value=True
        ), patch.object(resource_monitor.postgres, "health_snapshot", return_value=postgres_health):
            snapshot = resource_monitor.collect_resource_snapshot()
        self.assertEqual(snapshot["level"], "normal")
        self.assertEqual(snapshot["redis"]["connected_clients"], 8)
        self.assertEqual(snapshot["postgres"]["connection_ratio"], 0.12)
        self.assertEqual(resource_monitor.latest_resource_snapshot()["updated_at"], snapshot["updated_at"])


if __name__ == "__main__":
    unittest.main()
