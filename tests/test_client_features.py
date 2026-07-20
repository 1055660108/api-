from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import admin_auth, config, main, package_catalog, store, temp_access, users


class ClientFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.tasks_path = self.root / "tasks"
        self.patchers = [
            patch.object(config, "CONFIG_PATH", self.root / "config.json"),
            patch.object(config, "DATA_DIR", self.root),
            patch.object(config, "TASKS_DIR", self.tasks_path),
            patch.object(store, "TASKS_DIR", self.tasks_path),
            patch.object(store, "runtime_path", return_value=self.root / "runtime.json"),
            patch.object(temp_access, "TEMP_TOKENS_PATH", self.root / "temp_tokens.json"),
            patch.object(users, "USERS_PATH", self.root / "users.json"),
            patch.object(package_catalog, "PACKAGE_CATALOG_PATH", self.root / "point_packages.json"),
            patch.dict("os.environ", {"DOLA_ADMIN_USERNAME": "chosen-admin", "DOLA_ADMIN_PASSWORD": "StrongPassword123"}),
        ]
        for patcher in self.patchers:
            patcher.start()
        admin_auth.clear_sessions()
        with main._RATE_LOCK:
            main._RATE_BUCKETS.clear()
        config.ensure_config()
        config.update_config({"registration_email_verification_enabled": False})
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        self.client.close()
        admin_auth.clear_sessions()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def register(self, username: str = "client_user", password: str = "password123") -> dict:
        response = self.client.post("/auth/register", json={"username": username, "password": password, "confirm_password": password})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_client_password_change_rotates_token_and_login_password(self) -> None:
        registered = self.register()
        old_token = registered["token"]
        task = store.create_task("历史任务", "9:16", owner_token_hash=temp_access.hash_token(old_token))
        response = self.client.post(
            "/auth/password",
            headers={"X-API-Token": old_token},
            json={"current_password": "password123", "new_password": "new-password456", "confirm_password": "new-password456"},
        )
        self.assertEqual(response.status_code, 200)
        new_token = response.json()["token"]
        self.assertNotEqual(old_token, new_token)
        self.assertEqual(self.client.get("/auth/client", headers={"X-API-Token": old_token}).status_code, 403)
        self.assertEqual(self.client.post("/auth/login", json={"username": "client_user", "password": "password123"}).status_code, 401)
        self.assertEqual(self.client.post("/auth/login", json={"username": "client_user", "password": "new-password456"}).json()["token"], new_token)
        self.assertEqual(store.get_meta(task["id"])["owner_token_hash"], temp_access.hash_token(new_token))

    def test_users_endpoint_uses_backend_pagination(self) -> None:
        for index in range(5):
            self.register(f"user_{index}")
        self.client.post("/auth/admin/login", json={"username": "chosen-admin", "password": "StrongPassword123"})
        response = self.client.get("/users?page=2&page_size=2")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["users"]), 2)
        self.assertEqual(data["total"], 5)
        self.assertEqual(data["page"], 2)
        self.assertEqual(data["total_pages"], 3)

    def test_client_can_read_and_change_verified_email(self) -> None:
        config.update_config({"registration_email_verification_enabled": True, "registration_email_domains": ["qq.com", "163.com"]})
        registered = users.register_user("email_user", "password123", email="old@qq.com")
        headers = {"X-API-Token": registered["token"]}
        profile = self.client.get("/auth/profile", headers=headers)
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json()["email"], "old@qq.com")
        with patch.object(main, "send_registration_code") as send_code:
            response = self.client.post("/auth/email/code", headers=headers, json={"email": "new@163.com"})
        self.assertEqual(response.status_code, 200)
        send_code.assert_called_once()
        with patch.object(main, "consume_registration_code", return_value="new@163.com"):
            changed = self.client.patch("/auth/email", headers=headers, json={"email": "new@163.com", "email_code": "123456"})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.client.get("/auth/profile", headers=headers).json()["email"], "new@163.com")

    def test_registration_domains_are_public_but_admin_email_config_is_protected(self) -> None:
        config.update_config({"registration_email_domains": ["qq.com", "163.com"]})
        response = self.client.get("/auth/register/email-domains")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["domains"], ["@qq.com", "@163.com"])
        self.assertIn(self.client.get("/config/registration-email").status_code, {401, 403})

    def test_video_visibility_is_isolated_by_audience(self) -> None:
        registered = self.register()
        token = registered["token"]
        task = store.create_task("视频任务", "9:16", owner_token_hash=temp_access.hash_token(token))
        client_response = self.client.post(f"/tasks/{task['id']}/video-visibility", headers={"X-API-Token": token}, json={"hidden": True})
        self.assertEqual(client_response.status_code, 200)
        listed = store.list_tasks(owner_token_hash=temp_access.hash_token(token))[0]
        self.assertTrue(listed["video_hidden_for_client"])
        self.assertFalse(listed["video_hidden_for_admin"])
        self.client.post("/auth/admin/login", json={"username": "chosen-admin", "password": "StrongPassword123"})
        admin_response = self.client.post(f"/tasks/{task['id']}/video-visibility", json={"hidden": True})
        self.assertEqual(admin_response.status_code, 200)
        meta = store.get_meta(task["id"])
        self.assertTrue(meta["video_hidden_for_client"])
        self.assertTrue(meta["video_hidden_for_admin"])


if __name__ == "__main__":
    unittest.main()
