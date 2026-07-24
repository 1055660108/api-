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

        self.assertEqual(version, "1.4.12")
        self.assertEqual(__version__, version)
        self.assertIn(f"DOLA_IMAGE_TAG:-{version}", compose)
        self.assertIn("DOLA_DATABASE_POOL_SIZE: ${DOLA_DATABASE_POOL_SIZE:-24}", compose)
        self.assertIn("DOLA_DATABASE_POOL_SIZE=24", env_example)
        self.assertIn('os.environ.get("DOLA_DATABASE_POOL_SIZE") or 24', postgres_source)
        self.assertIn("COPY VERSION ./VERSION", dockerfile)


if __name__ == "__main__":
    unittest.main()
