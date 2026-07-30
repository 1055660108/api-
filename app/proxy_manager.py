from __future__ import annotations

import json
import math
import os
import re
import base64
import asyncio
import socket
import secrets
import subprocess
import time
import hashlib
import ipaddress
import tempfile
from urllib.parse import quote, unquote, urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx
import yaml

from .config import DATA_DIR


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
_MIHOMO_SELECTED_NODE_ID = ""
_SUBSCRIPTION_CACHE: dict[str, Any] = {"url": "", "nodes": (), "snapshot": b"", "provider": b"", "refreshed_at": 0.0}
_SUBSCRIPTION_CACHE_LOCK: asyncio.Lock | None = None
_SUBSCRIPTION_RESOLVE_LOCK: asyncio.Lock | None = None
_TASK_MIHOMO_CONDITION: asyncio.Condition | None = None
_TASK_MIHOMO_SLOTS: list["_TaskMihomoSlot"] = []
_PROXY_EXIT_CACHE: dict[str, tuple[str, float]] = {}
_NODE_DELAYS: dict[str, tuple[int | None, float]] = {}
_NODE_LAST_GOOD: dict[str, tuple[int, float]] = {}
_NODE_DOLA_HEALTH: dict[str, tuple[bool, float]] = {}
_NODE_COOLDOWNS: dict[str, tuple[float, str]] = {}
_NODE_GATEWAY_FAILURES: dict[str, tuple[int, float]] = {}
_PROXY_SOURCE_FAILURES: dict[str, float] = {}
_NODE_DELAYS_LOADED = False
NODE_DELAYS_PATH = DATA_DIR / "proxy_node_delays.json"
NODE_DELAY_TTL_SECONDS = 300
NODE_FAILURE_COOLDOWN_SECONDS = 90
NODE_SERVICE_FREQUENT_COOLDOWN_SECONDS = 600
NODE_GATEWAY_FAILURE_COOLDOWN_SECONDS = 300
NODE_GATEWAY_FAILURE_WINDOW_SECONDS = 300
NODE_GATEWAY_FAILURE_THRESHOLD = 2
PROXY_SOURCE_FAILURE_COOLDOWN_SECONDS = 90
DOLA_HEALTH_TTL_SECONDS = 300
DOLA_HEALTHCHECK_URL = "https://www.dola.com/"
DOLA_HEALTHCHECK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/149.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.environ.get(name) or default)))
    except (TypeError, ValueError):
        return default


TASK_MIHOMO_MAX_SLOTS = _bounded_env_int("DOLA_MIHOMO_EXIT_SLOTS", 12, 1, 12)
TASK_MIHOMO_CONTEXTS_PER_EXIT = _bounded_env_int("DOLA_MIHOMO_CONTEXTS_PER_EXIT", 4, 1, 4)
PROXY_EXIT_CACHE_SECONDS = 30 * 60
PROXY_EXIT_CHECK_URL = "https://api.ipify.org"
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


@dataclass(eq=False)
class _TaskMihomoSlot:
    slot_id: str
    node_id: str
    node_name: str
    subscription_url: str
    snapshot_digest: str
    process: subprocess.Popen | None = None
    port: int = 0
    controller_port: int = 0
    config_path: Path | None = None
    server: str = ""
    proxy_mode: str = "mihomo"
    active: int = 1
    exit_id: str = ""
    launching: bool = True
    retiring: bool = False


def _node_id(uri: str) -> str:
    return hashlib.sha256(uri.encode("utf-8")).hexdigest()[:16]


def _load_persisted_node_delays() -> None:
    global _NODE_DELAYS_LOADED
    if _NODE_DELAYS_LOADED:
        return
    _NODE_DELAYS_LOADED = True
    try:
        payload = json.loads(NODE_DELAYS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    now_wall = time.time()
    now_mono = time.monotonic()
    for node_id, item in (payload.get("nodes", {}) if isinstance(payload, dict) else {}).items():
        if not isinstance(item, dict):
            continue
        try:
            delay = max(1, int(item.get("latency_ms") or 0))
            measured_at = float(item.get("measured_at") or 0)
        except (TypeError, ValueError):
            continue
        if not node_id or not measured_at:
            continue
        age = max(0.0, now_wall - measured_at)
        monotonic_at = now_mono - age
        _NODE_LAST_GOOD[str(node_id)] = (delay, measured_at)
        _NODE_DELAYS.setdefault(str(node_id), (delay, monotonic_at))


def _persist_node_delays() -> None:
    try:
        NODE_DELAYS_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "nodes": {
                node_id: {"latency_ms": delay, "measured_at": measured_at}
                for node_id, (delay, measured_at) in _NODE_LAST_GOOD.items()
            },
            "updated_at": time.time(),
        }
        temporary = NODE_DELAYS_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(NODE_DELAYS_PATH)
    except OSError:
        pass


