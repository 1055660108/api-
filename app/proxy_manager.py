from __future__ import annotations

import json
import os
import re
import base64
import asyncio
import random
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx


PROXY_LINE_RE = re.compile(r"(?:(?:https?|socks5h?)://)?([A-Za-z0-9.-]+:\d{2,5})")
NATIVE_PROXY_LINE_RE = re.compile(r"^(https?|socks5h?)://([^\s]+)$", re.IGNORECASE)
SUBSCRIPTION_NODE_RE = re.compile(r"^(?:vless|vmess|trojan|ss|ssr|hysteria2?|hy2|tuic|anytls)://", re.IGNORECASE)
MIHOMO_USER_AGENT = "ClashMetaForAndroid/2.11.14.Meta"
MIHOMO_EXECUTABLE = Path(__file__).resolve().parent.parent / "bin" / (
    "mihomo-windows-amd64-compatible.exe" if os.name == "nt" else "mihomo"
)
MIHOMO_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "data" / "proxy_runtime"
_MIHOMO_PROCESS: subprocess.Popen | None = None
_MIHOMO_PORT = 0
_MIHOMO_REFRESHED_AT = 0.0
_MIHOMO_SUBSCRIPTION_URL = ""
_MIHOMO_LOCK: asyncio.Lock | None = None


@dataclass(frozen=True)
class SubscriptionNodes:
    native_proxies: tuple[str, ...]
    tunnel_nodes: tuple[str, ...]


def _proxy_candidates(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _proxy_candidates(item)
        return
    if isinstance(value, dict):
        host = value.get("ip") or value.get("host") or value.get("server")
        port = value.get("port")
        if host and port:
            yield f"{host}:{port}"
        for item in value.values():
            yield from _proxy_candidates(item)


def parse_proxy_api_response(text: str) -> str:
    cleaned = str(text or "").replace("\ufeff", "").strip()
    if not cleaned:
        raise RuntimeError("proxy api returned empty response")

    candidates: list[str] = []
    try:
        candidates.extend(_proxy_candidates(json.loads(cleaned)))
    except Exception:
        pass
    candidates.extend(cleaned.splitlines())

    for item in candidates:
        match = PROXY_LINE_RE.search(str(item).strip())
        if match:
            return match.group(1)

    preview = cleaned[:300].replace("\n", "\\n")
    raise RuntimeError(f"proxy api returned no usable ip:port: {preview}")


def parse_proxy_subscription(text: str) -> list[str]:
    parsed = parse_subscription_nodes(text)
    if parsed.native_proxies:
        return list(parsed.native_proxies)
    if parsed.tunnel_nodes:
        raise RuntimeError("proxy subscription contains tunnel nodes that require mihomo")
    raise RuntimeError("proxy subscription returned no usable nodes")


def parse_subscription_nodes(text: str) -> SubscriptionNodes:
    cleaned = str(text or "").replace("\ufeff", "").strip()
    if not cleaned:
        raise RuntimeError("proxy subscription returned empty response")
    sources = [cleaned]
    compact = re.sub(r"\s+", "", cleaned)
    try:
        decoded = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=False).decode("utf-8", errors="ignore")
        if decoded.strip() and decoded.strip() != cleaned:
            sources.append(decoded)
    except Exception:
        pass
    native_proxies: list[str] = []
    tunnel_nodes: list[str] = []
    seen_native: set[str] = set()
    seen_tunnel: set[str] = set()
    for source in sources:
        for raw_line in source.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            native_match = NATIVE_PROXY_LINE_RE.match(line)
            if native_match:
                proxy = f"{native_match.group(1).lower()}://{native_match.group(2)}"
                if proxy not in seen_native:
                    seen_native.add(proxy)
                    native_proxies.append(proxy)
                continue
            if SUBSCRIPTION_NODE_RE.match(line) and line not in seen_tunnel:
                seen_tunnel.add(line)
                tunnel_nodes.append(line)
    return SubscriptionNodes(tuple(native_proxies), tuple(tunnel_nodes))


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _port_is_open(port: int) -> bool:
    if port <= 0:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.2)
        return client.connect_ex(("127.0.0.1", port)) == 0


