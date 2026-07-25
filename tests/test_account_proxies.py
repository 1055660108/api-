from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import account_proxies, config


def empty_settings() -> SimpleNamespace:
    return SimpleNamespace(
        proxy_account_scheme="socks5",
        proxy_account_host="",
        proxy_account_port=0,
        proxy_account_username="",
        proxy_account_password="",
    )


class AccountProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(config, "DATA_DIR", Path(self.temporary_directory.name))
        self.data_patch.start()
        account_proxies._ROTATION_CURSOR = 0
        self.settings = empty_settings()

    def tearDown(self) -> None:
        self.data_patch.stop()
        self.temporary_directory.cleanup()

    def test_parses_url_and_common_colon_format_without_exposing_password(self) -> None:
        first = account_proxies.parse_account_proxy_line("socks5://fake-region-JP:p%40ss@jp.example.com:3010")
        second = account_proxies.parse_account_proxy_line("us.example.com:3020:fake-region-US:secret")
        self.assertEqual((first["scheme"], first["country"], first["password"]), ("socks5", "日本", "p@ss"))
        self.assertEqual((second["scheme"], second["country"], second["port"]), ("socks5", "美国", 3020))
        public = account_proxies.public_account_proxy(first, {first["id"]})
        self.assertTrue(public["selected"])
        self.assertNotIn("password", public)
        self.assertNotIn("username", public)
        self.assertEqual(public["username_masked"], "fak***JP")

    def test_import_deduplicates_and_round_robins_selected_entries(self) -> None:
        text = "\n".join([
            "socks5://fake-region-JP:one@jp.example.com:3010",
            "socks5://fake-region-US:two@us.example.com:3020",
            "socks5://fake-region-JP:one@jp.example.com:3010",
        ])
        result = account_proxies.import_account_proxies(text, self.settings)
        self.assertEqual((result["added"], result["duplicates"]), (2, 1))
        first = account_proxies.account_proxy_candidates(self.settings)
        second = account_proxies.account_proxy_candidates(self.settings)
        self.assertNotEqual(first[0]["id"], second[0]["id"])

        account_proxies.select_account_proxies([result["selected_ids"][0]], False, self.settings)
        fixed = account_proxies.account_proxy_candidates(self.settings)
        self.assertEqual([item["id"] for item in fixed], [result["selected_ids"][0]])

    def test_disable_removes_selection_and_delete_removes_secret_entry(self) -> None:
        imported = account_proxies.import_account_proxies("socks5://fake-region-JP:one@jp.example.com:3010", self.settings)
        proxy_id = imported["selected_ids"][0]
        disabled = account_proxies.set_account_proxies_enabled([proxy_id], False, self.settings)
        self.assertEqual(disabled["selected_ids"], [])
        self.assertFalse(disabled["proxies"][0]["enabled"])
        deleted = account_proxies.delete_account_proxies([proxy_id], self.settings)
        self.assertEqual(deleted["proxies"], [])


if __name__ == "__main__":
    unittest.main()
