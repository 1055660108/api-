from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_test_environment(base_url: str = "", allow_remote: bool = False) -> None:
    environment = str(os.environ.get("DOLA_TEST_ENV") or "").strip().lower()
    if environment not in {"1", "true", "test", "testing"}:
        raise RuntimeError("安全保护：仅允许测试环境，请设置 DOLA_TEST_ENV=test")
    if not base_url:
        return
    hostname = (urlparse(base_url).hostname or "").lower()
    local_names = {"localhost", "127.0.0.1", "::1", socket.gethostname().lower()}
    if hostname not in local_names and not allow_remote:
        raise RuntimeError("安全保护：默认仅允许本机地址，远程测试需显式传入 --allow-remote")


def run_process(command: list[str], cwd: Path, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)


def run_checked(command: list[str], cwd: Path, timeout: float = 120) -> str:
    result = run_process(command, cwd, timeout)
    if result.returncode:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"命令执行失败 ({' '.join(command)}): {detail}")
    return result.stdout.strip()


def parse_size(value: str) -> int:
    match = re.fullmatch(r"\s*([0-9.]+)\s*([kmgt]?i?b)\s*", str(value or ""), re.IGNORECASE)
    if not match:
        return 0
    units = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4, "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}
    return int(float(match.group(1)) * units[match.group(2).lower()])


def parse_pair(value: str) -> tuple[int, int]:
    parts = str(value or "").split("/")
    if len(parts) != 2:
        return 0, 0
    return parse_size(parts[0]), parse_size(parts[1])


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile_value)))
    return ordered[index]


def write_report(output_dir: Path, stem: str, payload: dict[str, Any], markdown: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return json_path, markdown_path


def compose_command(compose_file: Path, *arguments: str) -> list[str]:
    return ["docker", "compose", "-f", str(compose_file), *arguments]


def wait_until(predicate, timeout: float, interval: float = 1.0) -> tuple[bool, Any]:
    deadline = time.monotonic() + timeout
    last_value = None
    while time.monotonic() < deadline:
        try:
            last_value = predicate()
            if last_value:
                return True, last_value
        except Exception as exc:
            last_value = str(exc)
        time.sleep(interval)
    return False, last_value
