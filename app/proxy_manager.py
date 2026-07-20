from __future__ import annotations

import json
import os
import re
import base64
import asyncio
import socket
import subprocess
import time
import hashlib
from urllib.parse import quote, unquote, urlsplit
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
_MIHOMO_CONTROLLER_PORT = 0
_SUBSCRIPTION_CACHE: dict[str, Any] = {"url": "", "nodes": (), "refreshed_at": 0.0}
_SUBSCRIPTION_CACHE_LOCK: asyncio.Lock | None = None
_NODE_DELAYS: dict[str, tuple[int | None, float]] = {}
COUNTRY_MARKERS = {
    "香港": ("香港", "hong kong", "hongkong", " hk", "🇭🇰"),
    "台湾": ("台湾", "taiwan", " taipei", " tw", "🇹🇼"),
    "日本": ("日本", "japan", " tokyo", " osaka", " jp", "🇯🇵"),
    "新加坡": ("新加坡", "singapore", " sg", "🇸🇬"),
    "美国": ("美国", "united states", " los angeles", " san jose", " seattle", " us", "🇺🇸"),
    "韩国": ("韩国", "korea", " seoul", " kr", "🇰🇷"),
    "英国": ("英国", "united kingdom", " london", " uk", "🇬🇧"),
    "德国": ("德国", "germany", " frankfurt", " de", "🇩🇪"),
    "法国": ("法国", "france", " paris", " fr", "🇫🇷"),
    "加拿大": ("加拿大", "canada", " toronto", " ca", "🇨🇦"),
    "澳大利亚": ("澳大利亚", "australia", " sydney", " au", "🇦🇺"),
}


@dataclass(frozen=True)
class SubscriptionNodes:
    native_proxies: tuple[str, ...]
    tunnel_nodes: tuple[str, ...]


@dataclass(frozen=True)
class ProxyNode:
    id: str
    name: str
    country: str
    protocol: str
    server: str
    port: int
    uri: str


def _node_id(uri: str) -> str:
    return hashlib.sha256(uri.encode("utf-8")).hexdigest()[:16]


def identify_country(name: str, server: str = "") -> str:
    searchable = f" {name} {server} ".lower().replace("_", " ").replace("-", " ")
    for country, markers in COUNTRY_MARKERS.items():
        if any(marker in searchable for marker in markers):
            return country
    return "未知"


def _node_from_uri(uri: str, index: int) -> ProxyNode:
    parsed = urlsplit(uri)
    protocol = parsed.scheme.lower()
    name = unquote(parsed.fragment).strip() or f"{protocol.upper()} 节点 {index}"
    server = str(parsed.hostname or "")
    try:
        port = int(parsed.port or 0)
    except ValueError:
        port = 0
    if protocol == "vmess" and not server:
        try:
            payload = json.loads(base64.b64decode(parsed.netloc + parsed.path + "=" * (-(len(parsed.netloc + parsed.path)) % 4)))
            server = str(payload.get("add") or "")
            port = int(payload.get("port") or 0)
            name = str(payload.get("ps") or name).strip()
        except Exception:
            pass
    return ProxyNode(_node_id(uri), name[:200], identify_country(name, server), protocol, server, port, uri)


def subscription_node_list(text: str) -> tuple[ProxyNode, ...]:
    parsed = parse_subscription_nodes(text)
    values = [*parsed.native_proxies, *parsed.tunnel_nodes]
    nodes = [_node_from_uri(value, index) for index, value in enumerate(values, 1)]
    if nodes:
        return tuple(nodes)
    yaml_nodes: list[ProxyNode] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*-\s*name:\s*['\"]?(.*?)['\"]?\s*$", line)
        if match:
            if current.get("name"):
                uri = f"{current.get('type', 'proxy')}://{current.get('server', '')}:{current.get('port', '0')}#{current['name']}"
                yaml_nodes.append(_node_from_uri(uri, len(yaml_nodes) + 1))
            current = {"name": match.group(1).strip(" '\"")}
            continue
        field = re.match(r"^\s+(type|server|port):\s*['\"]?([^'\"\s]+)", line)
        if current and field:
            current[field.group(1)] = field.group(2)
    if current.get("name"):
        uri = f"{current.get('type', 'proxy')}://{current.get('server', '')}:{current.get('port', '0')}#{current['name']}"
        yaml_nodes.append(_node_from_uri(uri, len(yaml_nodes) + 1))
    return tuple(yaml_nodes)


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


