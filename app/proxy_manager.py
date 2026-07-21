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
import tempfile
from urllib.parse import quote, unquote, urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx
import yaml


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
_MIHOMO_SNAPSHOT_DIGEST = ""
_MIHOMO_CONFIG_PATH: Path | None = None
_SUBSCRIPTION_CACHE: dict[str, Any] = {"url": "", "nodes": (), "snapshot": b"", "provider": b"", "refreshed_at": 0.0}
_SUBSCRIPTION_CACHE_LOCK: asyncio.Lock | None = None
_NODE_DELAYS: dict[str, tuple[int | None, float]] = {}
NODE_DELAY_TTL_SECONDS = 300
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


def _subscription_sources(text: str) -> tuple[str, ...]:
    cleaned = str(text or "").replace("\ufeff", "").strip()
    if not cleaned:
        raise RuntimeError("proxy subscription returned empty response")
    sources = [cleaned]
    compact = re.sub(r"\s+", "", cleaned)
    padded = compact + "=" * (-len(compact) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            decoded = decoder(padded).decode("utf-8")
        except (ValueError, UnicodeDecodeError, base64.binascii.Error):
            continue
        if decoded.strip() and decoded.strip() != cleaned:
            sources.insert(0, decoded.strip())
            break
    return tuple(sources)


def _clash_nodes(source: str) -> tuple[ProxyNode, ...]:
    try:
        document = yaml.safe_load(source)
    except yaml.YAMLError:
        return ()
    if not isinstance(document, dict) or not isinstance(document.get("proxies"), list):
        return ()
    nodes: list[ProxyNode] = []
    for item in document["proxies"]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        protocol = str(item.get("type") or "").strip().lower()
        server = str(item.get("server") or "").strip()
        try:
            port = int(item.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not name or not protocol or not server or port < 1 or port > 65535:
            continue
        uri = f"{protocol}://{server}:{port}#{quote(name)}"
        nodes.append(_node_from_uri(uri, len(nodes) + 1))
    return tuple(nodes)


def subscription_node_list(text: str) -> tuple[ProxyNode, ...]:
    collected: list[ProxyNode] = []
    seen: set[str] = set()
    for source in _subscription_sources(text):
        parsed = parse_subscription_nodes(source)
        values = [*parsed.native_proxies, *parsed.tunnel_nodes]
        nodes = tuple(_node_from_uri(value, index) for index, value in enumerate(values, 1)) if values else _clash_nodes(source)
        for node in nodes:
            if node.id not in seen:
                seen.add(node.id)
                collected.append(node)
        if collected:
            break
    return tuple(collected)


def _provider_snapshot(snapshot: bytes) -> bytes:
    text = snapshot.decode("utf-8-sig", errors="replace")
    for source in _subscription_sources(text):
        try:
            document = yaml.safe_load(source)
        except yaml.YAMLError:
            continue
        if isinstance(document, dict) and isinstance(document.get("proxies"), list):
            return yaml.safe_dump({"proxies": document["proxies"]}, allow_unicode=True, sort_keys=False).encode("utf-8")
    return text.encode("utf-8")


def _mihomo_config(provider: bytes, port: int, controller_port: int) -> bytes:
    try:
        document = yaml.safe_load(provider.decode("utf-8-sig", errors="replace"))
    except yaml.YAMLError as exc:
        raise RuntimeError("proxy subscription is not a valid Clash configuration") from exc
    proxies = document.get("proxies") if isinstance(document, dict) else None
    if not isinstance(proxies, list) or not proxies:
        raise RuntimeError("proxy subscription cannot generate a local Mihomo configuration")
    config = {
        "mixed-port": port,
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "external-controller": f"127.0.0.1:{controller_port}",
        "mode": "rule",
        "log-level": "warning",
        "proxies": proxies,
        "proxy-groups": [{"name": "DOLA", "type": "select", "proxies": [str(item.get("name")) for item in proxies if isinstance(item, dict) and item.get("name")]}],
        "rules": ["MATCH,DOLA"],
    }
    return yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


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
    sources = _subscription_sources(text)
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


def _stop_mihomo() -> None:
    global _MIHOMO_PROCESS
    if not _MIHOMO_PROCESS or _MIHOMO_PROCESS.poll() is not None:
        return
    _MIHOMO_PROCESS.terminate()
    try:
        _MIHOMO_PROCESS.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _MIHOMO_PROCESS.kill()
        _MIHOMO_PROCESS.wait(timeout=5)


def _launch_mihomo(config_path: Path, port: int, controller_port: int) -> subprocess.Popen:
    if not MIHOMO_EXECUTABLE.exists():
        raise RuntimeError(f"mihomo executable not found: {MIHOMO_EXECUTABLE}")
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
        if _port_is_open(port) and _port_is_open(controller_port):
            return process
        time.sleep(0.2)
    process.terminate()
    raise RuntimeError("mihomo proxy startup timed out")


def _replace_mihomo(process: subprocess.Popen, port: int) -> None:
    global _MIHOMO_PROCESS, _MIHOMO_PORT
    previous = _MIHOMO_PROCESS
    _MIHOMO_PROCESS = process
    _MIHOMO_PORT = port
    if previous and previous.poll() is None:
        previous.terminate()
        try:
            previous.wait(timeout=5)
        except subprocess.TimeoutExpired:
            previous.kill()
            previous.wait(timeout=5)


async def _fetch_mihomo_config(subscription_url: str, timeout_seconds: int, port: int, controller_port: int | None = None) -> bytes:
    controller = controller_port or _available_port()
    provider = bytes(_SUBSCRIPTION_CACHE.get("provider") or b"")
    return _mihomo_config(provider, port, controller)


async def _mihomo_group_ready(controller_port: int, timeout_seconds: float = 2.0) -> bool:
    if not _port_is_open(controller_port):
        return False
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
            response = await client.get(f"http://127.0.0.1:{controller_port}/proxies/{quote('DOLA', safe='')}")
        return response.status_code == 200 and str(response.json().get("name") or "") == "DOLA"
    except (httpx.HTTPError, ValueError, TypeError):
        return False


async def _mihomo_ready(process: subprocess.Popen | None, port: int, controller_port: int) -> bool:
    return bool(process and process.poll() is None and _port_is_open(port) and _port_is_open(controller_port) and await _mihomo_group_ready(controller_port))


async def _proxy_from_mihomo(subscription_url: str, timeout_seconds: int, refresh_seconds: int, force_rebuild: bool = False) -> dict[str, str]:
    global _MIHOMO_LOCK, _MIHOMO_REFRESHED_AT, _MIHOMO_SUBSCRIPTION_URL, _MIHOMO_CONTROLLER_PORT, _MIHOMO_SNAPSHOT_DIGEST, _MIHOMO_CONFIG_PATH
    if _MIHOMO_LOCK is None:
        _MIHOMO_LOCK = asyncio.Lock()
    async with _MIHOMO_LOCK:
        provider = bytes(_SUBSCRIPTION_CACHE.get("provider") or b"")
        if _SUBSCRIPTION_CACHE.get("url") != subscription_url or not provider:
            await fetch_subscription_node_list(subscription_url, timeout_seconds=timeout_seconds, refresh_seconds=refresh_seconds)
            provider = bytes(_SUBSCRIPTION_CACHE.get("provider") or b"")
        digest = hashlib.sha256(provider).hexdigest()
        if (
            not force_rebuild
            and await _mihomo_ready(_MIHOMO_PROCESS, _MIHOMO_PORT, _MIHOMO_CONTROLLER_PORT)
            and _MIHOMO_SUBSCRIPTION_URL == subscription_url
            and _MIHOMO_SNAPSHOT_DIGEST == digest
        ):
            return {"server": f"http://127.0.0.1:{_MIHOMO_PORT}", "node_count": "managed"}
        MIHOMO_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        port = _available_port()
        controller_port = _available_port()
        config_path = MIHOMO_RUNTIME_DIR / f"config-{digest[:12]}-{port}.yaml"
        _atomic_write(config_path, _mihomo_config(provider, port, controller_port))
        process = _launch_mihomo(config_path, port, controller_port)
        if not await _mihomo_ready(process, port, controller_port):
            process.terminate()
            raise RuntimeError("mihomo DOLA proxy group is unavailable")
        previous_config = _MIHOMO_CONFIG_PATH
        _replace_mihomo(process, port)
        _MIHOMO_SUBSCRIPTION_URL = subscription_url
        _MIHOMO_REFRESHED_AT = time.monotonic()
        _MIHOMO_CONTROLLER_PORT = controller_port
        _MIHOMO_SNAPSHOT_DIGEST = digest
        _MIHOMO_CONFIG_PATH = config_path
        if previous_config and previous_config != config_path and previous_config.exists():
            previous_config.unlink()
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
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, max_redirects=5, trust_env=False) as client:
                response = await client.get(subscription_url, headers={"User-Agent": MIHOMO_USER_AGENT})
        except httpx.TooManyRedirects as exc:
            raise RuntimeError("proxy subscription exceeded redirect limit") from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError("proxy subscription request timed out") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"proxy subscription network error: {exc}") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"proxy subscription failed with HTTP {response.status_code}")
        if len(response.content) > 5 * 1024 * 1024:
            raise RuntimeError("proxy subscription response is too large")
        snapshot = bytes(response.content)
        nodes = subscription_node_list(snapshot.decode("utf-8-sig", errors="replace"))
        if not nodes:
            raise RuntimeError("proxy subscription returned no usable nodes")
        provider = _provider_snapshot(snapshot)
        _SUBSCRIPTION_CACHE.update(url=subscription_url, nodes=nodes, snapshot=snapshot, provider=provider, refreshed_at=time.monotonic())
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
    if any(node.protocol not in {"http", "https", "socks5", "socks5h"} for node in nodes) and not await _mihomo_ready(_MIHOMO_PROCESS, _MIHOMO_PORT, _MIHOMO_CONTROLLER_PORT):
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
    if not await _mihomo_ready(_MIHOMO_PROCESS, _MIHOMO_PORT, _MIHOMO_CONTROLLER_PORT):
        raise RuntimeError("mihomo controller is not available")
    endpoint = f"http://127.0.0.1:{_MIHOMO_CONTROLLER_PORT}/proxies/DOLA"
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.put(endpoint, json={"name": node.name})
    if response.status_code not in {200, 204}:
        raise RuntimeError(f"mihomo node selection failed with HTTP {response.status_code}")
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        current = await client.get(endpoint)
    if current.status_code != 200 or str(current.json().get("now") or "") != node.name:
        raise RuntimeError("mihomo node selection did not take effect")


