from __future__ import annotations

import asyncio
import json
import inspect
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app import __version__, accounts, admin_auth, client_auth, config, main, package_catalog, point_transactions, proxy_manager, store, temp_access, users


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
            patch.object(point_transactions, "TRANSACTIONS_PATH", self.root / "point_transactions.json"),
            patch.dict("os.environ", {"DOLA_ADMIN_USERNAME": "contract-admin", "DOLA_ADMIN_PASSWORD": "ContractPassword123"}),
        ]
        for patcher in self.patchers:
            patcher.start()
        admin_auth.clear_sessions()
        client_auth.clear_client_sessions()
        config.ensure_config()
        config.update_config({"registration_email_verification_enabled": False})
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()
        self.admin_token = config.load_settings().api_token

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        admin_auth.clear_sessions()
        client_auth.clear_client_sessions()
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

    def test_task_video_proxy_forwards_range_and_protects_task_ownership(self) -> None:
        registered = self.register("video_owner")
        owner_hash = temp_access.hash_token(registered["token"])
        task = store.create_task("视频播放", "9:16", owner_token_hash=owner_hash, platform="dola")
        store.save_result(task["id"], extra={"decoded_main_url": "https://cdn.example/video.mp4"})
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

        other = self.register("video_other")
        denied = self.client.get(f"/tasks/{task['id']}/video", headers={"X-API-Token": other["token"]})
        self.assertEqual(denied.status_code, 404)

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
        self.assertTrue({"quota", "token_concurrency", "task_retention_days", "user_name"} <= set(client))
        self.assertEqual(client["role"], "client")
        self.assertEqual(set(client["quota"]), {"limit", "used", "remaining", "free_remaining", "points"})
        self.assertNotIn("admin_username", client)
        self.assertNotIn("error", client["components"]["queue"])
        self.assertNotIn("executable_path", client["components"]["browser"])

    def test_runtime_submit_interval_is_admin_only_persistent_and_validated(self) -> None:
        self.assertEqual(self.client.get("/config/runtime").status_code, 403)
        self.login_admin()
        self.assertEqual(self.client.get("/config/runtime").json()["dola_submit_interval_seconds"], 5.0)
        updated = self.client.post("/config/runtime", json={"dola_submit_interval_seconds": 2.5})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["dola_submit_interval_seconds"], 2.5)
        self.assertEqual(config.load_settings().dola_submit_interval_seconds, 2.5)
        for invalid in (0.9, 5.1, "invalid"):
            self.assertEqual(self.client.post("/config/runtime", json={"dola_submit_interval_seconds": invalid}).status_code, 400)

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
            store.set_task_images(source["id"], [image])
            if status == "failed":
                store.mark_failed(source["id"], "test failure")
            else:
                store.update_meta(source["id"], status=store.STATUS_SUCCESS, finished_at=store.utc_now())
            source_ids.append(source["id"])

        retry_ids = []
        for source_id in source_ids:
            response = self.client.post(f"/tasks/{source_id}/retry", headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            retry_id = response.json()["id"]
            retry_ids.append(retry_id)
            retry_meta = store.get_meta(retry_id)
            self.assertEqual(retry_meta["status"], store.STATUS_PENDING)
            self.assertEqual(retry_meta["retry_of_task_id"], source_id)
            self.assertEqual(retry_meta["duration"], 10)
            self.assertEqual(store.task_image_paths(retry_id)[0].read_bytes(), b"reference-image")

        self.assertEqual(len(set(retry_ids)), 2)
        active = store.create_task("active task", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        self.assertEqual(self.client.post(f"/tasks/{active['id']}/retry", headers=headers).status_code, 409)

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

    def test_client_hides_technical_task_errors_but_admin_keeps_diagnostics(self) -> None:
        registered = self.register("safe_error_client")
        token = registered["token"]
        owner_hash = temp_access.hash_token(token)
        task = store.create_task("测试技术错误隔离", "9:16", owner_token_hash=owner_hash, model="Seedance 2.0")
        raw_error = "Page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://example.invalid"
        store.mark_failed(task["id"], raw_error)

        client_task = self.client.get("/tasks?page=1&page_size=20", headers={"X-API-Token": token}).json()["tasks"][0]
        self.assertEqual(client_task["error"], "你的输入可能包含违规内容请重试！")
        client_result = self.client.get(f"/tasks/{task['id']}", headers={"X-API-Token": token}).json()
        self.assertEqual(client_result["text"], "你的输入可能包含违规内容请重试！")

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
        self.assertEqual(client_task["status_reason"], "正在打开生成页面")
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
        self.assertEqual(set(workers), {"browser_workers", "max_effective_workers", "effective_browser_workers", "capacity_limit", "browser_pool_processes", "browser_contexts_per_process", "submission_concurrency", "remote_generation_limit"})
        self.assertEqual((workers["browser_pool_processes"], workers["browser_contexts_per_process"], workers["submission_concurrency"]), (8, 4, 32))
        self.assertEqual(set(proxy), {"proxy_api_url", "proxy_api_scheme", "proxy_api_timeout_seconds", "proxy_subscription_configured", "proxy_subscription_scheme", "proxy_subscription_refresh_seconds", "proxy_enabled", "proxy_auto_select", "proxy_selected_node", "proxy_auto_countries", "proxy_latency_threshold_ms", "proxy_health_refresh_seconds"})
        self.assertNotIn("proxy_subscription_url", proxy)
        self.assertEqual(set(platforms), {"default_platform", "platforms"})
        self.assertEqual({item["id"] for item in platforms["platforms"]}, {"dola", "doubao", "qianwen"})
        for platform in platforms["platforms"]:
            self.assertEqual(set(platform), {"id", "label", "models", "model_costs", "all_models", "enabled"})
            for model in platform["all_models"]:
                self.assertEqual(set(model), {"name", "enabled", "cost"})

    def test_proxy_health_refresh_switches_within_checked_countries(self) -> None:
        nodes = proxy_manager.subscription_node_list("http://us.example.com:8080#US\nhttp://jp.example.com:8080#Japan")
        config.update_config({
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

    def test_proxy_country_selection_preserves_json_array(self) -> None:
        self.login_admin()
        response = self.client.post(
            "/config/proxy-api",
            json={
                "proxy_enabled": True,
                "proxy_auto_select": True,
                "proxy_auto_countries": ["日本", "美国", "日本"],
                "proxy_latency_threshold_ms": 800,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["proxy_auto_countries"], ["日本", "美国"])
        self.assertEqual(config.load_settings().proxy_auto_countries, ["日本", "美国"])

    def test_global_worker_configuration_keeps_remote_generation_unlimited(self) -> None:
        headers = {"X-API-Token": self.admin_token}
        accepted = self.client.post("/config/workers", headers=headers, json={"browser_workers": 999, "max_effective_workers": 200, "remote_generation_limit": 150})
        self.assertEqual(accepted.status_code, 200)
        payload = accepted.json()
        self.assertEqual(payload["browser_workers"], 32)
        self.assertEqual(payload["max_effective_workers"], 32)
        self.assertEqual(payload["capacity_limit"], 32)
        self.assertEqual(payload["effective_browser_workers"], 32)
        self.assertEqual(payload["remote_generation_limit"], 0)
        self.assertEqual((payload["browser_pool_processes"], payload["browser_contexts_per_process"], payload["submission_concurrency"]), (8, 4, 32))
        remote_only = self.client.post("/config/workers", headers=headers, json={"remote_generation_limit": 175})
        self.assertEqual(remote_only.status_code, 200)
        self.assertEqual(remote_only.json()["remote_generation_limit"], 0)
        rejected = self.client.post("/config/workers", headers=headers, json={"browser_workers": 1000})
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(config.load_settings().browser_workers, 32)
        rejected_capacity = self.client.post("/config/workers", headers=headers, json={"browser_workers": 100, "max_effective_workers": 1000})
        self.assertEqual(rejected_capacity.status_code, 400)
        self.assertEqual(config.load_settings().max_effective_workers, 32)
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
