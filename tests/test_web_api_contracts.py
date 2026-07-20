from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import accounts, admin_auth, config, main, package_catalog, store, temp_access, users


class WebAPIContractTests(unittest.TestCase):
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
            patch.object(accounts, "ACCOUNTS_PATH", self.root / "accounts.json"),
            patch.object(temp_access, "TEMP_TOKENS_PATH", self.root / "temp_tokens.json"),
            patch.object(users, "USERS_PATH", self.root / "users.json"),
            patch.object(package_catalog, "PACKAGE_CATALOG_PATH", self.root / "point_packages.json"),
            patch.dict("os.environ", {"DOLA_ADMIN_USERNAME": "contract-admin", "DOLA_ADMIN_PASSWORD": "ContractPassword123"}),
        ]
        for patcher in self.patchers:
            patcher.start()
        admin_auth.clear_sessions()
        config.ensure_config()
        config.update_config({"registration_email_verification_enabled": False})
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()
        self.admin_token = config.load_settings().api_token

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        admin_auth.clear_sessions()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def register(self, username: str = "contract_client") -> dict:
        response = self.client.post(
            "/auth/register",
            json={"username": username, "password": "ClientPassword123", "confirm_password": "ClientPassword123"},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def login_admin(self) -> None:
        response = self.client.post(
            "/auth/admin/login",
            json={"username": "contract-admin", "password": "ContractPassword123"},
        )
        self.assertEqual(response.status_code, 200)

    def test_admin_and_client_entries_publish_the_same_static_bundle(self) -> None:
        admin = self.client.get("/admin")
        client = self.client.get("/client/")
        self.assertEqual(admin.status_code, 200)
        self.assertEqual(client.status_code, 200)
        self.assertEqual(admin.content, client.content)
        self.assertTrue(admin.headers["content-type"].startswith("text/html"))
        self.assertEqual(admin.headers["cache-control"], "no-store")
        self.assertEqual(client.headers["cache-control"], "no-store")
        for path, content_type in (
            ("/admin/assets/styles.css", "text/css"),
            ("/admin/assets/app.js", "javascript"),
            ("/admin/assets/runtime-config.js", "javascript"),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(content_type, response.headers["content-type"])
            self.assertTrue(response.content)

    def test_login_contracts_keep_credentials_and_error_shapes_compatible(self) -> None:
        invalid_admin = self.client.post("/auth/admin/login", json={"username": "contract-admin", "password": "invalid"})
        self.assertEqual(invalid_admin.status_code, 401)
        self.assertEqual(invalid_admin.json(), {"detail": "管理员账号或密码错误"})
        admin = self.client.post(
            "/auth/admin/login",
            json={"username": "contract-admin", "password": "ContractPassword123"},
        )
        self.assertEqual(admin.json(), {"ok": True, "username": "contract-admin"})
        self.assertIn("HttpOnly", admin.headers["set-cookie"])
        registered = self.register()
        self.assertEqual(set(registered), {"ok", "username", "token"})
        login = self.client.post("/auth/login", json={"username": "contract_client", "password": "ClientPassword123"})
        self.assertEqual(login.json(), registered)
        invalid_client = self.client.post("/auth/login", json={"username": "contract_client", "password": "invalid"})
        self.assertEqual(invalid_client.status_code, 401)
        self.assertEqual(invalid_client.json(), {"detail": "用户名或密码错误"})

    def test_health_contract_is_role_scoped_for_admin_and_client(self) -> None:
        registered = self.register()
        queue_health = {"ok": True, "backend": "file", "ready": 0, "processing": 0, "delayed": 0, "error": "internal queue detail"}
        queue = unittest.mock.Mock()
        queue.health.return_value = queue_health
        queue.client = None
        with patch("app.task_queue.get_task_queue", return_value=queue), patch("app.main.resolve_browser_executable", return_value="browser.exe"):
            admin = self.client.get("/health", headers={"X-API-Token": self.admin_token}).json()
            client = self.client.get("/auth/client", headers={"X-API-Token": registered["token"]}).json()
        self.assertTrue({"ok", "status", "role", "browser_workers", "active", "components", "admin_username"} <= set(admin))
        self.assertEqual(admin["role"], "admin")
        self.assertEqual(admin["components"]["queue"]["error"], "internal queue detail")
        self.assertTrue({"quota", "token_concurrency", "task_retention_days", "user_name"} <= set(client))
        self.assertEqual(client["role"], "client")
        self.assertEqual(set(client["quota"]), {"limit", "used", "remaining", "free_remaining", "points"})
        self.assertNotIn("admin_username", client)
        self.assertNotIn("error", client["components"]["queue"])
        self.assertNotIn("executable_path", client["components"]["browser"])

    def test_task_idempotency_replays_without_second_charge(self) -> None:
        registered = self.register("idempotent_client")
        headers = {"X-API-Token": registered["token"], "Idempotency-Key": "create-video-001"}
        payload = {"prompt": "海边日落延时摄影", "ratio": "9:16", "platform": "dola", "model": "Seedance 2.0", "task_type": "video"}
        first = self.client.post("/tasks", headers=headers, data=payload)
        second = self.client.post("/tasks", headers=headers, data=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertTrue(second.json()["replayed"])
        data = json.loads(temp_access.TEMP_TOKENS_PATH.read_text(encoding="utf-8"))
        entry = next(item for item in data["tokens"].values() if item["token"] == registered["token"])
        self.assertEqual(len(entry["reservations"]), 1)

    def test_task_idempotency_rejects_changed_payload(self) -> None:
        registered = self.register("idempotency_conflict")
        headers = {"X-API-Token": registered["token"], "Idempotency-Key": "create-video-002"}
        common = {"ratio": "9:16", "platform": "dola", "model": "Seedance 2.0", "task_type": "video"}
        first = self.client.post("/tasks", headers=headers, data={**common, "prompt": "森林溪流"})
        second = self.client.post("/tasks", headers=headers, data={**common, "prompt": "城市夜景"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)

    def test_query_parameter_token_is_not_accepted(self) -> None:
        registered = self.register("query_token_client")
        response = self.client.get(f"/tasks?token={registered['token']}")
        self.assertEqual(response.status_code, 403)

    def test_liveness_probe_requires_no_credentials(self) -> None:
        response = self.client.get("/health/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    def test_task_contract_preserves_fields_and_hides_client_internals(self) -> None:
        registered = self.register()
        token = registered["token"]
        owner_hash = temp_access.hash_token(token)
        owned = store.create_task("Dola 账号生成测试", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        other = store.create_task("管理员任务", "16:9", model="Seedance 2.0")
        store.update_meta(owned["id"], worker_id="worker-secret", failed_account_ids=["account-secret"])
        client_list = self.client.get("/tasks", headers={"X-API-Token": token}).json()["tasks"]
        self.assertEqual([item["id"] for item in client_list], [owned["id"]])
        client_task = client_list[0]
        self.assertTrue({"id", "prompt", "prompt_preview", "model", "status", "image_count", "error", "owner_name", "video_hidden_for_client"} <= set(client_task))
        for key in ("owner_token_hash", "worker_id", "failed_account_ids", "account_id", "platform", "video_hidden_for_admin"):
            self.assertNotIn(key, client_task)
        admin_tasks = self.client.get("/tasks", headers={"X-API-Token": self.admin_token}).json()["tasks"]
        self.assertEqual({item["id"] for item in admin_tasks}, {owned["id"], other["id"]})
        admin_owned = next(item for item in admin_tasks if item["id"] == owned["id"])
        self.assertEqual(admin_owned["owner_token_hash"], owner_hash)
        with patch("app.main.query_task", new=AsyncMock(return_value={"code": "0", "text": "Dola 账号等待中", "url": ""})):
            detail = self.client.get(f"/tasks/{owned['id']}", headers={"X-API-Token": token})
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(set(detail.json()), {"code", "text", "url"})
        self.assertNotIn("Dola", detail.json()["text"])
        self.assertNotIn("账号", detail.json()["text"])
        self.assertEqual(self.client.get(f"/tasks/{other['id']}", headers={"X-API-Token": token}).status_code, 404)

    def test_task_pagination_search_statistics_and_legacy_contract(self) -> None:
        registered = self.register()
        token = registered["token"]
        owner_hash = temp_access.hash_token(token)
        first = store.create_task("第一条可搜索任务", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        second = store.create_task("第二条任务", "16:9", owner_token_hash=owner_hash, model="Seedance 2.0")
        store.mark_failed(first["id"], "测试失败")
        legacy = self.client.get("/tasks", headers={"X-API-Token": token})
        self.assertEqual(set(legacy.json()), {"tasks"})
        paged = self.client.get("/tasks?page=1&page_size=1&q=可搜索&status=failed", headers={"X-API-Token": token})
        self.assertEqual(paged.status_code, 200)
        payload = paged.json()
        self.assertEqual(set(payload), {"tasks", "total", "page", "page_size", "total_pages", "stats"})
        self.assertEqual([item["id"] for item in payload["tasks"]], [first["id"]])
        self.assertEqual((payload["total"], payload["page"], payload["page_size"], payload["total_pages"]), (1, 1, 1, 1))
        self.assertEqual(payload["stats"]["total"], 2)
        self.assertEqual(payload["stats"]["failed"], 1)
        self.assertEqual(payload["stats"]["pending"], 1)
        out_of_range = self.client.get("/tasks?page=99&page_size=1", headers={"X-API-Token": token}).json()
        self.assertEqual(out_of_range["page"], 2)
        self.assertEqual([item["id"] for item in out_of_range["tasks"]], [first["id"]])
        self.assertNotEqual(first["id"], second["id"])

    def test_client_and_admin_task_deletion_are_independent(self) -> None:
        registered = self.register()
        token = registered["token"]
        owner_hash = temp_access.hash_token(token)
        task = store.create_task("独立历史记录", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        store.mark_failed(task["id"], "测试失败")

        client_delete = self.client.delete(f"/tasks/{task['id']}", headers={"X-API-Token": token})
        self.assertEqual(client_delete.status_code, 200)
        self.assertEqual(client_delete.json()["audience"], "client")
        self.assertEqual(self.client.get("/tasks", headers={"X-API-Token": token}).json()["tasks"], [])
        admin_tasks = self.client.get("/tasks", headers={"X-API-Token": self.admin_token}).json()["tasks"]
        self.assertEqual([item["id"] for item in admin_tasks], [task["id"]])
        self.assertTrue(store.get_meta(task["id"])["task_hidden_for_client"])

        admin_delete = self.client.delete(f"/tasks/{task['id']}", headers={"X-API-Token": self.admin_token})
        self.assertEqual(admin_delete.status_code, 200)
        self.assertEqual(admin_delete.json()["audience"], "admin")
        self.assertEqual(self.client.get("/tasks", headers={"X-API-Token": self.admin_token}).json()["tasks"], [])
        self.assertTrue(store.get_meta(task["id"])["task_hidden_for_admin"])

    def test_client_failed_cleanup_does_not_remove_admin_history(self) -> None:
        registered = self.register()
        token = registered["token"]
        owner_hash = temp_access.hash_token(token)
        task = store.create_task("批量隐藏历史记录", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        store.mark_failed(task["id"], "测试失败")

        response = self.client.delete("/tasks-failed", headers={"X-API-Token": token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["hidden"], 1)
        self.assertEqual(self.client.get("/tasks", headers={"X-API-Token": token}).json()["tasks"], [])
        admin_tasks = self.client.get("/tasks", headers={"X-API-Token": self.admin_token}).json()["tasks"]
        self.assertEqual([item["id"] for item in admin_tasks], [task["id"]])

    def test_account_pagination_search_filter_statistics_and_legacy_contract(self) -> None:
        self.login_admin()
        dola = self.client.post("/accounts", json={"name": "搜索账号", "cookie_data": "session=dola", "platform": "dola"}).json()["account"]
        self.client.post("/accounts", json={"name": "豆包账号", "cookie_data": "session=doubao", "platform": "doubao"})
        legacy = self.client.get("/accounts").json()
        self.assertEqual(set(legacy), {"accounts", "quota_summary", "next_quota_reset_at"})
        paged = self.client.get("/accounts?page=1&page_size=1&q=搜索&platform=dola")
        self.assertEqual(paged.status_code, 200)
        payload = paged.json()
        self.assertEqual(set(payload), {"accounts", "quota_summary", "next_quota_reset_at", "total", "page", "page_size", "total_pages", "stats"})
        self.assertEqual([item["id"] for item in payload["accounts"]], [dola["id"]])
        self.assertEqual((payload["total"], payload["page"], payload["page_size"], payload["total_pages"]), (1, 1, 1, 1))
        self.assertEqual(payload["stats"]["total"], 1)
        self.assertEqual(payload["stats"]["normal"], 1)
        self.assertEqual(payload["stats"]["by_platform"], {"dola": 1, "doubao": 1, "qianwen": 0})
        self.assertEqual(self.client.get("/accounts?page=1&platform=unknown").status_code, 422)

    def test_account_user_and_configuration_responses_keep_web_contracts(self) -> None:
        registered = self.register()
        self.login_admin()
        created = self.client.post(
            "/accounts",
            json={"name": "契约账号", "cookie_data": "session=secret-value", "quota_limit": 3, "platform": "dola"},
        ).json()["account"]
        self.assertTrue({"id", "platform", "name", "enabled", "account_status", "quota_limit", "quota_used", "quota_remaining", "cookie_count", "cookie_names", "created_at", "updated_at"} <= set(created))
        self.assertNotIn("cookies", created)
        self.assertNotIn("cookie_header", created)
        accounts_payload = self.client.get("/accounts").json()
        self.assertEqual(set(accounts_payload), {"accounts", "quota_summary", "next_quota_reset_at"})
        self.assertEqual(set(accounts_payload["quota_summary"]), {"total_limit", "total_used", "total_remaining", "unlimited_count"})
        users_payload = self.client.get("/users?page=1&page_size=20").json()
        self.assertEqual(set(users_payload), {"users", "online", "total", "page", "page_size", "total_pages"})
        user = users_payload["users"][0]
        self.assertTrue({"id", "username", "created_at", "last_login_at", "last_seen_at", "online", "free_remaining", "points", "used", "enabled", "token", "concurrency"} <= set(user))
        self.assertEqual(user["token"], registered["token"])
        self.assertNotIn("password_hash", user)
        self.assertNotIn("password_salt", user)
        workers = self.client.get("/config/workers").json()
        proxy = self.client.get("/config/proxy-api").json()
        platforms = self.client.get("/config/platforms").json()
        self.assertEqual(set(workers), {"browser_workers"})
        self.assertEqual(set(proxy), {"proxy_api_url", "proxy_api_scheme", "proxy_api_timeout_seconds", "proxy_subscription_configured", "proxy_subscription_scheme", "proxy_subscription_refresh_seconds"})
        self.assertNotIn("proxy_subscription_url", proxy)
        self.assertEqual(set(platforms), {"default_platform", "platforms"})
        self.assertEqual({item["id"] for item in platforms["platforms"]}, {"dola", "doubao", "qianwen"})
        for platform in platforms["platforms"]:
            self.assertEqual(set(platform), {"id", "label", "models", "model_costs", "all_models", "enabled"})
            for model in platform["all_models"]:
                self.assertEqual(set(model), {"name", "enabled", "cost"})

    def test_admin_can_update_model_costs(self) -> None:
        response = self.client.post(
            "/config/platforms",
            headers={"X-API-Token": self.admin_token},
            json={
                "default_platform": "dola",
                "platforms": [
                    {"id": "dola", "models": [{"name": "Seedance 2.0", "enabled": True, "cost": 1.7}]},
                    {"id": "doubao", "models": []},
                    {"id": "qianwen", "models": []},
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        dola = next(item for item in response.json()["platforms"] if item["id"] == "dola")
        self.assertEqual(dola["model_costs"]["Seedance 2.0"], 1.7)
        self.assertEqual(dola["all_models"][0]["cost"], 1.7)

    def test_registration_email_config_preserves_saved_credentials(self) -> None:
        response = self.client.post(
            "/config/registration-email",
            headers={"X-API-Token": self.admin_token},
            json={
                "enabled": True,
                "domains": "@qq.com, @163.com",
                "smtp_host": "smtp.qq.com",
                "smtp_port": 465,
                "smtp_username": "sender@qq.com",
                "authorization_code": "saved-authorization-code",
                "sender_name": "注册服务",
                "code_ttl_minutes": 10,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["smtp_username"], "sender@qq.com")
        self.assertTrue(response.json()["authorization_code_configured"])
        response = self.client.post(
            "/config/registration-email",
            headers={"X-API-Token": self.admin_token},
            json={
                "enabled": True,
                "domains": "@qq.com",
                "smtp_host": "smtp.qq.com",
                "smtp_port": 465,
                "smtp_username": "",
                "authorization_code": "",
                "sender_name": "注册服务",
                "code_ttl_minutes": 10,
            },
        )
        self.assertEqual(response.status_code, 200)
        settings = config.load_settings()
        self.assertEqual(settings.registration_smtp_username, "sender@qq.com")
        self.assertEqual(settings.registration_smtp_authorization_code, "saved-authorization-code")

    def test_model_cost_rejects_invalid_precision(self) -> None:
        response = self.client.post(
            "/config/platforms",
            headers={"X-API-Token": self.admin_token},
            json={"platforms": [{"id": "dola", "models": [{"name": "Seedance 2.0", "cost": 1.25}]}]},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
