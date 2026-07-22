from __future__ import annotations

import json
import hashlib
import os
import secrets
import threading
import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .admin_auth import hash_password, validate_username
from .platforms import DEFAULT_MODELS, DEFAULT_PLATFORM, normalize_platform


APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(
    os.environ.get(
        "DOLA_DATA_DIR",
        "/var/lib/dola-fetch-service" if os.name != "nt" else str(APP_ROOT / "data"),
    )
)
CONFIG_PATH = Path(os.environ.get("DOLA_CONFIG_PATH", str(DATA_DIR / "config.json")))
TASKS_DIR = DATA_DIR / "tasks"
RUNTIME_PATH = DATA_DIR / "runtime.json"
ACCOUNTS_PATH = DATA_DIR / "accounts.json"
DOUBAO_STATES_DIR = DATA_DIR / "doubao_states"
DOUBAO_PROFILES_DIR = DATA_DIR / "doubao_profiles"
QIANWEN_PROFILES_DIR = DATA_DIR / "qianwen_profiles"

TARGET_URL = "https://www.dola.com/chat/create-image"
VALID_RATIOS = {"1:1", "3:4", "4:3", "9:16", "16:9", "21:9"}
DEFAULT_RATIO = "9:16"
DEFAULT_PROXY_API_URL = os.environ.get(
    "DOLA_DEFAULT_PROXY_API_URL",
    "",
)
VALID_PROXY_API_SCHEMES = {"http", "https"}
VALID_PROXY_SERVER_SCHEMES = {"http", "https", "socks5", "socks5h"}
_CONFIG_LOCK = threading.Lock()
DEFAULT_MODEL_COSTS = {
    "dola": {"Seedance 2.0": 1},
    "doubao": {"Seedance 2.0 Mini": 1, "Seedance 2.0 Fast": 1},
    "qianwen": {"万相 2.7": 0.8, "万相 2.6": 0.5, "HappyHorse 1.0": 0.8},
}


def _read_mem_gb() -> float:
    if os.name == "nt":
        return 4.0
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return 4.0
    for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) / 1024 / 1024
    return 4.0


def recommended_browser_workers() -> int:
    return 32


def default_max_effective_workers() -> int:
    try:
        value = int(os.environ.get("DOLA_MAX_EFFECTIVE_WORKERS", "32"))
    except (TypeError, ValueError):
        value = 32
    return max(1, min(999, value))


def default_config() -> dict[str, Any]:
    return {
        "api_token": "",
        "admin_username": os.environ.get("DOLA_ADMIN_USERNAME", "1055660108"),
        "admin_password_hash": "",
        "host": "0.0.0.0",
        "port": 8088,
        "browser_workers": recommended_browser_workers(),
        "max_effective_workers": default_max_effective_workers(),
        "browser_executable_path": "",
        "headless": True,
        "task_timeout_seconds": 180,
        "video_duration": 15,
        "max_image_count": 9,
        "task_cache_retention_days": 7,
        "default_platform": DEFAULT_PLATFORM,
        "platform_models": DEFAULT_MODELS,
        "platform_model_states": {},
        "model_costs": DEFAULT_MODEL_COSTS,
        "proxy_api_url": "",
        "proxy_api_scheme": "http",
        "proxy_api_timeout_seconds": 20,
        "proxy_subscription_url": "",
        "proxy_subscription_scheme": "http",
        "proxy_subscription_refresh_seconds": 900,
        "proxy_enabled": True,
        "proxy_auto_select": True,
        "proxy_selected_node": "",
        "registration_email_verification_enabled": True,
        "registration_email_domains": ["qq.com", "163.com"],
        "registration_smtp_host": "smtp.qq.com",
        "registration_smtp_port": 465,
        "registration_smtp_username": "",
        "registration_smtp_authorization_code": "",
        "registration_email_sender_name": "视频生成服务",
        "registration_email_code_ttl_minutes": 10,
        "reclaim_memory_after_task": True,
        "drop_os_cache_when_idle": False,
    }


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    DOUBAO_STATES_DIR.mkdir(parents=True, exist_ok=True)
    DOUBAO_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    QIANWEN_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)


def _load_config_dict() -> dict[str, Any]:
    ensure_dirs()
    if not CONFIG_PATH.exists():
        raw: dict[str, Any] = {}
    else:
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            raw = loaded if isinstance(loaded, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"config data is corrupt: {CONFIG_PATH}") from exc
        if not isinstance(loaded, dict):
            raise RuntimeError(f"config data is corrupt: {CONFIG_PATH}")

    defaults = default_config()
    data = {key: raw.get(key, value) for key, value in defaults.items()}
    changed = data != raw
    if not data.get("api_token"):
        data["api_token"] = secrets.token_urlsafe(32)
        changed = True
    if not data.get("admin_password_hash"):
        data["admin_password_hash"] = hash_password(os.environ.get("DOLA_ADMIN_PASSWORD", "fxbtn123"))
        changed = True
    legacy_salt = str(raw.get("admin_password_salt") or "")
    if legacy_salt and "$" not in str(data.get("admin_password_hash") or ""):
        initial_password = os.environ.get("DOLA_ADMIN_PASSWORD", "fxbtn123")
        legacy_digest = hashlib.pbkdf2_hmac("sha256", initial_password.encode("utf-8"), bytes.fromhex(legacy_salt), 240_000).hex()
        if secrets.compare_digest(legacy_digest, str(data.get("admin_password_hash") or "")):
            data["admin_password_hash"] = hash_password(initial_password)
            changed = True
    if changed or not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def ensure_config() -> dict[str, Any]:
    return _load_config_dict()