async def _fetch_mihomo_config(subscription_url: str, timeout_seconds: int, port: int, controller_port: int | None = None) -> bytes:
    controller = controller_port or _available_port()
    safe_url = subscription_url.replace("\\", "\\\\").replace("'", "''")
    text = f"""mixed-port: {port}
allow-lan: false
bind-address: 127.0.0.1
external-controller: 127.0.0.1:{controller}
mode: rule
log-level: warning
proxy-providers:
  dola-subscription:
    type: http
    url: '{safe_url}'
    path: ./providers/dola.yaml
    interval: 900
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
proxy-groups:
  - name: DOLA
    type: select
    use:
      - dola-subscription
rules:
  - MATCH,DOLA
"""
    return text.encode("utf-8")


async def _proxy_from_mihomo(subscription_url: str, timeout_seconds: int, refresh_seconds: int) -> dict[str, str]:
    global _MIHOMO_LOCK, _MIHOMO_REFRESHED_AT, _MIHOMO_SUBSCRIPTION_URL, _MIHOMO_CONTROLLER_PORT
    if _MIHOMO_LOCK is None:
        _MIHOMO_LOCK = asyncio.Lock()
    async with _MIHOMO_LOCK:
        if (
            _MIHOMO_PROCESS
            and _MIHOMO_PROCESS.poll() is None
            and _port_is_open(_MIHOMO_PORT)
            and _MIHOMO_SUBSCRIPTION_URL == subscription_url
            and time.monotonic() - _MIHOMO_REFRESHED_AT < refresh_seconds
        ):
            return {"server": f"http://127.0.0.1:{_MIHOMO_PORT}", "node_count": "managed"}
        MIHOMO_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        port = _available_port()
        controller_port = _available_port()
        config_path = MIHOMO_RUNTIME_DIR / "config.yaml"
        config_path.write_bytes(await _fetch_mihomo_config(subscription_url, timeout_seconds, port, controller_port))
        _start_mihomo(config_path, port)
        _MIHOMO_SUBSCRIPTION_URL = subscription_url
        _MIHOMO_REFRESHED_AT = time.monotonic()
        _MIHOMO_CONTROLLER_PORT = controller_port
        return {"server": f"http://127.0.0.1:{port}", "node_count": "managed"}


async def fetch_subscription_node_list(
    subscription_url: str,
    *,
    timeout_seconds: int = 20,
    refresh_seconds: int = 900,
    force: bool = False,
) -> tuple[ProxyNode, ...]:
    global _SUBSCRIPTION_CACHE_LOCK
    if not subscription_url:
        return ()
    if _SUBSCRIPTION_CACHE_LOCK is None:
        _SUBSCRIPTION_CACHE_LOCK = asyncio.Lock()
    async with _SUBSCRIPTION_CACHE_LOCK:
        fresh = time.monotonic() - float(_SUBSCRIPTION_CACHE["refreshed_at"]) < refresh_seconds
        if not force and fresh and _SUBSCRIPTION_CACHE["url"] == subscription_url:
            return tuple(_SUBSCRIPTION_CACHE["nodes"])
        timeout = httpx.Timeout(float(timeout_seconds), connect=min(10.0, float(timeout_seconds)))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            response = await client.get(subscription_url, headers={"User-Agent": MIHOMO_USER_AGENT})
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"proxy subscription failed with HTTP {response.status_code}")
        if len(response.content) > 5 * 1024 * 1024:
            raise RuntimeError("proxy subscription response is too large")
        nodes = subscription_node_list(response.content.decode("utf-8-sig", errors="replace"))
        if not nodes:
            raise RuntimeError("proxy subscription returned no usable nodes")
        _SUBSCRIPTION_CACHE.update(url=subscription_url, nodes=nodes, refreshed_at=time.monotonic())
        return nodes


async def _native_node_delay(node: ProxyNode, timeout_seconds: float = 5.0) -> int | None:
    if not node.server or not node.port:
        return None
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(node.server, node.port), timeout_seconds)
        writer.close()
        await writer.wait_closed()
        return max(1, round((time.perf_counter() - started) * 1000))
    except (OSError, asyncio.TimeoutError):
        return None


