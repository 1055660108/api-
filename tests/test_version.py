from __future__ import annotations

import unittest
from pathlib import Path

from app import __version__


class VersionTests(unittest.TestCase):
    def test_release_version_is_synchronized_across_build_inputs(self) -> None:
        root = Path(__file__).resolve().parents[1]
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
        compose = (root / "compose.yaml").read_text(encoding="utf-8")
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        env_example = (root / ".env.example").read_text(encoding="utf-8")
        postgres_source = (root / "app" / "postgres.py").read_text(encoding="utf-8")
        config_source = (root / "app" / "config.py").read_text(encoding="utf-8")
        admin_html = (root / "app" / "admin" / "index.html").read_text(encoding="utf-8")

        self.assertEqual(version, "1.4.74")
        self.assertEqual(__version__, version)
        self.assertIn(f"DOLA_IMAGE_TAG:-{version}", compose)
        self.assertEqual(compose.count("build:"), 1)
        self.assertIn(f"styles.css?v={version}", admin_html)
        self.assertIn(f"app.js?v={version}", admin_html)
        self.assertIn("DOLA_DATABASE_POOL_SIZE: ${DOLA_DATABASE_POOL_SIZE:-24}", compose)
        self.assertIn("DOLA_IMAGE_UPLOAD_CONCURRENCY: ${DOLA_IMAGE_UPLOAD_CONCURRENCY:-8}", compose)
        self.assertIn("DOLA_REFERENCE_CACHE_TTL_SECONDS: ${DOLA_REFERENCE_CACHE_TTL_SECONDS:-1800}", compose)
        self.assertIn("DOLA_DATABASE_POOL_SIZE=24", env_example)
        self.assertIn('os.environ.get("DOLA_DATABASE_POOL_SIZE") or 24', postgres_source)
        self.assertIn("COALESCE(payload->>'account_status', 'normal') <> 'abnormal'", postgres_source)
        self.assertIn("DOLA_DATABASE_POOL_TIMEOUT: ${DOLA_DATABASE_POOL_TIMEOUT:-3}", compose)
        self.assertIn("DOLA_DATABASE_POOL_TIMEOUT=3", env_example)
        self.assertIn("DOLA_API_MAX_CONNECTIONS: ${DOLA_API_MAX_CONNECTIONS:-512}", compose)
        self.assertIn("DOLA_API_HEALTH_FAILURES_BEFORE_RESTART: ${DOLA_API_HEALTH_FAILURES_BEFORE_RESTART:-5}", compose)
        self.assertIn("DOLA_API_HEALTH_TIMEOUT_SECONDS: ${DOLA_API_HEALTH_TIMEOUT_SECONDS:-4}", compose)
        self.assertIn("DOLA_API_HEALTH_FAILURES_BEFORE_RESTART=5", env_example)
        self.assertIn("DOLA_API_HEALTH_TIMEOUT_SECONDS=4", env_example)
        self.assertIn("DOLA_NOFILE_SOFT_LIMIT:-65535", compose)
        self.assertIn('test: ["CMD", "python", "-m", "app.api_healthcheck"]', compose)
        self.assertIn('"remote_generation_limit": 0', config_source)
        self.assertIn("COPY VERSION ./VERSION", dockerfile)
        self.assertIn("COPY requirements.txt requirements.lock ./", dockerfile)
        self.assertIn("pip install --no-cache-dir -r requirements.lock", dockerfile)
        lock_lines = [line for line in (root / "requirements.lock").read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]
        self.assertTrue(lock_lines)
        self.assertTrue(all("==" in line for line in lock_lines))


if __name__ == "__main__":
    unittest.main()
