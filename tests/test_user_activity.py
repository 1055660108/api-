from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, postgres, user_activity


class UserActivityTests(unittest.TestCase):
    def test_local_activity_survives_a_fresh_read_and_is_isolated_by_user(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
            config, "DATA_DIR", Path(temporary_directory)
        ), patch.object(postgres, "enabled", return_value=False):
            user_activity.record_activity("user-1", "login", "登录账号", detail="IP 127.0.0.1")
            user_activity.record_activity("user-2", "login", "其他账号")
            path = Path(temporary_directory) / "user_activity.json"

            self.assertTrue(path.exists())
            result = user_activity.list_activity("user-1", page=1, page_size=10)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["activities"][0]["action"], "login")
        self.assertEqual(result["activities"][0]["detail"], "IP 127.0.0.1")


if __name__ == "__main__":
    unittest.main()