def mark_node_unavailable(
    node_id: str,
    *,
    cooldown_seconds: int = NODE_FAILURE_COOLDOWN_SECONDS,
    reason: str = "runtime_failure",
) -> None:
    _load_persisted_node_delays()
    normalized = str(node_id or "").strip()
    if normalized:
        now = time.monotonic()
        _NODE_DELAYS[normalized] = (None, now)
        _NODE_DOLA_HEALTH[normalized] = (False, now)
        deadline = now + max(1, int(cooldown_seconds))
        previous_deadline, _ = _NODE_COOLDOWNS.get(normalized, (0.0, ""))
        if deadline >= previous_deadline:
            _NODE_COOLDOWNS[normalized] = (deadline, str(reason or "runtime_failure")[:80])


def node_retry_after(node_id: str) -> int:
    normalized = str(node_id or "").strip()
    if not normalized:
        return 0
    now = time.monotonic()
    deadline, _ = _NODE_COOLDOWNS.get(normalized, (0.0, ""))
    if deadline > now:
        return max(1, math.ceil(deadline - now))
    if deadline:
        _NODE_COOLDOWNS.pop(normalized, None)
    delay, checked_at = _NODE_DELAYS.get(normalized, (0, 0.0))
    if delay is None and checked_at > 0:
        remaining = NODE_FAILURE_COOLDOWN_SECONDS - (now - checked_at)
        return max(0, math.ceil(remaining))
    return 0


def record_node_gateway_failure(node_id: str, status: int) -> bool:
    normalized = str(node_id or "").strip()
    if not normalized or int(status or 0) not in {502, 503, 504}:
        return False
    now = time.monotonic()
    previous_count, previous_at = _NODE_GATEWAY_FAILURES.get(normalized, (0, 0.0))
    count = previous_count + 1 if now - previous_at <= NODE_GATEWAY_FAILURE_WINDOW_SECONDS else 1
    _NODE_GATEWAY_FAILURES[normalized] = (count, now)
    if count < NODE_GATEWAY_FAILURE_THRESHOLD:
        return False
    mark_node_unavailable(
        normalized,
        cooldown_seconds=NODE_GATEWAY_FAILURE_COOLDOWN_SECONDS,
        reason=f"gateway_http_{int(status)}",
    )
    _NODE_GATEWAY_FAILURES.pop(normalized, None)
    return True


def record_node_success(node_id: str) -> None:
    normalized = str(node_id or "").strip()
    if normalized:
        _NODE_GATEWAY_FAILURES.pop(normalized, None)


def mark_proxy_source_unavailable(source: str) -> None:
    normalized = str(source or "").strip().lower()
    if normalized in {"subscription", "account", "api"}:
        _PROXY_SOURCE_FAILURES[normalized] = time.monotonic()


def mark_proxy_source_available(source: str) -> None:
    _PROXY_SOURCE_FAILURES.pop(str(source or "").strip().lower(), None)


def proxy_source_available(source: str) -> bool:
    return proxy_source_retry_after(source) == 0


def proxy_source_retry_after(source: str) -> int:
    failed_at = _PROXY_SOURCE_FAILURES.get(str(source or "").strip().lower(), 0.0)
    if not failed_at:
        return 0
    remaining = PROXY_SOURCE_FAILURE_COOLDOWN_SECONDS - (time.monotonic() - failed_at)
    return max(0, math.ceil(remaining))


def _node_is_cooling_down(node_id: str) -> bool:
    return node_retry_after(node_id) > 0


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
    _terminate_mihomo_process(_MIHOMO_PROCESS)


def _terminate_mihomo_process(process: subprocess.Popen | None) -> None:
    if not process or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _launch_mihomo(config_path: Path, port: int, controller_port: int, runtime_dir: Path | None = None) -> subprocess.Popen:
    if not MIHOMO_EXECUTABLE.exists():
        raise RuntimeError(f"mihomo executable not found: {MIHOMO_EXECUTABLE}")
    working_directory = runtime_dir or MIHOMO_RUNTIME_DIR
    working_directory.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [str(MIHOMO_EXECUTABLE), "-d", str(working_directory), "-f", str(config_path)],
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
    global _MIHOMO_LOCK, _MIHOMO_REFRESHED_AT, _MIHOMO_SUBSCRIPTION_URL, _MIHOMO_CONTROLLER_PORT, _MIHOMO_SNAPSHOT_DIGEST, _MIHOMO_CONFIG_PATH, _MIHOMO_SELECTED_NODE_ID
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
        _MIHOMO_SELECTED_NODE_ID = ""
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


def _dola_response_is_available(response: httpx.Response) -> bool:
    final_url = str(getattr(response, "url", "") or "").lower()
    location = str(response.headers.get("location") or "").lower()
    preview = str(response.text or "")[:8000].lower()
    restricted = any(
        marker in f"{final_url} {location} {preview}"
        for marker in ("region-restricted", "country restricted", "current region is not available", "当前地区不可用")
    )
    status = int(response.status_code)
    return 200 <= status < 500 and status != 407 and not restricted


