import unittest
from contextlib import contextmanager
from unittest.mock import patch

from app import postgres


class _EmptyResult:
    def fetchone(self):
        return None


class _SingleResult:
    def fetchone(self):
        return (1,)


class _RecordingConnection:
    def __init__(self) -> None:
        self.query = ""
        self.params = ()

    def execute(self, query, params=()):
        self.query = query
        self.params = params
        return _EmptyResult()


class PostgresAccountPriorityTests(unittest.TestCase):
    def test_claim_query_prioritizes_api_accounts_with_matching_parameters(self) -> None:
        connection = _RecordingConnection()

        @contextmanager
        def fake_connection():
            yield connection

        with patch.object(postgres, "connection", fake_connection):
            result = postgres.claim_available_account(
                "doubao",
                {"excluded-account"},
                "2026-08-01",
                "2026-08-01T00:00:00+00:00",
                lambda account: account,
                quota_cost=2,
                duration=10,
            )

        self.assertIsNone(result)
        self.assertIn("payload->>'account_source'", connection.query)
        self.assertIn("= 'api' THEN 0 ELSE 1", connection.query)
        self.assertEqual(connection.query.count("%s"), len(connection.params))
        self.assertEqual(connection.params[:4], ("doubao", "2026-08-01", 2, "2026-08-01T00:00:00+00:00"))

    def test_claim_query_supports_admin_priority_and_random_modes(self) -> None:
        @contextmanager
        def fake_connection():
            yield connection

        connection = _RecordingConnection()
        with patch.object(postgres, "connection", fake_connection):
            postgres.claim_available_account("dola", set(), "2026-08-01", "2026-08-01T00:00:00+00:00", lambda account: account, selection_mode="admin_first")
        self.assertIn("= 'admin' THEN 0 ELSE 1", connection.query)

        connection = _RecordingConnection()
        with patch.object(postgres, "connection", fake_connection):
            postgres.claim_available_account("dola", set(), "2026-08-01", "2026-08-01T00:00:00+00:00", lambda account: account, selection_mode="random")
        self.assertIn("ORDER BY random()", connection.query)

    def test_dola_ten_second_filter_only_applies_above_ten_seconds(self) -> None:
        @contextmanager
        def fake_connection():
            yield connection

        connection = _RecordingConnection()
        with patch.object(postgres, "connection", fake_connection):
            postgres.claim_available_account("dola", set(), "2026-08-01", "2026-08-01T00:00:00+00:00", lambda account: account, duration=10)
        self.assertNotIn("ten_second_only", connection.query)

        connection = _RecordingConnection()
        with patch.object(postgres, "connection", fake_connection):
            postgres.claim_available_account("dola", set(), "2026-08-01", "2026-08-01T00:00:00+00:00", lambda account: account, duration=15)
        self.assertIn("COALESCE(payload->>'ten_second_only', 'false') <> 'true'", connection.query)

    def test_qianwen_ai_studio_claim_requires_nonempty_ticket(self) -> None:
        connection = _RecordingConnection()

        @contextmanager
        def fake_connection():
            yield connection

        with patch.object(postgres, "connection", fake_connection):
            postgres.claim_available_account(
                "qianwen",
                set(),
                "2026-08-01",
                "2026-08-01T00:00:00+00:00",
                lambda account: account,
                quota_bucket="qianwen_ai_studio",
            )

        self.assertIn("jsonb_array_elements(payload->'cookies')", connection.query)
        self.assertIn("tongyi_sso_ticket", connection.query)
        self.assertIn("btrim(COALESCE(cookie->>'value', '')) <> ''", connection.query)

    def test_duration_support_uses_one_account_query(self) -> None:
        connection = _RecordingConnection()

        @contextmanager
        def fake_connection():
            yield connection

        with patch.object(postgres, "connection", fake_connection):
            self.assertFalse(postgres.account_supports_duration("account-1", "dola", 15))
        self.assertIn("id = %s", connection.query)
        self.assertIn("ten_second_only", connection.query)
        self.assertEqual(connection.params, ("account-1", "dola"))

        connection = _RecordingConnection()
        connection.execute = lambda query, params=(): _SingleResult()
        with patch.object(postgres, "connection", fake_connection):
            self.assertTrue(postgres.account_supports_duration("account-1", "dola", 10))


if __name__ == "__main__":
    unittest.main()
