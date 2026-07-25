from __future__ import annotations

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
            for name in ("0", "1", "2", "8"):
                (process / "fd" / name).touch()

            self.assertEqual(api_healthcheck.api_process_fd_count(root), 4)

    def test_three_consecutive_failures_terminate_container(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
            api_healthcheck, "FAILURE_STATE_PATH", Path(temporary_directory) / "failures"
        ), patch.object(api_healthcheck, "probe_api", return_value=(False, "timed out")), patch.object(
            api_healthcheck.os, "kill"
        ) as terminate:
            self.assertEqual(api_healthcheck.main(), 1)
            self.assertEqual(api_healthcheck.main(), 1)
            terminate.assert_not_called()
            self.assertEqual(api_healthcheck.main(), 1)
            terminate.assert_called_once_with(1, api_healthcheck.signal.SIGTERM)

    def test_success_resets_previous_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = Path(temporary_directory) / "failures"
            state.write_text("2", encoding="ascii")
            with patch.object(api_healthcheck, "FAILURE_STATE_PATH", state), patch.object(
                api_healthcheck, "probe_api", return_value=(True, "ok")
            ):
                self.assertEqual(api_healthcheck.main(), 0)
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