async def _probe_dola_proxy(server: str, timeout_seconds: float = 8.0) -> tuple[bool, int | None]:
    started = time.perf_counter()
    try:
        timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
        async with httpx.AsyncClient(
            proxy=str(server or ""),
            timeout=timeout,
            follow_redirects=True,
            max_redirects=5,
            trust_env=False,
            headers=DOLA_HEALTHCHECK_HEADERS,
        ) as client:
            response = await client.get(DOLA_HEALTHCHECK_URL)
        elapsed = max(1, round((time.perf_counter() - started) * 1000))
        return _dola_response_is_available(response), elapsed
    except (httpx.HTTPError, RuntimeError, ValueError, TypeError):
        return False, None


async def dola_proxy_available(server: str, timeout_seconds: float = 8.0) -> bool:
    available, _ = await _probe_dola_proxy(server, timeout_seconds)
    return available


async def proxy_exit_identity(server: str, fallback: str, timeout_seconds: float = 5.0) -> str:
    normalized_server = str(server or "").strip()
    normalized_fallback = str(fallback or "proxy").strip()[:120] or "proxy"
    cache_key = hashlib.sha256(normalized_server.encode("utf-8", errors="replace")).hexdigest()
    cached = _PROXY_EXIT_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[1] < PROXY_EXIT_CACHE_SECONDS:
        return cached[0]
    exit_id = f"node:{normalized_fallback}"
    if normalized_server:
        try:
            timeout = httpx.Timeout(timeout_seconds, connect=min(3.0, timeout_seconds))
            async with httpx.AsyncClient(proxy=normalized_server, timeout=timeout, trust_env=False) as client:
                response = await client.get(PROXY_EXIT_CHECK_URL)
            if response.status_code == 200:
                address = str(ipaddress.ip_address(str(response.text or "").strip()))
                exit_id = f"ip:{address}"
        except (httpx.HTTPError, ValueError, TypeError):
            pass
    _PROXY_EXIT_CACHE[cache_key] = (exit_id, time.monotonic())
    if len(_PROXY_EXIT_CACHE) > 512:
        stale = [key for key, (_, checked_at) in _PROXY_EXIT_CACHE.items() if time.monotonic() - checked_at >= PROXY_EXIT_CACHE_SECONDS]
        for key in stale:
            _PROXY_EXIT_CACHE.pop(key, None)
        while len(_PROXY_EXIT_CACHE) > 512:
            _PROXY_EXIT_CACHE.pop(next(iter(_PROXY_EXIT_CACHE)))
    return exit_id


async def probe_dola_proxy(server: str, timeout_seconds: float = 8.0) -> tuple[bool, int | None]:
    return await _probe_dola_proxy(server, timeout_seconds)


async def _node_dola_available(node_id: str, server: str, timeout_seconds: float = 8.0) -> bool:
    normalized = str(node_id or "")
    cached = _NODE_DOLA_HEALTH.get(normalized)
    if cached and cached[0] and time.monotonic() - cached[1] < DOLA_HEALTH_TTL_SECONDS:
        return True
    available, _ = await _probe_dola_proxy(server, timeout_seconds)
    _NODE_DOLA_HEALTH[normalized] = (available, time.monotonic())
    if not available:
        mark_node_unavailable(normalized)
    return available


async def _native_node_delay(node: ProxyNode, timeout_seconds: float = 8.0) -> int | None:
    if not node.server or not node.port:
        return None
    server = node.uri.split("#", 1)[0]
    available, elapsed = await _probe_dola_proxy(server, timeout_seconds)
    _NODE_DOLA_HEALTH[node.id] = (available, time.monotonic())
    return elapsed if available else None


async def _mihomo_node_delay(node: ProxyNode, timeout_seconds: float = 8.0) -> int | None:
    if not _MIHOMO_CONTROLLER_PORT:
        return None
    timeout = httpx.Timeout(timeout_seconds)
    endpoint = f"http://127.0.0.1:{_MIHOMO_CONTROLLER_PORT}/proxies/{quote(node.name, safe='')}/delay"
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(endpoint, params={"url": DOLA_HEALTHCHECK_URL, "timeout": round(timeout_seconds * 1000)})
        if response.status_code == 200:
            return int(response.json().get("delay") or 0) or None
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    return None


async def measure_node_delays(nodes: tuple[ProxyNode, ...], subscription_url: str, timeout_seconds: int = 20) -> dict[str, int | None]:
    _load_persisted_node_delays()
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
            if delay is not None:
                _NODE_LAST_GOOD[node.id] = (delay, time.time())
            return node.id, delay

    measured = dict(await asyncio.gather(*(measure(node) for node in nodes)))
    _persist_node_delays()
    return measured


