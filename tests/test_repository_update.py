from __future__ import annotations

import tempfile
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

    def test_status_rejects_unexpected_origin(self) -> None:
        with patch.object(repository_update, "_run_git", side_effect=["https://github.com/other/project.git"]):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                repository_update.repository_status(self.root)

    def test_update_uses_fetch_and_fast_forward_merge(self) -> None:
        outputs = [
            "git@github.com:1055660108/api-.git",
            "abc1234",
            "fetched",
            "app/main.py",
            "",
            "updated",
            "def5678",
        ]
        with patch.object(repository_update, "_run_git", side_effect=outputs) as run_git:
            result = repository_update.update_repository(self.root)

        self.assertTrue(result["updated"])
        self.assertTrue(result["restart_required"])
        self.assertEqual(result["revision"], "def5678")
        self.assertEqual(run_git.call_args_list[5].args[1:], ("merge", "--ff-only", "origin/main"))

    def test_update_rejects_uncommitted_changes(self) -> None:
        outputs = ["https://github.com/1055660108/api-.git", "abc1234", "fetched", "app/main.py", " M app/main.py"]
        with patch.object(repository_update, "_run_git", side_effect=outputs):
            with self.assertRaisesRegex(RuntimeError, "local changes conflict"):
                repository_update.update_repository(self.root)


if __name__ == "__main__":
    unittest.main()