async def _mihomo_node_delay(node: ProxyNode, timeout_seconds: float = 8.0) -> int | None:
    if not _MIHOMO_CONTROLLER_PORT:
        return None
    timeout = httpx.Timeout(timeout_seconds)
    endpoint = f"http://127.0.0.1:{_MIHOMO_CONTROLLER_PORT}/proxies/{quote(node.name, safe='')}/delay"
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(endpoint, params={"url": "https://www.gstatic.com/generate_204", "timeout": round(timeout_seconds * 1000)})
        if response.status_code == 200:
            return int(response.json().get("delay") or 0) or None
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    return None


async def measure_node_delays(nodes: tuple[ProxyNode, ...], subscription_url: str, timeout_seconds: int = 20) -> dict[str, int | None]:
    if any(node.protocol not in {"http", "https", "socks5", "socks5h"} for node in nodes) and not _MIHOMO_CONTROLLER_PORT:
        await _proxy_from_mihomo(subscription_url, timeout_seconds, 900)
    semaphore = asyncio.Semaphore(20)

    async def measure(node: ProxyNode) -> tuple[str, int | None]:
        async with semaphore:
            if node.protocol in {"http", "https", "socks5", "socks5h"}:
                delay = await _native_node_delay(node)
            else:
                delay = await _mihomo_node_delay(node)
            _NODE_DELAYS[node.id] = (delay, time.monotonic())
            return node.id, delay

    return dict(await asyncio.gather(*(measure(node) for node in nodes)))


async def _select_mihomo_node(node: ProxyNode) -> None:
    if not _MIHOMO_CONTROLLER_PORT:
        raise RuntimeError("mihomo controller is not available")
    endpoint = f"http://127.0.0.1:{_MIHOMO_CONTROLLER_PORT}/proxies/DOLA"
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.put(endpoint, json={"name": node.name})
    if response.status_code not in {200, 204}:
        raise RuntimeError(f"mihomo node selection failed with HTTP {response.status_code}")


def node_payload(node: ProxyNode, selected_node: str = "") -> dict[str, Any]:
    delay, measured_at = _NODE_DELAYS.get(node.id, (None, 0.0))
    return {
        "id": node.id,
        "name": node.name,
        "country": node.country,
        "protocol": node.protocol,
        "server": node.server,
        "port": node.port,
        "latency_ms": delay,
        "latency_measured": measured_at > 0,
        "selected": node.id == selected_node,
    }


async def resolve_subscription_proxy(
    subscription_url: str,
    *,
    timeout_seconds: int = 20,
    scheme: str = "http",
    refresh_seconds: int = 900,
    auto_select: bool = True,
    selected_node: str = "",
) -> dict[str, str]:
    nodes = await fetch_subscription_node_list(
        subscription_url,
        timeout_seconds=timeout_seconds,
        refresh_seconds=refresh_seconds,
    )
    chosen = next((node for node in nodes if node.id == selected_node), None)
    if auto_select:
        if not any(_NODE_DELAYS.get(node.id, (None, 0.0))[0] is not None for node in nodes):
            await measure_node_delays(nodes, subscription_url, timeout_seconds)
        available = [(delay, node) for node in nodes if (delay := _NODE_DELAYS.get(node.id, (None, 0.0))[0]) is not None]
        chosen = min(available, key=lambda item: item[0])[1] if available else chosen or nodes[0]
    elif chosen is None:
        raise RuntimeError("selected proxy node is unavailable")
    if chosen.protocol in {"http", "https", "socks5", "socks5h"}:
        server = chosen.uri.split("#", 1)[0]
        return {
            "server": server,
            "host_port": server.rsplit("//", 1)[-1],
            "node_count": str(len(nodes)),
            "node_id": chosen.id,
            "node_name": chosen.name,
        }
    managed = await _proxy_from_mihomo(subscription_url, timeout_seconds, refresh_seconds)
    await _select_mihomo_node(chosen)
    return {**managed, "node_count": str(len(nodes)), "node_id": chosen.id, "node_name": chosen.name}


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
    auto_select: bool = True,
    selected_node: str = "",
) -> dict[str, str]:
    return await resolve_subscription_proxy(
        subscription_url,
        timeout_seconds=timeout_seconds,
        scheme=scheme,
        refresh_seconds=refresh_seconds,
        auto_select=auto_select,
        selected_node=selected_node,
    )
