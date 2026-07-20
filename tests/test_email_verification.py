from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, email_verification


class EmailVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.patchers = [
            patch.object(config, "CONFIG_PATH", self.root / "config.json"),
            patch.object(config, "DATA_DIR", self.root),
            patch.object(email_verification, "EMAIL_VERIFICATIONS_PATH", self.root / "email_verifications.json"),
            patch("app.email_verification.postgres.enabled", return_value=False),
        ]
        for patcher in self.patchers:
            patcher.start()
        config.ensure_config()
        config.update_config({
            "registration_email_domains": ["qq.com", "163.com"],
            "registration_smtp_username": "sender@qq.com",
            "registration_smtp_authorization_code": "authorization-code",
        })

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def test_only_configured_email_domains_are_allowed(self) -> None:
        settings = config.load_settings()
        self.assertEqual(email_verification.validate_allowed_email("User@QQ.com", settings), "user@qq.com")
        with self.assertRaisesRegex(ValueError, "暂不支持"):
            email_verification.validate_allowed_email("user@gmail.com", settings)

    def test_code_is_hashed_and_consumed_once(self) -> None:
        settings = config.load_settings()
        with patch("app.email_verification.secrets.randbelow", return_value=123456), patch("app.email_verification._send_qq_email") as sender:
            email_verification.send_registration_code("user@qq.com", settings)
        sender.assert_called_once()
        raw = email_verification.EMAIL_VERIFICATIONS_PATH.read_text(encoding="utf-8")
        self.assertNotIn("123456", raw)
        self.assertEqual(email_verification.consume_registration_code("user@qq.com", "123456", settings), "user@qq.com")
        with self.assertRaises(ValueError):
            email_verification.consume_registration_code("user@qq.com", "123456", settings)


if __name__ == "__main__":
    unittest.main()
