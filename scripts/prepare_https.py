from __future__ import annotations

import argparse
import ipaddress
import json
import re
from pathlib import Path


DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")


def validate_domain(value: str) -> str:
    domain = value.strip().lower()
    if not DOMAIN_RE.fullmatch(domain):
        raise ValueError("domain 必须是有效的完整域名")
    return domain


def validate_cidrs(values: list[str]) -> list[str]:
    cidrs = [str(ipaddress.ip_network(value.strip(), strict=False)) for value in values if value.strip()]
    if not cidrs:
        raise ValueError("至少需要一个 IP 白名单网段")
    if any(item in {"0.0.0.0/0", "::/0"} for item in cidrs):
        raise ValueError("IP 白名单禁止使用全网开放网段")
    return list(dict.fromkeys(cidrs))


def validate_certificate(path: Path, marker: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"文件不存在：{resolved}")
    content = resolved.read_text(encoding="utf-8", errors="ignore")
    if marker not in content:
        raise ValueError(f"文件格式不正确：{resolved}")
    return resolved


def render_nginx(domain: str, certificate: Path, private_key: Path, cidrs: list[str], upstream: str) -> str:
    allow_rules = "\n".join(f"        allow {cidr};" for cidr in cidrs)
    return f"""server {{
    listen 80;
    listen [::]:80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name {domain};
    ssl_certificate {certificate};
    ssl_certificate_key {private_key};
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_timeout 1d;
    ssl_session_cache shared:DOLA:10m;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    client_max_body_size 50m;

    location / {{
{allow_rules}
        deny all;
        proxy_pass {upstream};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 300s;
    }}
}}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="生成带 IP 白名单的 Nginx HTTPS 配置")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--allow-cidr", action="append", required=True)
    parser.add_argument("--upstream", default="http://127.0.0.1:8088")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        domain = validate_domain(args.domain)
        cidrs = validate_cidrs(args.allow_cidr)
        certificate = validate_certificate(args.certificate, "BEGIN CERTIFICATE")
        private_key = validate_certificate(args.private_key, "PRIVATE KEY")
        if not args.upstream.startswith("http://127.0.0.1:") and not args.upstream.startswith("http://localhost:"):
            raise ValueError("upstream 仅允许本机 HTTP 服务")
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_nginx(domain, certificate, private_key, cidrs, args.upstream), encoding="utf-8")
        output.chmod(0o640)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({"ok": True, "domain": domain, "allow_cidrs": cidrs, "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