async def _select_mihomo_node(node: ProxyNode) -> None:
    global _MIHOMO_SELECTED_NODE_ID
    if not await _mihomo_ready(_MIHOMO_PROCESS, _MIHOMO_PORT, _MIHOMO_CONTROLLER_PORT):
        raise RuntimeError("mihomo controller is not available")
    await _select_mihomo_node_on(_MIHOMO_CONTROLLER_PORT, node)
    _MIHOMO_SELECTED_NODE_ID = node.id


async def _select_mihomo_node_on(controller_port: int, node: ProxyNode) -> None:
    endpoint = f"http://127.0.0.1:{int(controller_port)}/proxies/DOLA"
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.put(endpoint, json={"name": node.name})
    if response.status_code not in {200, 204}:
        raise RuntimeError(f"mihomo node selection failed with HTTP {response.status_code}")
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        current = await client.get(endpoint)
    if current.status_code != 200 or str(current.json().get("now") or "") != node.name:
        raise RuntimeError("mihomo node selection did not take effect")


async def _launch_task_mihomo_slot(
    slot: _TaskMihomoSlot,
    node: ProxyNode,
    provider: bytes,
) -> dict[str, str]:
    runtime_dir = MIHOMO_RUNTIME_DIR / f"task-slot-{slot.slot_id}"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    port = _available_port()
    controller_port = _available_port()
    config_path = runtime_dir / "config.yaml"
    _atomic_write(config_path, _mihomo_config(provider, port, controller_port))
    process = await asyncio.to_thread(_launch_mihomo, config_path, port, controller_port, runtime_dir)
    try:
        if not await _mihomo_ready(process, port, controller_port):
            raise RuntimeError("task Mihomo proxy group is unavailable")
        await _select_mihomo_node_on(controller_port, node)
        server = f"http://127.0.0.1:{port}"
        if not await dola_proxy_available(server, 12.0):
            raise RuntimeError("selected task proxy node is unavailable for Dola")
        exit_id = await proxy_exit_identity(server, node.id)
    except Exception:
        await asyncio.to_thread(_terminate_mihomo_process, process)
        raise
    slot.process = process
    slot.port = port
    slot.controller_port = controller_port
    slot.config_path = config_path
    slot.exit_id = exit_id
    slot.server = server
    slot.launching = False
    return {
        "server": server,
        "node_count": "managed",
        "node_id": node.id,
        "node_name": node.name,
        "proxy_mode": "mihomo",
        "mihomo_lease": "1",
        "mihomo_slot_id": slot.slot_id,
        "exit_id": exit_id,
    }