def _start_mihomo(config_path: Path, port: int) -> None:
    global _MIHOMO_PROCESS, _MIHOMO_PORT
    if not MIHOMO_EXECUTABLE.exists():
        raise RuntimeError(f"mihomo executable not found: {MIHOMO_EXECUTABLE}")
    if _MIHOMO_PROCESS and _MIHOMO_PROCESS.poll() is None:
        _MIHOMO_PROCESS.terminate()
        try:
            _MIHOMO_PROCESS.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _MIHOMO_PROCESS.kill()
            _MIHOMO_PROCESS.wait(timeout=5)
    process = subprocess.Popen(
        [str(MIHOMO_EXECUTABLE), "-d", str(MIHOMO_RUNTIME_DIR), "-f", str(config_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"mihomo exited with code {process.returncode}")
        if _port_is_open(port):
            _MIHOMO_PROCESS = process
            _MIHOMO_PORT = port
            return
        time.sleep(0.2)
    process.terminate()
    raise RuntimeError("mihomo proxy startup timed out")


async def _fetch_mihomo_config(subscription_url: str, timeout_seconds: int, port: int) -> bytes:
    timeout = httpx.Timeout(float(timeout_seconds), connect=min(10.0, float(timeout_seconds)))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        response = await client.get(subscription_url, headers={"User-Agent": MIHOMO_USER_AGENT})
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"proxy subscription failed with HTTP {response.status_code}")
    if len(response.content) > 5 * 1024 * 1024:
        raise RuntimeError("proxy subscription response is too large")
    text = response.content.decode("utf-8-sig", errors="replace")
    if "proxies:" not in text:
        raise RuntimeError("proxy subscription did not return mihomo configuration")
    directives = {
        "mixed-port": str(port),
        "allow-lan": "false",
        "bind-address": "127.0.0.1",
        "external-controller": "''",
    }
    for name, value in directives.items():
        pattern = rf"(?m)^{re.escape(name)}:\s*.*$"
        if re.search(pattern, text):
            text = re.sub(pattern, f"{name}: {value}", text, count=1)
        else:
            text = f"{name}: {value}\n{text}"
    text = re.sub(r"(?m)^\s*fallback-filter:\s*\{[^\n]*\}\s*$", "", text, count=1)
    return text.encode("utf-8")


async def _proxy_from_mihomo(subscription_url: str, timeout_seconds: int, refresh_seconds: int) -> dict[str, str]:
    global _MIHOMO_LOCK, _MIHOMO_REFRESHED_AT, _MIHOMO_SUBSCRIPTION_URL
    if _MIHOMO_LOCK is None:
        _MIHOMO_LOCK = asyncio.Lock()
    async with _MIHOMO_LOCK:
        if (
            _MIHOMO_PROCESS
            and _MIHOMO_PROCESS.poll() is None
            and _port_is_open(_MIHOMO_PORT)
            and _MIHOMO_SUBSCRIPTION_URL == subscription_url
        ):
            return {"server": f"http://127.0.0.1:{_MIHOMO_PORT}", "node_count": "managed"}
        MIHOMO_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        port = _available_port()
        config_path = MIHOMO_RUNTIME_DIR / "config.yaml"
        config_path.write_bytes(await _fetch_mihomo_config(subscription_url, timeout_seconds, port))
        _start_mihomo(config_path, port)
        _MIHOMO_SUBSCRIPTION_URL = subscription_url
        _MIHOMO_REFRESHED_AT = time.monotonic()
        return {"server": f"http://127.0.0.1:{port}", "node_count": "managed"}


async def fetch_proxy_from_api(api_url: str, *, timeout_seconds: int = 20, scheme: str = "http") -> dict[str, str]:
    if not api_url:
        raise RuntimeError("proxy api url is empty")

    timeout = httpx.Timeout(float(timeout_seconds), connect=min(10.0, float(timeout_seconds)))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False) as client:
        response = await client.get(api_url, headers={"User-Agent": "dola-fetch-service/1.0"})

    text = response.content.decode("utf-8-sig", errors="replace")
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"proxy api failed with HTTP {response.status_code}: {text[:300]}")

    host_port = parse_proxy_api_response(text)
    normalized_scheme = scheme if scheme in {"http", "https", "socks5", "socks5h"} else "http"
    return {
        "server": f"{normalized_scheme}://{host_port}",
        "host_port": host_port,
        "raw": text.strip()[:1000],
    }


async def fetch_proxy_from_subscription(
    subscription_url: str,
    *,
    timeout_seconds: int = 20,
    scheme: str = "http",
    refresh_seconds: int = 900,
) -> dict[str, str]:
    if not subscription_url:
        raise RuntimeError("proxy subscription url is empty")
    timeout = httpx.Timeout(float(timeout_seconds), connect=min(10.0, float(timeout_seconds)))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        response = await client.get(subscription_url, headers={"User-Agent": "dola-fetch-service/1.0"})
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"proxy subscription failed with HTTP {response.status_code}")
    if len(response.content) > 5 * 1024 * 1024:
        raise RuntimeError("proxy subscription response is too large")
    parsed = parse_subscription_nodes(response.content.decode("utf-8-sig", errors="replace"))
    if parsed.tunnel_nodes:
        return await _proxy_from_mihomo(subscription_url, timeout_seconds, refresh_seconds)
    if not parsed.native_proxies:
        raise RuntimeError("proxy subscription returned no usable nodes")
    normalized_scheme = scheme if scheme in {"http", "https", "socks5", "socks5h"} else "http"
    selected = random.choice(parsed.native_proxies)
    if "://" not in selected:
        selected = f"{normalized_scheme}://{selected}"
    return {"server": selected, "host_port": selected.rsplit("//", 1)[-1], "node_count": str(len(parsed.native_proxies))}
