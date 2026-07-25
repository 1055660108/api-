from __future__ import annotations

import unittest
from pathlib import Path


class PublicHttpsInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (Path(__file__).resolve().parents[1] / "scripts" / "install_public_https.sh").read_text(encoding="utf-8")

    def test_installer_requires_dns_and_certificate_email(self) -> None:
        self.assertIn('DOMAIN="${1:-huisull.xyz}"', self.script)
        self.assertIn('EXPECTED_IP="${DOLA_PUBLIC_IP:-186.241.120.51}"', self.script)
        self.assertIn('getent ahostsv4 "$DOMAIN"', self.script)
        self.assertIn("尚未解析", self.script)
        self.assertIn("Let's Encrypt", self.script)

    def test_installer_only_proxies_the_loopback_api(self) -> None:
        self.assertIn('UPSTREAM="http://127.0.0.1:${DOLA_PORT:-8088}"', self.script)
        self.assertIn("proxy_pass $UPSTREAM;", self.script)
        self.assertIn("return 302 /client;", self.script)
        self.assertIn("proxy_set_header X-Forwarded-Proto", self.script)
        self.assertIn("proxy_read_timeout 600s", self.script)

    def test_installer_enables_https_redirect_without_touching_data(self) -> None:
        self.assertIn("certbot --nginx", self.script)
        self.assertIn("--redirect", self.script)
        self.assertIn('apply_public_hardening.sh', self.script)
        self.assertNotIn("docker compose down", self.script)
        self.assertNotIn("docker volume", self.script)

    def test_hardening_limits_abnormal_connections_without_reducing_upload_size(self) -> None:
        hardening = (Path(__file__).resolve().parents[1] / "scripts" / "apply_public_hardening.sh").read_text(encoding="utf-8")
        self.assertIn("limit_conn dola_per_ip 80", hardening)
        self.assertIn("rate=60r/s", hardening)
        self.assertIn("burst=180 nodelay", hardening)
        self.assertIn("client_header_timeout 15s", hardening)
        self.assertIn("nginx -t", hardening)
        self.assertIn("systemctl reload nginx", hardening)


if __name__ == "__main__":
    unittest.main()
