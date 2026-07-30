from __future__ import annotations

import io
import json
import secrets
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import postgres
from .accounts import ACCOUNTS_PATH
from .config import DATA_DIR, ensure_dirs
from .users import USERS_PATH


BACKUP_FORMAT = "dola-user-account-backup"
BACKUP_VERSION = 1
MAX_BACKUP_BYTES = 128 * 1024 * 1024
_NAMES = ("users.json", "temp_tokens.json", "accounts.json")


def _read_json_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid backup source: {path.name}")
    return data


def _current_payloads() -> dict[str, dict[str, Any]]:
    ensure_dirs()
    if postgres.enabled():
        return {
            "users.json": postgres.read_document("users", {"users": {}}),
            "temp_tokens.json": postgres.read_document("temp_tokens", {"tokens": {}}),
            "accounts.json": postgres.read_document("accounts", {"accounts": []}),
        }
    return {
        "users.json": _read_json_file(USERS_PATH, {"users": {}}),
        "temp_tokens.json": _read_json_file(DATA_DIR / "temp_tokens.json", {"tokens": {}}),
        "accounts.json": _read_json_file(ACCOUNTS_PATH, {"accounts": []}),
    }


def _validate_payloads(payloads: dict[str, dict[str, Any]]) -> None:
    users = payloads.get("users.json")
    tokens = payloads.get("temp_tokens.json")
    accounts = payloads.get("accounts.json")
    if not isinstance(users, dict) or not isinstance(users.get("users"), dict):
        raise ValueError("users.json format is invalid")
    if not isinstance(tokens, dict) or not isinstance(tokens.get("tokens"), dict):
        raise ValueError("temp_tokens.json format is invalid")
    if not isinstance(accounts, dict) or not isinstance(accounts.get("accounts"), list):
        raise ValueError("accounts.json format is invalid")
    if len(users["users"]) > 200_000 or len(tokens["tokens"]) > 200_000 or len(accounts["accounts"]) > 100_000:
        raise ValueError("backup contains too many records")
    for account in accounts["accounts"]:
        if not isinstance(account, dict) or not str(account.get("id") or ""):
            raise ValueError("accounts.json contains an invalid account")


def create_backup() -> bytes:
    payloads = _current_payloads()
    _validate_payloads(payloads)
    metadata = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "includes": ["users", "temporary_tokens", "accounts"],
        "record_counts": {
            "users": len(payloads["users.json"].get("users", {})),
            "temporary_tokens": len(payloads["temp_tokens.json"].get("tokens", {})),
            "accounts": len(payloads["accounts.json"].get("accounts", [])),
        },
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
        for name in _NAMES:
            archive.writestr(name, json.dumps(payloads[name], ensure_ascii=False, indent=2))
    return output.getvalue()


def _decode_backup(raw: bytes) -> dict[str, dict[str, Any]]:
    if not raw or len(raw) > MAX_BACKUP_BYTES:
        raise ValueError("backup file is empty or too large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError("backup must be a valid ZIP file") from exc
    with archive:
        names = set(archive.namelist())
        if any(name.startswith("/") or ".." in Path(name).parts for name in names):
            raise ValueError("backup contains an unsafe path")
        if sum(item.file_size for item in archive.infolist()) > MAX_BACKUP_BYTES:
            raise ValueError("backup expands beyond the allowed size")
        if not set(_NAMES).issubset(names) or "metadata.json" not in names:
            raise ValueError("backup is missing required files")
        metadata = json.loads(archive.read("metadata.json").decode("utf-8"))
        if metadata.get("format") != BACKUP_FORMAT or int(metadata.get("version") or 0) != BACKUP_VERSION:
            raise ValueError("unsupported backup format")
        payloads: dict[str, dict[str, Any]] = {}
        for name in _NAMES:
            data = json.loads(archive.read(name).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"{name} is invalid")
            payloads[name] = data
    _validate_payloads(payloads)
    return payloads


def restore_backup(raw: bytes) -> dict[str, Any]:
    payloads = _decode_backup(raw)
    # Keep an on-disk recovery copy before replacing any live records.
    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = backup_dir / f"pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}.zip"
    snapshot_path.write_bytes(create_backup())
    if postgres.enabled():
        postgres.write_document("users", payloads["users.json"])
        postgres.write_document("temp_tokens", payloads["temp_tokens.json"])
        postgres.write_document("accounts", payloads["accounts.json"])
    else:
        for name, path in (("users.json", USERS_PATH), ("temp_tokens.json", DATA_DIR / "temp_tokens.json"), ("accounts.json", ACCOUNTS_PATH)):
            temporary = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
            temporary.write_text(json.dumps(payloads[name], ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
    return {
        "users": len(payloads["users.json"]["users"]),
        "temporary_tokens": len(payloads["temp_tokens.json"]["tokens"]),
        "accounts": len(payloads["accounts.json"]["accounts"]),
        "pre_restore_snapshot": snapshot_path.name,
    }
