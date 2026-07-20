from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import acceptance_linux, backup_restore, docker_fault_injection, ladder_concurrency, prepare_https, validation_common


class ValidationCommonTests(unittest.TestCase):
    def test_test_environment_guard_requires_explicit_opt_in(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "仅允许测试环境"):
                validation_common.require_test_environment("http://127.0.0.1:8088")
        with patch.dict(os.environ, {"DOLA_TEST_ENV": "test"}, clear=True):
            validation_common.require_test_environment("http://127.0.0.1:8088")
            with self.assertRaisesRegex(RuntimeError, "默认仅允许本机地址"):
                validation_common.require_test_environment("https://production.example.com")

    def test_size_pair_and_percentile_are_stable(self) -> None:
        self.assertEqual(validation_common.parse_pair("1.5GiB / 4GiB"), (1610612736, 4294967296))
        self.assertEqual(validation_common.percentile([40, 10, 30, 20], 0.95), 30)

    def test_report_writer_creates_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            json_path, markdown_path = validation_common.write_report(Path(directory), "result", {"passed": True}, "# 通过")
            self.assertIn('"passed": true', json_path.read_text(encoding="utf-8"))
            self.assertEqual(markdown_path.read_text(encoding="utf-8"), "# 通过")


class LadderConcurrencyTests(unittest.TestCase):
    def test_stage_parser_rejects_unsafe_values(self) -> None:
        self.assertEqual(ladder_concurrency.parse_stages("1,5,10"), [1, 5, 10])
        with self.assertRaises(Exception):
            ladder_concurrency.parse_stages("0,5")

    def test_stage_summary_reports_latency_and_resource_peaks(self) -> None:
        results = [{"status": 200, "latency_ms": 10}, {"status": 503, "latency_ms": 30}, {"status": 200, "latency_ms": 20}]
        resources = [
            {"name": "api", "cpu_percent": 12.0, "memory_used_bytes": 100, "memory_percent": 3.0, "pids": 5},
            {"name": "api", "cpu_percent": 18.0, "memory_used_bytes": 90, "memory_percent": 2.5, "pids": 7},
        ]
        summary = ladder_concurrency.summarize_stage(3, 1.0, results, resources)
        self.assertEqual(summary["successes"], 2)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["latency_ms"]["p95"], 20)
        self.assertEqual(summary["resource_peaks"]["api"]["cpu_percent"], 18.0)
        self.assertEqual(summary["resource_peaks"]["api"]["memory_used_bytes"], 100)


class FaultInjectionTests(unittest.TestCase):
    def test_supported_services_are_non_destructive(self) -> None:
        self.assertEqual(docker_fault_injection.SERVICES, ("redis", "postgres", "worker"))
        report = {
            "finished_at": "2026-01-01T00:00:00+00:00",
            "compose_file": "compose.yaml",
            "passed": True,
            "events": [{"service": "redis", "fault": {"observed": True}, "recovery": {"observed": True, "dependency_probe": True, "api": {"status": 200}}, "passed": True}],
        }
        markdown = docker_fault_injection.render_markdown(report)
        self.assertIn("不执行 `down -v`", markdown)
        self.assertIn("| redis | True | True | True | True | 通过 |", markdown)

    def test_failed_event_is_rendered_without_recovery_fields(self) -> None:
        report = {
            "finished_at": "2026-01-01T00:00:00+00:00",
            "compose_file": "compose.yaml",
            "passed": False,
            "events": [{"service": "worker", "passed": False, "error": "startup failed"}],
        }
        markdown = docker_fault_injection.render_markdown(report)
        self.assertIn("| worker | False | False | False | False | 未通过 |", markdown)


