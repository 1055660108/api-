from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import admin_auth, config, main, resilience, store


class ResilienceUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        resilience._LOCAL_BUCKETS.clear()
        resilience._LOCAL_CIRCUITS.clear()

    def tearDown(self) -> None:
        resilience._LOCAL_BUCKETS.clear()
        resilience._LOCAL_CIRCUITS.clear()

    def test_platform_rate_limit_is_shared_by_platform(self) -> None:
        with patch.dict(os.environ, {"DOLA_PLATFORM_RATE_PER_MINUTE": "1", "DOLA_PLATFORM_BURST": "2"}):
            first = resilience.PlatformGuard(namespace="test").admit("dola")
            second = resilience.PlatformGuard(namespace="test").admit("dola")
            limited = resilience.PlatformGuard(namespace="test").admit("dola")
        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)
        self.assertFalse(limited.allowed)
        self.assertEqual(limited.reason, "rate_limited")
        self.assertGreaterEqual(limited.retry_after, 1)

    def test_circuit_opens_and_allows_single_half_open_probe(self) -> None:
        environment = {
            "DOLA_PLATFORM_RATE_PER_MINUTE": "600",
            "DOLA_PLATFORM_BURST": "10",
            "DOLA_CIRCUIT_FAILURE_THRESHOLD": "2",
            "DOLA_CIRCUIT_RECOVERY_SECONDS": "10",
        }
        guard = resilience.PlatformGuard(namespace="circuit")
        with patch.dict(os.environ, environment), patch("app.resilience.time.time", return_value=100.0):
            guard.record_failure("doubao")
            guard.record_failure("doubao")
            blocked = guard.admit("doubao")
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "circuit_open")
        with patch.dict(os.environ, environment), patch("app.resilience.time.time", return_value=111.0):
            probe = guard.admit("doubao")
            second = guard.admit("doubao")
        self.assertTrue(probe.allowed)
        self.assertFalse(second.allowed)
        guard.record_success("doubao")
        self.assertEqual(guard.snapshot("doubao")["state"], "closed")

    def test_adaptive_worker_limit_reacts_to_memory_pressure(self) -> None:
        environment = {"DOLA_MEMORY_HIGH_RATIO": "0.80", "DOLA_MEMORY_CRITICAL_RATIO": "0.90", "DOLA_MINIMUM_WORKERS": "2", "DOLA_MAX_EFFECTIVE_WORKERS": "20"}
        with patch.dict(os.environ, environment), patch("app.resilience.memory_pressure", return_value=(0.95, 95, 100)):
            effective, snapshot = resilience.adaptive_worker_limit(12)
        self.assertEqual(effective, 2)
        self.assertEqual(snapshot["level"], "critical")
        with patch.dict(os.environ, environment), patch("app.resilience.memory_pressure", return_value=(0.50, 50, 100)):
            effective, snapshot = resilience.adaptive_worker_limit(12)
        self.assertEqual(effective, 12)
        self.assertEqual(snapshot["level"], "normal")

    def test_worker_limit_caps_low_pressure_browser_concurrency(self) -> None:
        with patch.dict(os.environ, {"DOLA_MAX_EFFECTIVE_WORKERS": "8"}), patch("app.resilience.memory_pressure", return_value=(0.03, 128, 4096)):
            effective, snapshot = resilience.adaptive_worker_limit(100)
        self.assertEqual(effective, 8)
        self.assertEqual(snapshot["configured_workers"], 100)
        self.assertEqual(snapshot["capacity_limit"], 8)

    def test_queue_high_watermark_rejects_new_work(self) -> None:
        with patch.dict(os.environ, {"DOLA_QUEUE_HIGH_WATERMARK": "3"}):
            admission = resilience.queue_admission({"ready": 2, "processing": 1, "delayed": 0})
        self.assertFalse(admission.allowed)
        self.assertEqual(admission.reason, "queue_overloaded")


class ResilienceCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.tasks = self.root / "tasks"
        self.patchers = [
            patch.object(config, "CONFIG_PATH", self.root / "config.json"),
            patch.object(config, "DATA_DIR", self.root),
            patch.object(config, "TASKS_DIR", self.tasks),
            patch.object(store, "TASKS_DIR", self.tasks),
            patch.object(store, "runtime_path", return_value=self.root / "runtime.json"),
            patch.dict(os.environ, {"DOLA_ADMIN_USERNAME": "admin", "DOLA_ADMIN_PASSWORD": "password123"}),
        ]
        for patcher in self.patchers:
            patcher.start()
        admin_auth.clear_sessions()
        config.ensure_config()
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()
        self.token = config.load_settings().api_token

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        admin_auth.clear_sessions()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def test_health_preserves_fields_and_adds_resilience_state(self) -> None:
        response = self.client.get("/health", headers={"X-API-Token": self.token})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        for key in ("ok", "version", "status", "role", "browser_workers", "active", "components"):
            self.assertIn(key, payload)
        self.assertIn("resources", payload["components"])
        self.assertEqual(set(payload["components"]["platforms"]), {"dola", "doubao", "qianwen"})

    def test_standard_api_overload_keeps_fastapi_error_shape(self) -> None:
        queue = unittest.mock.Mock()
        queue.health.return_value = {"ok": True, "backend": "redis", "ready": 3, "processing": 0, "delayed": 0}
        with patch.dict(os.environ, {"DOLA_QUEUE_HIGH_WATERMARK": "3"}), patch("app.task_queue.get_task_queue", return_value=queue):
            response = self.client.post(
                "/tasks",
                headers={"X-API-Token": self.token},
                data={"prompt": "测试", "ratio": "9:16", "platform": "dola", "model": "Seedance 2.0"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "任务队列繁忙，请稍后重试"})
        self.assertEqual(response.headers["retry-after"], "10")

    def test_openai_overload_keeps_openai_error_shape(self) -> None:
        queue = unittest.mock.Mock()
        queue.health.return_value = {"ok": True, "backend": "redis", "ready": 3, "processing": 0, "delayed": 0}
        with patch.dict(os.environ, {"DOLA_QUEUE_HIGH_WATERMARK": "3"}), patch("app.task_queue.get_task_queue", return_value=queue):
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"model": "dola:Seedance 2.0", "messages": [{"role": "user", "content": "测试"}]},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "service_unavailable")
        self.assertEqual(response.headers["retry-after"], "10")


if __name__ == "__main__":
    unittest.main()