async def activate_mihomo_node(node: ProxyNode, subscription_url: str, timeout_seconds: int = 20, refresh_seconds: int = 900) -> None:
    if node.protocol in {"http", "https", "socks5", "socks5h"}:
        return
    await _proxy_from_mihomo(subscription_url, timeout_seconds, refresh_seconds)
    await _select_mihomo_node(node)


async def rebuild_mihomo_from_snapshot(subscription_url: str, nodes: tuple[ProxyNode, ...], timeout_seconds: int = 20, refresh_seconds: int = 900) -> None:
    _NODE_DELAYS.clear()
    if any(node.protocol not in {"http", "https", "socks5", "socks5h"} for node in nodes):
        await _proxy_from_mihomo(subscription_url, timeout_seconds, refresh_seconds, force_rebuild=True)


def node_payload(node: ProxyNode, selected_node: str = "") -> dict[str, Any]:
    delay, measured_at = _NODE_DELAYS.get(node.id, (None, 0.0))
    fresh = measured_at > 0 and time.monotonic() - measured_at < NODE_DELAY_TTL_SECONDS
    return {
        "id": node.id,
        "name": node.name,
        "country": node.country,
        "protocol": node.protocol,
        "server": node.server,
        "port": node.port,
        "latency_ms": delay if fresh else None,
        "latency_measured": fresh,
        "latency_status": "available" if fresh and delay is not None else "unavailable" if fresh else "expired" if measured_at else "pending",
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
        fresh_delays = {
            node.id: delay
            for node in nodes
            if (delay := _NODE_DELAYS.get(node.id, (None, 0.0))[0]) is not None
            and time.monotonic() - _NODE_DELAYS[node.id][1] < NODE_DELAY_TTL_SECONDS
        }
        if not fresh_delays:
            await measure_node_delays(nodes, subscription_url, timeout_seconds)
        available = [
            (delay, node)
            for node in nodes
            if (delay := _NODE_DELAYS.get(node.id, (None, 0.0))[0]) is not None
            and time.monotonic() - _NODE_DELAYS[node.id][1] < NODE_DELAY_TTL_SECONDS
        ]
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
