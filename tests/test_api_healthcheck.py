from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import api_healthcheck


class APIHealthcheckTests(unittest.TestCase):
    def test_api_process_fd_count_finds_python_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            process = root / "6"
            (process / "fd").mkdir(parents=True)
            (process / "cmdline").write_bytes(b"python\0run.py\0")
            (process / "comm").write_text("python", encoding="utf-8")
            (process / "stat").write_text(f"6 (python) {' '.join(['S', *(['0'] * 18), '12345'])}", encoding="ascii")
            for name in ("0", "1", "2", "8"):
                (process / "fd" / name).touch()

            self.assertEqual(api_healthcheck.api_process_fd_count(root), 4)
            self.assertEqual(api_healthcheck.api_process_snapshot(root), ("6:12345", 4))

    def test_five_consecutive_failures_terminate_container(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
            api_healthcheck, "FAILURE_STATE_PATH", Path(temporary_directory) / "failures"
        ), patch.object(api_healthcheck, "api_process_identity", return_value="6:12345"), patch.dict(
            api_healthcheck.os.environ, {"DOLA_API_HEALTH_FAILURES_BEFORE_RESTART": "5"}
        ), patch.object(api_healthcheck, "probe_api", return_value=(False, "timed out")), patch.object(
            api_healthcheck.os, "kill"
        ) as terminate:
            for _ in range(4):
                self.assertEqual(api_healthcheck.main(), 1)
            terminate.assert_not_called()
            self.assertEqual(api_healthcheck.main(), 1)
            terminate.assert_called_once_with(1, api_healthcheck.signal.SIGTERM)

    def test_new_api_process_resets_previous_failure_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = Path(temporary_directory) / "failures"
            state.write_text('{"process_identity":"6:100","failures":5}', encoding="ascii")
            with patch.object(api_healthcheck, "FAILURE_STATE_PATH", state), patch.object(
                api_healthcheck, "api_process_identity", return_value="7:200"
            ), patch.object(api_healthcheck, "probe_api", return_value=(False, "starting")), patch.object(
                api_healthcheck.os, "kill"
            ) as terminate:
                self.assertEqual(api_healthcheck.main(), 1)

            terminate.assert_not_called()
            self.assertEqual(json.loads(state.read_text(encoding="ascii")), {"process_identity": "7:200", "failures": 1})

    def test_legacy_numeric_failure_state_does_not_poison_new_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = Path(temporary_directory) / "failures"
            state.write_text("5", encoding="ascii")
            with patch.object(api_healthcheck, "FAILURE_STATE_PATH", state), patch.object(
                api_healthcheck, "api_process_identity", return_value="7:200"
            ), patch.object(api_healthcheck, "probe_api", return_value=(False, "starting")), patch.object(
                api_healthcheck.os, "kill"
            ) as terminate:
                self.assertEqual(api_healthcheck.main(), 1)

            terminate.assert_not_called()
            self.assertEqual(json.loads(state.read_text(encoding="ascii")), {"process_identity": "7:200", "failures": 1})

    def test_success_resets_previous_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = Path(temporary_directory) / "failures"
            state.write_text('{"process_identity":"6:12345","failures":2}', encoding="ascii")
            with patch.object(api_healthcheck, "FAILURE_STATE_PATH", state), patch.object(
                api_healthcheck, "api_process_identity", return_value="6:12345"
            ), patch.object(
                api_healthcheck, "probe_api", return_value=(True, "ok")
            ):
                self.assertEqual(api_healthcheck.main(), 0)
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
