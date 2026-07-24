from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from app import repository_update


class RepositoryUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / ".git").mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_git_failure_keeps_stderr_when_stdout_has_progress(self) -> None:
        result = subprocess.CompletedProcess(
            args=("git", "fetch"),
            returncode=1,
            stdout="fetching origin\n",
            stderr="fatal: repository unavailable\n",
        )
        with patch.object(repository_update.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "repository unavailable") as raised:
                repository_update._run_git(self.root, "fetch")
        self.assertIn("fetching origin", str(raised.exception))

    def test_status_rejects_unexpected_origin(self) -> None:
        with patch("pathlib.Path.is_socket", return_value=False), patch.object(repository_update, "_run_git", side_effect=["https://github.com/other/project.git"]):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                repository_update.repository_status(self.root)

    def test_update_uses_fetch_and_fast_forward_merge(self) -> None:
        outputs = [
            "git@github.com:1055660108/api-.git",
            "fetched",
            "abc1234",
            "current commit",
            "def5678",
            "latest commit",
            "1.2.4",
            "app/main.py",
            "",
            "updated",
            "def5678",
            "latest commit",
        ]
        with patch("pathlib.Path.is_socket", return_value=False), patch.object(repository_update, "_run_git", side_effect=outputs) as run_git:
            result = repository_update.update_repository(self.root)

        self.assertTrue(result["updated"])
        self.assertTrue(result["restart_required"])
        self.assertEqual(result["revision"], "def5678")
        self.assertEqual(result["commit_message"], "latest commit")
        self.assertFalse(result["update_available"])
        self.assertEqual(run_git.call_args_list[9].args[1:], ("merge", "--ff-only", "origin/main"))

    def test_update_rejects_uncommitted_changes(self) -> None:
        outputs = [
            "https://github.com/1055660108/api-",
            "fetched",
            "abc1234",
            "current commit",
            "def5678",
            "latest commit",
            "1.2.4",
            "app/main.py",
            " M app/main.py",
        ]
        with patch("pathlib.Path.is_socket", return_value=False), patch.object(repository_update, "_run_git", side_effect=outputs):
            with self.assertRaisesRegex(RuntimeError, "local changes conflict"):
                repository_update.update_repository(self.root)

    def test_status_reports_version_commit_and_available_update(self) -> None:
        outputs = [
            "https://github.com/1055660108/api-.git",
            "fetched",
            "abc1234",
            "current commit",
            "def5678",
            "latest commit",
            "1.2.4",
        ]
        with patch("pathlib.Path.is_socket", return_value=False), patch.object(repository_update, "_run_git", side_effect=outputs):
            result = repository_update.repository_status(self.root)

        self.assertEqual(result["version"], "1.4.13")
        self.assertEqual(result["commit_message"], "current commit")
        self.assertEqual(result["latest_commit_message"], "latest commit")
        self.assertEqual(result["latest_version"], "1.2.4")
        self.assertTrue(result["update_available"])

    def test_https_and_ssh_repository_urls_are_equivalent(self) -> None:
        self.assertEqual(
            repository_update._normalized_repository("git@github.com:1055660108/api-.git"),
            repository_update._normalized_repository("https://github.com/1055660108/api-"),
        )

    def test_container_mode_uses_deployment_controller(self) -> None:
        with patch("pathlib.Path.is_socket", return_value=True), patch.object(
            repository_update,
            "_controller_request",
            return_value={"revision": "abc1234", "updating": False},
        ) as request:
            result = repository_update.repository_status(Path("/app"))

        self.assertEqual(result["revision"], "abc1234")
        request.assert_called_once_with("GET", "/status")

    def test_controller_connection_error_is_reported_as_runtime_error(self) -> None:
        with patch("pathlib.Path.is_socket", return_value=True), patch.object(
            repository_update.httpx,
            "Client",
            side_effect=repository_update.httpx.ConnectError("connection refused"),
        ):
            with self.assertRaisesRegex(RuntimeError, "controller unavailable"):
                repository_update.repository_status(Path("/app"))


if __name__ == "__main__":
    unittest.main()