async def _acquire_task_mihomo_proxy(
    nodes: tuple[ProxyNode, ...],
    subscription_url: str,
    *,
    timeout_seconds: int,
    auto_select: bool,
    selected_node: str,
    selected_countries: Iterable[str],
    latency_threshold_ms: int,
    excluded_node_ids: Iterable[str],
) -> dict[str, str]:
    global _TASK_MIHOMO_CONDITION
    if _TASK_MIHOMO_CONDITION is None:
        _TASK_MIHOMO_CONDITION = asyncio.Condition()
    provider = bytes(_SUBSCRIPTION_CACHE.get("provider") or b"")
    if not provider:
        raise RuntimeError("proxy subscription cannot generate a local Mihomo configuration")
    digest = hashlib.sha256(provider).hexdigest()
    base_excluded = {str(item).strip() for item in excluded_node_ids if str(item or "").strip()}
    selected_country_set = {str(item).strip() for item in selected_countries if str(item or "").strip()}
    if auto_select and not selected_country_set:
        raise RuntimeError("automatic proxy selection requires at least one selected country")
    allowed_node_ids = {
        node.id for node in nodes
        if (auto_select and node.country in selected_country_set)
        or (not auto_select and (not selected_node or node.id == selected_node))
    }
    if auto_select and not allowed_node_ids:
        raise RuntimeError("selected proxy countries contain no usable nodes")
    failed_launch_ids: set[str] = set()
    while True:
        retired: list[_TaskMihomoSlot] = []
        async with _TASK_MIHOMO_CONDITION:
            for slot in list(_TASK_MIHOMO_SLOTS):
                stale = slot.subscription_url != subscription_url or slot.snapshot_digest != digest
                unavailable = _node_is_cooling_down(slot.node_id)
                outside_selection = bool(allowed_node_ids) and slot.node_id not in allowed_node_ids
                disconnected = bool(slot.process and slot.process.poll() is not None)
                if stale or unavailable or outside_selection or disconnected:
                    slot.retiring = True
                if slot.retiring and slot.active == 0 and not slot.launching:
                    _TASK_MIHOMO_SLOTS.remove(slot)
                    retired.append(slot)
            if retired:
                await asyncio.gather(*(asyncio.to_thread(_terminate_mihomo_process, slot.process) for slot in retired), return_exceptions=True)

            ready = [
                slot for slot in _TASK_MIHOMO_SLOTS
                if not slot.launching
                and not slot.retiring
                and slot.node_id not in base_excluded
                and (not allowed_node_ids or slot.node_id in allowed_node_ids)
                and slot.active < TASK_MIHOMO_CONTEXTS_PER_EXIT
                and (slot.proxy_mode == "native" or (slot.process is not None and slot.process.poll() is None))
            ]
            active_node_ids = {slot.node_id for slot in _TASK_MIHOMO_SLOTS}
            matching_slots = [
                slot for slot in _TASK_MIHOMO_SLOTS
                if slot.subscription_url == subscription_url and slot.snapshot_digest == digest and not slot.retiring
            ]
            can_launch = len(_TASK_MIHOMO_SLOTS) < TASK_MIHOMO_MAX_SLOTS and (auto_select or not matching_slots)
            chosen: ProxyNode | None = None
            choose_error: Exception | None = None
            if can_launch:
                try:
                    chosen = await _choose_subscription_node(
                        nodes,
                        subscription_url,
                        timeout_seconds=timeout_seconds,
                        auto_select=auto_select,
                        selected_node=selected_node,
                        selected_countries=selected_countries,
                        latency_threshold_ms=latency_threshold_ms,
                        excluded_node_ids=base_excluded | failed_launch_ids | active_node_ids,
                    )
                except Exception as exc:
                    choose_error = exc
            if chosen is None:
                if ready:
                    slot = min(ready, key=lambda item: (item.active, item.slot_id))
                    slot.active += 1
                    proxy = {
                        "server": slot.server or f"http://127.0.0.1:{slot.port}",
                        "node_count": "managed",
                        "node_id": slot.node_id,
                        "node_name": slot.node_name,
                        "proxy_mode": slot.proxy_mode,
                        "mihomo_lease": "1",
                        "mihomo_slot_id": slot.slot_id,
                        "exit_id": slot.exit_id or f"node:{slot.node_id}",
                    }
                    _TASK_MIHOMO_CONDITION.notify_all()
                elif choose_error and not _TASK_MIHOMO_SLOTS:
                    raise choose_error
                else:
                    await _TASK_MIHOMO_CONDITION.wait()
                    continue
            else:
                if chosen.protocol in {"http", "https", "socks5", "socks5h"}:
                    server = chosen.uri.split("#", 1)[0]
                    if not await _node_dola_available(chosen.id, server, min(12.0, float(timeout_seconds))):
                        continue
                    exit_id = await proxy_exit_identity(server, chosen.id)
                    slot = _TaskMihomoSlot(
                        slot_id=secrets.token_hex(6),
                        node_id=chosen.id,
                        node_name=chosen.name,
                        subscription_url=subscription_url,
                        snapshot_digest=digest,
                        server=server,
                        proxy_mode="native",
                        exit_id=exit_id,
                        launching=False,
                    )
                    _TASK_MIHOMO_SLOTS.append(slot)
                    _TASK_MIHOMO_CONDITION.notify_all()
                    return {
                        "server": server,
                        "host_port": server.rsplit("//", 1)[-1],
                        "node_count": str(len(nodes)),
                        "node_id": chosen.id,
                        "node_name": chosen.name,
                        "proxy_mode": "native",
                        "mihomo_lease": "1",
                        "mihomo_slot_id": slot.slot_id,
                        "exit_id": exit_id,
                    }
                slot = _TaskMihomoSlot(
                    slot_id=secrets.token_hex(6),
                    node_id=chosen.id,
                    node_name=chosen.name,
                    subscription_url=subscription_url,
                    snapshot_digest=digest,
                )
                _TASK_MIHOMO_SLOTS.append(slot)
                try:
                    proxy = await _launch_task_mihomo_slot(slot, chosen, provider)
                except Exception:
                    if slot in _TASK_MIHOMO_SLOTS:
                        _TASK_MIHOMO_SLOTS.remove(slot)
                    failed_launch_ids.add(chosen.id)
                    mark_node_unavailable(chosen.id)
                    _TASK_MIHOMO_CONDITION.notify_all()
                    if len(failed_launch_ids | base_excluded) >= len(nodes):
                        raise
                    continue
                _TASK_MIHOMO_CONDITION.notify_all()
        return proxy


async def release_task_mihomo_proxy(proxy: dict[str, str] | None) -> None:
    global _TASK_MIHOMO_CONDITION
    slot_id = str((proxy or {}).get("mihomo_slot_id") or "")
    if not slot_id or _TASK_MIHOMO_CONDITION is None:
        return
    retired: _TaskMihomoSlot | None = None
    async with _TASK_MIHOMO_CONDITION:
        slot = next((item for item in _TASK_MIHOMO_SLOTS if item.slot_id == slot_id), None)
        if not slot:
            return
        slot.active = max(0, slot.active - 1)
        if _node_is_cooling_down(slot.node_id):
            slot.retiring = True
        if slot.active == 0 and slot.retiring:
            _TASK_MIHOMO_SLOTS.remove(slot)
            retired = slot
        _TASK_MIHOMO_CONDITION.notify_all()
    if retired:
        await asyncio.to_thread(_terminate_mihomo_process, retired.process)


