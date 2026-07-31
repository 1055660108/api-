from __future__ import annotations

import errno
import copy
import json
import hashlib
import os
import secrets
import threading
import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, unquote, urlparse

from .admin_auth import hash_password, validate_username
from .platforms import DEFAULT_MODELS, DEFAULT_PLATFORM, PLATFORM_VIDEO_DURATIONS, normalize_platform


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
INSECURE_INITIAL_PASSWORDS = {"", "admin", "change-me", "change-me-now", "fxbtn123", "password", "test"}
_CONFIG_LOCK = threading.Lock()
DEFAULT_MODEL_COSTS = {
    "dola": {"Seedance 2.0": 1},
    "doubao": {"Seedance 2.0 Mini": 1, "Seedance 2.0 Fast": 1},
    "qianwen": {"万相 2.7": 0.8, "万相 2.6": 0.5, "HappyHorse 1.0": 0.8},
}
DEFAULT_MODEL_DURATIONS = {
    platform: {model: list(PLATFORM_VIDEO_DURATIONS[platform]) for model in models}
    for platform, models in DEFAULT_MODELS.items()
}
DEFAULT_MODEL_DURATION_COSTS = {
    platform: {
        model: {str(duration): DEFAULT_MODEL_COSTS.get(platform, {}).get(model, 1) for duration in durations}
        for model, durations in models.items()
    }
    for platform, models in DEFAULT_MODEL_DURATIONS.items()
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
    return 48


def default_max_effective_workers() -> int:
    try:
        value = int(os.environ.get("DOLA_MAX_EFFECTIVE_WORKERS", "48"))
    except (TypeError, ValueError):
        value = 48
    return max(1, min(999, value))


def default_config() -> dict[str, Any]:
    return {
        "api_token": "",
        "account_access_key_hash": "",
        "account_access_key_hint": "",
        "account_access_key_enabled": False,
        "admin_username": os.environ.get("DOLA_ADMIN_USERNAME", "1055660108"),
        "admin_password_hash": "",
        "host": "0.0.0.0",
        "port": 8088,
        "browser_workers": recommended_browser_workers(),
        "max_effective_workers": default_max_effective_workers(),
        "remote_generation_limit": 0,
        "browser_executable_path": "",
        "headless": True,
        "task_timeout_seconds": 180,
        "task_retry_limit": 2,
        "dola_submit_interval_seconds": 5.0,
        "dola_global_submit_interval_seconds": 8.0,
        # Kept to migrate settings written by releases before global pacing.
        "dola_exit_submit_interval_seconds": 8.0,
        "video_duration": 15,
        "max_image_count": 9,
        "task_cache_retention_days": 7,
        "batch_history_retention_days": 30,
        "default_platform": DEFAULT_PLATFORM,
        "platform_models": DEFAULT_MODELS,
        "platform_model_states": {},
        "model_costs": DEFAULT_MODEL_COSTS,
        "model_durations": DEFAULT_MODEL_DURATIONS,
        "model_duration_costs": DEFAULT_MODEL_DURATION_COSTS,
        "proxy_api_url": "",
        "proxy_api_scheme": "http",
        "proxy_api_timeout_seconds": 20,
        "proxy_source": "direct",
        "platform_proxy_sources": {platform: "direct" for platform in DEFAULT_MODELS},
        "platform_proxy_random": {platform: False for platform in DEFAULT_MODELS},
        "proxy_subscription_url": "",
        "proxy_subscription_scheme": "http",
        "proxy_subscription_refresh_seconds": 900,
        "proxy_account_scheme": "socks5",
        "proxy_account_host": "",
        "proxy_account_port": 0,
        "proxy_account_username": "",
        "proxy_account_password": "",
        "proxy_enabled": True,
        "proxy_auto_select": True,
        "proxy_selected_node": "",
        "proxy_auto_countries": [],
        "proxy_latency_threshold_ms": 800,
        "proxy_health_refresh_seconds": 600,
        "registration_abuse_detection_enabled": False,
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
    existing_config = CONFIG_PATH.exists()
    if not existing_config:
        raw: dict[str, Any] = {}
    else:
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            raw = loaded if isinstance(loaded, dict) else {}
        except OSError as exc:
            if exc.errno in {errno.EMFILE, errno.ENFILE}:
                raise RuntimeError("service file descriptor limit is exhausted") from exc
            raise RuntimeError(f"config data cannot be read: {CONFIG_PATH}") from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"config data is corrupt: {CONFIG_PATH}") from exc
        if not isinstance(loaded, dict):
            raise RuntimeError(f"config data is corrupt: {CONFIG_PATH}")

    defaults = default_config()
    data = {key: raw.get(key, value) for key, value in defaults.items()}
    # A previously configured per-exit interval becomes the global interval on
    # upgrade. This preserves the operator's chosen submission cadence.
    if "dola_global_submit_interval_seconds" not in raw and "dola_exit_submit_interval_seconds" in raw:
        data["dola_global_submit_interval_seconds"] = raw["dola_exit_submit_interval_seconds"]
    if "proxy_source" not in raw:
        if str(raw.get("proxy_subscription_url") or "").strip():
            data["proxy_source"] = "subscription"
        elif str(raw.get("proxy_account_host") or "").strip():
            data["proxy_source"] = "account"
        elif str(raw.get("proxy_api_url") or "").strip():
            data["proxy_source"] = "api"
    if "platform_proxy_sources" not in raw:
        data["platform_proxy_sources"] = {
            platform: str(data.get("proxy_source") or "direct")
            for platform in DEFAULT_MODELS
        }
    changed = data != raw
    if not data.get("api_token"):
        data["api_token"] = secrets.token_urlsafe(32)
        changed = True
    if not data.get("admin_password_hash"):
        initial_password = str(os.environ.get("DOLA_ADMIN_PASSWORD") or "")
        if initial_password.strip().lower() in INSECURE_INITIAL_PASSWORDS or len(initial_password) < 12:
            raise RuntimeError("DOLA_ADMIN_PASSWORD must be set to a non-default password with at least 12 characters before first startup")
        data["admin_password_hash"] = hash_password(initial_password)
        changed = True
    legacy_salt = str(raw.get("admin_password_salt") or "")
    if legacy_salt and "$" not in str(data.get("admin_password_hash") or ""):
        initial_password = str(os.environ.get("DOLA_ADMIN_PASSWORD") or "")
        if not initial_password:
            raise RuntimeError("DOLA_ADMIN_PASSWORD is required once to migrate the legacy administrator password")
        legacy_digest = hashlib.pbkdf2_hmac("sha256", initial_password.encode("utf-8"), bytes.fromhex(legacy_salt), 240_000).hex()
        if secrets.compare_digest(legacy_digest, str(data.get("admin_password_hash") or "")):
            data["admin_password_hash"] = hash_password(initial_password)
            changed = True
    if not existing_config:
        database_url = str(os.environ.get("DOLA_DATABASE_URL") or "").strip()
        parsed_database = urlparse(database_url) if database_url else None
        database_password = unquote(parsed_database.password or "") if parsed_database else ""
        if parsed_database and parsed_database.scheme.startswith("postgres") and database_password.lower() in INSECURE_INITIAL_PASSWORDS:
            raise RuntimeError("POSTGRES_PASSWORD must be set to a non-default password before first startup")
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


def validate_proxy_account_host(value: str | None) -> str:
    host = str(value or "").strip().lower().rstrip(".")
    if not host or len(host) > 253 or any(char in host for char in "\r\n\0/@?#:") or any(char.isspace() for char in host):
        raise ValueError("proxy_account_host is invalid")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("proxy_account_host is not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") or not all(char.isalnum() or char == "-" for char in label) for label in labels):
            raise ValueError("proxy_account_host is invalid")
    else:
        if address.is_loopback or address.is_unspecified or address.is_link_local:
            raise ValueError("proxy_account_host is not allowed")
    return host


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
        if "model_costs" in updates and "model_duration_costs" not in updates:
            raw_model_costs = updates.get("model_costs") if isinstance(updates.get("model_costs"), dict) else {}
            duration_costs = copy.deepcopy(data.get("model_duration_costs") or {})
            for platform, platform_costs in raw_model_costs.items():
                if platform not in PLATFORM_VIDEO_DURATIONS or not isinstance(platform_costs, dict):
                    continue
                duration_costs.setdefault(platform, {})
                for model, cost in platform_costs.items():
                    duration_costs[platform][str(model)] = {
                        str(duration): cost for duration in PLATFORM_VIDEO_DURATIONS[platform]
                    }
            data["model_duration_costs"] = duration_costs
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
    account_access_key_hash: str
    account_access_key_hint: str
    account_access_key_enabled: bool
    admin_username: str
    admin_password_hash: str
    host: str
    port: int
    browser_workers: int
    max_effective_workers: int
    remote_generation_limit: int
    browser_executable_path: str
    headless: bool
    task_timeout_seconds: int
    task_retry_limit: int
    dola_submit_interval_seconds: float
    dola_global_submit_interval_seconds: float
    dola_exit_submit_interval_seconds: float
    video_duration: int
    max_image_count: int
    task_cache_retention_days: int
    batch_history_retention_days: int
    default_platform: str
    platform_models: dict[str, list[str]]
    platform_model_states: dict[str, dict[str, bool]]
    model_costs: dict[str, dict[str, int | float]]
    model_durations: dict[str, dict[str, list[int]]]
    model_duration_costs: dict[str, dict[str, dict[int, int | float]]]
    proxy_api_url: str
    proxy_api_scheme: str
    proxy_api_timeout_seconds: int
    proxy_source: str
    platform_proxy_sources: dict[str, str]
    platform_proxy_random: dict[str, bool]
    proxy_subscription_url: str
    proxy_subscription_scheme: str
    proxy_subscription_refresh_seconds: int
    proxy_account_scheme: str
    proxy_account_host: str
    proxy_account_port: int
    proxy_account_username: str
    proxy_account_password: str
    proxy_enabled: bool
    proxy_auto_select: bool
    proxy_selected_node: str
    proxy_auto_countries: list[str]
    proxy_latency_threshold_ms: int
    proxy_health_refresh_seconds: int
    registration_abuse_detection_enabled: bool
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
    proxy_source = str(data.get("proxy_source") or "direct").strip().lower()
    if proxy_source not in {"subscription", "account", "api", "direct"}:
        proxy_source = "direct"
    raw_platform_proxy_sources = data.get("platform_proxy_sources") if isinstance(data.get("platform_proxy_sources"), dict) else {}
    raw_platform_proxy_random = data.get("platform_proxy_random") if isinstance(data.get("platform_proxy_random"), dict) else {}
    platform_proxy_sources: dict[str, str] = {}
    platform_proxy_random: dict[str, bool] = {}
    for platform in DEFAULT_MODELS:
        source = str(raw_platform_proxy_sources.get(platform) or proxy_source).strip().lower()
        platform_proxy_sources[platform] = source if source in {"subscription", "account", "api", "direct"} else proxy_source
        platform_proxy_random[platform] = _as_bool(raw_platform_proxy_random.get(platform), False)
    proxy_account_scheme = str(data.get("proxy_account_scheme") or "socks5").strip().lower()
    if proxy_account_scheme not in VALID_PROXY_SERVER_SCHEMES:
        proxy_account_scheme = "socks5"
    try:
        default_platform = normalize_platform(str(data.get("default_platform") or DEFAULT_PLATFORM))
    except ValueError:
        default_platform = DEFAULT_PLATFORM
    raw_models = data.get("platform_models") if isinstance(data.get("platform_models"), dict) else {}
    raw_states = data.get("platform_model_states") if isinstance(data.get("platform_model_states"), dict) else {}
    raw_costs = data.get("model_costs") if isinstance(data.get("model_costs"), dict) else {}
    raw_durations = data.get("model_durations") if isinstance(data.get("model_durations"), dict) else {}
    raw_duration_costs = data.get("model_duration_costs") if isinstance(data.get("model_duration_costs"), dict) else {}
    platform_models: dict[str, list[str]] = {}
    platform_model_states: dict[str, dict[str, bool]] = {}
    model_costs: dict[str, dict[str, int | float]] = {}
    model_durations: dict[str, dict[str, list[int]]] = {}
    model_duration_costs: dict[str, dict[str, dict[int, int | float]]] = {}
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
        configured_durations = raw_durations.get(platform, {}) if isinstance(raw_durations.get(platform, {}), dict) else {}
        supported_durations = PLATFORM_VIDEO_DURATIONS[platform]
        model_durations[platform] = {}
        configured_duration_costs = raw_duration_costs.get(platform, {}) if isinstance(raw_duration_costs.get(platform, {}), dict) else {}
        model_duration_costs[platform] = {}
        for model in models:
            value = costs.get(model, default_costs.get(model, 1))
            try:
                cost = float(value)
            except (TypeError, ValueError):
                cost = 1.0
            if cost <= 0 or round(cost * 10) != cost * 10:
                cost = 1.0
            model_costs[platform][model] = int(cost) if cost.is_integer() else cost
            raw_model_durations = configured_durations.get(model, supported_durations)
            if not isinstance(raw_model_durations, list):
                raw_model_durations = supported_durations
            selected = {
                int(item)
                for item in raw_model_durations
                if isinstance(item, (int, float, str)) and str(item).strip().isdigit() and int(item) in supported_durations
            }
            model_durations[platform][model] = [duration for duration in supported_durations if duration in selected]
            raw_model_duration_costs = configured_duration_costs.get(model, {}) if isinstance(configured_duration_costs.get(model, {}), dict) else {}
            model_duration_costs[platform][model] = {}
            for duration in supported_durations:
                value = raw_model_duration_costs.get(str(duration), raw_model_duration_costs.get(duration, model_costs[platform][model]))
                try:
                    duration_cost = float(value)
                except (TypeError, ValueError):
                    duration_cost = float(model_costs[platform][model])
                if duration_cost <= 0 or round(duration_cost * 10) != duration_cost * 10:
                    duration_cost = float(model_costs[platform][model])
                model_duration_costs[platform][model][duration] = int(duration_cost) if duration_cost.is_integer() else duration_cost
    return Settings(
        api_token=str(data.get("api_token") or ""),
        account_access_key_hash=str(data.get("account_access_key_hash") or ""),
        account_access_key_hint=str(data.get("account_access_key_hint") or "")[:40],
        account_access_key_enabled=_as_bool(data.get("account_access_key_enabled"), False),
        admin_username=validate_username(str(data.get("admin_username") or "admin")),
        admin_password_hash=str(data.get("admin_password_hash") or ""),
        host=str(data.get("host") or "0.0.0.0"),
        port=int(data.get("port") or 8088),
        browser_workers=max(1, min(999, int(data.get("browser_workers") or recommended_browser_workers()))),
        max_effective_workers=max(1, min(999, int(data.get("max_effective_workers") or default_max_effective_workers()))),
        remote_generation_limit=max(0, min(999, int(data.get("remote_generation_limit") or 0))),
        browser_executable_path=str(data.get("browser_executable_path") or "").strip(),
        headless=_as_bool(data.get("headless"), True),
        task_timeout_seconds=max(30, int(data.get("task_timeout_seconds") or 180)),
        task_retry_limit=max(0, min(10, int(data.get("task_retry_limit") if data.get("task_retry_limit") is not None else 2))),
        dola_submit_interval_seconds=max(1.0, min(5.0, float(data.get("dola_submit_interval_seconds") or 5.0))),
        dola_global_submit_interval_seconds=max(3.0, min(30.0, float(data.get("dola_global_submit_interval_seconds") or 8.0))),
        dola_exit_submit_interval_seconds=max(3.0, min(30.0, float(data.get("dola_exit_submit_interval_seconds") or 8.0))),
        video_duration=max(1, int(data.get("video_duration") or 15)),
        max_image_count=max(0, min(9, int(data.get("max_image_count") or 9))),
        task_cache_retention_days=max(1, int(data.get("task_cache_retention_days") or 7)),
        batch_history_retention_days=max(7, min(30, int(data.get("batch_history_retention_days") or 30))),
        default_platform=default_platform,
        platform_models=platform_models,
        platform_model_states=platform_model_states,
        model_costs=model_costs,
        model_durations=model_durations,
        model_duration_costs=model_duration_costs,
        proxy_api_url=str(data.get("proxy_api_url") or "").strip(),
        proxy_api_scheme=proxy_api_scheme,
        proxy_api_timeout_seconds=max(3, int(data.get("proxy_api_timeout_seconds") or 20)),
        proxy_source=proxy_source,
        platform_proxy_sources=platform_proxy_sources,
        platform_proxy_random=platform_proxy_random,
        proxy_subscription_url=str(data.get("proxy_subscription_url") or "").strip(),
        proxy_subscription_scheme=proxy_api_scheme if str(data.get("proxy_subscription_scheme") or "").strip().lower() not in VALID_PROXY_SERVER_SCHEMES else str(data.get("proxy_subscription_scheme")).strip().lower(),
        proxy_subscription_refresh_seconds=max(60, min(86400, int(data.get("proxy_subscription_refresh_seconds") or 900))),
        proxy_account_scheme=proxy_account_scheme,
        proxy_account_host=str(data.get("proxy_account_host") or "").strip(),
        proxy_account_port=max(0, min(65535, int(data.get("proxy_account_port") or 0))),
        proxy_account_username=str(data.get("proxy_account_username") or "").strip(),
        proxy_account_password=str(data.get("proxy_account_password") or ""),
        proxy_enabled=_as_bool(data.get("proxy_enabled"), True),
        proxy_auto_select=_as_bool(data.get("proxy_auto_select"), True),
        proxy_selected_node=str(data.get("proxy_selected_node") or "").strip()[:200],
        proxy_auto_countries=list(dict.fromkeys(
            str(item).strip()[:40]
            for item in (data.get("proxy_auto_countries") if isinstance(data.get("proxy_auto_countries"), list) else [])
            if str(item or "").strip()
        )),
        proxy_latency_threshold_ms=max(100, min(5000, int(data.get("proxy_latency_threshold_ms") or 800))),
        proxy_health_refresh_seconds=max(60, min(86400, int(data.get("proxy_health_refresh_seconds") or 600))),
        registration_abuse_detection_enabled=_as_bool(data.get("registration_abuse_detection_enabled"), False),
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
    if proxy_server.lower().startswith("socks5h://"):
        proxy_server = f"socks5://{proxy_server[len('socks5h://') :]}"
    return {"server": proxy_server}


def account_proxy_url_for(scheme: str, host: str, port: int, username: str, password: str) -> str:
    normalized_scheme = validate_proxy_api_scheme(scheme)
    normalized_host = str(host or "").strip()
    if not normalized_host or not int(port or 0) or not str(username or "") or not str(password or ""):
        return ""
    credentials = f"{quote(str(username), safe='')}:{quote(str(password), safe='')}"
    return f"{normalized_scheme}://{credentials}@{normalized_host}:{int(port)}"


def account_browser_proxy_config_for(scheme: str, host: str, port: int, username: str, password: str) -> dict[str, str] | None:
    normalized_scheme = validate_proxy_api_scheme(scheme)
    normalized_host = str(host or "").strip()
    if not normalized_host or not int(port or 0) or not str(username or "") or not str(password or ""):
        return None
    return {
        "server": f"{normalized_scheme}://{normalized_host}:{int(port)}",
        "username": str(username),
        "password": str(password),
    }
