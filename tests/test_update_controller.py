from __future__ import annotations

import importlib
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import call, patch

from scripts import update_controller


class UpdateControllerTests(unittest.TestCase):
    def test_failed_command_keeps_stderr_when_stdout_contains_progress(self) -> None:
        result = subprocess.CompletedProcess(
            args=("docker", "compose", "build"),
            returncode=1,
            stdout="#1 loading build definition\n",
            stderr="failed to solve: no space left on device\n",
        )
        with patch.object(update_controller.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "no space left on device") as raised:
                update_controller.run("docker", "compose", "build")
        self.assertIn("#1 loading build definition", str(raised.exception))

    def test_command_timeout_has_actionable_message(self) -> None:
        with patch.object(
            update_controller.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(("docker", "compose", "build"), 900),
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out after 900 seconds"):
                update_controller.run("docker", "compose", "build")

    def test_deploy_waits_until_update_response_can_be_sent(self) -> None:
        with patch.object(update_controller.time, "sleep") as sleep, patch.object(update_controller, "deploy") as deploy:
            update_controller.deploy_after_response()
        sleep.assert_called_once_with(update_controller.DEPLOY_START_DELAY_SECONDS)
        deploy.assert_called_once_with()

    def test_uncommitted_paths_include_modified_and_renamed_files(self) -> None:
        self.assertEqual(
            update_controller.uncommitted_paths(" M app/main.py\nR  old.py -> new.py\n?? local.txt"),
            ["app/main.py", "new.py", "local.txt"],
        )

    def test_installer_does_not_modify_tracked_runtime_config(self) -> None:
        install_script = (Path(__file__).resolve().parents[1] / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertNotIn("write_runtime_config", install_script)

    def test_controller_service_uses_writable_docker_home(self) -> None:
        installer = (Path(__file__).resolve().parents[1] / "scripts" / "install_update_controller.sh").read_text(encoding="utf-8")
        self.assertIn('install -d -m 0700 "$CONTROLLER_HOME" "$CONTROLLER_HOME/.docker"', installer)
        self.assertIn("Environment=HOME=$CONTROLLER_HOME", installer)
        self.assertIn("Environment=DOCKER_CONFIG=$CONTROLLER_HOME/.docker", installer)
        self.assertIn("Environment=DOLA_UPDATE_BUILD_TIMEOUT_SECONDS=$DOLA_UPDATE_BUILD_TIMEOUT_SECONDS", installer)
        self.assertIn("ProtectHome=true", installer)

    def test_deploy_builds_shared_image_once_with_extended_timeout(self) -> None:
        def fake_git(*arguments: str, timeout: int = 120) -> str:
            if arguments == ("remote", "get-url", "origin"):
                return update_controller.EXPECTED_ORIGIN
            if arguments == ("status", "--porcelain", "--untracked-files=all"):
                return ""
            if arguments == ("rev-parse", "HEAD"):
                return "old-revision"
            if arguments == ("rev-parse", f"origin/{update_controller.BRANCH}"):
                return "new-revision"
            return ""

        with patch.object(update_controller, "git", side_effect=fake_git), patch.object(
            update_controller, "image_name", return_value="dola-fetch-service:old"
        ), patch.object(
            update_controller, "running_service_image", return_value="sha256:running"
        ), patch.object(
            update_controller, "running_service_version", return_value="old-version"
        ), patch.object(update_controller, "run", return_value="") as run, patch.object(
            update_controller, "wait_for_health"
        ), patch(
            "scripts.update_controller.Path.read_text", return_value="new-version"
        ), patch.dict(update_controller.STATE, {"updating": True, "phase": "准备更新", "error": ""}, clear=True):
            update_controller.deploy()

        self.assertIn(
            call("docker", "compose", "build", "api", timeout=update_controller.BUILD_TIMEOUT_SECONDS),
            run.call_args_list,
        )
        build_calls = [item for item in run.call_args_list if item.args[:3] == ("docker", "compose", "build")]
        self.assertEqual(len(build_calls), 1)
        self.assertNotIn("worker", build_calls[0].args)

    def test_deploy_repairs_runtime_when_repository_is_already_current(self) -> None:
        def fake_git(*arguments: str, timeout: int = 120) -> str:
            if arguments == ("remote", "get-url", "origin"):
                return update_controller.EXPECTED_ORIGIN
            if arguments == ("status", "--porcelain", "--untracked-files=all"):
                return ""
            if arguments in {("rev-parse", "HEAD"), ("rev-parse", f"origin/{update_controller.BRANCH}")}:
                return "current-revision"
            return ""

        with patch.object(update_controller, "git", side_effect=fake_git), patch.object(
            update_controller, "running_service_image", return_value="sha256:running"
        ), patch.object(update_controller, "running_service_version", return_value="1.9.5"), patch.object(
            update_controller, "image_name", return_value="dola-fetch-service:1.9.6"
        ), patch.object(update_controller, "run", return_value="") as run, patch.object(
            update_controller, "wait_for_health"
        ), patch("scripts.update_controller.Path.read_text", return_value="1.9.6"), patch.dict(
            update_controller.STATE, {"updating": True, "phase": "准备更新", "error": ""}, clear=True
        ):
            update_controller.deploy()

        self.assertIn(
            call("docker", "compose", "build", "api", timeout=update_controller.BUILD_TIMEOUT_SECONDS),
            run.call_args_list,
        )
        self.assertIn(call("docker", "compose", "up", "-d", "--force-recreate", "api", "worker"), run.call_args_list)

    def test_deploy_retains_running_image_tag_when_code_and_runtime_match(self) -> None:
        def fake_git(*arguments: str, timeout: int = 120) -> str:
            if arguments == ("remote", "get-url", "origin"):
                return update_controller.EXPECTED_ORIGIN
            if arguments == ("status", "--porcelain", "--untracked-files=all"):
                return ""
            if arguments in {("rev-parse", "HEAD"), ("rev-parse", f"origin/{update_controller.BRANCH}")}:
                return "current-revision"
            return ""

        with patch.object(update_controller, "git", side_effect=fake_git), patch.object(
            update_controller, "running_service_image", return_value="sha256:running"
        ), patch.object(update_controller, "running_service_version", return_value="1.9.6"), patch.object(
            update_controller, "image_name", return_value="dola-fetch-service:1.9.6"
        ), patch.object(update_controller, "run", return_value="") as run, patch(
            "scripts.update_controller.Path.read_text", return_value="1.9.6"
        ), patch.dict(update_controller.STATE, {"updating": True, "phase": "准备更新", "error": ""}, clear=True):
            update_controller.deploy()
            phase = update_controller.STATE["phase"]

        self.assertIn(call("docker", "tag", "sha256:running", "dola-fetch-service:1.9.6", timeout=30), run.call_args_list)
        self.assertFalse(any(item.args[:3] == ("docker", "compose", "build") for item in run.call_args_list))
        self.assertEqual(phase, "已是最新")

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

        with patch.object(update_controller, "git", side_effect=fake_git), patch.object(
            update_controller, "running_service_version", return_value=""
        ), patch(
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

        with patch.object(update_controller, "git", side_effect=fake_git), patch.object(
            update_controller, "running_service_version", return_value=""
        ), patch(
            "scripts.update_controller.Path.read_text", return_value="1.0.1"
        ), patch.dict(update_controller.STATE, {"updating": True, "phase": "构建镜像", "error": ""}, clear=True):
            result = update_controller.status()

        self.assertTrue(result["updating"])

    def test_status_reports_runtime_version_and_detects_source_mismatch(self) -> None:
        def fake_git(*arguments: str, timeout: int = 120) -> str:
            if arguments[:2] == ("rev-parse", "--short"):
                return "same-revision"
            if arguments[:2] == ("log", "-1"):
                return "release"
            if arguments[:1] == ("show",):
                return "1.9.6"
            return ""

        with patch.object(update_controller, "git", side_effect=fake_git), patch.object(
            update_controller, "running_service_version", return_value="1.9.5"
        ), patch("scripts.update_controller.Path.read_text", return_value="1.9.6"), patch.dict(
            update_controller.STATE, {"updating": False, "phase": "空闲", "error": ""}, clear=True
        ):
            result = update_controller.status()

        self.assertEqual(result["version"], "1.9.5")
        self.assertEqual(result["source_version"], "1.9.6")
        self.assertTrue(result["update_available"])


if __name__ == "__main__":
    unittest.main()
