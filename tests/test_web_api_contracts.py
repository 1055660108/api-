from __future__ import annotations

import asyncio
import json
import inspect
import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app import __version__, accounts, admin_audit, admin_auth, batch_jobs, client_auth, config, data_backup, invitation_codes, main, package_catalog, point_transactions, proxy_manager, registration_security, store, temp_access, users


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
            patch.object(data_backup, "DATA_DIR", self.root),
            patch.object(data_backup, "USERS_PATH", self.root / "users.json"),
            patch.object(data_backup, "ACCOUNTS_PATH", self.root / "accounts.json"),
            patch.object(invitation_codes, "INVITATION_CODES_PATH", self.root / "invitation_codes.json"),
            patch.object(admin_audit, "ADMIN_AUDIT_PATH", self.root / "admin_audit.json"),
            patch.object(package_catalog, "PACKAGE_CATALOG_PATH", self.root / "point_packages.json"),
            patch.object(point_transactions, "TRANSACTIONS_PATH", self.root / "point_transactions.json"),
            patch.dict("os.environ", {"DOLA_ADMIN_USERNAME": "contract-admin", "DOLA_ADMIN_PASSWORD": "ContractPassword123"}),
        ]
        for patcher in self.patchers:
            patcher.start()
        admin_auth.clear_sessions()
        client_auth.clear_client_sessions()
        config.ensure_config()
        config.update_config({"registration_email_verification_enabled": False})
        invitation_codes.set_registration_required(False)
        registration_security.clear_local_state()
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()
        self.admin_token = config.load_settings().api_token

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        admin_auth.clear_sessions()
        client_auth.clear_client_sessions()
        registration_security.clear_local_state()
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

    def test_region_restriction_is_hidden_for_all_client_task_states(self) -> None:
        for reason in ("Dola 当前地区不可用", "region restricted", "country restricted"):
            self.assertEqual(main._client_safe_text(reason, "Seedance 2.0"), "正在重试中，请稍等！")
            self.assertEqual(main._client_safe_text(reason, "Seedance 2.0", terminal=True), "生成失败，请重试！")
        self.assertEqual(main._client_safe_text("正在分配浏览器资源", "Seedance 2.0"), "正在分配服务资源")
        self.assertEqual(main._client_safe_text("正在启动浏览器", "Seedance 2.0"), "正在启动服务")
        self.assertEqual(main._client_safe_text("浏览器超时", "Seedance 2.0"), "服务超时")
        self.assertEqual(main._client_safe_text("游客模式暂不支持生成图片和视频，请登录后再试", "Seedance 2.0"), "正在重试中，请稍等！")
        self.assertEqual(main._client_safe_text("游客模式暂不支持生成图片和视频，请登录后再试", "Seedance 2.0", terminal=True), "生成失败，请重试！")
        self.assertEqual(main._client_safe_text("Dola slider verification required 710022004", "Seedance 2.0"), "正在重试中，请稍等！")
        self.assertEqual(main._client_safe_text("Dola 跳验证（滑块风控）", "Seedance 2.0", terminal=True), "生成失败，请重试！")
        ten_second_reason = "Currently generating videos longer than 10 seconds is not supported, do you want to continue generating for you?"
        self.assertEqual(main._client_safe_text(ten_second_reason, "Seedance 2.0"), "生成接口繁忙请稍后重试！")
        self.assertEqual(main._client_safe_text(ten_second_reason, "Seedance 2.0", terminal=True), "生成接口繁忙请稍后重试！")
        self.assertEqual(main._client_safe_text("生成超过20分钟，仍未返回结果", "Seedance 2.0"), "正在生成中，请稍等！")
        self.assertEqual(main._client_safe_text("生成超过20分钟，仍未返回结果", "Seedance 2.0", terminal=True), "生成失败，请重试！")
        self.assertEqual(main._client_safe_text("reference image upload timed out", "Seedance 2.0"), "参考图上传超时，正在重试！")
        self.assertEqual(main._client_safe_text("prepare_upload timed out", "Seedance 2.0", terminal=True), "参考图上传超时，请重试！")
        self.assertEqual(main._client_safe_text("请选择勾选真人按钮并重试", "Seedance 2.0", terminal=True), "请选择勾选真人按钮并重试")

    def test_failed_region_task_never_displays_retrying_text(self) -> None:
        registered = self.register("failed_region_client")
        owner_hash = temp_access.hash_token(registered["token"])
        task = store.create_task("地区失败终态", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        store.mark_failed(task["id"], "region restricted")
        headers = {"X-API-Token": registered["token"]}

        listed = self.client.get("/tasks?page=1&page_size=20", headers=headers).json()["tasks"][0]
        result = self.client.get(f"/tasks/{task['id']}", headers=headers).json()

        self.assertEqual(listed["status"], "failed")
        self.assertEqual(listed["error"], "生成失败，请重试！")
        self.assertEqual(result, {"code": "0", "text": "生成失败，请重试！", "url": ""})

    def test_result_timeout_reason_is_visible_only_to_admin(self) -> None:
        registered = self.register("timeout_reason_client")
        owner_hash = temp_access.hash_token(registered["token"])
        task = store.create_task("超时原因隔离", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        store.mark_failed(task["id"], "生成超过20分钟，仍未返回结果")

        client_task = self.client.get(
            "/tasks?page=1&page_size=20",
            headers={"X-API-Token": registered["token"]},
        ).json()["tasks"][0]
        admin_task = next(
            item
            for item in self.client.get(
                "/tasks?page=1&page_size=20",
                headers={"X-API-Token": self.admin_token},
            ).json()["tasks"]
            if item["id"] == task["id"]
        )

        self.assertEqual(client_task["error"], "生成失败，请重试！")
        self.assertEqual(admin_task["error"], "生成超过20分钟，仍未返回结果")

    def test_proxy_cooldown_reason_is_visible_only_to_admin(self) -> None:
        registered = self.register("proxy_cooldown_client")
        owner_hash = temp_access.hash_token(registered["token"])
        task = store.create_task("代理冷却原因隔离", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        raw_error = "all configured proxy modes are unavailable (subscription: cooling down)"
        store.mark_failed(task["id"], raw_error)

        client_task = self.client.get(
            "/tasks?page=1&page_size=20",
            headers={"X-API-Token": registered["token"]},
        ).json()["tasks"][0]
        admin_task = next(
            item
            for item in self.client.get(
                "/tasks?page=1&page_size=20",
                headers={"X-API-Token": self.admin_token},
            ).json()["tasks"]
            if item["id"] == task["id"]
        )

        self.assertEqual(client_task["error"], "生成失败，请重试！")
        self.assertEqual(admin_task["error"], raw_error)

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

    def test_video_download_filename_prefers_reference_then_task_prompt(self) -> None:
        self.assertEqual(
            main._video_download_filename(
                {"prompt": "不会使用这个名称", "reference_image_names": ["角色正面参考图.png"]},
                "task-id",
            ),
            "角色正面参考图.mp4",
        )
        self.assertEqual(
            main._video_download_filename({"prompt": "清晨 酒店：镜头/一"}, "task-id"),
            "清晨 酒店：镜头_一.mp4",
        )
        long_name = main._video_download_filename({"prompt": "长" * 200}, "task-id")
        self.assertLessEqual(len(long_name.encode("utf-8")), 184)
        self.assertTrue(long_name.endswith(".mp4"))

    def test_task_video_proxy_forwards_range_and_protects_task_ownership(self) -> None:
        registered = self.register("video_owner")
        owner_hash = temp_access.hash_token(registered["token"])
        task = store.create_task("视频播放", "9:16", owner_token_hash=owner_hash, platform="dola")
        store.save_result(task["id"], extra={"decoded_main_url": "https://cdn.example/video.mp4"})
        store.set_task_images(task["id"], [self.root / "01.png"], ["角色正面参考图.png"])
        captured: dict[str, object] = {}

        class UpstreamResponse:
            status_code = 206
            headers = {
                "content-type": "video/mp4",
                "content-length": "4",
                "content-range": "bytes 0-3/10",
                "accept-ranges": "bytes",
            }

            async def aiter_raw(self):
                yield b"test"

            async def aclose(self):
                captured["response_closed"] = True

        class UpstreamClient:
            def build_request(self, method, url, headers):
                captured.update(method=method, url=url, headers=dict(headers))
                return object()

            async def send(self, request, stream=False):
                captured["stream"] = stream
                return UpstreamResponse()

            async def aclose(self):
                captured["client_closed"] = True

        with patch.object(main.httpx, "AsyncClient", return_value=UpstreamClient()):
            response = self.client.get(
                f"/tasks/{task['id']}/video",
                headers={"X-API-Token": registered["token"], "Range": "bytes=0-3"},
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"test")
        self.assertEqual(response.headers["content-range"], "bytes 0-3/10")
        self.assertEqual(captured["headers"]["Range"], "bytes=0-3")
        self.assertEqual(captured["headers"]["Referer"], "https://www.dola.com/")
        self.assertTrue(captured["response_closed"])
        self.assertTrue(captured["client_closed"])
        self.assertTrue(response.headers["content-disposition"].startswith("inline;"))
        self.assertIn("filename*=UTF-8''%E8%A7%92%E8%89%B2%E6%AD%A3%E9%9D%A2%E5%8F%82%E8%80%83%E5%9B%BE.mp4", response.headers["content-disposition"])

        with patch.object(main.httpx, "AsyncClient", return_value=UpstreamClient()):
            download = self.client.get(
                f"/tasks/{task['id']}/video?download=true",
                headers={"X-API-Token": registered["token"]},
            )
        disposition = download.headers["content-disposition"]
        self.assertIn(f'filename="{task["id"]}.mp4"', disposition)
        self.assertIn("filename*=UTF-8''%E8%A7%92%E8%89%B2%E6%AD%A3%E9%9D%A2%E5%8F%82%E8%80%83%E5%9B%BE.mp4", disposition)

        other = self.register("video_other")
        denied = self.client.get(f"/tasks/{task['id']}/video", headers={"X-API-Token": other["token"]})
        self.assertEqual(denied.status_code, 404)

    def test_task_video_proxy_closes_upstream_on_invalid_redirect(self) -> None:
        registered = self.register("video_redirect_owner")
        owner_hash = temp_access.hash_token(registered["token"])
        task = store.create_task("视频重定向", "9:16", owner_token_hash=owner_hash, platform="dola")
        store.save_result(task["id"], extra={"decoded_main_url": "https://cdn.example/video.mp4"})
        closed: dict[str, bool] = {}

        class RedirectResponse:
            status_code = 302
            headers = {"location": "http://127.0.0.1/private.mp4"}

            async def aclose(self):
                closed["response"] = True

        class RedirectClient:
            def build_request(self, method, url, headers):
                return object()

            async def send(self, request, stream=False):
                return RedirectResponse()

            async def aclose(self):
                closed["client"] = True

        with patch.object(main.httpx, "AsyncClient", return_value=RedirectClient()):
            response = self.client.get(f"/tasks/{task['id']}/video", headers={"X-API-Token": registered["token"]})

        self.assertEqual(response.status_code, 400)
        self.assertTrue(closed["response"])
        self.assertTrue(closed["client"])

    def test_sensitive_probe_paths_are_rejected_before_routing(self) -> None:
        for path in ("/.env", "/.git/config", "/wp-config.php", "/backup.sql"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404, path)
            self.assertEqual(response.json(), {"detail": "Not Found"})
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")

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
        self.assertIn("HttpOnly", login.headers["set-cookie"])
        self.assertIn("SameSite=strict", login.headers["set-cookie"])
        self.assertEqual(self.client.get("/auth/client").status_code, 200)
        invalid_client = self.client.post("/auth/login", json={"username": "contract_client", "password": "invalid"})
        self.assertEqual(invalid_client.status_code, 401)
        self.assertEqual(invalid_client.json(), {"detail": "用户名或密码错误"})

    def test_client_cookie_is_secure_on_https_and_legacy_token_can_upgrade(self) -> None:
        registered = self.register("cookie_upgrade_client")
        self.client.cookies.clear()
        upgraded = self.client.post(
            "/auth/session",
            headers={"X-API-Token": registered["token"], "X-Forwarded-Proto": "https"},
        )
        self.assertEqual(upgraded.status_code, 200)
        cookie = upgraded.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=strict", cookie)

    def test_cookie_authenticated_writes_reject_cross_site_origin(self) -> None:
        self.register("csrf_client")
        rejected = self.client.post(
            "/feedback",
            headers={"Origin": "https://attacker.example", "X-Dola-Portal": "client"},
            json={"category": "其他", "content": "cross site"},
        )
        self.assertEqual(rejected.status_code, 403)
        allowed = self.client.post(
            "/feedback",
            headers={"Origin": "http://testserver", "X-Dola-Portal": "client"},
            json={"category": "其他", "content": "same site"},
        )
        self.assertEqual(allowed.status_code, 201)

    def test_client_login_moves_password_work_off_event_loop(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("await asyncio.to_thread(login_user", source)
        self.assertIn('await _rate_limit(request, "client-login-ip", 200, 60)', source)

    def test_health_contract_is_role_scoped_for_admin_and_client(self) -> None:
        registered = self.register()
        queue_health = {"ok": True, "backend": "file", "ready": 0, "processing": 0, "delayed": 0, "error": "internal queue detail"}
        queue = unittest.mock.Mock()
        queue.health.return_value = queue_health
        queue.client = None
        with patch("app.task_queue.get_task_queue", return_value=queue), patch("app.main.resolve_browser_executable", return_value="browser.exe"):
            admin = self.client.get("/health", headers={"X-API-Token": self.admin_token}).json()
            client = self.client.get("/auth/client", headers={"X-API-Token": registered["token"]}).json()
        self.assertTrue({"ok", "version", "status", "role", "browser_workers", "active", "components", "admin_username"} <= set(admin))
        self.assertEqual(admin["version"], __version__)
        self.assertEqual(admin["role"], "admin")
        self.assertEqual(admin["components"]["queue"]["error"], "internal queue detail")
        self.assertIn("monitoring", admin["components"])
        self.assertTrue({"quota", "token_concurrency", "active_task_count", "task_retention_days", "user_name"} <= set(client))
        self.assertEqual(client["role"], "client")
        self.assertEqual(client["active_task_count"], 0)
        self.assertEqual(set(client["quota"]), {"limit", "used", "remaining", "free_remaining", "points"})
        self.assertNotIn("admin_username", client)
        self.assertNotIn("error", client["components"]["queue"])
        self.assertNotIn("executable_path", client["components"]["browser"])
        self.assertNotIn("monitoring", client["components"])

    def test_runtime_submit_interval_is_admin_only_persistent_and_validated(self) -> None:
        self.assertEqual(self.client.get("/config/runtime").status_code, 403)
        self.login_admin()
        current = self.client.get("/config/runtime").json()
        self.assertEqual(current["dola_submit_interval_seconds"], 5.0)
        self.assertEqual(current["dola_global_submit_interval_seconds"], 8.0)
        self.assertEqual(current["task_retry_limit"], 2)
        self.assertEqual(current["doubao_submit_retry_limit"], 2)
        self.assertEqual(current["batch_history_retention_days"], 30)
        updated = self.client.post("/config/runtime", json={"dola_submit_interval_seconds": 2.5, "dola_global_submit_interval_seconds": 9.5, "task_retry_limit": 5, "doubao_submit_retry_limit": 4, "batch_history_retention_days": 14})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["dola_submit_interval_seconds"], 2.5)
        self.assertEqual(updated.json()["dola_global_submit_interval_seconds"], 9.5)
        self.assertEqual(updated.json()["task_retry_limit"], 5)
        self.assertEqual(updated.json()["doubao_submit_retry_limit"], 4)
        self.assertEqual(updated.json()["batch_history_retention_days"], 14)
        self.assertEqual(config.load_settings().dola_submit_interval_seconds, 2.5)
        self.assertEqual(config.load_settings().dola_global_submit_interval_seconds, 9.5)
        self.assertEqual(config.load_settings().task_retry_limit, 5)
        self.assertEqual(config.load_settings().doubao_submit_retry_limit, 4)
        self.assertEqual(config.load_settings().batch_history_retention_days, 14)
        for invalid in (0.9, 5.1, "invalid"):
            self.assertEqual(self.client.post("/config/runtime", json={"dola_submit_interval_seconds": invalid}).status_code, 400)
        for invalid in (2.9, 30.1, "invalid"):
            self.assertEqual(self.client.post("/config/runtime", json={"dola_global_submit_interval_seconds": invalid}).status_code, 400)
        for invalid in (-1, 11, "invalid"):
            self.assertEqual(self.client.post("/config/runtime", json={"task_retry_limit": invalid}).status_code, 400)
            self.assertEqual(self.client.post("/config/runtime", json={"doubao_submit_retry_limit": invalid}).status_code, 400)
        for invalid in (6, 31, "invalid"):
            self.assertEqual(self.client.post("/config/runtime", json={"batch_history_retention_days": invalid}).status_code, 400)

    def test_batch_history_cleanup_removes_only_expired_terminal_jobs(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        with patch.object(batch_jobs, "_now", return_value=old):
            expired = batch_jobs.create_job("owner", [{"prompt": "expired"}], ratio="9:16", concurrency=1)
            active = batch_jobs.create_job("owner", [{"prompt": "active"}], ratio="9:16", concurrency=1)

            def finish(job: dict) -> None:
                job["status"] = "completed"
                job["finished_at"] = old
                job["rows"][0]["status"] = "completed"

            batch_jobs._mutate_job(expired["id"], finish)
        recent = batch_jobs.create_job("owner", [{"prompt": "recent"}], ratio="9:16", concurrency=1)
        batch_jobs._mutate_job(recent["id"], finish)
        removed = batch_jobs.cleanup_history(30)
        self.assertEqual([item["id"] for item in removed], [expired["id"]])
        self.assertIsNone(batch_jobs.get_job(expired["id"]))
        self.assertIsNotNone(batch_jobs.get_job(active["id"]))
        self.assertIsNotNone(batch_jobs.get_job(recent["id"]))

    def test_admin_audit_prunes_old_entries_and_keeps_recent_limit(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        recent = datetime.now(timezone.utc).isoformat()
        admin_audit.ADMIN_AUDIT_PATH.write_text(json.dumps({"entries": [
            {"id": "old", "created_at": old},
            {"id": "new-1", "created_at": recent},
            {"id": "new-2", "created_at": recent},
        ]}), encoding="utf-8")
        self.assertEqual(admin_audit.prune_admin_actions(90, 1), 2)
        self.assertEqual([item["id"] for item in admin_audit.list_admin_actions()["entries"]], ["new-2"])

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

    def test_client_can_parse_spreadsheet_prompts_and_submit_selected_duration(self) -> None:
        registered = self.register("batch_spreadsheet_client")
        headers = {"X-API-Token": registered["token"]}
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["序号", "视频提示词"])
        sheet.append([1, "雨夜街道上的电影感镜头"])
        sheet.append([2, "清晨云海延时摄影"])
        payload = BytesIO()
        workbook.save(payload)

        parsed = self.client.post(
            "/batch-prompts/parse",
            headers=headers,
            files={"spreadsheet": ("prompts.xlsx", payload.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        self.assertEqual(parsed.status_code, 200, parsed.text)
        self.assertEqual(parsed.json()["count"], 2)
        self.assertEqual([item["prompt"] for item in parsed.json()["prompts"]], ["雨夜街道上的电影感镜头", "清晨云海延时摄影"])

        submitted = self.client.post(
            "/tasks",
            headers={**headers, "Idempotency-Key": "batch-duration-10"},
            data={"prompt": parsed.json()["prompts"][0]["prompt"], "ratio": "16:9", "duration": "10", "batch": "true", "batch_id": "batch-order-1", "batch_index": "1", "batch_row": "2", "platform": "dola", "model": "Seedance 2.0"},
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        meta = store.get_meta(submitted.json()["id"])
        self.assertEqual(meta["duration"], 10)
        self.assertEqual((meta["batch_id"], meta["batch_index"], meta["batch_row"]), ("batch-order-1", 1, 2))

    def test_duration_configuration_controls_submission_and_points_charge(self) -> None:
        config.update_config({
            "model_durations": {
                "dola": {"Seedance 2.0": [5, 15]},
                "doubao": {"Seedance 2.0 Mini": [10], "Seedance 2.0 Fast": [10]},
                "qianwen": {"万相 2.7": [10]},
            },
            "model_duration_costs": {
                "dola": {"Seedance 2.0": {"5": 0.5, "10": 1.1, "15": 2.3}},
                "doubao": {"Seedance 2.0 Mini": {"10": 1}, "Seedance 2.0 Fast": {"10": 1}},
                "qianwen": {"万相 2.7": {"10": 0.8}},
            },
        })
        registered = self.register("duration_price_client")
        owner_hash = temp_access.hash_token(registered["token"])
        temp_access.add_temp_credit_units(owner_hash, 50)
        temp_access.set_temp_billing_priority(owner_hash, "points_first")
        headers = {"X-API-Token": registered["token"]}

        disabled = self.client.post("/tasks", headers=headers, data={"prompt": "禁用时长", "platform": "dola", "model": "Seedance 2.0", "duration": "10"})
        self.assertEqual(disabled.status_code, 400)
        self.assertIn("未开启 10 秒", disabled.text)
        doubao_invalid = self.client.post("/tasks", headers=headers, data={"prompt": "豆包错误时长", "platform": "doubao", "model": "Seedance 2.0 Mini", "duration": "15"})
        self.assertEqual(doubao_invalid.status_code, 400)

        before = temp_access.get_temp_context_by_hash(owner_hash)
        submitted = self.client.post("/tasks", headers=headers, data={"prompt": "十五秒正确扣费", "platform": "dola", "model": "Seedance 2.0", "duration": "15"})
        self.assertEqual(submitted.status_code, 200, submitted.text)
        meta = store.get_meta(submitted.json()["id"])
        after = temp_access.get_temp_context_by_hash(owner_hash)
        self.assertEqual(meta["duration"], 15)
        self.assertEqual(before.credit_units - after.credit_units, 23)

    def test_persistent_batch_plan_has_no_task_id_or_charge_before_scheduler_claim(self) -> None:
        registered = self.register("persistent_batch_waiting")
        owner_hash = temp_access.hash_token(registered["token"])
        temp_access.add_temp_credit_units(owner_hash, 20)
        temp_access.set_temp_billing_priority(owner_hash, "points_first")
        headers = {"X-API-Token": registered["token"]}
        before = temp_access.get_temp_context_by_hash(owner_hash)
        manifest = {
            "ratio": "9:16",
            "concurrency": 1,
            "rows": [
                {"client_index": index, "sheet_row": index + 2, "prompt": f"后台公平排队 {index + 1}", "image_count": 0}
                for index in range(3)
            ],
        }
        with patch.object(main, "batch_scheduler_tick", new=AsyncMock(return_value=False)):
            response = self.client.post("/batch-prompts/jobs", headers=headers, data={"manifest": json.dumps(manifest, ensure_ascii=False)})
            self.assertEqual(response.status_code, 201, response.text)
            job = response.json()["job"]
            self.assertEqual(job["counts"]["queued"], 3)
            self.assertTrue(all(not row["task_id"] for row in job["rows"]))
            self.assertEqual(store.list_tasks(owner_token_hash=owner_hash), [])
            after = temp_access.get_temp_context_by_hash(owner_hash)
            self.assertEqual((after.free_remaining, after.credit_units), (before.free_remaining, before.credit_units))
            current = self.client.get("/batch-prompts/jobs/current", headers=headers)
            self.assertEqual(current.status_code, 200)
            self.assertEqual(current.json()["job"]["id"], job["id"])
            canceled = self.client.post(f"/batch-prompts/{job['id']}/cancel", headers=headers)
            self.assertEqual(canceled.status_code, 200, canceled.text)
            self.assertEqual(canceled.json()["job"]["counts"]["canceled"], 3)
        self.assertEqual(store.list_tasks(owner_token_hash=owner_hash), [])

    def test_persistent_batch_uses_selected_platform_and_model(self) -> None:
        registered = self.register("persistent_batch_model")
        owner_hash = temp_access.hash_token(registered["token"])
        temp_access.add_temp_credit_units(owner_hash, 20)
        headers = {"X-API-Token": registered["token"]}
        manifest = {
            "platform": "doubao",
            "model": "Seedance 2.0 Fast",
            "duration": 10,
            "ratio": "16:9",
            "concurrency": 1,
            "rows": [{"client_index": 0, "sheet_row": 2, "prompt": "豆包批量模型任务", "image_count": 0}],
        }
        with patch.object(main, "batch_scheduler_tick", new=AsyncMock(return_value=False)):
            response = self.client.post("/batch-prompts/jobs", headers=headers, data={"manifest": json.dumps(manifest, ensure_ascii=False)})
            self.assertEqual(response.status_code, 201, response.text)
            job = response.json()["job"]
            self.assertEqual((job["platform"], job["model"], job["duration"]), ("doubao", "Seedance 2.0 Fast", 10))
            claim = batch_jobs.claim_next_row(owner_hash)
            task_id = self.client.portal.call(main._create_scheduled_batch_task, claim)

        meta = store.get_meta(task_id)
        self.assertEqual((meta["platform"], meta["model"], meta["duration"]), ("doubao", "Seedance 2.0 Fast", 10))

    def test_one_hundred_row_images_are_uploaded_in_chunks_before_job_creation(self) -> None:
        registered = self.register("chunked_batch_images")
        owner_hash = temp_access.hash_token(registered["token"])
        headers = {"X-API-Token": registered["token"]}
        batch_id = "chunked-images-100"
        upload_id = ""
        with patch.object(main, "batch_scheduler_tick", new=AsyncMock(return_value=False)):
            for start in range(0, 100, 16):
                entries = [
                    {"row_index": index + 1, "image_index": 1, "name": f"reference-{index + 1:03d}.png"}
                    for index in range(start, min(100, start + 16))
                ]
                files = [
                    ("images", (entry["name"], b"\x89PNG\r\n\x1a\n" + entry["name"].encode(), "image/png"))
                    for entry in entries
                ]
                response = self.client.post(
                    "/batch-prompts/job-assets",
                    headers=headers,
                    data={"batch_id": batch_id, "upload_id": upload_id, "manifest": json.dumps(entries)},
                    files=files,
                )
                self.assertEqual(response.status_code, 200, response.text)
                upload_id = response.json()["upload_id"]
            self.assertEqual(response.json()["uploaded_count"], 100)
            manifest = {
                "ratio": "9:16",
                "concurrency": 20,
                "reference_batch_id": batch_id,
                "asset_upload_id": upload_id,
                "rows": [
                    {"client_index": index, "sheet_row": index + 2, "prompt": f"分片参考图任务 {index + 1}", "image_count": 1}
                    for index in range(100)
                ],
            }
            created = self.client.post(
                "/batch-prompts/jobs",
                headers=headers,
                data={"manifest": json.dumps(manifest, ensure_ascii=False)},
            )
        self.assertEqual(created.status_code, 201, created.text)
        job = created.json()["job"]
        self.assertEqual(job["counts"]["queued"], 100)
        self.assertFalse(main._batch_asset_upload_path(upload_id).exists())
        asset_files = list(main._batch_job_assets_path(job["id"]).glob("*.png"))
        self.assertEqual(len(asset_files), 100)
        persisted = batch_jobs.get_job(job["id"], owner_hash)
        self.assertEqual(persisted["rows"][0]["image_names"], ["reference-001.png"])
        self.assertEqual(persisted["rows"][-1]["image_names"], ["reference-100.png"])

    def test_chunked_batch_job_rejects_missing_row_image(self) -> None:
        registered = self.register("chunked_batch_missing")
        headers = {"X-API-Token": registered["token"]}
        batch_id = "chunked-images-missing"
        uploaded = self.client.post(
            "/batch-prompts/job-assets",
            headers=headers,
            data={
                "batch_id": batch_id,
                "manifest": json.dumps([{"row_index": 1, "image_index": 1, "name": "first.png"}]),
            },
            files=[("images", ("first.png", b"\x89PNG\r\n\x1a\nfirst", "image/png"))],
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        upload_id = uploaded.json()["upload_id"]
        manifest = {
            "ratio": "9:16",
            "concurrency": 2,
            "reference_batch_id": batch_id,
            "asset_upload_id": upload_id,
            "rows": [
                {"client_index": 0, "sheet_row": 2, "prompt": "第一行", "image_count": 1},
                {"client_index": 1, "sheet_row": 3, "prompt": "第二行", "image_count": 1},
            ],
        }
        created = self.client.post(
            "/batch-prompts/jobs",
            headers=headers,
            data={"manifest": json.dumps(manifest, ensure_ascii=False)},
        )
        self.assertEqual(created.status_code, 400, created.text)
        self.assertTrue(main._batch_asset_upload_path(upload_id).exists())

    def test_persistent_batch_claim_creates_and_charges_only_one_row(self) -> None:
        registered = self.register("persistent_batch_claim")
        owner_hash = temp_access.hash_token(registered["token"])
        temp_access.add_temp_credit_units(owner_hash, 20)
        temp_access.set_temp_billing_priority(owner_hash, "points_first")
        headers = {"X-API-Token": registered["token"]}
        manifest = {
            "ratio": "16:9",
            "concurrency": 1,
            "rows": [
                {"client_index": 0, "sheet_row": 2, "prompt": "第一条后台任务", "image_count": 0},
                {"client_index": 1, "sheet_row": 3, "prompt": "第二条仍在排队", "image_count": 0},
            ],
        }
        with patch.object(main, "batch_scheduler_tick", new=AsyncMock(return_value=False)):
            response = self.client.post("/batch-prompts/jobs", headers=headers, data={"manifest": json.dumps(manifest, ensure_ascii=False)})
            self.assertEqual(response.status_code, 201, response.text)
            job_id = response.json()["job"]["id"]
            claim = batch_jobs.claim_next_row(owner_hash)
            self.assertIsNotNone(claim)
            task_id = self.client.portal.call(main._create_scheduled_batch_task, claim)
            batch_jobs.finish_row_creation(job_id, 1, task_id)
            job = batch_jobs.public_job(batch_jobs.get_job(job_id, owner_hash))
        self.assertEqual(job["rows"][0]["task_id"], task_id)
        self.assertEqual(job["rows"][0]["status"], "running")
        self.assertEqual(job["rows"][1]["status"], "queued")
        self.assertEqual(job["rows"][1]["task_id"], "")
        self.assertEqual(len(store.list_tasks(owner_token_hash=owner_hash)), 1)
        reservation = temp_access.get_temp_reservation(owner_hash, task_id)
        self.assertEqual(reservation["status"], "reserved")

    def test_persistent_batch_status_reconciles_failed_task(self) -> None:
        registered = self.register("batch_failed_sync")
        owner_hash = temp_access.hash_token(registered["token"])
        temp_access.add_temp_credit_units(owner_hash, 20)
        headers = {"X-API-Token": registered["token"]}
        manifest = {
            "ratio": "9:16",
            "concurrency": 1,
            "rows": [{"client_index": 0, "sheet_row": 2, "prompt": "失败状态同步", "image_count": 0}],
        }
        with patch.object(main, "batch_scheduler_tick", new=AsyncMock(return_value=False)):
            created = self.client.post(
                "/batch-prompts/jobs",
                headers=headers,
                data={"manifest": json.dumps(manifest, ensure_ascii=False)},
            )
            self.assertEqual(created.status_code, 201, created.text)
            job_id = created.json()["job"]["id"]
            claim = batch_jobs.claim_next_row(owner_hash)
            task_id = self.client.portal.call(main._create_scheduled_batch_task, claim)
            batch_jobs.finish_row_creation(job_id, 1, task_id)
            store.mark_failed(task_id, "多次生成失败")

            refreshed = self.client.get(f"/batch-prompts/jobs/{job_id}", headers=headers)

        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        job = refreshed.json()["job"]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["counts"]["failed"], 1)
        self.assertEqual(job["rows"][0]["status"], "failed")
        self.assertEqual(job["rows"][0]["error"], "多次生成失败")

    def test_persistent_batch_status_returns_only_rows_changed_after_revision(self) -> None:
        registered = self.register("batch_incremental_status")
        owner_hash = temp_access.hash_token(registered["token"])
        headers = {"X-API-Token": registered["token"]}
        manifest = {
            "ratio": "9:16",
            "concurrency": 1,
            "rows": [
                {"client_index": index, "sheet_row": index + 2, "prompt": f"增量状态 {index + 1}", "image_count": 0}
                for index in range(3)
            ],
        }
        with patch.object(main, "batch_scheduler_tick", new=AsyncMock(return_value=False)):
            created = self.client.post(
                "/batch-prompts/jobs",
                headers=headers,
                data={"manifest": json.dumps(manifest, ensure_ascii=False)},
            )
            self.assertEqual(created.status_code, 201, created.text)
            initial = created.json()["job"]
            self.assertFalse(initial["delta"])
            self.assertEqual(initial["total"], 3)
            self.assertEqual(len(initial["rows"]), 3)
            initial_revision = initial["revision"]

            unchanged = self.client.get(
                f"/batch-prompts/jobs/{initial['id']}?since_revision={initial_revision}",
                headers=headers,
            ).json()["job"]
            self.assertTrue(unchanged["delta"])
            self.assertEqual(unchanged["revision"], initial_revision)
            self.assertEqual(unchanged["rows"], [])
            self.assertEqual(unchanged["total"], 3)

            claim = batch_jobs.claim_next_row(owner_hash)
            self.assertIsNotNone(claim)
            changed = self.client.get(
                f"/batch-prompts/jobs/{initial['id']}?since_revision={initial_revision}",
                headers=headers,
            ).json()["job"]
            self.assertTrue(changed["delta"])
            self.assertGreater(changed["revision"], initial_revision)
            self.assertEqual([row["index"] for row in changed["rows"]], [1])
            self.assertEqual(changed["rows"][0]["status"], "creating")

            recovered = self.client.get(
                f"/batch-prompts/jobs/{initial['id']}?since_revision=999999",
                headers=headers,
            ).json()["job"]
            self.assertFalse(recovered["delta"])
            self.assertEqual(len(recovered["rows"]), 3)

    def test_persistent_batch_scheduler_copies_shared_and_row_reference_images(self) -> None:
        registered = self.register("persistent_batch_images")
        owner_hash = temp_access.hash_token(registered["token"])
        temp_access.add_temp_credit_units(owner_hash, 20)
        headers = {"X-API-Token": registered["token"]}
        reference_batch_id = "persistent-reference-session"
        shared_image = b"\x89PNG\r\n\x1a\nshared-persistent"
        row_image = b"\x89PNG\r\n\x1a\nrow-persistent"
        shared = self.client.post(
            "/batch-prompts/references",
            headers=headers,
            data={"batch_id": reference_batch_id},
            files=[("images", ("shared.png", shared_image, "image/png"))],
        )
        self.assertEqual(shared.status_code, 200, shared.text)
        manifest = {
            "ratio": "9:16",
            "concurrency": 1,
            "reference_id": shared.json()["reference_id"],
            "reference_count": 1,
            "reference_batch_id": reference_batch_id,
            "reference_is_real_person": True,
            "rows": [{"client_index": 0, "sheet_row": 2, "prompt": "带两类参考图的任务", "image_count": 1}],
        }
        with patch.object(main, "batch_scheduler_tick", new=AsyncMock(return_value=False)):
            created = self.client.post(
                "/batch-prompts/jobs",
                headers=headers,
                data={"manifest": json.dumps(manifest, ensure_ascii=False)},
                files=[("images", ("row.png", row_image, "image/png"))],
            )
            self.assertEqual(created.status_code, 201, created.text)
            self.assertTrue(created.json()["job"]["reference_is_real_person"])
            claim = batch_jobs.claim_next_row(owner_hash)
            task_id = self.client.portal.call(main._create_scheduled_batch_task, claim)
        self.assertEqual([path.read_bytes() for path in store.task_image_paths(task_id)], [shared_image, row_image])
        self.assertEqual(store.get_meta(task_id)["reference_image_names"], ["shared.png", "row.png"])
        self.assertTrue(store.get_meta(task_id)["reference_is_real_person"])

    def test_manual_reference_real_person_flag_defaults_off_and_persists_when_checked(self) -> None:
        registered = self.register("real_person_flag")
        owner_hash = temp_access.hash_token(registered["token"])
        temp_access.add_temp_credit_units(owner_hash, 20)
        temp_access.set_temp_billing_priority(owner_hash, "points_first")
        headers = {"X-API-Token": registered["token"]}

        unchecked = self.client.post(
            "/tasks",
            headers={**headers, "Idempotency-Key": "reference-real-person-off"},
            data={"prompt": "未勾选真人参考图", "ratio": "9:16", "platform": "dola", "model": "Seedance 2.0"},
            files=[("images", ("scene.png", b"\x89PNG\r\n\x1a\nscene", "image/png"))],
        )
        self.assertEqual(unchecked.status_code, 200, unchecked.text)
        self.assertFalse(store.get_meta(unchecked.json()["id"])["reference_is_real_person"])

        checked = self.client.post(
            "/tasks",
            headers={**headers, "Idempotency-Key": "reference-real-person-on"},
            data={"prompt": "已勾选真人参考图", "ratio": "9:16", "platform": "dola", "model": "Seedance 2.0", "reference_is_real_person": "true"},
            files=[("images", ("person.png", b"\x89PNG\r\n\x1a\nperson", "image/png"))],
        )
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertTrue(store.get_meta(checked.json()["id"])["reference_is_real_person"])

    def test_batch_coordinator_rotates_eligible_owners(self) -> None:
        batch_jobs._LOCAL_OWNER_CURSOR = 0
        coordinator = batch_jobs.BatchCoordinator()
        self.assertEqual([coordinator.next_owner({"owner-a", "owner-b"}) for _ in range(4)], ["owner-a", "owner-b", "owner-a", "owner-b"])

    def test_one_hundred_batch_tasks_are_enqueued_in_spreadsheet_order(self) -> None:
        registered = self.register("batch_order_100_client")
        owner_hash = temp_access.hash_token(registered["token"])
        temp_access.add_temp_credit_units(owner_hash, 2000)
        temp_access.set_temp_billing_priority(owner_hash, "points_first")
        headers = {"X-API-Token": registered["token"]}

        for index in range(1, 101):
            response = self.client.post(
                "/tasks",
                headers={**headers, "Idempotency-Key": f"batch-order-100-{index:04d}"},
                data={
                    "prompt": f"按顺序生成的视频提示词 {index:03d}",
                    "ratio": "9:16",
                    "duration": "10",
                    "batch": "true",
                    "batch_id": "batch-order-100",
                    "batch_index": str(index),
                    "batch_row": str(index + 1),
                    "platform": "dola",
                    "model": "Seedance 2.0",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)

        metas = [item for item in store.list_tasks(owner_token_hash=owner_hash) if item["batch_id"] == "batch-order-100"]
        metas.sort(key=lambda item: item["queued_at"])
        self.assertEqual([item["batch_index"] for item in metas], list(range(1, 101)))
        self.assertEqual([item["batch_row"] for item in metas], list(range(2, 102)))

    def test_batch_shared_reference_is_uploaded_once_and_copied_to_following_tasks(self) -> None:
        registered = self.register("batch_shared_ref")
        owner_hash = temp_access.hash_token(registered["token"])
        temp_access.add_temp_credit_units(owner_hash, 500)
        temp_access.set_temp_billing_priority(owner_hash, "points_first")
        headers = {"X-API-Token": registered["token"]}
        common = {
            "ratio": "9:16",
            "duration": "15",
            "batch": "true",
            "batch_id": "batch-shared-reference",
            "platform": "dola",
            "model": "Seedance 2.0",
        }
        shared_image = b"\x89PNG\r\n\x1a\nshared-reference"
        row_image = b"\x89PNG\r\n\x1a\nrow-reference"
        uploaded = self.client.post(
            "/batch-prompts/references",
            headers=headers,
            data={"batch_id": common["batch_id"]},
            files=[("images", ("shared.png", shared_image, "image/png"))],
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        reference_id = uploaded.json()["reference_id"]
        self.assertEqual(uploaded.json()["image_count"], 1)

        created = []
        for index in range(1, 21):
            files = [("images", ("row.png", row_image, "image/png"))] if index == 2 else []
            response = self.client.post(
                "/tasks",
                headers={**headers, "Idempotency-Key": f"batch-shared-reference-{index:04d}"},
                data={**common, "prompt": f"第{index}条", "batch_index": str(index), "batch_row": str(index + 1), "batch_reference_id": reference_id, "batch_reference_image_count": "1"},
                files=files,
            )
            self.assertEqual(response.status_code, 200, response.text)
            created.append(response.json()["id"])
            copied = [path.read_bytes() for path in store.task_image_paths(response.json()["id"])]
            self.assertEqual(copied, [shared_image, row_image] if index == 2 else [shared_image])
            self.assertEqual(
                store.get_meta(response.json()["id"])["reference_image_names"],
                ["shared.png", "row.png"] if index == 2 else ["shared.png"],
            )
        second_id = created[1]
        self.assertEqual(self.client.delete(f"/batch-prompts/references/{reference_id}", headers=headers).status_code, 200)
        store.save_result(second_id, extra={"decoded_main_url": "https://example.com/batch-result.mp4"})
        store.mark_success(second_id)
        statuses = self.client.post(
            "/batch-prompts/status",
            headers=headers,
            json={"task_ids": created},
        )
        self.assertEqual(statuses.status_code, 200, statuses.text)
        by_id = {item["id"]: item for item in statuses.json()["tasks"]}
        self.assertEqual(by_id[second_id]["code"], "2")
        self.assertEqual(by_id[second_id]["url"], "https://example.com/batch-result.mp4")

    def test_task_creation_semaphore_is_isolated_per_user(self) -> None:
        first = temp_access.AccessContext(token_hash="owner-a", is_admin=False, is_temp=True)
        second = temp_access.AccessContext(token_hash="owner-b", is_admin=False, is_temp=True)
        main._OWNER_CREATE_SEMAPHORES.clear()
        self.assertIs(main._owner_create_semaphore(first), main._owner_create_semaphore(first))
        self.assertIsNot(main._owner_create_semaphore(first), main._owner_create_semaphore(second))

    def test_canceled_batch_does_not_create_or_charge_following_tasks(self) -> None:
        registered = self.register("cancel_batch_client")
        owner_hash = temp_access.hash_token(registered["token"])
        temp_access.add_temp_credit_units(owner_hash, 10)
        headers = {"X-API-Token": registered["token"]}
        batch_id = "batch-stop-before-create"

        canceled = self.client.post(f"/batch-prompts/{batch_id}/cancel", headers=headers)
        self.assertEqual(canceled.status_code, 200, canceled.text)
        response = self.client.post(
            "/tasks",
            headers={**headers, "Idempotency-Key": "canceled-batch-task"},
            data={
                "prompt": "不应创建的任务",
                "ratio": "9:16",
                "duration": "15",
                "batch": "true",
                "batch_id": batch_id,
                "batch_index": "1",
                "platform": "dola",
                "model": "Seedance 2.0",
            },
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "批量提交已停止")
        self.assertEqual(store.list_tasks(owner_token_hash=owner_hash), [])
        self.assertEqual(temp_access.get_temp_context_by_hash(owner_hash).credit_units, 10)

    def test_batch_scheduler_uses_fair_owner_capacity_limits(self) -> None:
        source = inspect.getsource(main.batch_scheduler_tick)
        self.assertIn("fair_owner_capacity_limits(owner_limits, global_capacity)", source)
        self.assertIn("owner_active < fair_limit", source)

    def test_canceled_batch_plan_is_rejected_before_persistent_job_creation(self) -> None:
        registered = self.register("cancel_batch_plan_client")
        owner_hash = temp_access.hash_token(registered["token"])
        headers = {"X-API-Token": registered["token"]}
        batch_id = "batch-stop-before-plan"
        canceled = self.client.post(f"/batch-prompts/{batch_id}/cancel", headers=headers)
        self.assertEqual(canceled.status_code, 200, canceled.text)

        manifest = {
            "ratio": "9:16",
            "concurrency": 1,
            "reference_batch_id": batch_id,
            "rows": [{"client_index": 0, "sheet_row": 2, "prompt": "不应保存的批量计划", "image_count": 0}],
        }
        response = self.client.post(
            "/batch-prompts/jobs",
            headers=headers,
            data={"manifest": json.dumps(manifest, ensure_ascii=False)},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "批量提交已停止")
        self.assertEqual(batch_jobs.list_jobs(owner_hash), [])
        self.assertEqual(store.list_tasks(owner_token_hash=owner_hash), [])

    def test_large_batch_from_one_user_does_not_make_another_user_busy(self) -> None:
        first = self.register("busy_owner_a")
        second = self.register("busy_owner_b")
        first_hash = temp_access.hash_token(first["token"])
        second_hash = temp_access.hash_token(second["token"])
        temp_access.add_temp_credit_units(first_hash, 500)
        temp_access.add_temp_credit_units(second_hash, 50)
        temp_access.set_temp_billing_priority(first_hash, "points_first")
        temp_access.set_temp_billing_priority(second_hash, "points_first")

        def submit(owner: dict, index: int):
            return self.client.post(
                "/tasks",
                headers={"X-API-Token": owner["token"], "Idempotency-Key": f"fair-{owner['username']}-{index}"},
                data={"prompt": f"并发公平任务 {index}", "ratio": "9:16", "platform": "dola", "model": "Seedance 2.0", "task_type": "video", "batch": "true", "batch_id": f"fair-{owner['username']}", "batch_index": str(index)},
            )

        with ThreadPoolExecutor(max_workers=12) as executor:
            first_futures = [executor.submit(submit, first, index) for index in range(1, 11)]
            second_future = executor.submit(submit, second, 1)
            second_response = second_future.result(timeout=20)
            first_responses = [future.result(timeout=30) for future in first_futures]

        self.assertEqual(second_response.status_code, 200, second_response.text)
        self.assertTrue(all(response.status_code == 200 for response in first_responses), [response.text for response in first_responses])

    def test_successful_and_failed_tasks_can_be_retried_as_new_charged_tasks(self) -> None:
        registered = self.register("retry_completed_client")
        owner_hash = temp_access.hash_token(registered["token"])
        temp_access.add_temp_credit_units(owner_hash, 20)
        temp_access.set_temp_billing_priority(owner_hash, "points_first")
        headers = {"X-API-Token": registered["token"]}
        source_ids = []
        for status in ("failed", "success"):
            source = store.create_task(f"{status} task", "16:9", owner_token_hash=owner_hash, model="Seedance 2.0", duration=10)
            image = store.images_dir(source["id"]) / "01.png"
            image.write_bytes(b"reference-image")
            store.set_task_images(source["id"], [image], [f"{status}-reference.png"])
            store.update_meta(source["id"], reference_is_real_person=True)
            if status == "failed":
                store.mark_failed(source["id"], "test failure")
            else:
                store.update_meta(source["id"], status=store.STATUS_SUCCESS, finished_at=store.utc_now())
            source_ids.append(source["id"])

        retry_ids = []
        for source_id, expected_reference_name in zip(source_ids, ["failed-reference.png", "success-reference.png"], strict=True):
            response = self.client.post(f"/tasks/{source_id}/retry", headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            retry_id = response.json()["id"]
            retry_ids.append(retry_id)
            retry_meta = store.get_meta(retry_id)
            self.assertEqual(retry_meta["status"], store.STATUS_PENDING)
            self.assertEqual(retry_meta["retry_of_task_id"], source_id)
            self.assertEqual(retry_meta["duration"], 10)
            self.assertEqual(retry_meta["reference_image_names"], [expected_reference_name])
            self.assertTrue(retry_meta["reference_is_real_person"])
            self.assertEqual(store.task_image_paths(retry_id)[0].read_bytes(), b"reference-image")

        self.assertEqual(len(set(retry_ids)), 2)
        active = store.create_task("active task", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        self.assertEqual(self.client.post(f"/tasks/{active['id']}/retry", headers=headers).status_code, 409)

    def test_task_status_filter_groups_generating_states_across_pages(self) -> None:
        pending = store.create_task("筛选 pending", "9:16", model="Seedance 2.0")
        running = store.create_task("筛选 running", "9:16", model="Seedance 2.0")
        submitted = store.create_task("筛选 submitted", "9:16", model="Seedance 2.0")
        success = store.create_task("筛选 success", "9:16", model="Seedance 2.0")
        failed = store.create_task("筛选 failed", "9:16", model="Seedance 2.0")
        canceled = store.create_task("筛选 canceled", "9:16", model="Seedance 2.0")
        self.assertTrue(store.mark_running(running["id"], "worker-running"))
        self.assertTrue(store.mark_running(submitted["id"], "worker-submitted"))
        store.mark_submitted(submitted["id"])
        store.update_meta(success["id"], status=store.STATUS_SUCCESS, finished_at=store.utc_now())
        store.mark_failed(failed["id"], "测试失败")
        store.update_meta(canceled["id"], status=store.STATUS_CANCELED, finished_at=store.utc_now())

        first_page = self.client.get(
            "/tasks?page=1&page_size=2&status=generating",
            headers={"X-API-Token": self.admin_token},
        )
        second_page = self.client.get(
            "/tasks?page=2&page_size=2&status=generating",
            headers={"X-API-Token": self.admin_token},
        )
        self.assertEqual(first_page.status_code, 200)
        self.assertEqual((first_page.json()["total"], first_page.json()["total_pages"]), (3, 2))
        generating_ids = {item["id"] for item in first_page.json()["tasks"] + second_page.json()["tasks"]}
        self.assertEqual(generating_ids, {pending["id"], running["id"], submitted["id"]})

        failed_page = self.client.get(
            "/tasks?page=1&page_size=20&status=failed",
            headers={"X-API-Token": self.admin_token},
        ).json()
        self.assertEqual([item["id"] for item in failed_page["tasks"]], [failed["id"]])

    def test_admin_bulk_retry_selected_failures_isolated_per_task(self) -> None:
        registered = self.register("bulk_retry_owner")
        owner_hash = temp_access.hash_token(registered["token"])
        temp_access.add_temp_credit_units(owner_hash, 50)
        temp_access.set_temp_billing_priority(owner_hash, "points_first")
        failed_tasks = []
        for index in range(2):
            source = store.create_task(f"批量重试失败任务 {index}", "16:9", owner_token_hash=owner_hash, model="Seedance 2.0", duration=10)
            image = store.images_dir(source["id"]) / "01.png"
            image.write_bytes(f"reference-{index}".encode())
            store.set_task_images(source["id"], [image], [f"reference-{index}.png"])
            store.update_meta(source["id"], reference_is_real_person=True)
            store.mark_failed(source["id"], "测试失败")
            failed_tasks.append(source)
        success = store.create_task("不可批量重试的成功任务", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        store.update_meta(success["id"], status=store.STATUS_SUCCESS, finished_at=store.utc_now())
        invalid_owner = store.create_task("所属用户已失效", "9:16", owner_token_hash="f" * 64, model="Seedance 2.0")
        store.mark_failed(invalid_owner["id"], "测试失败")

        client_forbidden = self.client.post(
            "/tasks/bulk-retry",
            headers={"X-API-Token": registered["token"]},
            json={"task_ids": [failed_tasks[0]["id"]]},
        )
        self.assertEqual(client_forbidden.status_code, 403)

        response = self.client.post(
            "/tasks/bulk-retry",
            headers={"X-API-Token": self.admin_token},
            json={"task_ids": [failed_tasks[0]["id"], invalid_owner["id"], success["id"], failed_tasks[1]["id"]]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual((payload["created"], payload["skipped"], payload["failed"]), (2, 1, 1))
        self.assertEqual(payload["results"]["failed"][0]["reason"], "任务所属用户已失效")
        retry_ids = [item["retry_id"] for item in payload["results"]["created"]]
        for retry_id, index in zip(retry_ids, range(2), strict=True):
            retry_meta = store.get_meta(retry_id)
            self.assertEqual(retry_meta["status"], store.STATUS_PENDING)
            self.assertTrue(retry_meta["reference_is_real_person"])
            self.assertEqual(retry_meta["reference_image_names"], [f"reference-{index}.png"])
            self.assertEqual(store.task_image_paths(retry_id)[0].read_bytes(), f"reference-{index}".encode())

    def test_admin_bulk_retry_all_uses_all_matching_failures_not_current_page(self) -> None:
        config.update_config({"dola_submit_interval_seconds": 1})
        registered = self.register("bulk_retry_all_owner")
        owner_hash = temp_access.hash_token(registered["token"])
        temp_access.add_temp_credit_units(owner_hash, 100)
        temp_access.set_temp_billing_priority(owner_hash, "points_first")
        matched_ids = []
        for index in range(3):
            source = store.create_task(f"跨页目标 {index}", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
            store.mark_failed(source["id"], "测试失败")
            matched_ids.append(source["id"])
        unrelated = store.create_task("其他失败任务", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        store.mark_failed(unrelated["id"], "测试失败")

        response = self.client.post(
            "/tasks/bulk-retry",
            headers={"X-API-Token": self.admin_token},
            json={"retry_all": True, "q": "跨页目标"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual((payload["matched_total"], payload["requested"], payload["created"]), (3, 3, 3))
        self.assertEqual([item["id"] for item in payload["results"]["created"]], matched_ids)
        self.assertEqual(payload["release_interval_seconds"], 1)
        available = [datetime.fromisoformat(item["available_at"]) for item in payload["results"]["created"]]
        self.assertEqual([(available[index] - available[index - 1]).total_seconds() for index in range(1, len(available))], [1, 1])
        for source_id, item in zip(matched_ids, payload["results"]["created"], strict=True):
            source_meta = store.get_meta(source_id)
            retry_meta = store.get_meta(item["retry_id"])
            self.assertEqual(retry_meta["queue_priority_at"], source_meta["created_at"])
            self.assertEqual(retry_meta["next_attempt_at"], item["available_at"])
        self.assertFalse(payload["truncated"])

    def test_concurrency_overflow_is_precharged_and_queued_until_capacity_is_free(self) -> None:
        registered = self.register("limited_client")
        owner_hash = temp_access.hash_token(registered["token"])
        temp_access.add_temp_credit_units(owner_hash, 10)
        temp_access.set_temp_billing_priority(owner_hash, "points_first")
        existing = store.create_task("正在生成的任务", "9:16", owner_token_hash=owner_hash)
        self.assertTrue(store.mark_running(existing["id"], "worker-existing"))
        store.mark_submitted(existing["id"])
        response = self.client.post(
            "/tasks",
            headers={"X-API-Token": registered["token"]},
            data={"prompt": "等待空闲并发", "ratio": "9:16", "platform": "dola", "model": "Seedance 2.0", "task_type": "video"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["queued_for_concurrency"])
        self.assertFalse(response.json()["billing"]["free_used"])
        self.assertEqual(response.json()["billing"]["points_used"], 1)
        queued_id = response.json()["id"]
        tasks = store.list_tasks(owner_token_hash=owner_hash)
        self.assertEqual({item["id"] for item in tasks}, {existing["id"], queued_id})
        self.assertEqual(store.get_meta(queued_id)["status"], store.STATUS_PENDING)
        self.assertNotIn("initializing", {item["status"] for item in tasks})
        access_state = self.client.get("/auth/access-state", headers={"X-API-Token": registered["token"]}).json()
        self.assertEqual(access_state["quota"]["free_remaining"], 1)
        self.assertEqual(access_state["quota"]["points"], 0)
        self.assertEqual(access_state["token_concurrency"], 1)
        canceled = self.client.delete(f"/tasks/{queued_id}", headers={"X-API-Token": registered["token"]})
        self.assertEqual(canceled.status_code, 200)
        self.assertTrue(canceled.json()["canceled"])
        refunded = self.client.get("/auth/access-state", headers={"X-API-Token": registered["token"]}).json()
        self.assertEqual(refunded["quota"]["free_remaining"], 1)
        self.assertEqual(refunded["quota"]["points"], 1)

    def test_transient_task_creation_failure_returns_structured_retryable_error(self) -> None:
        registered = self.register("batch_fail_client")
        owner_hash = temp_access.hash_token(registered["token"])
        temp_access.add_temp_credit_units(owner_hash, 10)
        with self.assertLogs("app.main", level="WARNING"), patch.object(
            main, "reserve_temp_quota", side_effect=RuntimeError("database pool exhausted")
        ) as reserve:
            response = self.client.post(
                "/tasks",
                headers={"X-API-Token": registered["token"], "Idempotency-Key": "transient-batch-failure"},
                data={
                    "prompt": "测试临时创建失败",
                    "ratio": "9:16",
                    "duration": "15",
                    "batch": "true",
                    "batch_id": "batch-transient-failure",
                    "batch_index": "1",
                    "batch_row": "2",
                    "platform": "dola",
                    "model": "Seedance 2.0",
                },
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "任务创建暂时繁忙，请稍后重试")
        self.assertEqual(response.headers["retry-after"], "2")
        self.assertEqual(reserve.call_count, 3)
        self.assertEqual(store.list_tasks(owner_token_hash=owner_hash), [])

    def test_transient_database_failure_is_retried_without_losing_batch_task(self) -> None:
        registered = self.register("batch_retry_client")
        owner_hash = temp_access.hash_token(registered["token"])
        temp_access.add_temp_credit_units(owner_hash, 10)
        original_reserve = main.reserve_temp_quota
        calls = 0

        def flaky_reserve(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("couldn't get a connection after 8.00 sec")
            return original_reserve(*args, **kwargs)

        with self.assertLogs("app.main", level="WARNING"), patch.object(main, "reserve_temp_quota", side_effect=flaky_reserve):
            response = self.client.post(
                "/tasks",
                headers={"X-API-Token": registered["token"], "Idempotency-Key": "transient-batch-recovery"},
                data={
                    "prompt": "数据库恢复后继续创建",
                    "ratio": "9:16",
                    "duration": "15",
                    "batch": "true",
                    "batch_id": "batch-transient-recovery",
                    "batch_index": "1",
                    "batch_row": "2",
                    "platform": "dola",
                    "model": "Seedance 2.0",
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(calls, 2)
        self.assertEqual(store.get_meta(response.json()["id"])["status"], store.STATUS_PENDING)

    def test_openai_concurrency_overflow_returns_a_pending_queued_task(self) -> None:
        registered = self.register("limited_openai_client")
        owner_hash = temp_access.hash_token(registered["token"])
        existing = store.create_task("正在生成的任务", "9:16", owner_token_hash=owner_hash)
        self.assertTrue(store.mark_running(existing["id"], "worker-existing"))
        store.mark_submitted(existing["id"])
        response = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {registered['token']}"},
            json={"model": "dola:Seedance 2.0", "messages": [{"role": "user", "content": "等待空闲并发"}]},
        )
        self.assertEqual(response.status_code, 200)
        content = json.loads(response.json()["choices"][0]["message"]["content"])
        self.assertEqual(content["status"], "pending")
        self.assertTrue(content["queued_for_concurrency"])
        self.assertEqual({item["id"] for item in store.list_tasks(owner_token_hash=owner_hash)}, {existing["id"], content["task_id"]})

    def test_query_parameter_token_is_not_accepted(self) -> None:
        registered = self.register("query_token_client")
        self.client.cookies.clear()
        response = self.client.get(f"/tasks?token={registered['token']}")
        self.assertEqual(response.status_code, 403)

    def test_liveness_probe_requires_no_credentials(self) -> None:
        response = self.client.get("/health/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "version": __version__})

    def test_openapi_metadata_uses_release_version(self) -> None:
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["info"]["version"], __version__)

    def test_task_contract_preserves_fields_and_hides_client_internals(self) -> None:
        registered = self.register()
        token = registered["token"]
        owner_hash = temp_access.hash_token(token)
        owned = store.create_task("Dola 账号生成测试", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        other = store.create_task("管理员任务", "16:9", model="Seedance 2.0")
        store.update_meta(
            owned["id"],
            worker_id="worker-secret",
            failed_account_ids=["account-secret"],
            attempt_history=[{"at": "2026-07-27T00:00:00+00:00", "kind": "execution_retry", "reason": "ApplyImageUpload HTTP 429"}],
            last_attempt_error="ApplyImageUpload HTTP 429",
            last_attempt_kind="execution_retry",
            last_attempt_at="2026-07-27T00:00:00+00:00",
        )
        client_list = self.client.get("/tasks", headers={"X-API-Token": token}).json()["tasks"]
        self.assertEqual([item["id"] for item in client_list], [owned["id"]])
        client_task = client_list[0]
        self.assertTrue({"id", "prompt", "prompt_preview", "model", "status", "image_count", "error", "owner_name", "video_hidden_for_client"} <= set(client_task))
        for key in ("owner_token_hash", "worker_id", "failed_account_ids", "account_id", "platform", "video_hidden_for_admin", "attempt_history", "last_attempt_error", "last_attempt_kind", "last_attempt_at"):
            self.assertNotIn(key, client_task)
        admin_tasks = self.client.get("/tasks", headers={"X-API-Token": self.admin_token}).json()["tasks"]
        self.assertEqual({item["id"] for item in admin_tasks}, {owned["id"], other["id"]})
        admin_owned = next(item for item in admin_tasks if item["id"] == owned["id"])
        self.assertEqual(admin_owned["owner_token_hash"], owner_hash)
        self.assertEqual(admin_owned["attempt_history"][0]["reason"], "ApplyImageUpload HTTP 429")
        with patch("app.main.query_task", new=AsyncMock(return_value={"code": "0", "text": "Dola 账号等待中", "url": ""})):
            detail = self.client.get(f"/tasks/{owned['id']}", headers={"X-API-Token": token})
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(set(detail.json()), {"code", "text", "url"})
        self.assertNotIn("Dola", detail.json()["text"])
        self.assertNotIn("账号", detail.json()["text"])
        self.assertEqual(self.client.get(f"/tasks/{other['id']}", headers={"X-API-Token": token}).status_code, 404)

    def test_task_reference_endpoint_returns_only_the_owned_original_image(self) -> None:
        owner = self.register("reference_owner")
        other = self.register("reference_other")
        owner_hash = temp_access.hash_token(owner["token"])
        task = store.create_task("带参考图任务", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        original = b"\x89PNG\r\n\x1a\noriginal-user-reference"
        reference = store.images_dir(task["id"]) / "01.png"
        reference.write_bytes(original)
        store.set_task_images(task["id"], [reference], ["source-photo.png"])

        response = self.client.get(
            f"/tasks/{task['id']}/references/1",
            headers={"X-API-Token": owner["token"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, original)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertIn("inline", response.headers["content-disposition"])
        self.assertIn("source-photo.png", response.headers["content-disposition"])
        self.assertEqual(
            self.client.get(f"/tasks/{task['id']}/references/1", headers={"X-API-Token": other["token"]}).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/tasks/{task['id']}/references/2", headers={"X-API-Token": owner["token"]}).status_code,
            404,
        )

    def test_admin_can_download_and_restore_user_account_backup(self) -> None:
        self.register("backup_client")
        self.login_admin()
        downloaded = self.client.get("/admin/data-backup")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.headers["content-type"], "application/zip")
        self.assertIn("dola-user-account-backup-", downloaded.headers["content-disposition"])

        active = store.create_task("backup guard", "9:16", model="Seedance 2.0")
        blocked = self.client.post(
            "/admin/data-restore",
            data={"confirm": "true"},
            files={"upload": ("backup.zip", downloaded.content, "application/zip")},
        )
        self.assertEqual(blocked.status_code, 409)
        store.mark_failed(active["id"], "test cleanup")

        restored = self.client.post(
            "/admin/data-restore",
            data={"confirm": "true"},
            files={"upload": ("backup.zip", downloaded.content, "application/zip")},
        )
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["restored"]["users"], 1)
        self.assertTrue((self.root / "backups" / restored.json()["restored"]["pre_restore_snapshot"]).is_file())

    def test_restricted_account_access_key_lifecycle_and_scope(self) -> None:
        admin_headers = {"X-API-Token": self.admin_token}
        self.assertEqual(self.client.get("/account-access/groups").status_code, 403)

        initial = self.client.get("/config/account-access", headers=admin_headers)
        self.assertEqual(initial.status_code, 200)
        self.assertFalse(initial.json()["configured"])
        self.assertNotIn("account_access_key_hash", initial.json())

        rotated = self.client.post("/config/account-access/rotate", headers=admin_headers)
        self.assertEqual(rotated.status_code, 200)
        first_key = rotated.json()["key"]
        self.assertTrue(first_key.startswith("acct_"))
        key_headers = {"Authorization": f"Bearer {first_key}"}

        groups = self.client.get("/account-access/groups", headers=key_headers)
        self.assertEqual(groups.status_code, 200)
        self.assertEqual({item["id"] for item in groups.json()["groups"]}, {"dola", "doubao", "qianwen"})

        created = self.client.post(
            "/account-access/accounts",
            headers=key_headers,
            json={"group": "dola", "name": "外部导入账号", "cookie_data": "sessionid=restricted-key-test", "quota_limit": 12},
        )
        self.assertEqual(created.status_code, 201, created.text)
        account = created.json()["account"]
        self.assertEqual(account["name"], "外部导入账号")
        self.assertEqual(account["quota_limit"], 12)
        for secret_field in ("cookies", "cookie_header", "cookie_names", "cookie_count", "current_task_id", "current_worker_id"):
            self.assertNotIn(secret_field, account)

        listed = self.client.get("/account-access/accounts?group=dola", headers={"X-Account-Access-Key": first_key})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["accounts"][0]["id"], account["id"])
        self.assertNotIn("cookies", listed.text)

        updated = self.client.patch(
            f"/account-access/accounts/{account['id']}",
            headers=key_headers,
            json={"name": "重命名账号", "quota_limit": 25},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["account"]["name"], "重命名账号")
        self.assertEqual(updated.json()["account"]["quota_limit"], 25)
        forbidden_update = self.client.patch(
            f"/account-access/accounts/{account['id']}",
            headers=key_headers,
            json={"enabled": False},
        )
        self.assertEqual(forbidden_update.status_code, 400)

        second_rotation = self.client.post("/config/account-access/rotate", headers=admin_headers)
        self.assertEqual(second_rotation.status_code, 200)
        second_key = second_rotation.json()["key"]
        self.assertEqual(self.client.get("/account-access/groups", headers=key_headers).status_code, 403)
        second_headers = {"Authorization": f"Bearer {second_key}"}
        self.assertEqual(self.client.get("/account-access/groups", headers=second_headers).status_code, 200)

        disabled = self.client.patch("/config/account-access", headers=admin_headers, json={"enabled": False})
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()["enabled"])
        self.assertEqual(self.client.get("/account-access/groups", headers=second_headers).status_code, 403)

        revoked = self.client.delete("/config/account-access", headers=admin_headers)
        self.assertEqual(revoked.status_code, 200)
        self.assertFalse(revoked.json()["configured"])
        saved_config = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved_config["account_access_key_hash"], "")
        self.assertNotIn(first_key, config.CONFIG_PATH.read_text(encoding="utf-8"))

    def test_direct_reference_upload_persists_original_filename_in_task_list(self) -> None:
        owner = self.register("reference_filename_owner")
        headers = {"X-API-Token": owner["token"]}
        response = self.client.post(
            "/tasks",
            headers=headers,
            data={"prompt": "保留参考图文件名", "ratio": "9:16", "platform": "dola", "model": "Seedance 2.0"},
            files=[("images", ("scene-reference.png", b"\x89PNG\r\n\x1a\nreference-name", "image/png"))],
        )
        self.assertEqual(response.status_code, 200, response.text)
        task_id = response.json()["id"]
        self.assertEqual(store.get_meta(task_id)["reference_image_names"], ["scene-reference.png"])
        tasks = self.client.get("/tasks", headers=headers).json()["tasks"]
        listed = next(item for item in tasks if item["id"] == task_id)
        self.assertEqual(listed["reference_image_names"], ["scene-reference.png"])

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

    def test_client_hides_technical_task_errors_but_admin_keeps_diagnostics(self) -> None:
        registered = self.register("safe_error_client")
        token = registered["token"]
        owner_hash = temp_access.hash_token(token)
        task = store.create_task("测试技术错误隔离", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        raw_error = "Page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://example.invalid"
        store.mark_failed(task["id"], raw_error)

        client_task = self.client.get("/tasks?page=1&page_size=20", headers={"X-API-Token": token}).json()["tasks"][0]
        self.assertEqual(client_task["error"], "服务暂时异常，请重试！")
        client_result = self.client.get(f"/tasks/{task['id']}", headers={"X-API-Token": token}).json()
        self.assertEqual(client_result["text"], "服务暂时异常，请重试！")

        admin_task = self.client.get("/tasks?page=1&page_size=20", headers={"X-API-Token": self.admin_token}).json()["tasks"][0]
        self.assertEqual(admin_task["error"], raw_error)

    def test_client_hides_ten_second_limit_but_admin_keeps_reason(self) -> None:
        registered = self.register("ten_second_reason_client")
        token = registered["token"]
        owner_hash = temp_access.hash_token(token)
        task = store.create_task("测试十秒限制文案", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        raw_error = "Currently generating videos longer than 10 seconds is not supported, do you want to continue generating for you?"
        store.mark_failed(task["id"], raw_error)

        client_task = self.client.get("/tasks?page=1&page_size=20", headers={"X-API-Token": token}).json()["tasks"][0]
        self.assertEqual(client_task["error"], "生成接口繁忙请稍后重试！")
        client_result = self.client.get(f"/tasks/{task['id']}", headers={"X-API-Token": token}).json()
        self.assertEqual(client_result["text"], "生成接口繁忙请稍后重试！")

        admin_task = self.client.get("/tasks?page=1&page_size=20", headers={"X-API-Token": self.admin_token}).json()["tasks"][0]
        self.assertEqual(admin_task["error"], raw_error)

    def test_running_task_progress_is_visible_without_client_diagnostics(self) -> None:
        registered = self.register("progress_client")
        token = registered["token"]
        owner_hash = temp_access.hash_token(token)
        task = store.create_task("测试运行阶段", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        self.assertTrue(store.mark_running(task["id"], "worker-progress"))
        store.set_execution_phase(task["id"], "opening_generation_page", "正在打开生成页面")

        client_task = self.client.get("/tasks?page=1&page_size=20", headers={"X-API-Token": token}).json()["tasks"][0]
        self.assertEqual(client_task["status_reason"], "正在启动服务")
        self.assertNotIn("execution_phase", client_task)
        self.assertNotIn("phase_updated_at", client_task)

        admin_task = self.client.get("/tasks?page=1&page_size=20", headers={"X-API-Token": self.admin_token}).json()["tasks"][0]
        self.assertEqual(admin_task["execution_phase"], "opening_generation_page")
        self.assertTrue(admin_task["phase_updated_at"])

    def test_retrying_task_hides_previous_failure_from_client(self) -> None:
        registered = self.register("retry_text_client")
        token = registered["token"]
        owner_hash = temp_access.hash_token(token)
        task = store.create_task("测试重试文案", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        self.assertTrue(store.mark_running(task["id"], "worker-retry-text"))
        store.mark_submitted(task["id"])
        previous_failure = "视频生成失败，生成额度未扣除。"
        self.assertEqual(store.retry_submitted_task(task["id"], previous_failure, delay_seconds=10), 1)

        client_task = self.client.get("/tasks?page=1&page_size=20", headers={"X-API-Token": token}).json()["tasks"][0]
        self.assertEqual(client_task["status"], "pending")
        self.assertEqual(client_task["error"], "正在重试中，请稍等！")
        self.assertEqual(client_task["status_reason"], "重试已入队，等待执行")

        admin_task = self.client.get("/tasks?page=1&page_size=20", headers={"X-API-Token": self.admin_token}).json()["tasks"][0]
        self.assertEqual(admin_task["error"], previous_failure)

    def test_task_create_moves_blocking_storage_off_event_loop(self) -> None:
        source = inspect.getsource(main.submit_task)
        self.assertIn("await asyncio.to_thread", source)
        self.assertIn("find_or_create_task", source)
        self.assertIn("reserve_temp_quota", source)
        self.assertIn("record_transaction", source)

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

    def test_submitted_task_rejects_cancel_without_setting_cancel_requested(self) -> None:
        registered = self.register("submitted_owner")
        token = registered["token"]
        owner_hash = temp_access.hash_token(token)
        task = store.create_task("已提交任务", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        self.assertTrue(store.mark_running(task["id"], "worker-1"))
        store.mark_submitted(task["id"])
        response = self.client.delete(f"/tasks/{task['id']}", headers={"X-API-Token": token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["detail"], "已提交生成，无法取消")
        self.assertFalse(response.json()["ok"])
        meta = store.get_meta(task["id"])
        self.assertEqual(meta["status"], store.STATUS_SUBMITTED)
        self.assertFalse(bool(meta.get("cancel_requested")))

    def test_admin_can_cancel_submitted_task(self) -> None:
        task = store.create_task("管理员取消已提交任务", "9:16", model="Seedance 2.0")
        self.assertTrue(store.mark_running(task["id"], "worker-admin-cancel"))
        store.mark_submitted(task["id"])

        response = self.client.delete(f"/tasks/{task['id']}", headers={"X-API-Token": self.admin_token})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["canceled"])
        meta = store.get_meta(task["id"])
        self.assertEqual(meta["status"], store.STATUS_CANCELED)
        self.assertTrue(meta["cancel_requested"])

    def test_admin_pause_cancels_pending_and_blocks_new_tasks(self) -> None:
        pending = store.create_task("等待暂停取消", "9:16", model="Seedance 2.0")
        running = store.create_task("继续生成", "9:16", model="Seedance 2.0")
        self.assertTrue(store.mark_running(running["id"], "worker-keep-running"))
        batch_job = batch_jobs.create_job("batch-owner", [{"prompt": "批量排队任务"}], ratio="9:16", concurrency=1)
        headers = {"X-API-Token": self.admin_token}

        paused = self.client.post("/admin/task-pause", headers=headers, json={"paused": True})

        self.assertEqual(paused.status_code, 200)
        self.assertTrue(paused.json()["paused"])
        self.assertEqual(paused.json()["canceled_pending"], 1)
        self.assertEqual(paused.json()["canceled_batch_rows"], 1)
        self.assertEqual(store.get_meta(pending["id"])["status"], store.STATUS_CANCELED)
        self.assertEqual(store.get_meta(running["id"])["status"], store.STATUS_RUNNING)
        self.assertEqual(batch_jobs.get_job(batch_job["id"])["rows"][0]["status"], "canceled")
        self.assertTrue(self.client.get("/admin/task-pause", headers=headers).json()["paused"])
        blocked = self.client.post("/tasks", headers=headers, data={"prompt": "暂停期间提交"})
        self.assertEqual(blocked.status_code, 503)
        self.assertEqual(blocked.json()["detail"], "任务发布已暂停")

        resumed = self.client.post("/admin/task-pause", headers=headers, json={"paused": False})
        self.assertEqual(resumed.status_code, 200)
        self.assertFalse(resumed.json()["paused"])

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

    def test_admin_disk_cleanup_removes_only_old_terminal_tasks(self) -> None:
        old_success = store.create_task("old completed task", "9:16", model="Seedance 2.0")
        self.assertTrue(store.mark_running(old_success["id"], "worker-old-success"))
        store.save_result(old_success["id"], extra={"decoded_main_url": "https://example.com/old.mp4"})
        store.mark_success(old_success["id"])
        old_meta = store.get_meta(old_success["id"])
        old_meta["created_at"] = (datetime.now(store.LOCAL_TZ) - timedelta(days=2)).isoformat()
        store.meta_path(old_success["id"]).write_text(json.dumps(old_meta), encoding="utf-8")

        recent_success = store.create_task("recent completed task", "9:16", model="Seedance 2.0")
        self.assertTrue(store.mark_running(recent_success["id"], "worker-recent-success"))
        store.save_result(recent_success["id"], extra={"decoded_main_url": "https://example.com/recent.mp4"})
        store.mark_success(recent_success["id"])

        old_running = store.create_task("old running task", "9:16", model="Seedance 2.0")
        self.assertTrue(store.mark_running(old_running["id"], "worker-old-running"))
        running_meta = store.get_meta(old_running["id"])
        running_meta["created_at"] = (datetime.now(store.LOCAL_TZ) - timedelta(days=2)).isoformat()
        store.meta_path(old_running["id"]).write_text(json.dumps(running_meta), encoding="utf-8")

        with patch.object(main, "DATA_DIR", self.root):
            response = self.client.post("/admin/disk-cleanup", headers={"X-API-Token": self.admin_token})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["deleted"], 1)
        self.assertFalse(store.task_exists(old_success["id"]))
        self.assertTrue(store.task_exists(recent_success["id"]))
        self.assertTrue(store.task_exists(old_running["id"]))

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
        self.assertEqual(payload["stats"]["ten_second"], 0)
        self.assertEqual(payload["stats"]["by_platform"], {"dola": 1, "doubao": 1, "qianwen": 0})
        accounts.disable_account_for_login(dola["id"], "登录失效")
        abnormal = self.client.get("/accounts?page=1&page_size=20&status=abnormal").json()
        self.assertEqual([item["id"] for item in abnormal["accounts"]], [dola["id"]])
        slider_account = self.client.post("/accounts", json={"name": "跳验证账号", "cookie_data": "session=slider", "platform": "dola"}).json()["account"]
        accounts.mark_account_slider_verification(slider_account["id"])
        slider = self.client.get("/accounts?page=1&page_size=20&status=slider_verification").json()
        self.assertEqual([item["id"] for item in slider["accounts"]], [slider_account["id"]])
        self.assertEqual(slider["stats"]["slider_verification"], 1)
        ten_second_account = self.client.post("/accounts", json={"name": "10秒账号", "cookie_data": "session=ten-second", "platform": "dola"}).json()["account"]
        accounts.mark_account_ten_second_limit(ten_second_account["id"])
        ten_second = self.client.get("/accounts?page=1&page_size=20&status=ten_second").json()
        self.assertEqual([item["id"] for item in ten_second["accounts"]], [ten_second_account["id"]])
        self.assertTrue(ten_second["accounts"][0]["ten_second_only"])
        normal = self.client.get("/accounts?page=1&page_size=20&status=normal").json()
        self.assertIn(ten_second_account["id"], [item["id"] for item in normal["accounts"]])
        disabled_account = self.client.post("/accounts", json={"name": "停用账号", "cookie_data": "session=disabled", "platform": "dola", "enabled": False}).json()["account"]
        self.assertFalse(disabled_account["enabled"])
        disabled = self.client.get("/accounts?page=1&page_size=20&status=disabled").json()
        self.assertEqual([item["id"] for item in disabled["accounts"]], [disabled_account["id"]])
        self.assertEqual(self.client.get("/accounts?page=1&status=unknown").status_code, 422)
        self.assertEqual(self.client.get("/accounts?page=1&platform=unknown").status_code, 422)

    def test_account_deletion_history_endpoint_reports_daily_status_counts(self) -> None:
        self.login_admin()
        slider = self.client.post(
            "/accounts",
            json={"name": "待删除跳验证", "cookie_data": "session=slider-delete", "platform": "dola"},
        ).json()["account"]
        abnormal = self.client.post(
            "/accounts",
            json={"name": "待删除异常", "cookie_data": "session=abnormal-delete", "platform": "dola"},
        ).json()["account"]
        accounts.mark_account_slider_verification(slider["id"])
        accounts.disable_account_for_login(abnormal["id"], "登录失效")
        now = datetime.now(accounts.LOCAL_TZ).replace(hour=23, minute=0, second=0, microsecond=0)
        result = accounts.cleanup_flagged_accounts(now)
        self.assertEqual(result["removed"], 2)

        response = self.client.get("/accounts/deletion-history?limit=30")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["cleanup_time"], "23:00")
        self.assertEqual(payload["timezone"], "Asia/Shanghai")
        self.assertEqual(payload["days"][0]["total"], 2)
        self.assertEqual(payload["days"][0]["by_status"], {"abnormal": 1, "slider_verification": 1})

    def test_account_list_snapshot_coalesces_repeated_expensive_builds(self) -> None:
        snapshot = {
            "accounts": [],
            "quota_summary": {"total_limit": 0, "total_used": 0, "total_remaining": 0, "unlimited_count": 0},
            "next_quota_reset_at": "2026-07-30T00:00:00+08:00",
        }
        main._clear_account_list_cache()
        started = threading.Event()
        release = threading.Event()

        def build_snapshot():
            started.set()
            release.wait(5)
            return snapshot

        try:
            with patch.object(main, "_build_account_list_snapshot", side_effect=build_snapshot) as build, ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(main._account_list_snapshot)
                self.assertTrue(started.wait(2))
                second = executor.submit(main._account_list_snapshot)
                release.set()
                self.assertEqual(first.result(5), snapshot)
                self.assertEqual(second.result(5), snapshot)
            build.assert_called_once_with()
        finally:
            main._clear_account_list_cache()

    def test_account_list_maintenance_is_rate_limited(self) -> None:
        previous_due = main._ACCOUNT_LIST_MAINTENANCE_AT
        main._ACCOUNT_LIST_MAINTENANCE_AT = 0
        try:
            with patch.object(main, "reset_daily_account_quotas_if_needed") as reset, patch.object(
                main, "reconcile_account_quotas"
            ) as reconcile:
                main._run_account_list_maintenance()
                main._run_account_list_maintenance()
            reset.assert_called_once_with()
            reconcile.assert_called_once_with()
        finally:
            main._ACCOUNT_LIST_MAINTENANCE_AT = previous_due

    def test_account_list_work_is_offloaded_from_the_api_event_loop(self) -> None:
        source = inspect.getsource(main.accounts_list)
        self.assertIn("await asyncio.to_thread(_accounts_list_payload", source)
        self.assertNotIn("list_tasks()", inspect.getsource(main._build_account_list_snapshot))
        self.assertNotIn("list_tasks()", inspect.getsource(store.account_active_tasks))

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
        self.assertEqual(set(workers), {"browser_workers", "max_effective_workers", "effective_browser_workers", "capacity_limit", "browser_pool_processes", "browser_contexts_per_process", "submission_concurrency", "remote_generation_limit"})
        self.assertEqual((workers["browser_pool_processes"], workers["browser_contexts_per_process"], workers["submission_concurrency"]), (12, 3, 36))
        self.assertEqual(set(proxy), {"proxy_api_url", "proxy_api_scheme", "proxy_api_timeout_seconds", "proxy_source", "platform_proxy_sources", "platform_proxy_random", "proxy_subscription_configured", "proxy_subscription_scheme", "proxy_subscription_refresh_seconds", "proxy_account_configured", "proxy_account_count", "proxy_account_scheme", "proxy_account_host", "proxy_account_port", "proxy_account_username_masked", "proxy_enabled", "proxy_auto_select", "proxy_selected_node", "proxy_auto_countries", "proxy_latency_threshold_ms", "proxy_health_refresh_seconds"})
        self.assertNotIn("proxy_subscription_url", proxy)
        self.assertNotIn("proxy_account_password", proxy)
        self.assertEqual(set(platforms), {"default_platform", "platforms"})
        self.assertEqual({item["id"] for item in platforms["platforms"]}, {"dola", "doubao", "qianwen"})
        for platform in platforms["platforms"]:
            self.assertEqual(set(platform), {"id", "label", "models", "model_costs", "model_durations", "model_duration_costs", "supported_durations", "all_models", "enabled"})
            for model in platform["all_models"]:
                self.assertEqual(set(model), {"name", "enabled", "cost", "durations", "duration_costs"})

    def test_proxy_health_refresh_switches_within_checked_countries(self) -> None:
        nodes = proxy_manager.subscription_node_list("http://us.example.com:8080#US\nhttp://jp.example.com:8080#Japan")
        config.update_config({
            "proxy_source": "subscription",
            "proxy_subscription_url": "https://subscription.example/token",
            "proxy_auto_select": True,
            "proxy_auto_countries": ["日本"],
            "proxy_selected_node": nodes[0].id,
        })

        async def exercise() -> dict:
            with patch("app.main.fetch_subscription_node_list", new=AsyncMock(return_value=nodes)), patch(
                "app.main.measure_node_delays", new=AsyncMock(return_value={nodes[1].id: 35})
            ), patch("app.main.node_payload", side_effect=lambda node: {"latency_ms": 35}), patch(
                "app.main.activate_mihomo_node", new=AsyncMock()
            ) as activate:
                result = await main.refresh_proxy_health_once()
                activate.assert_awaited_once()
                return result

        result = asyncio.run(exercise())
        self.assertTrue(result["switched"])
        self.assertEqual(result["selected_node"], nodes[1].id)
        self.assertEqual(config.load_settings().proxy_selected_node, nodes[1].id)

    def test_api_proxy_nodes_expose_worker_pool_usage(self) -> None:
        config.update_config({"proxy_source": "api", "proxy_api_url": "https://proxy.example/api", "proxy_api_scheme": "socks5"})
        (self.root / ".worker-health.json").write_text(
            json.dumps(
                {
                    "api_proxy_pool": {
                        "endpoint_limit": 12,
                        "contexts_per_endpoint": 1,
                        "capacity": 12,
                        "endpoints": 2,
                        "active": 1,
                        "last_error": "",
                        "slots": [
                            {"id": "api:198.51.100.10:10001", "host_port": "198.51.100.10:10001", "active": 1, "total_leases": 7, "last_leased_at": 123.0, "state": "active"},
                            {"id": "api:198.51.100.11:10002", "host_port": "198.51.100.11:10002", "active": 0, "total_leases": 3, "last_leased_at": 120.0, "state": "idle"},
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )

        response = self.client.get("/config/proxy-nodes", headers={"X-API-Token": self.admin_token})

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["source"], "api")
        self.assertEqual(payload["pool"]["contexts_per_endpoint"], 1)
        self.assertEqual(payload["pool"]["active"], 1)
        self.assertEqual(payload["nodes"][0]["host_port"], "198.51.100.10:10001")
        self.assertEqual(payload["nodes"][0]["total_leases"], 7)

    def test_proxy_nodes_follow_the_requested_platform_source(self) -> None:
        config.update_config({
            "proxy_source": "direct",
            "platform_proxy_sources": {"dola": "api", "doubao": "subscription", "qianwen": "direct"},
            "proxy_subscription_url": "https://subscription.example/token",
        })
        (self.root / ".worker-health.json").write_text(
            json.dumps({
                "api_proxy_pool": {
                    "capacity": 1,
                    "endpoints": 1,
                    "active": 1,
                    "slots": [{"id": "api:198.51.100.30:10003", "host_port": "198.51.100.30:10003", "active": 1, "total_leases": 2, "state": "active"}],
                }
            }),
            encoding="utf-8",
        )
        nodes = proxy_manager.subscription_node_list(
            "vless://user@hk.example.com:443#Hong%20Kong\ntrojan://secret@jp.example.com:443#Japan"
        )
        headers = {"X-API-Token": self.admin_token}
        with patch("app.main.fetch_subscription_node_list", new=AsyncMock(return_value=nodes)), patch(
            "app.main.measure_node_delays", new=AsyncMock(return_value={node.id: 35 + index for index, node in enumerate(nodes)})
        ):
            dola = self.client.get("/config/proxy-nodes?platform=dola", headers=headers)
            doubao = self.client.get("/config/proxy-nodes?platform=doubao", headers=headers)
            measured = self.client.post("/config/proxy-nodes/latency", headers=headers, json={"platform": "doubao"})
            qianwen = self.client.get("/config/proxy-nodes?platform=qianwen", headers=headers)

        self.assertEqual(dola.status_code, 200, dola.text)
        self.assertEqual((dola.json()["platform"], dola.json()["source"]), ("dola", "api"))
        self.assertEqual(dola.json()["nodes"][0]["host_port"], "198.51.100.30:10003")
        self.assertEqual(doubao.status_code, 200, doubao.text)
        self.assertEqual((doubao.json()["platform"], doubao.json()["source"]), ("doubao", "subscription"))
        self.assertEqual([item["country"] for item in doubao.json()["nodes"]], ["香港", "日本"])
        self.assertEqual(measured.status_code, 200, measured.text)
        self.assertEqual([item["latency_ms"] for item in measured.json()["nodes"]], [35, 36])
        self.assertEqual(qianwen.status_code, 200, qianwen.text)
        self.assertEqual((qianwen.json()["platform"], qianwen.json()["source"], qianwen.json()["nodes"]), ("qianwen", "direct", []))
        invalid = self.client.get("/config/proxy-nodes?platform=unknown", headers=headers)
        self.assertEqual(invalid.status_code, 400)

    def test_api_proxy_draft_can_be_tested_without_saving(self) -> None:
        self.assertEqual(
            self.client.post(
                "/config/proxy-api/test",
                json={"proxy_api_url": "https://proxy.example/api", "proxy_api_scheme": "socks5h"},
            ).status_code,
            403,
        )
        self.login_admin()
        original = config.load_settings()
        extracted = {
            "server": "socks5h://198.51.100.20:14044",
            "host_port": "198.51.100.20:14044",
        }
        with patch("app.main.fetch_proxy_from_api", new=AsyncMock(return_value=extracted)) as fetch, patch(
            "app.main.probe_dola_proxy", new=AsyncMock(return_value=(True, 86))
        ) as probe:
            response = self.client.post(
                "/config/proxy-api/test",
                json={"proxy_api_url": "https://proxy.example/api", "proxy_api_scheme": "socks5h"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "ok": True,
            "proxy_host_port": "198.51.100.20:14044",
            "proxy_scheme": "socks5h",
            "latency_ms": 86,
        })
        fetch.assert_awaited_once_with(
            "https://proxy.example/api",
            timeout_seconds=original.proxy_api_timeout_seconds,
            scheme="socks5h",
        )
        probe.assert_awaited_once()
        refreshed = config.load_settings()
        self.assertEqual(refreshed.proxy_api_url, original.proxy_api_url)
        self.assertEqual(refreshed.proxy_api_scheme, original.proxy_api_scheme)

    def test_api_proxy_test_reports_unreachable_dola(self) -> None:
        self.login_admin()
        with patch(
            "app.main.fetch_proxy_from_api",
            new=AsyncMock(return_value={"server": "http://198.51.100.21:14045", "host_port": "198.51.100.21:14045"}),
        ), patch("app.main.probe_dola_proxy", new=AsyncMock(return_value=(False, None))):
            response = self.client.post(
                "/config/proxy-api/test",
                json={"proxy_api_url": "https://proxy.example/api", "proxy_api_scheme": "http"},
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn("cannot connect to Dola", response.text)

    def test_admin_can_submit_with_an_available_preferred_account(self) -> None:
        self.login_admin()
        selected = accounts.add_account("Selected Dola", "session=selected", quota_limit=3)
        selected_doubao = accounts.add_account("Selected Doubao", "session=selected-doubao", quota_limit=3, platform="doubao")
        selected_qianwen = accounts.add_account("Selected Qianwen", "session=selected-qianwen", quota_limit=5, platform="qianwen")
        exhausted = accounts.add_account("Exhausted Dola", "session=exhausted", quota_limit=1)
        disabled = accounts.add_account("Disabled Dola", "session=disabled", enabled=False, quota_limit=3)
        accounts.exhaust_account_quota(exhausted["id"])

        available = self.client.get("/accounts/available?platform=dola")

        self.assertEqual(available.status_code, 200, available.text)
        self.assertEqual([item["id"] for item in available.json()["accounts"]], [selected["id"]])
        self.assertNotIn(exhausted["id"], available.text)
        self.assertNotIn(disabled["id"], available.text)

        submitted = self.client.post(
            "/tasks",
            data={
                "prompt": "admin selected account task",
                "ratio": "9:16",
                "platform": "dola",
                "model": "Seedance 2.0",
                "preferred_account_id": selected["id"],
            },
        )

        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["preferred_account_id"], selected["id"])
        self.assertEqual(store.get_meta(submitted.json()["id"])["preferred_account_id"], selected["id"])

        for platform, model, account in (
            ("doubao", "Seedance 2.0 Mini", selected_doubao),
            ("qianwen", "万相 2.7", selected_qianwen),
        ):
            available_platform = self.client.get(f"/accounts/available?platform={platform}")
            self.assertEqual(available_platform.status_code, 200, available_platform.text)
            self.assertEqual([item["id"] for item in available_platform.json()["accounts"]], [account["id"]])
            submitted_platform = self.client.post(
                "/tasks",
                data={
                    "prompt": f"admin selected {platform} account task",
                    "ratio": "9:16",
                    "platform": platform,
                    "model": model,
                    "preferred_account_id": account["id"],
                },
            )
            self.assertEqual(submitted_platform.status_code, 200, submitted_platform.text)
            self.assertEqual(submitted_platform.json()["preferred_account_id"], account["id"])
            self.assertEqual(store.get_meta(submitted_platform.json()["id"])["preferred_account_id"], account["id"])

        cross_platform = self.client.post(
            "/tasks",
            data={
                "prompt": "cross platform selected account",
                "ratio": "9:16",
                "platform": "qianwen",
                "model": "万相 2.7",
                "preferred_account_id": selected["id"],
            },
        )
        self.assertEqual(cross_platform.status_code, 409)

        unavailable = self.client.post(
            "/tasks",
            data={
                "prompt": "unavailable account task",
                "ratio": "9:16",
                "platform": "dola",
                "model": "Seedance 2.0",
                "preferred_account_id": exhausted["id"],
            },
        )
        self.assertEqual(unavailable.status_code, 409)

    def test_client_cannot_select_a_generation_account(self) -> None:
        selected = accounts.add_account("Admin-only Dola", "session=admin-only", quota_limit=3)
        registered = self.register("preferred_account_client")

        response = self.client.post(
            "/tasks",
            headers={"X-API-Token": registered["token"]},
            data={
                "prompt": "client account selection attempt",
                "ratio": "9:16",
                "platform": "dola",
                "model": "Seedance 2.0",
                "preferred_account_id": selected["id"],
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(store.list_tasks(owner_token_hash=temp_access.hash_token(registered["token"])), [])

    def test_batch_failed_rows_can_retry_with_original_real_person_setting(self) -> None:
        registered = self.register("batch_failed_retry_owner")
        token = registered["token"]
        owner_hash = temp_access.hash_token(token)
        temp_access.add_temp_credit_units(owner_hash, 10)
        temp_access.set_temp_billing_priority(owner_hash, "points_first")
        source = store.create_task("batch retry reference", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        image = store.images_dir(source["id"]) / "01.png"
        image.write_bytes(b"reference-image")
        store.set_task_images(source["id"], [image], ["reference.png"])
        store.update_meta(source["id"], reference_is_real_person=True)
        store.mark_failed(source["id"], "failed for retry")
        job = batch_jobs.create_job(
            owner_hash,
            [{"prompt": "batch retry reference", "image_count": 1, "image_names": ["reference.png"]}],
            ratio="9:16",
            concurrency=1,
            reference_is_real_person=True,
        )
        batch_jobs.finish_row_creation(job["id"], 1, source["id"])
        batch_jobs.reconcile_job(job["id"], {source["id"]: {"status": "failed", "error": "failed for retry", "video_url": ""}})

        response = self.client.post(
            f"/batch-prompts/jobs/{job['id']}/retry",
            headers={"X-API-Token": token},
            json={"row_indices": [1]},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual((payload["requested"], payload["created"]), (1, 1))
        self.assertTrue(payload["job"]["reference_is_real_person"])
        retry_id = payload["job"]["rows"][0]["task_id"]
        retry_meta = store.get_meta(retry_id)
        self.assertTrue(retry_meta["reference_is_real_person"])
        self.assertEqual(store.task_image_paths(retry_id)[0].read_bytes(), b"reference-image")

    def test_proxy_country_selection_preserves_json_array(self) -> None:
        self.login_admin()
        response = self.client.post(
            "/config/proxy-api",
            json={
                "proxy_enabled": True,
                "proxy_auto_select": True,
                "proxy_auto_countries": ["日本", "美国", "日本"],
                "proxy_latency_threshold_ms": 800,
                "proxy_health_refresh_seconds": 120,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["proxy_auto_countries"], ["日本", "美国"])
        self.assertEqual(config.load_settings().proxy_auto_countries, ["日本", "美国"])
        self.assertEqual(response.json()["proxy_health_refresh_seconds"], 120)
        self.assertEqual(config.load_settings().proxy_health_refresh_seconds, 120)

    def test_platform_proxy_sources_and_random_modes_are_independent(self) -> None:
        self.login_admin()
        response = self.client.post(
            "/config/proxy-api",
            json={
                "proxy_api_url": "https://proxy.example/api",
                "proxy_subscription_url": "https://subscription.example/token",
                "platform_proxy_sources": {"dola": "api", "doubao": "subscription", "qianwen": "direct"},
                "platform_proxy_random": {"dola": True, "doubao": True, "qianwen": False},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["platform_proxy_sources"], {"dola": "api", "doubao": "subscription", "qianwen": "direct"})
        self.assertEqual(response.json()["platform_proxy_random"], {"dola": True, "doubao": True, "qianwen": False})
        settings = config.load_settings()
        self.assertEqual(settings.platform_proxy_sources, response.json()["platform_proxy_sources"])
        self.assertEqual(settings.platform_proxy_random, response.json()["platform_proxy_random"])

    def test_proxy_latency_filters_threshold_and_persists_automatic_selection(self) -> None:
        self.login_admin()
        nodes = proxy_manager.subscription_node_list(
            "http://slow.example.com:8080#US\nhttp://fast.example.com:8081#Japan\nhttp://down.example.com:8082#Singapore"
        )
        self.client.post(
            "/config/proxy-api",
            json={
                "proxy_source": "subscription",
                "proxy_subscription_url": "https://subscription.example/token",
                "proxy_auto_select": True,
                "proxy_auto_countries": ["美国", "日本"],
                "proxy_latency_threshold_ms": 100,
            },
        )
        delays = {nodes[0].id: 180, nodes[1].id: 45, nodes[2].id: None}

        def payload(node, selected_node="") -> dict:
            delay = delays[node.id]
            return {
                "id": node.id,
                "name": node.name,
                "country": node.country,
                "protocol": node.protocol,
                "latency_ms": delay,
                "latency_status": "available" if delay is not None else "unavailable",
                "selected": node.id == selected_node,
            }

        with patch("app.main.fetch_subscription_node_list", new=AsyncMock(return_value=nodes)), patch(
            "app.main.measure_node_delays", new=AsyncMock(return_value=delays)
        ), patch("app.main.node_payload", side_effect=payload):
            measured = self.client.post("/config/proxy-nodes/latency")

        self.assertEqual(measured.status_code, 200, measured.text)
        result = measured.json()
        self.assertEqual([item["id"] for item in result["nodes"]], [nodes[1].id])
        self.assertEqual(result["filtered_count"], 2)
        self.assertEqual(result["selected_node"], nodes[1].id)
        self.assertTrue(result["nodes"][0]["selected"])
        self.assertEqual(config.load_settings().proxy_selected_node, nodes[1].id)

    def test_authenticated_proxy_is_saved_without_returning_credentials(self) -> None:
        self.login_admin()
        saved = self.client.post(
            "/config/proxy-api",
            json={
                "proxy_source": "account",
                "proxy_account_scheme": "socks5",
                "proxy_account_host": "proxy.example.com",
                "proxy_account_port": 3010,
                "proxy_account_username": "fake-region-JP",
                "proxy_account_password": "fake-password",
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        payload = saved.json()
        self.assertTrue(payload["proxy_account_configured"])
        self.assertEqual(payload["proxy_source"], "account")
        self.assertEqual(payload["proxy_account_username_masked"], "fak***JP")
        self.assertNotIn("proxy_account_password", payload)
        self.assertNotIn("fake-password", saved.text)

        preserved = self.client.post(
            "/config/proxy-api",
            json={
                "proxy_source": "account",
                "proxy_account_scheme": "socks5",
                "proxy_account_host": "proxy.example.com",
                "proxy_account_port": 3010,
                "proxy_account_password": "",
            },
        )
        self.assertEqual(preserved.status_code, 200, preserved.text)
        self.assertEqual(config.load_settings().proxy_account_password, "fake-password")
        self.assertNotIn("fake-password", self.client.get("/config/proxy-api").text)

    def test_authenticated_proxy_requires_valid_complete_configuration(self) -> None:
        self.login_admin()
        empty_pool = self.client.post(
            "/config/proxy-api",
            json={"proxy_source": "account"},
        )
        self.assertEqual(empty_pool.status_code, 200)
        missing = self.client.post(
            "/config/proxy-api",
            json={"proxy_source": "account", "proxy_account_host": "proxy.example.com", "proxy_account_port": 3010},
        )
        self.assertEqual(missing.status_code, 400)
        invalid_port = self.client.post(
            "/config/proxy-api",
            json={"proxy_source": "account", "proxy_account_host": "proxy.example.com", "proxy_account_port": 70000, "proxy_account_username": "fake", "proxy_account_password": "fake"},
        )
        self.assertEqual(invalid_port.status_code, 400)
        invalid_host = self.client.post(
            "/config/proxy-api",
            json={"proxy_source": "account", "proxy_account_host": "localhost", "proxy_account_port": 3010, "proxy_account_username": "fake", "proxy_account_password": "fake"},
        )
        self.assertEqual(invalid_host.status_code, 400)

    def test_authenticated_proxy_pool_import_measure_select_disable_and_delete(self) -> None:
        self.login_admin()
        lines = "\n".join([
            "socks5://fake-region-JP:fake-pass-1@jp-proxy.example.com:3010",
            "us-proxy.example.com:3020:fake-region-US:fake-pass-2",
        ])
        with patch("app.main.probe_dola_proxy", new=AsyncMock(side_effect=[(True, 48), (False, None)])):
            imported = self.client.post("/config/account-proxies/import", json={"text": lines})
        self.assertEqual(imported.status_code, 200, imported.text)
        payload = imported.json()
        self.assertEqual(payload["added"], 2)
        self.assertEqual(len(payload["proxies"]), 2)
        self.assertEqual(payload["selected_ids"], [item["id"] for item in payload["proxies"]])
        self.assertEqual([item["country"] for item in payload["proxies"]], ["日本", "美国"])
        self.assertEqual([item["latency_status"] for item in payload["proxies"]], ["available", "unavailable"])
        self.assertNotIn("fake-pass", imported.text)
        self.assertNotIn("username\"", imported.text)

        switched = self.client.post("/config/proxy-api", json={"proxy_source": "account"})
        self.assertEqual(switched.status_code, 200, switched.text)
        listed = self.client.get("/config/proxy-nodes")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["source"], "account")
        self.assertEqual(len(listed.json()["nodes"]), 2)

        first_id, second_id = payload["selected_ids"]
        selected = self.client.post("/config/proxy-nodes/select", json={"node_ids": [first_id], "rotation_enabled": False})
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual(selected.json()["selected_ids"], [first_id])
        disabled = self.client.post("/config/account-proxies/action", json={"action": "disable", "proxy_ids": [first_id]})
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertEqual(disabled.json()["selected_ids"], [])
        deleted = self.client.post("/config/account-proxies/action", json={"action": "delete", "proxy_ids": [second_id]})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(len(deleted.json()["proxies"]), 1)

    def test_global_worker_configuration_keeps_remote_generation_unlimited(self) -> None:
        headers = {"X-API-Token": self.admin_token}
        accepted = self.client.post("/config/workers", headers=headers, json={"browser_workers": 999, "max_effective_workers": 200, "remote_generation_limit": 150})
        self.assertEqual(accepted.status_code, 200)
        payload = accepted.json()
        self.assertEqual(payload["browser_workers"], 36)
        self.assertEqual(payload["max_effective_workers"], 36)
        self.assertEqual(payload["capacity_limit"], 36)
        self.assertEqual(payload["effective_browser_workers"], 36)
        self.assertEqual(payload["remote_generation_limit"], 0)
        self.assertEqual((payload["browser_pool_processes"], payload["browser_contexts_per_process"], payload["submission_concurrency"]), (12, 3, 36))
        remote_only = self.client.post("/config/workers", headers=headers, json={"remote_generation_limit": 175})
        self.assertEqual(remote_only.status_code, 200)
        self.assertEqual(remote_only.json()["remote_generation_limit"], 0)
        rejected = self.client.post("/config/workers", headers=headers, json={"browser_workers": 1000})
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(config.load_settings().browser_workers, 36)
        rejected_capacity = self.client.post("/config/workers", headers=headers, json={"browser_workers": 100, "max_effective_workers": 1000})
        self.assertEqual(rejected_capacity.status_code, 400)
        self.assertEqual(config.load_settings().max_effective_workers, 36)
        ignored_remote = self.client.post("/config/workers", headers=headers, json={"browser_workers": 32, "max_effective_workers": 32, "remote_generation_limit": 1000})
        self.assertEqual(ignored_remote.status_code, 200)
        self.assertEqual(config.load_settings().remote_generation_limit, 0)

    def test_proxy_node_apis_list_measure_select_and_switch(self) -> None:
        self.client.post(
            "/config/proxy-api",
            headers={"X-API-Token": self.admin_token},
            json={"proxy_subscription_url": "https://subscription.example/token", "proxy_enabled": True},
        )
        nodes = proxy_manager.subscription_node_list("vless://user@hk.example.com:443#Hong%20Kong\ntrojan://secret@jp.example.com:443#Japan")
        with patch("app.main.fetch_subscription_node_list", new=AsyncMock(return_value=nodes)), patch(
            "app.main.measure_node_delays", new=AsyncMock(return_value={node.id: 20 for node in nodes})
        ), patch(
            "app.main.activate_mihomo_node", new=AsyncMock()
        ):
            listed = self.client.get("/config/proxy-nodes", headers={"X-API-Token": self.admin_token})
            measured = self.client.post("/config/proxy-nodes/latency", headers={"X-API-Token": self.admin_token})
            selected = self.client.post(
                "/config/proxy-nodes/select",
                headers={"X-API-Token": self.admin_token},
                json={"node_id": nodes[1].id},
            )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual([item["country"] for item in listed.json()["nodes"]], ["香港", "日本"])
        self.assertEqual(measured.status_code, 200)
        self.assertEqual(selected.status_code, 200)
        settings = config.load_settings()
        self.assertFalse(settings.proxy_auto_select)
        self.assertEqual(settings.proxy_selected_node, nodes[1].id)
        switched = self.client.post(
            "/config/proxy-api",
            headers={"X-API-Token": self.admin_token},
            json={"proxy_enabled": False, "proxy_auto_select": True},
        )
        self.assertEqual(switched.status_code, 200)
        self.assertFalse(switched.json()["proxy_enabled"])
        self.assertTrue(switched.json()["proxy_auto_select"])

    def test_admin_can_update_model_costs(self) -> None:
        response = self.client.post(
            "/config/platforms",
            headers={"X-API-Token": self.admin_token},
            json={
                "default_platform": "dola",
                "platforms": [
                    {"id": "dola", "models": [{"name": "Seedance 2.0", "enabled": True, "cost": 1.7, "durations": [5, 15], "duration_costs": {"5": 0.5, "10": 1.1, "15": 2.3}}]},
                    {"id": "doubao", "models": []},
                    {"id": "qianwen", "models": []},
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        dola = next(item for item in response.json()["platforms"] if item["id"] == "dola")
        self.assertEqual(dola["model_durations"]["Seedance 2.0"], [5, 15])
        self.assertEqual(dola["model_duration_costs"]["Seedance 2.0"], {"5": 0.5, "10": 1.1, "15": 2.3})
        self.assertEqual(dola["all_models"][0]["duration_costs"]["15"], 2.3)

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