async def acquire_authenticated_socks_proxy(
    server: str,
    node_id: str,
    node_name: str,
) -> dict[str, str]:
    """Expose an authenticated SOCKS proxy as a local HTTP proxy for Chromium."""
    global _TASK_MIHOMO_CONDITION
    parsed = urlsplit(str(server or ""))
    scheme = parsed.scheme.lower()
    if scheme not in {"socks5", "socks5h"} or not parsed.hostname or not parsed.port:
        raise RuntimeError("authenticated SOCKS proxy configuration is invalid")
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not username or not password:
        raise RuntimeError("authenticated SOCKS proxy credentials are required")
    normalized_node_id = str(node_id or _node_id(server))
    normalized_name = str(node_name or f"{parsed.hostname}:{parsed.port}")[:200]
    provider = yaml.safe_dump(
        {
            "proxies": [
                {
                    "name": normalized_name,
                    "type": "socks5",
                    "server": parsed.hostname,
                    "port": int(parsed.port),
                    "username": username,
                    "password": password,
                    "udp": False,
                }
            ]
        },
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")
    digest = hashlib.sha256(provider).hexdigest()
    pool_key = f"authenticated-socks:{normalized_node_id}"
    node = ProxyNode(
        normalized_node_id,
        normalized_name,
        identify_country(normalized_name, str(parsed.hostname)),
        scheme,
        str(parsed.hostname),
        int(parsed.port),
        server,
    )
    if _TASK_MIHOMO_CONDITION is None:
        _TASK_MIHOMO_CONDITION = asyncio.Condition()
    while True:
        retired: list[_TaskMihomoSlot] = []
        launch_slot: _TaskMihomoSlot | None = None
        async with _TASK_MIHOMO_CONDITION:
            for slot in list(_TASK_MIHOMO_SLOTS):
                disconnected = bool(slot.process and slot.process.poll() is not None)
                if disconnected or _node_is_cooling_down(slot.node_id):
                    slot.retiring = True
                if slot.retiring and slot.active == 0 and not slot.launching:
                    _TASK_MIHOMO_SLOTS.remove(slot)
                    retired.append(slot)
            ready = [
                slot
                for slot in _TASK_MIHOMO_SLOTS
                if slot.subscription_url == pool_key
                and slot.snapshot_digest == digest
                and not slot.launching
                and not slot.retiring
                and slot.process is not None
                and slot.process.poll() is None
                and slot.active < TASK_MIHOMO_CONTEXTS_PER_EXIT
            ]
            if ready:
                slot = min(ready, key=lambda item: (item.active, item.slot_id))
                slot.active += 1
                _TASK_MIHOMO_CONDITION.notify_all()
                result = {
                    "server": slot.server,
                    "node_count": "managed",
                    "node_id": slot.node_id,
                    "node_name": slot.node_name,
                    "proxy_mode": "mihomo",
                    "mihomo_lease": "1",
                    "mihomo_slot_id": slot.slot_id,
                    "exit_id": slot.exit_id or f"node:{slot.node_id}",
                }
            else:
                if len(_TASK_MIHOMO_SLOTS) >= TASK_MIHOMO_MAX_SLOTS:
                    idle = next(
                        (
                            slot
                            for slot in _TASK_MIHOMO_SLOTS
                            if not slot.launching and slot.active == 0
                        ),
                        None,
                    )
                    if idle is not None:
                        _TASK_MIHOMO_SLOTS.remove(idle)
                        retired.append(idle)
                if len(_TASK_MIHOMO_SLOTS) >= TASK_MIHOMO_MAX_SLOTS:
                    await _TASK_MIHOMO_CONDITION.wait()
                    result = None
                else:
                    launch_slot = _TaskMihomoSlot(
                        slot_id=secrets.token_hex(6),
                        node_id=normalized_node_id,
                        node_name=normalized_name,
                        subscription_url=pool_key,
                        snapshot_digest=digest,
                    )
                    _TASK_MIHOMO_SLOTS.append(launch_slot)
                    result = None
        if retired:
            await asyncio.gather(
                *(asyncio.to_thread(_terminate_mihomo_process, slot.process) for slot in retired),
                return_exceptions=True,
            )
        if result is not None:
            return result
        if launch_slot is None:
            continue
        try:
            proxy = await _launch_task_mihomo_slot(launch_slot, node, provider)
        except Exception:
            async with _TASK_MIHOMO_CONDITION:
                if launch_slot in _TASK_MIHOMO_SLOTS:
                    _TASK_MIHOMO_SLOTS.remove(launch_slot)
                _TASK_MIHOMO_CONDITION.notify_all()
            raise
        async with _TASK_MIHOMO_CONDITION:
            _TASK_MIHOMO_CONDITION.notify_all()
        return proxy


async def shutdown_task_mihomo_pool() -> None:
    global _TASK_MIHOMO_CONDITION
    if _TASK_MIHOMO_CONDITION is None:
        return
    async with _TASK_MIHOMO_CONDITION:
        slots = list(_TASK_MIHOMO_SLOTS)
        _TASK_MIHOMO_SLOTS.clear()
        _TASK_MIHOMO_CONDITION.notify_all()
    await asyncio.gather(*(asyncio.to_thread(_terminate_mihomo_process, slot.process) for slot in slots), return_exceptions=True)


def task_mihomo_pool_snapshot() -> dict[str, Any]:
    slots = list(_TASK_MIHOMO_SLOTS)
    return {
        "slot_limit": TASK_MIHOMO_MAX_SLOTS,
        "contexts_per_exit": TASK_MIHOMO_CONTEXTS_PER_EXIT,
        "slots": len(slots),
        "active": sum(max(0, int(slot.active)) for slot in slots),
        "distinct_nodes": len({slot.node_id for slot in slots if not slot.retiring}),
        "distinct_exits": len({slot.exit_id for slot in slots if slot.exit_id and not slot.retiring}),
    }


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
    _load_persisted_node_delays()
    delay, measured_at = _NODE_DELAYS.get(node.id, (None, 0.0))
    fresh = measured_at > 0 and time.monotonic() - measured_at < NODE_DELAY_TTL_SECONDS
    cached_delay, cached_at = _NODE_LAST_GOOD.get(node.id, (0, 0.0))
    shown_delay = delay if fresh and delay is not None else cached_delay or None
    cached = shown_delay is not None and not (fresh and delay is not None)
    return {
        "id": node.id,
        "name": node.name,
        "country": node.country,
        "protocol": node.protocol,
        "server": node.server,
        "port": node.port,
        "latency_ms": shown_delay,
        "latency_measured": fresh and delay is not None,
        "latency_cached": cached,
        "latency_status": "available" if fresh and delay is not None else "unavailable" if fresh else "cached" if cached else "expired" if measured_at else "pending",
        "selected": node.id == selected_node,
    }


async def _choose_subscription_node(
    nodes: tuple[ProxyNode, ...],
    subscription_url: str,
    *,
    timeout_seconds: int,
    auto_select: bool,
    selected_node: str,
    selected_countries: Iterable[str],
    latency_threshold_ms: int,
    excluded_node_ids: Iterable[str],
) -> ProxyNode:
    countries = {str(item).strip() for item in selected_countries if str(item or "").strip()}
    excluded = {str(item).strip() for item in excluded_node_ids if str(item or "").strip()}
    eligible_nodes = tuple(node for node in nodes if not auto_select or (countries and node.country in countries))
    if not eligible_nodes:
        raise RuntimeError("automatic proxy selection requires at least one selected country" if auto_select and not countries else "selected proxy countries contain no usable nodes")
    selectable_nodes = tuple(node for node in eligible_nodes if node.id not in excluded and not _node_is_cooling_down(node.id))
    if not selectable_nodes:
        raise RuntimeError("no alternative proxy node is currently available" if excluded else "all eligible proxy nodes are temporarily unavailable")
    chosen = next((node for node in selectable_nodes if node.id == selected_node), None)
    if auto_select:
        fresh_delays = {
            node.id: delay
            for node in selectable_nodes
            if (delay := _NODE_DELAYS.get(node.id, (None, 0.0))[0]) is not None
            and time.monotonic() - _NODE_DELAYS[node.id][1] < NODE_DELAY_TTL_SECONDS
        }
        if not fresh_delays:
            await measure_node_delays(selectable_nodes, subscription_url, timeout_seconds)
        all_available = [
            (delay, node)
            for node in selectable_nodes
            if (delay := _NODE_DELAYS.get(node.id, (None, 0.0))[0]) is not None
            and time.monotonic() - _NODE_DELAYS[node.id][1] < NODE_DELAY_TTL_SECONDS
        ]
        available = [(delay, node) for delay, node in all_available if int(delay) <= int(latency_threshold_ms)]
        if not available:
            if all_available:
                raise RuntimeError("all eligible proxy nodes exceed the latency threshold")
            raise RuntimeError("all eligible proxy nodes are unavailable")
        chosen = min(available, key=lambda item: item[0])[1]
    elif chosen is None and not selected_node:
        chosen = selectable_nodes[0]
    elif chosen is None:
        fresh_available = [
            (delay, node)
            for node in selectable_nodes
            if (delay := _NODE_DELAYS.get(node.id, (None, 0.0))[0]) is not None
            and time.monotonic() - _NODE_DELAYS[node.id][1] < NODE_DELAY_TTL_SECONDS
        ]
        if not fresh_available:
            await measure_node_delays(selectable_nodes, subscription_url, timeout_seconds)
            fresh_available = [
                (delay, node)
                for node in selectable_nodes
                if (delay := _NODE_DELAYS.get(node.id, (None, 0.0))[0]) is not None
            ]
        if not fresh_available:
            raise RuntimeError("selected proxy node is unavailable")
        chosen = min(fresh_available, key=lambda item: item[0])[1]
    return chosen


async def resolve_subscription_proxy(
    subscription_url: str,
    *,
    timeout_seconds: int = 20,
    scheme: str = "http",
    refresh_seconds: int = 900,
    auto_select: bool = True,
    selected_node: str = "",
    selected_countries: Iterable[str] = (),
    latency_threshold_ms: int = 5000,
    excluded_node_ids: Iterable[str] = (),
) -> dict[str, str]:
    global _SUBSCRIPTION_RESOLVE_LOCK
    if _SUBSCRIPTION_RESOLVE_LOCK is None:
        _SUBSCRIPTION_RESOLVE_LOCK = asyncio.Lock()
    async with _SUBSCRIPTION_RESOLVE_LOCK:
        nodes = await fetch_subscription_node_list(
            subscription_url,
            timeout_seconds=timeout_seconds,
            refresh_seconds=refresh_seconds,
        )
        chosen = await _choose_subscription_node(
            nodes,
            subscription_url,
            timeout_seconds=timeout_seconds,
            auto_select=auto_select,
            selected_node=selected_node,
            selected_countries=selected_countries,
            latency_threshold_ms=latency_threshold_ms,
            excluded_node_ids=excluded_node_ids,
        )
        if chosen.protocol in {"http", "https", "socks5", "socks5h"}:
            server = chosen.uri.split("#", 1)[0]
            return {
                "server": server,
                "host_port": server.rsplit("//", 1)[-1],
                "node_count": str(len(nodes)),
                "node_id": chosen.id,
                "node_name": chosen.name,
                "proxy_mode": "native",
            }
        managed = await _proxy_from_mihomo(subscription_url, timeout_seconds, refresh_seconds)
        if _MIHOMO_SELECTED_NODE_ID != chosen.id:
            try:
                await _select_mihomo_node(chosen)
            except RuntimeError:
                managed = await _proxy_from_mihomo(subscription_url, timeout_seconds, refresh_seconds, force_rebuild=True)
                await _select_mihomo_node(chosen)
        return {**managed, "node_count": str(len(nodes)), "node_id": chosen.id, "node_name": chosen.name, "proxy_mode": "mihomo"}


async def acquire_dola_subscription_proxy(
    subscription_url: str,
    *,
    timeout_seconds: int = 20,
    scheme: str = "http",
    refresh_seconds: int = 900,
    auto_select: bool = True,
    selected_node: str = "",
    selected_countries: Iterable[str] = (),
    latency_threshold_ms: int = 5000,
    excluded_node_ids: Iterable[str] = (),
) -> dict[str, str]:
    nodes = await fetch_subscription_node_list(
        subscription_url,
        timeout_seconds=timeout_seconds,
        refresh_seconds=refresh_seconds,
    )
    if not nodes:
        raise RuntimeError("proxy subscription returned no usable nodes")
    return await _acquire_task_mihomo_proxy(
        nodes,
        subscription_url,
        timeout_seconds=timeout_seconds,
        auto_select=auto_select,
        selected_node=selected_node,
        selected_countries=selected_countries,
        latency_threshold_ms=latency_threshold_ms,
        excluded_node_ids=excluded_node_ids,
    )


async def release_dola_subscription_proxy(proxy: dict[str, str] | None) -> None:
    await release_task_mihomo_proxy(proxy)


async def fetch_proxy_from_api(api_url: str, *, timeout_seconds: int = 20, scheme: str = "http") -> dict[str, str]:
    if not api_url:
        raise RuntimeError("proxy api url is empty")

    timeout = httpx.Timeout(float(timeout_seconds), connect=min(10.0, float(timeout_seconds)))
    headers = {"User-Agent": "dola-fetch-service/1.0"}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False) as client:
            response = await client.get(api_url, headers=headers)
    except (httpx.ConnectTimeout, httpx.ConnectError):
        # Keep proxy acquisition independent from a temporarily unhealthy worker event loop/network path.
        response = await asyncio.to_thread(
            httpx.get,
            api_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
        )

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
    auto_select: bool = False,
    selected_node: str = "",
    selected_countries: Iterable[str] = (),
) -> dict[str, str]:
    return await resolve_subscription_proxy(
        subscription_url,
        timeout_seconds=timeout_seconds,
        scheme=scheme,
        refresh_seconds=refresh_seconds,
        auto_select=auto_select,
        selected_node=selected_node,
        selected_countries=selected_countries,
    )
