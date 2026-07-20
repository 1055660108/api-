from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import admin_auth, config, main, package_catalog


class AdminAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temporary_directory.name) / "config.json"
        self.patchers = [
            patch.object(config, "CONFIG_PATH", self.config_path),
            patch.object(config, "DATA_DIR", Path(self.temporary_directory.name)),
            patch.object(config, "TASKS_DIR", Path(self.temporary_directory.name) / "tasks"),
            patch.object(config, "DOUBAO_STATES_DIR", Path(self.temporary_directory.name) / "doubao_states"),
            patch.object(config, "DOUBAO_PROFILES_DIR", Path(self.temporary_directory.name) / "doubao_profiles"),
            patch.object(config, "QIANWEN_PROFILES_DIR", Path(self.temporary_directory.name) / "qianwen_profiles"),
            patch.object(package_catalog, "PACKAGE_CATALOG_PATH", Path(self.temporary_directory.name) / "point_packages.json"),
            patch.dict("os.environ", {"DOLA_ADMIN_USERNAME": "chosen-admin", "DOLA_ADMIN_PASSWORD": "StrongPassword123"}),
        ]
        for patcher in self.patchers:
            patcher.start()
        admin_auth.clear_sessions()
        config.ensure_config()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        self.client.close()
        admin_auth.clear_sessions()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def test_password_is_hashed_and_login_sets_secure_session_cookie(self) -> None:
        stored = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("StrongPassword123", json.dumps(stored))
        self.assertTrue(stored["admin_password_hash"].startswith("pbkdf2_sha256$"))
        response = self.client.post("/auth/admin/login", json={"username": "chosen-admin", "password": "StrongPassword123"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        self.assertEqual(self.client.get("/auth/admin").status_code, 200)

    def test_invalid_password_is_rejected(self) -> None:
        response = self.client.post("/auth/admin/login", json={"username": "chosen-admin", "password": "wrong-password"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get("/auth/admin").status_code, 403)

    def test_password_change_invalidates_session_and_accepts_new_password(self) -> None:
        self.client.post("/auth/admin/login", json={"username": "chosen-admin", "password": "StrongPassword123"})
        response = self.client.post("/auth/admin/password", json={"current_password": "StrongPassword123", "new_password": "NewStrongPassword456", "confirm_password": "NewStrongPassword456"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/auth/admin").status_code, 403)
        self.assertEqual(self.client.post("/auth/admin/login", json={"username": "chosen-admin", "password": "StrongPassword123"}).status_code, 401)
        self.assertEqual(self.client.post("/auth/admin/login", json={"username": "chosen-admin", "password": "NewStrongPassword456"}).status_code, 200)

    def test_api_token_remains_compatible_without_session(self) -> None:
        token = config.load_settings().api_token
        self.assertEqual(self.client.get("/auth/admin", headers={"X-API-Token": token}).status_code, 200)
        self.assertEqual(self.client.get("/auth/admin", headers={"Authorization": f"Bearer {token}"}).status_code, 200)

    def test_logout_revokes_session(self) -> None:
        self.client.post("/auth/admin/login", json={"username": "chosen-admin", "password": "StrongPassword123"})
        self.assertEqual(self.client.post("/auth/admin/logout").status_code, 200)
        self.assertEqual(self.client.get("/auth/admin").status_code, 403)

    def test_admin_package_api_publishes_adjusts_and_disables(self) -> None:
        self.client.post("/auth/admin/login", json={"username": "chosen-admin", "password": "StrongPassword123"})
        created = self.client.post("/admin/points/packages", json={"name": "API 套餐", "points": 9.9, "bonus_free_uses": 2, "sort_order": 0})
        self.assertEqual(created.status_code, 201)
        package_id = created.json()["package"]["id"]
        adjusted = self.client.patch(f"/admin/points/packages/{package_id}", json={"points": 10, "name": "新 API 套餐"})
        self.assertEqual(adjusted.status_code, 200)
        self.assertEqual(adjusted.json()["package"]["points"], 10)
        self.assertEqual(self.client.delete(f"/admin/points/packages/{package_id}").status_code, 200)
        listed = self.client.get("/admin/points/packages").json()["packages"]
        self.assertFalse(next(item for item in listed if item["id"] == package_id)["enabled"])

    def test_admin_auth_returns_read_only_account_name(self) -> None:
        self.client.post("/auth/admin/login", json={"username": "chosen-admin", "password": "StrongPassword123"})
        self.assertEqual(self.client.get("/auth/admin").json()["admin_username"], "chosen-admin")


if __name__ == "__main__":
    unittest.main()
