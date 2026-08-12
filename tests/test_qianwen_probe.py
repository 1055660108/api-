from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.qianwen_probe import QianwenProbeManager


class QianwenProbeTests(unittest.TestCase):
    def test_probe_reports_progress_and_releases_maintenance_claim(self) -> None:
        manager = QianwenProbeManager()
        account = {
            "id": "qianwen-account",
            "name": "测试账号",
            "platform": "qianwen",
            "enabled": True,
            "account_status": "normal",
            "cookies": [{"name": "tongyi_sso_ticket", "value": "ticket"}],
        }
        manager._probe_account = AsyncMock(return_value={"ok": True, "reason": "登录正常"})
        with patch("app.qianwen_probe.list_accounts", return_value=[account]), patch(
            "app.qianwen_probe.claim_account_for_maintenance", return_value=account
        ), patch("app.qianwen_probe.clear_account_current_task") as clear:
            asyncio.run(manager._run_once(False))

        status = manager.snapshot()
        self.assertFalse(status["running"])
        self.assertEqual(status["total"], 1)
        self.assertEqual(status["completed"], 1)
        self.assertEqual(status["success"], 1)
        self.assertEqual(status["failed"], 0)
        clear.assert_called_once_with("qianwen-account", "maintenance:qianwen-probe")

    def test_probe_skips_account_claimed_by_generation_task(self) -> None:
        manager = QianwenProbeManager()
        account = {"id": "busy", "name": "忙碌账号", "platform": "qianwen", "enabled": True, "account_status": "normal"}
        manager._probe_account = AsyncMock()
        with patch("app.qianwen_probe.list_accounts", return_value=[account]), patch(
            "app.qianwen_probe.claim_account_for_maintenance", return_value=None
        ):
            asyncio.run(manager._run_once(True))

        status = manager.snapshot()
        self.assertEqual(status["skipped"], 1)
        self.assertEqual(status["completed"], 1)
        manager._probe_account.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
