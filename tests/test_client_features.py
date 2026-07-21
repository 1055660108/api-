from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import admin_auth, config, feedback, main, membership_catalog, notifications, package_catalog, point_cards, point_transactions, store, temp_access, users


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
            patch.object(feedback, "FEEDBACK_PATH", self.root / "feedback.json"),
            patch.object(notifications, "NOTIFICATIONS_PATH", self.root / "notifications.json"),
            patch.object(package_catalog, "PACKAGE_CATALOG_PATH", self.root / "point_packages.json"),
            patch.object(point_cards, "POINT_CARDS_PATH", self.root / "point_cards.json"),
            patch.object(point_transactions, "TRANSACTIONS_PATH", self.root / "point_transactions.json"),
            patch.object(membership_catalog, "MEMBERSHIP_PATH", self.root / "membership_packages.json"),
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

    def test_message_center_exposes_feedback_reply_and_targeted_notifications(self) -> None:
        first = self.register("message_user_one")
        second = self.register("message_user_two")
        first_headers = {"X-API-Token": first["token"]}
        second_headers = {"X-API-Token": second["token"]}

        created = self.client.post(
            "/feedback",
            headers=first_headers,
            json={"category": "问题反馈", "content": "任务状态没有及时更新"},
        )
        self.assertEqual(created.status_code, 201)
        feedback_id = created.json()["feedback"]["id"]

        self.client.post("/auth/admin/login", json={"username": "chosen-admin", "password": "StrongPassword123"})
        updated = self.client.patch(
            f"/admin/feedback/{feedback_id}",
            json={"status": "resolved", "admin_note": "问题已经处理，请刷新后重试。"},
        )
        self.assertEqual(updated.status_code, 200)
        recipients = self.client.get("/admin/notification-recipients").json()["users"]
        recipient_ids = [item["id"] for item in recipients]
        sent = self.client.post(
            "/admin/notifications",
            json={"user_ids": recipient_ids, "title": "服务通知", "content": "消息中心已经上线。"},
        )
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(sent.json()["recipient_count"], 2)

        own_feedback = self.client.get("/feedback", headers=first_headers).json()["feedback"]
        self.assertEqual(len(own_feedback), 1)
        self.assertEqual(own_feedback[0]["status"], "resolved")
        self.assertEqual(own_feedback[0]["admin_note"], "问题已经处理，请刷新后重试。")
        self.assertEqual(self.client.get("/feedback", headers=second_headers).json()["feedback"], [])

        first_notifications = self.client.get("/notifications", headers=first_headers).json()
        self.assertEqual(first_notifications["unread"], 1)
        notification_id = first_notifications["notifications"][0]["id"]
        marked = self.client.patch(f"/notifications/{notification_id}/read", headers=first_headers)
        self.assertEqual(marked.status_code, 200)
        self.assertEqual(self.client.get("/notifications", headers=first_headers).json()["unread"], 0)
        self.assertEqual(self.client.get("/notifications", headers=second_headers).json()["unread"], 1)

        read_all = self.client.post("/notifications/read-all", headers=second_headers)
        self.assertEqual(read_all.status_code, 200)
        self.assertEqual(read_all.json()["updated"], 1)
        self.assertEqual(self.client.get("/notifications", headers=second_headers).json()["unread"], 0)

    def test_point_cards_redeem_once_and_create_transaction(self) -> None:
        first = self.register("card_user_one")
        second = self.register("card_user_two")
        self.client.post("/auth/admin/login", json={"username": "chosen-admin", "password": "StrongPassword123"})
        generated = self.client.post("/admin/point-cards", json={"points": 12.5, "count": 1, "note": "测试批次"})
        self.assertEqual(generated.status_code, 201)
        code = generated.json()["cards"][0]["code"]
        listed_before_redeem = self.client.get("/admin/point-cards").json()["cards"]
        self.assertEqual(listed_before_redeem[0]["code"], code)
        self.assertEqual(point_cards._read()["cards"][point_cards._digest(code)]["code"], code)
        first_headers = {"X-API-Token": first["token"]}
        redeemed = self.client.post("/points/redeem", headers=first_headers, json={"code": code})
        self.assertEqual(redeemed.status_code, 200)
        self.assertEqual(redeemed.json()["points"], 12.5)
        self.assertEqual(self.client.post("/points/redeem", headers={"X-API-Token": second["token"]}, json={"code": code}).status_code, 400)
        transactions = self.client.get("/points/transactions", headers=first_headers).json()["transactions"]
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0]["kind"], "redeem")
        self.assertEqual(transactions[0]["amount"], 12.5)
        history = self.client.get(f"/admin/point-cards?q={code}").json()["cards"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["redeemed_username"], "card_user_one")

    def test_announcements_are_seen_per_user(self) -> None:
        first = self.register("announcement_one")
        second = self.register("announcement_two")
        self.client.post("/auth/admin/login", json={"username": "chosen-admin", "password": "StrongPassword123"})
        created = self.client.post("/admin/announcements", json={"title": "维护通知", "content": "今晚进行维护。"})
        self.assertEqual(created.status_code, 201)
        announcement_id = created.json()["announcement"]["id"]
        first_headers = {"X-API-Token": first["token"]}
        second_headers = {"X-API-Token": second["token"]}
        self.assertEqual(self.client.get("/announcements", headers=first_headers).json()["unseen"], 1)
        self.assertEqual(self.client.patch(f"/announcements/{announcement_id}/seen", headers=first_headers).status_code, 200)
        self.assertEqual(self.client.get("/announcements", headers=first_headers).json()["unseen"], 0)
        self.assertEqual(self.client.get("/announcements", headers=second_headers).json()["unseen"], 1)

        emergency = self.client.post("/admin/announcements", json={"title": "紧急维护", "content": "服务维护中", "level": "emergency", "lock_screen": True})
        emergency_id = emergency.json()["announcement"]["id"]
        listed = self.client.get("/announcements", headers=second_headers).json()["announcements"]
        emergency_row = next(item for item in listed if item["id"] == emergency_id)
        self.assertEqual(emergency_row["level"], "emergency")
        self.assertTrue(emergency_row["lock_screen"])
        unlocked = self.client.patch(f"/admin/announcements/{emergency_id}", json={"lock_screen": False})
        self.assertFalse(unlocked.json()["announcement"]["lock_screen"])

    def test_membership_catalog_admin_crud_and_public_filtering(self) -> None:
        registered = self.register("member_user")
        headers = {"X-API-Token": registered["token"]}
        self.client.post("/auth/admin/login", json={"username": "chosen-admin", "password": "StrongPassword123"})
        user_id = next(item["id"] for item in self.client.get("/users").json()["users"] if item["username"] == "member_user")
        self.client.post(f"/users/{user_id}/points", json={"amount": 50})
        self.assertEqual(self.client.patch(f"/users/{user_id}", json={"concurrency": 2}).status_code, 200)
        created = self.client.post("/admin/memberships", json={"name": "月度会员", "points_cost": 10, "duration_days": 30, "concurrency": 3, "bonus_free_uses": 4, "description": "月度套餐"})
        self.assertEqual(created.status_code, 201)
        package_id = created.json()["package"]["id"]
        public = self.client.get("/memberships", headers=headers).json()
        self.assertEqual(public["packages"][0]["name"], "月度会员")
        purchased = self.client.post(f"/memberships/{package_id}/purchase", headers=headers)
        self.assertEqual(purchased.status_code, 200)
        self.assertEqual(purchased.json()["balance"]["credit_units"], 400)
        self.assertEqual(purchased.json()["balance"]["free_remaining"], 5)
        self.assertEqual(purchased.json()["balance"]["concurrency"], 5)
        self.assertEqual(self.client.get("/auth/client", headers=headers).json()["token_concurrency"], 5)
        profile = self.client.get("/auth/profile", headers=headers).json()
        self.assertEqual(profile["membership"]["name"], "月度会员")
        self.assertEqual(profile["membership"]["concurrency_bonus"], 3)
        self.assertEqual(profile["membership"]["effective_concurrency"], 5)
        self.assertEqual(profile["membership"]["purchased_package_ids"], [package_id])
        duplicate = self.client.post(f"/memberships/{package_id}/purchase", headers=headers)
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("只能购买一次", duplicate.json()["detail"])
        transactions = self.client.get("/points/transactions", headers=headers).json()["transactions"]
        self.assertEqual(transactions[0]["kind"], "membership_purchase")
        self.assertEqual(sum(item["kind"] == "membership_purchase" for item in transactions), 1)

        user_data = json.loads(users.USERS_PATH.read_text(encoding="utf-8"))
        member_entry = next(item for item in user_data["users"].values() if item["id"] == user_id)
        member_entry["membership"]["expires_at"] = "2000-01-01T00:00:00+00:00"
        users.USERS_PATH.write_text(json.dumps(user_data, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.client.get("/auth/client", headers=headers).json()["token_concurrency"], 2)
        repurchased = self.client.post(f"/memberships/{package_id}/purchase", headers=headers)
        self.assertEqual(repurchased.status_code, 200)
        self.assertEqual(repurchased.json()["membership"]["purchased_package_ids"], [package_id])

        self.assertEqual(self.client.patch(f"/admin/memberships/{package_id}", json={"points_cost": 12.5}).json()["package"]["points_cost"], 12.5)
        self.assertEqual(self.client.delete(f"/admin/memberships/{package_id}").status_code, 200)
        self.assertEqual(self.client.get("/memberships", headers=headers).json()["packages"], [])

    def test_user_search_prefers_exact_username_email_or_id(self) -> None:
        first = self.register("search_user")
        self.register("search_user_extra")
        self.client.post("/auth/admin/login", json={"username": "chosen-admin", "password": "StrongPassword123"})
        exact = self.client.get("/users?q=search_user").json()
        self.assertEqual(exact["users"][0]["username"], "search_user")
        user_id = exact["users"][0]["id"]
        self.assertEqual(self.client.get(f"/users?q={user_id}").json()["users"][0]["id"], user_id)

    def test_task_consumption_and_admin_adjustments_are_recorded(self) -> None:
        registered = self.register("ledger_user")
        headers = {"X-API-Token": registered["token"]}
        self.client.post("/auth/admin/login", json={"username": "chosen-admin", "password": "StrongPassword123"})
        user_id = next(item["id"] for item in self.client.get("/users").json()["users"] if item["username"] == "ledger_user")
        self.assertEqual(self.client.post(f"/users/{user_id}/points", json={"amount": 5}).status_code, 200)
        with patch.object(main, "active_task_count_for_owner", return_value=0), patch.object(main, "create_sem", asyncio.Semaphore(1)):
            for index in range(4):
                response = self.client.post(
                    "/tasks",
                    headers=headers,
                    data={"prompt": f"测试任务 {index}", "ratio": "9:16", "platform": "dola", "model": "Seedance 2.0", "task_type": "video"},
                )
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post(f"/users/{user_id}/points/deduct", json={"amount": 1}).status_code, 200)
        rows = self.client.get("/points/transactions", headers=headers).json()["transactions"]
        kinds = [item["kind"] for item in rows]
        self.assertIn("admin_credit", kinds)
        self.assertIn("consume", kinds)
        self.assertIn("admin_deduct", kinds)
        consumed = next(item for item in rows if item["kind"] == "consume")
        self.assertEqual(consumed["amount"], -1)
        self.assertEqual(consumed["title"], "视频任务消费")
        self.assertEqual(consumed["reference_id"], consumed["detail"].splitlines()[0].removeprefix("任务 ID："))
        self.assertIn(f"任务 ID：{consumed['reference_id']}", consumed["detail"])


if __name__ == "__main__":
    unittest.main()
