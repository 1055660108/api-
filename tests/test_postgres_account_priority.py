import unittest
from contextlib import contextmanager
from unittest.mock import patch

from app import postgres


class _EmptyResult:
    def fetchone(self):
        return None


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
            )

        self.assertIsNone(result)
        self.assertIn("payload->>'account_source'", connection.query)
        self.assertIn("= 'api' THEN 0 ELSE 1", connection.query)
        self.assertEqual(connection.query.count("%s"), len(connection.params))
        self.assertEqual(connection.params[:4], ("doubao", "2026-08-01", 2, "2026-08-01T00:00:00+00:00"))


if __name__ == "__main__":
    unittest.main()