def validate_startup_credentials(data: Mapping[str, Any] | None = None) -> None:
    current = data or _load_config_dict()
    if not str(os.environ.get("DOLA_ADMIN_PASSWORD") or "").strip() and not str(current.get("admin_password_hash") or ""):
        raise RuntimeError("DOLA_ADMIN_PASSWORD must be set before first startup")


def validate_proxy_api_url(value: str) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if any(char in url for char in "\r\n\0"):
        raise ValueError("proxy_api_url must be a single-line URL")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in VALID_PROXY_API_SCHEMES or not parsed.netloc:
        raise ValueError("proxy_api_url must be an http or https URL")
    hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
    blocked_names = {"localhost", "metadata.google.internal", "instance-data.ec2.internal"}
    if hostname in blocked_names or hostname.endswith(".localhost"):
        raise ValueError("proxy_api_url host is not allowed")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and (address.is_loopback or address.is_private or address.is_link_local or address.is_reserved):
        raise ValueError("proxy_api_url host is not allowed")
    return url


def validate_proxy_api_scheme(value: str | None) -> str:
    scheme = str(value or "http").strip().lower()
    if scheme not in VALID_PROXY_SERVER_SCHEMES:
        raise ValueError("proxy_api_scheme must be one of http, https, socks5, socks5h")
    return scheme


def update_config(updates: Mapping[str, Any]) -> dict[str, Any]:
    defaults = default_config()
    unknown = sorted(set(updates) - set(defaults))
    if unknown:
        raise KeyError(f"unknown config key: {', '.join(unknown)}")

    ensure_dirs()
    with _CONFIG_LOCK:
        if CONFIG_PATH.exists():
            try:
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                raw = loaded if isinstance(loaded, dict) else {}
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"config data is corrupt: {CONFIG_PATH}") from exc
            if not isinstance(loaded, dict):
                raise RuntimeError(f"config data is corrupt: {CONFIG_PATH}")
        else:
            raw = {}

        data = {key: raw.get(key, value) for key, value in defaults.items()}
        data.update(updates)
        if not data.get("api_token"):
            data["api_token"] = secrets.token_urlsafe(32)

        temp_path = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.{secrets.token_hex(8)}.tmp")
        try:
            temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(CONFIG_PATH)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
        return data