class LinuxAcceptanceTests(unittest.TestCase):
    def valid_environment(self) -> dict[str, str]:
        return {
            "DOLA_TEST_ENV": "test",
            "DOLA_ACCEPTANCE_ENV": "acceptance",
            "COMPOSE_PROJECT_NAME": "dola-acceptance-unit",
            "POSTGRES_PASSWORD": "postgres-password-123456",
            "DOLA_ADMIN_PASSWORD": "admin-password-123456",
        }

    def test_security_gate_requires_linux_and_isolated_project(self) -> None:
        environment = self.valid_environment()
        with patch("scripts.acceptance_linux.platform.system", return_value="Linux"), patch.dict(os.environ, environment, clear=True):
            result = acceptance_linux.security_gate("http://127.0.0.1:8088", environment, False)
        self.assertEqual(result["project"], "dola-acceptance-unit")
        environment["COMPOSE_PROJECT_NAME"] = "dola-fetch"
        with patch("scripts.acceptance_linux.platform.system", return_value="Linux"), patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "禁止复用生产卷"):
                acceptance_linux.security_gate("http://127.0.0.1:8088", environment, False)

    def test_security_gate_rejects_default_passwords_and_non_linux(self) -> None:
        environment = self.valid_environment()
        with patch("scripts.acceptance_linux.platform.system", return_value="Windows"):
            with self.assertRaisesRegex(RuntimeError, "仅允许在 Linux"):
                acceptance_linux.security_gate("http://127.0.0.1:8088", environment, False)
        environment["POSTGRES_PASSWORD"] = "change-me"
        with patch("scripts.acceptance_linux.platform.system", return_value="Linux"), patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "非默认密码"):
                acceptance_linux.security_gate("http://127.0.0.1:8088", environment, False)

    def test_four_service_readiness_requires_every_service(self) -> None:
        self.assertEqual(acceptance_linux.FAULT_SERVICES, ("api", "redis", "postgres", "worker"))
        states = {service: {"status": "running", "health": "healthy"} for service in acceptance_linux.SERVICES}
        self.assertTrue(acceptance_linux.all_services_ready(states))
        states["worker"]["health"] = "unhealthy"
        self.assertFalse(acceptance_linux.all_services_ready(states))

    def test_json_snapshot_summary_tracks_counts_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime.json").write_text(json.dumps({"active_task_ids": []}), encoding="utf-8")
            task = root / "tasks" / ("a" * 32)
            task.mkdir(parents=True)
            (task / "meta.json").write_text(json.dumps({"id": "a" * 32}), encoding="utf-8")
            summary = acceptance_linux.json_snapshot_summary(root)
            self.assertEqual((summary["tasks"], summary["documents"], summary["files"]), (1, 1, 2))
            self.assertEqual(len(summary["sha256"]), 64)

    def test_real_dola_stages_are_explicit_and_limited(self) -> None:
        self.assertEqual(acceptance_linux.parse_real_dola_stages("1,3,5"), [1, 3, 5])
        self.assertEqual(acceptance_linux.parse_real_dola_stages(""), [])
        with self.assertRaisesRegex(ValueError, "1,3,5"):
            acceptance_linux.parse_real_dola_stages("1,2")

    def test_probe_monitor_separates_expected_api_outage(self) -> None:
        monitor = acceptance_linux.ProbeMonitor("http://127.0.0.1:8088", "token", 1)
        monitor.samples = [
            {"target": name, "stage": "故障恢复:api", "expected_outage": True, "ok": False}
            for name, _, _ in acceptance_linux.PROBE_TARGETS
        ]
        monitor._thread = unittest.mock.Mock()
        result = monitor.stop()
        self.assertTrue(result["passed"])
        self.assertEqual(result["counts"]["admin"]["expected_failed"], 1)


class HttpsPreparationTests(unittest.TestCase):
    def test_allowlist_rejects_public_network_and_normalizes_hosts(self) -> None:
        self.assertEqual(prepare_https.validate_cidrs(["192.0.2.10", "2001:db8::10"]), ["192.0.2.10/32", "2001:db8::10/128"])
        with self.assertRaisesRegex(ValueError, "全网开放"):
            prepare_https.validate_cidrs(["0.0.0.0/0"])

    def test_nginx_configuration_enforces_https_and_allowlist(self) -> None:
        rendered = prepare_https.render_nginx("acceptance.example.com", Path("/cert.pem"), Path("/key.pem"), ["192.0.2.10/32"], "http://127.0.0.1:8088")
        self.assertIn("return 301 https://$host$request_uri", rendered)
        self.assertIn("allow 192.0.2.10/32;", rendered)
        self.assertIn("deny all;", rendered)
        self.assertIn("ssl_protocols TLSv1.2 TLSv1.3;", rendered)


class BackupRestoreTests(unittest.TestCase):
    def test_checksum_and_report_include_restore_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "postgres.sql"
            artifact.write_bytes(b"select 1;")
            self.assertEqual(len(backup_restore.sha256(artifact)), 64)
        report = {"finished_at": "2026-01-01T00:00:00+00:00", "passed": True, "backup": {"artifacts": {"postgres.sql": {"bytes": 9, "sha256": "abc"}}}, "restore": {"database_tables": 3}}
        markdown = backup_restore.render_markdown(report)
        self.assertIn("postgres.sql", markdown)
        self.assertIn("业务表数：3", markdown)


if __name__ == "__main__":
    unittest.main()
