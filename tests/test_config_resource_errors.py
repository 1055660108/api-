from __future__ import annotations

import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config


class ConfigResourceErrorTests(unittest.TestCase):
    def test_file_descriptor_exhaustion_is_not_reported_as_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            with patch.object(config, "CONFIG_PATH", config_path), patch.object(
                Path, "read_text", side_effect=OSError(errno.EMFILE, "Too many open files")
            ):
                with self.assertRaisesRegex(RuntimeError, "file descriptor limit is exhausted"):
                    config._load_config_dict()


if __name__ == "__main__":
    unittest.main()