@dataclass(frozen=True)
class Settings:
    api_token: str
    admin_username: str
    admin_password_hash: str
    host: str
    port: int
    browser_workers: int
    max_effective_workers: int
    browser_executable_path: str
    headless: bool
    task_timeout_seconds: int
    video_duration: int
    max_image_count: int
    task_cache_retention_days: int
    default_platform: str
    platform_models: dict[str, list[str]]
    platform_model_states: dict[str, dict[str, bool]]
    model_costs: dict[str, dict[str, int | float]]
    proxy_api_url: str
    proxy_api_scheme: str
    proxy_api_timeout_seconds: int
    proxy_subscription_url: str
    proxy_subscription_scheme: str
    proxy_subscription_refresh_seconds: int
    proxy_enabled: bool
    proxy_auto_select: bool
    proxy_selected_node: str
    registration_email_verification_enabled: bool
    registration_email_domains: list[str]
    registration_smtp_host: str
    registration_smtp_port: int
    registration_smtp_username: str
    registration_smtp_authorization_code: str
    registration_email_sender_name: str
    registration_email_code_ttl_minutes: int
    reclaim_memory_after_task: bool
    drop_os_cache_when_idle: bool


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def load_settings() -> Settings:
    data = _load_config_dict()
    proxy_api_scheme = str(data.get("proxy_api_scheme") or "http").strip().lower()
    if proxy_api_scheme not in VALID_PROXY_SERVER_SCHEMES:
        proxy_api_scheme = "http"
    try:
        default_platform = normalize_platform(str(data.get("default_platform") or DEFAULT_PLATFORM))
    except ValueError:
        default_platform = DEFAULT_PLATFORM
    raw_models = data.get("platform_models") if isinstance(data.get("platform_models"), dict) else {}
    raw_states = data.get("platform_model_states") if isinstance(data.get("platform_model_states"), dict) else {}
    raw_costs = data.get("model_costs") if isinstance(data.get("model_costs"), dict) else {}
    platform_models: dict[str, list[str]] = {}
    platform_model_states: dict[str, dict[str, bool]] = {}
    model_costs: dict[str, dict[str, int | float]] = {}
    for platform, defaults in DEFAULT_MODELS.items():
        values = raw_models.get(platform, defaults) if isinstance(raw_models, dict) else defaults
        if isinstance(values, list):
            models = [str(item).strip()[:80] for item in values if str(item or "").strip()]
        else:
            models = []
        platform_models[platform] = models
        states = raw_states.get(platform, {}) if isinstance(raw_states.get(platform, {}), dict) else {}
        platform_model_states[platform] = {model: _as_bool(states.get(model), True) for model in models}
        costs = raw_costs.get(platform, {}) if isinstance(raw_costs.get(platform, {}), dict) else {}
        default_costs = DEFAULT_MODEL_COSTS.get(platform, {})
        model_costs[platform] = {}
        for model in models:
            value = costs.get(model, default_costs.get(model, 1))
            try:
                cost = float(value)
            except (TypeError, ValueError):
                cost = 1.0
            if cost <= 0 or round(cost * 10) != cost * 10:
                cost = 1.0
            model_costs[platform][model] = int(cost) if cost.is_integer() else cost
    return Settings(
        api_token=str(data.get("api_token") or ""),
        admin_username=validate_username(str(data.get("admin_username") or "admin")),
        admin_password_hash=str(data.get("admin_password_hash") or ""),
        host=str(data.get("host") or "0.0.0.0"),
        port=int(data.get("port") or 8088),
        browser_workers=max(1, min(999, int(data.get("browser_workers") or recommended_browser_workers()))),
        max_effective_workers=max(1, min(999, int(data.get("max_effective_workers") or default_max_effective_workers()))),
        browser_executable_path=str(data.get("browser_executable_path") or "").strip(),
        headless=_as_bool(data.get("headless"), True),
        task_timeout_seconds=max(30, int(data.get("task_timeout_seconds") or 180)),
        video_duration=max(1, int(data.get("video_duration") or 15)),
        max_image_count=max(0, min(9, int(data.get("max_image_count") or 9))),
        task_cache_retention_days=max(1, int(data.get("task_cache_retention_days") or 7)),
        default_platform=default_platform,
        platform_models=platform_models,
        platform_model_states=platform_model_states,
        model_costs=model_costs,
        proxy_api_url=str(data.get("proxy_api_url") or "").strip(),
        proxy_api_scheme=proxy_api_scheme,
        proxy_api_timeout_seconds=max(3, int(data.get("proxy_api_timeout_seconds") or 20)),
        proxy_subscription_url=str(data.get("proxy_subscription_url") or "").strip(),
        proxy_subscription_scheme=proxy_api_scheme if str(data.get("proxy_subscription_scheme") or "").strip().lower() not in VALID_PROXY_SERVER_SCHEMES else str(data.get("proxy_subscription_scheme")).strip().lower(),
        proxy_subscription_refresh_seconds=max(60, min(86400, int(data.get("proxy_subscription_refresh_seconds") or 900))),
        proxy_enabled=_as_bool(data.get("proxy_enabled"), True),
        proxy_auto_select=_as_bool(data.get("proxy_auto_select"), True),
        proxy_selected_node=str(data.get("proxy_selected_node") or "").strip()[:200],
        registration_email_verification_enabled=_as_bool(data.get("registration_email_verification_enabled"), True),
        registration_email_domains=[str(item).strip().lower().lstrip("@") for item in data.get("registration_email_domains", []) if str(item or "").strip()],
        registration_smtp_host=str(data.get("registration_smtp_host") or "smtp.qq.com").strip(),
        registration_smtp_port=max(1, min(65535, int(data.get("registration_smtp_port") or 465))),
        registration_smtp_username=str(data.get("registration_smtp_username") or "").strip().lower(),
        registration_smtp_authorization_code=str(os.environ.get("DOLA_QQ_SMTP_AUTHORIZATION_CODE") or data.get("registration_smtp_authorization_code") or "").strip(),
        registration_email_sender_name=str(data.get("registration_email_sender_name") or "视频生成服务").strip()[:80],
        registration_email_code_ttl_minutes=max(3, min(30, int(data.get("registration_email_code_ttl_minutes") or 10))),
        reclaim_memory_after_task=_as_bool(data.get("reclaim_memory_after_task"), True),
        drop_os_cache_when_idle=_as_bool(data.get("drop_os_cache_when_idle"), False),
    )


def normalize_proxy_server(server: str, default_scheme: str = "http") -> str:
    value = str(server or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    scheme = (default_scheme or "http").strip().lower()
    if scheme not in VALID_PROXY_SERVER_SCHEMES:
        scheme = "http"
    return f"{scheme}://{value}"


def browser_proxy_config_for(server: str, default_scheme: str = "http") -> dict[str, str] | None:
    proxy_server = normalize_proxy_server(server, default_scheme)
    if not proxy_server:
        return None
    return {"server": proxy_server}
