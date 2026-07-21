from __future__ import annotations

import importlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import update_controller


class UpdateControllerTests(unittest.TestCase):
    def test_uncommitted_paths_include_modified_and_renamed_files(self) -> None:
        self.assertEqual(
            update_controller.uncommitted_paths(" M app/main.py\nR  old.py -> new.py\n?? local.txt"),
            ["app/main.py", "new.py", "local.txt"],
        )

    def test_installer_does_not_modify_tracked_runtime_config(self) -> None:
        install_script = (Path(__file__).resolve().parents[1] / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertNotIn("write_runtime_config", install_script)

    def test_custom_port_and_image_are_used(self) -> None:
        environment = {"DOLA_PORT": "9191", "DOLA_IMAGE_NAME": "registry.example/dola", "DOLA_IMAGE_TAG": "stable"}
        with patch.dict(os.environ, environment, clear=False):
            module = importlib.reload(update_controller)
            self.assertEqual(module.APP_PORT, 9191)
            self.assertEqual(module.image_name(), "registry.example/dola:stable")
            self.assertEqual(module.rollback_image_name(), "registry.example/dola:rollback")
        importlib.reload(update_controller)

    def test_status_serializes_fetches(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_git(*arguments: str, timeout: int = 120) -> str:
            calls.append(arguments)
            if arguments[:2] == ("rev-parse", "--short"):
                return "abc1234" if arguments[-1] == "HEAD" else "def5678"
            if arguments[:2] == ("log", "-1"):
                return "release"
            return ""

        with patch.object(update_controller, "git", side_effect=fake_git), patch(
            "scripts.update_controller.Path.read_text", return_value="1.0.1"
        ), patch.dict(update_controller.STATE, {"updating": False, "phase": "空闲", "error": ""}, clear=True):
            result = update_controller.status()

        self.assertEqual(calls[0], ("fetch", "--prune", "origin", update_controller.BRANCH))
        self.assertTrue(result["update_available"])

    def test_updating_status_does_not_fetch_repository(self) -> None:
        def fake_git(*arguments: str, timeout: int = 120) -> str:
            if arguments[0] == "fetch":
                raise AssertionError("fetch must not run while deploying")
            return "abc1234" if arguments[0] == "rev-parse" else "release"

        with patch.object(update_controller, "git", side_effect=fake_git), patch(
            "scripts.update_controller.Path.read_text", return_value="1.0.1"
        ), patch.dict(update_controller.STATE, {"updating": True, "phase": "构建镜像", "error": ""}, clear=True):
            result = update_controller.status()

        self.assertTrue(result["updating"])


if __name__ == "__main__":
    unittest.main()
