from __future__ import annotations

import asyncio
import base64
import copy
from concurrent.futures import Future
import json
import hmac
import hashlib
import logging
import re
import secrets
import shutil
import smtplib
import subprocess
import threading
import time
from urllib.parse import quote, urljoin, urlsplit
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
import httpx
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import config as app_config
from .account_access import generate_key as generate_account_access_key, revoke_key as revoke_account_access_key, set_enabled as set_account_access_enabled, status as account_access_status, verify_key as verify_account_access_key
from .admin_audit import list_admin_actions, prune_admin_actions, record_admin_action
from .admin_auth import SESSION_COOKIE_NAME, SESSION_TTL_SECONDS, create_session, delete_session, delete_user_sessions, hash_password, session_username, validate_password, verify_password
from .client_auth import CLIENT_SESSION_COOKIE_NAME, CLIENT_SESSION_TTL_SECONDS, client_session_token_hash, create_client_session, delete_client_session
from .accounts import account_for_current_task, add_account, add_accounts_bulk_result, cleanup_flagged_accounts, clear_account_current_task, delete_account, list_account_deletion_history, list_accounts, migrate_ten_second_accounts_to_api, reconcile_account_quotas, refund_account_quota, reset_account_quota, reset_daily_account_quotas_if_needed, set_account_enabled, sync_account_default_quotas, update_account_details, update_account_quota
from .account_proxies import account_proxy_entries, account_proxy_url, delete_account_proxies, import_account_proxies, list_account_proxies, select_account_proxies, set_account_proxies_enabled, update_account_proxy_latencies
from .billing import model_cost_points, model_cost_units, nonnegative_points_to_units, points_to_units, units_to_points
from .data_backup import MAX_BACKUP_BYTES, create_backup, restore_backup
from .batch_jobs import (
    cancel_job as cancel_persistent_batch_job,
    claim_next_row as claim_next_batch_row,
    cleanup_history as cleanup_batch_history,
    coordinator as batch_coordinator,
    create_job as create_batch_job,
    create_retry_job as create_batch_retry_job,
    fail_or_requeue_row as fail_or_requeue_batch_row,
    finish_row_creation as finish_batch_row_creation,
    get_job as get_batch_job,
    list_jobs as list_batch_jobs,
    public_job as public_batch_job,
    reconcile_job as reconcile_batch_job,
    recover_stale_creating_rows,
)
from .browser_runtime import BROWSER_CONTEXTS_PER_PROCESS, BROWSER_POOL_PROCESSES, BROWSER_SUBMISSION_CONCURRENCY, resolve_browser_executable
from .config import (
    DATA_DIR,
    DEFAULT_RATIO,
    VALID_RATIOS,
    ensure_config,
    load_settings,
    update_config,
    validate_proxy_account_host,
    validate_proxy_api_scheme,
    validate_proxy_api_url,
    validate_startup_credentials,
)
from .email_verification import consume_registration_code, normalize_domains, normalize_email, send_registration_code, validate_allowed_email
from .feedback import create_feedback, delete_feedback, list_feedback, list_feedback_for_user, update_feedback
from .invitation_codes import complete_reservation as complete_invitation_reservation, delete_code as delete_invitation_code, generate_codes as generate_invitation_codes, invitation_state, registration_required as invitation_registration_required, release_reservation as release_invitation_reservation, reserve_code as reserve_invitation_code, set_registration_required as set_invitation_registration_required, update_code_note as update_invitation_code_note
from .notifications import create_announcement, create_notifications, delete_announcement, delete_notification, list_admin_notifications, list_announcements, list_notifications_for_user, mark_all_notifications_read, mark_announcement_seen, mark_notification_read, update_announcement
from .platforms import DEFAULT_PLATFORM, PLATFORM_LABELS, PLATFORM_VIDEO_DURATIONS, normalize_model, normalize_platform
from .query import query_task
from .qianwen_models import fetch_qianwen_video_models
from .platform_model_sync import fetch_platform_video_models
from .proxy_manager import activate_mihomo_node, fetch_proxy_from_api, fetch_subscription_node_list, measure_node_delays, node_payload, probe_dola_proxy, rebuild_mihomo_from_snapshot
from .resilience import PlatformGuard, adaptive_worker_limit, fair_owner_capacity_limits, queue_admission
from .registration_security import block_retry_after as registration_block_retry_after, clear_local_state as clear_registration_security_state, record_failure as record_registration_failure, reset_failures as reset_registration_failures
from .repository_update import repository_status, update_repository
from .resource_monitor import collect_resource_snapshot, latest_resource_snapshot
from .spreadsheet_import import MAX_SPREADSHEET_BYTES, SUPPORTED_SPREADSHEET_SUFFIXES, SpreadsheetImportError, parse_spreadsheet
from .postgres import ensure_schema as ensure_postgres_schema
from .postgres import enabled as postgres_enabled
from .postgres import is_transient_error as is_transient_postgres_error
from .package_catalog import create_package, disable_package, list_packages, update_package
from .membership_catalog import DEFAULT_PAYMENT_URL, create_membership, disable_membership, get_membership, list_memberships, update_membership
from .point_cards import delete_cards, generate_cards, list_cards, purge_legacy_cards, redeem_card
from .point_transactions import archive_old_transactions, list_transactions, record_transaction
from .user_activity import list_activity, record_activity
from .store import (
    active_task_ids,
    active_task_count_for_owner,
    account_active_tasks,
    cancel_pending_tasks,
    cleanup_terminal_tasks_before_local_day,
    create_task,
    fail_initializing_tasks,
    finalize_task_creation,
    find_or_create_task,
    load_runtime,
    load_result,
    delete_task,
    get_meta,
    images_dir,
    mark_failed,
    migrate_task_owner,
    list_tasks,
    list_task_metas_by_statuses,
    list_tasks_page,
    set_task_video_hidden,
    set_task_hidden,
    set_task_images,
    task_image_paths,
    task_reference_display_paths,
    task_states,
    request_task_cancel,
    set_task_submission_paused,
    task_has_video,
    task_submission_paused,
    validate_task_id,
    update_meta,
)
from .temp_access import (
    AccessContext,
    QuotaExceeded,
    create_temp_tokens,
    delete_temp_token,
    get_temp_context,
    get_temp_context_by_hash,
    get_temp_reservation,
    hash_token,
    list_temp_tokens,
    refund_temp_quota_hash,
    reserve_temp_quota,
    set_temp_billing_priority,
    temp_token_remarks,
    update_temp_token,
)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_BATCH_ASSET_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_BATCH_ASSET_CHUNK_FILES = 16
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
IMAGE_MAGIC = {
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".webp": (b"RIFF",),
}
MAX_REFERENCE_IMAGE_NAME_LENGTH = 180
logger = logging.getLogger(__name__)

ACCOUNT_LIST_MAINTENANCE_SECONDS = 60.0
_ACCOUNT_LIST_CACHE_LOCK = threading.RLock()
_ACCOUNT_LIST_MAINTENANCE_LOCK = threading.Lock()
_ACCOUNT_LIST_IN_FLIGHT: Future | None = None
_ACCOUNT_LIST_MAINTENANCE_AT = 0.0


def _save_uploaded_image(upload: UploadFile, target: Path) -> None:
    suffix = target.suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        raise HTTPException(status_code=400, detail="unsupported image type")
    total = 0
    first = b""
    with target.open("wb") as output:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            if not first:
                first = chunk[:16]
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="image is too large")
            output.write(chunk)
    if not any(first.startswith(magic) for magic in IMAGE_MAGIC[suffix]):
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="invalid image content")


def _save_image_bytes(data: bytes, filename: str, target: Path) -> None:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        raise HTTPException(status_code=400, detail="unsupported image type")
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413 if data else 400, detail="image is too large" if data else "image is empty")
    if not any(data.startswith(magic) for magic in IMAGE_MAGIC[suffix]):
        raise HTTPException(status_code=400, detail="invalid image content")
    target.write_bytes(data)


def _reference_image_name(value: object, index: int, suffix: str = "") -> str:
    raw = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(character for character in raw if character >= " " and character != "\x7f").strip()
    fallback_suffix = str(suffix or "").lower() if str(suffix or "").lower() in ALLOWED_IMAGE_SUFFIXES else ""
    if not cleaned:
        return f"参考图-{max(1, int(index))}{fallback_suffix}"
    if len(cleaned) <= MAX_REFERENCE_IMAGE_NAME_LENGTH:
        return cleaned
    extension = Path(cleaned).suffix[:16]
    stem_limit = max(1, MAX_REFERENCE_IMAGE_NAME_LENGTH - len(extension))
    return f"{cleaned[:-len(extension)][:stem_limit]}{extension}" if extension else cleaned[:MAX_REFERENCE_IMAGE_NAME_LENGTH]


def _video_download_filename(meta: dict, task_id: str) -> str:
    names = meta.get("reference_image_names") if isinstance(meta.get("reference_image_names"), list) else []
    source_name = _reference_image_name(names[0], 1) if names else str(
        meta.get("video_name") or meta.get("task_name") or meta.get("prompt") or ""
    )
    stem = Path(source_name).stem.strip() if names and source_name else source_name.strip()
    stem = " ".join(stem.split())
    stem = "".join("_" if character in '<>:"/\\|?*' else character for character in stem).strip(" .")
    encoded = stem.encode("utf-8")[:180]
    while encoded:
        try:
            stem = encoded.decode("utf-8")
            break
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return f"{(stem or task_id)}.mp4"


def _video_referer(platform: str) -> str:
    return {
        "qianwen": "https://chat.qwen.ai/",
        "doubao": "https://www.doubao.com/",
    }.get(str(platform or "").lower(), "https://www.dola.com/")


def _validate_video_url(value: str) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    hostname = str(parsed.hostname or "").strip().lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise HTTPException(status_code=404, detail="video not found")
    if hostname in {"localhost", "0.0.0.0", "127.0.0.1", "::1"} or hostname.endswith(".local"):
        raise HTTPException(status_code=400, detail="invalid video host")
    return url


def _task_video_url(meta: dict[str, Any], result: dict[str, Any]) -> str:
    url = str(result.get("decoded_main_url") or "").strip()
    if not url or str(meta.get("platform") or "dola").strip().lower() != "doubao":
        return url
    source = str(result.get("doubao_result_source") or result.get("doubao_video_detection_source") or "").strip().lower()
    watermark_status = str(result.get("doubao_watermark_status") or "").strip().lower()
    allowed_sources = {"fallback_unwatermarked", "single_chain_explicit_unwatermarked", "network_explicit_unwatermarked"}
    return url if source in allowed_sources and watermark_status == "original" else ""
from .textfix import repair_text
from .version import __version__
from .worker import refund_account_quota_once, refund_temp_quota_once
from .users import add_user_points, adjust_user_video_quota, change_user_email_by_token_hash, change_user_password_by_token_hash, deduct_user_points, delete_user, has_verified_enabled_email, list_users, login_user, purchase_user_membership, register_user, repair_registered_user_tokens, reset_user_password_by_email, rotate_user_token_by_hash, set_user_concurrency, set_user_concurrency_by_token_hash, set_user_enabled, set_user_model_discounts, set_user_remote_generation_limit, sync_user_membership_by_token_hash, task_discount_units_by_token_hash, touch_user_by_token, touch_user_by_token_hash, user_balance_by_token_hash, user_identity_by_token_hash, user_model_discounts_by_token_hash, user_profile_by_token_hash, user_token_is_enabled


create_sem = None
query_sem = None
list_sem = None
delete_sem = None
_OWNER_CREATE_SEMAPHORES: dict[str, asyncio.Semaphore] = {}
OWNER_TASK_CREATION_CONCURRENCY = 1
MAX_BATCH_REFERENCE_AGE_SECONDS = 24 * 60 * 60
_CANCELED_BATCHES: dict[tuple[str, str], float] = {}
_CANCELED_BATCHES_LOCK = threading.RLock()
_RATE_LOCK = threading.RLock()
_RATE_BUCKETS: dict[str, list[float]] = {}
_RATE_BUCKET_LIMIT = 10_000
_BATCH_RECONCILE_AT = 0.0
_BATCH_RECOVER_AT = 0.0
_BATCH_ASSET_LOCK = threading.RLock()


def _owner_create_semaphore(access: AccessContext) -> asyncio.Semaphore:
    key = access.token_hash if access.is_temp else "admin"
    semaphore = _OWNER_CREATE_SEMAPHORES.get(key)
    if semaphore is None:
        semaphore = asyncio.Semaphore(OWNER_TASK_CREATION_CONCURRENCY)
        _OWNER_CREATE_SEMAPHORES[key] = semaphore
    return semaphore


def _batch_owner(access: AccessContext) -> str:
    return access.token_hash if access.is_temp else "admin"


def _set_batch_canceled(access: AccessContext, batch_id: str) -> None:
    normalized = str(batch_id or "").strip()[:100]
    if not normalized:
        return
    now = time.monotonic()
    with _CANCELED_BATCHES_LOCK:
        expired = [key for key, canceled_at in _CANCELED_BATCHES.items() if now - canceled_at > MAX_BATCH_REFERENCE_AGE_SECONDS]
        for key in expired:
            _CANCELED_BATCHES.pop(key, None)
        _CANCELED_BATCHES[(_batch_owner(access), normalized)] = now


def _batch_is_canceled(access: AccessContext, batch_id: str) -> bool:
    normalized = str(batch_id or "").strip()[:100]
    if not normalized:
        return False
    with _CANCELED_BATCHES_LOCK:
        return (_batch_owner(access), normalized) in _CANCELED_BATCHES


def _ensure_batch_active(access: AccessContext, batch_id: str) -> None:
    if batch_id and _batch_is_canceled(access, batch_id):
        raise HTTPException(status_code=409, detail="批量提交已停止")


def _batch_reference_path(reference_id: str) -> Path:
    normalized = str(reference_id or "").strip().lower()
    if len(normalized) != 32 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("invalid batch reference id")
    return app_config.DATA_DIR / "batch_references" / normalized


def _batch_job_assets_path(job_id: str) -> Path:
    normalized = str(job_id or "").strip().lower()
    if len(normalized) != 32 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("invalid batch job id")
    return app_config.DATA_DIR / "batch_job_assets" / normalized


def _batch_asset_upload_path(upload_id: str) -> Path:
    normalized = str(upload_id or "").strip().lower()
    if len(normalized) != 32 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("invalid batch asset upload id")
    return app_config.DATA_DIR / "batch_asset_uploads" / normalized


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _cleanup_batch_asset_uploads() -> None:
    uploads_dir = app_config.DATA_DIR / "batch_asset_uploads"
    if not uploads_dir.exists():
        return
    cutoff = time.time() - MAX_BATCH_REFERENCE_AGE_SECONDS
    for path in uploads_dir.iterdir():
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
        except OSError:
            continue


def _load_batch_asset_upload(upload_id: str) -> tuple[Path, dict[str, object]]:
    try:
        target_dir = _batch_asset_upload_path(upload_id)
        metadata = json.loads((target_dir / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="批量参考图上传已失效，请重新上传")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("images"), list):
        raise HTTPException(status_code=400, detail="批量参考图上传记录无效，请重新上传")
    return target_dir, metadata


def _save_batch_asset_chunk(
    owner_token_hash: str,
    batch_id: str,
    upload_id: str,
    entries: list[dict[str, object]],
    uploads: list[UploadFile],
) -> dict[str, object]:
    if len(entries) != len(uploads) or not uploads:
        raise HTTPException(status_code=400, detail="批量参考图分片不完整")
    if len(uploads) > MAX_BATCH_ASSET_CHUNK_FILES:
        raise HTTPException(status_code=400, detail="单次上传的参考图数量过多")
    _cleanup_batch_asset_uploads()
    normalized_upload_id = str(upload_id or "").strip().lower() or secrets.token_hex(16)
    temporary_paths: list[Path] = []
    with _BATCH_ASSET_LOCK:
        created = False
        if upload_id:
            target_dir, metadata = _load_batch_asset_upload(normalized_upload_id)
            if str(metadata.get("owner_token_hash") or "") != owner_token_hash or str(metadata.get("batch_id") or "") != batch_id:
                raise HTTPException(status_code=403, detail="批量参考图上传记录不可用")
        else:
            target_dir = _batch_asset_upload_path(normalized_upload_id)
            target_dir.mkdir(parents=True, exist_ok=False)
            created = True
            metadata = {
                "id": normalized_upload_id,
                "owner_token_hash": owner_token_hash,
                "batch_id": batch_id,
                "images": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        existing = {
            (int(item.get("row_index") or 0), int(item.get("image_index") or 0)): dict(item)
            for item in metadata.get("images", [])
            if isinstance(item, dict)
        }
        prepared: list[tuple[tuple[int, int], Path, str, str]] = []
        try:
            seen: set[tuple[int, int]] = set()
            for offset, (entry, upload) in enumerate(zip(entries, uploads, strict=True), start=1):
                row_index = int(entry.get("row_index") or 0)
                image_index = int(entry.get("image_index") or 0)
                key = (row_index, image_index)
                if row_index < 1 or row_index > 2000 or image_index < 1 or image_index > load_settings().max_image_count or key in seen:
                    raise HTTPException(status_code=400, detail="批量参考图位置无效")
                seen.add(key)
                suffix = Path(upload.filename or "").suffix.lower()
                if suffix not in IMAGE_MAGIC:
                    raise HTTPException(status_code=400, detail="unsupported image type")
                temporary = target_dir / f".{secrets.token_hex(8)}-{offset:02d}{suffix}"
                temporary_paths.append(temporary)
                _save_uploaded_image(upload, temporary)
                filename = f"{row_index:06d}-{image_index:02d}{suffix}"
                original_name = _reference_image_name(entry.get("name") or upload.filename, image_index, suffix)
                prepared.append((key, temporary, filename, original_name))
            replaced_keys = {item[0] for item in prepared}
            retained_bytes = sum(
                max(0, int(item.get("size") or 0))
                for key, item in existing.items()
                if key not in replaced_keys
            )
            total_bytes = retained_bytes + sum(path.stat().st_size for _, path, _, _ in prepared)
            if total_bytes > MAX_BATCH_ASSET_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="批量参考图总大小超过 2 GB")
            for key, temporary, filename, original_name in prepared:
                previous = existing.get(key, {})
                previous_name = str(previous.get("file") or "")
                previous_path = target_dir / previous_name if previous_name else None
                final_path = target_dir / filename
                if previous_path is not None and previous_path != final_path:
                    previous_path.unlink(missing_ok=True)
                temporary.replace(final_path)
                temporary_paths.remove(temporary)
                existing[key] = {
                    "row_index": key[0],
                    "image_index": key[1],
                    "file": filename,
                    "original_name": original_name,
                    "size": final_path.stat().st_size,
                }
            metadata["images"] = [existing[key] for key in sorted(existing)]
            metadata["total_bytes"] = total_bytes
            metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
            _write_json_atomic(target_dir / "metadata.json", metadata)
            return {
                "id": normalized_upload_id,
                "uploaded_count": len(metadata["images"]),
                "total_bytes": total_bytes,
            }
        except Exception:
            for path in temporary_paths:
                path.unlink(missing_ok=True)
            if created:
                shutil.rmtree(target_dir, ignore_errors=True)
            raise


def _consume_batch_asset_upload(
    upload_id: str,
    owner_token_hash: str,
    batch_id: str,
    rows: list[dict[str, object]],
    target_dir: Path,
) -> None:
    with _BATCH_ASSET_LOCK:
        source_dir, metadata = _load_batch_asset_upload(upload_id)
        if str(metadata.get("owner_token_hash") or "") != owner_token_hash or str(metadata.get("batch_id") or "") != batch_id:
            raise HTTPException(status_code=403, detail="批量参考图上传记录不可用")
        records = {
            (int(item.get("row_index") or 0), int(item.get("image_index") or 0)): dict(item)
            for item in metadata.get("images", [])
            if isinstance(item, dict)
        }
        expected = {
            (row_index, image_index)
            for row_index, row in enumerate(rows, start=1)
            for image_index in range(1, int(row.get("image_count") or 0) + 1)
        }
        if set(records) != expected:
            raise HTTPException(status_code=400, detail="批量任务参考图上传不完整，请重新提交")
        for row_index, row in enumerate(rows, start=1):
            row_records = [records[(row_index, image_index)] for image_index in range(1, int(row.get("image_count") or 0) + 1)]
            paths = [source_dir / str(item.get("file") or "") for item in row_records]
            if any(not path.is_file() for path in paths):
                raise HTTPException(status_code=400, detail="批量任务参考图上传不完整，请重新提交")
            row["image_files"] = [path.name for path in paths]
            row["image_names"] = [str(item.get("original_name") or "") for item in row_records]
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        source_dir.replace(target_dir)


def _restore_batch_asset_upload(upload_id: str, consumed_dir: Path) -> None:
    with _BATCH_ASSET_LOCK:
        target = _batch_asset_upload_path(upload_id)
        if consumed_dir.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            consumed_dir.replace(target)


def _delete_batch_asset_uploads(owner_token_hash: str, batch_id: str) -> int:
    uploads_dir = app_config.DATA_DIR / "batch_asset_uploads"
    if not uploads_dir.exists():
        return 0
    removed = 0
    with _BATCH_ASSET_LOCK:
        for path in uploads_dir.iterdir():
            try:
                metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if str(metadata.get("owner_token_hash") or "") != owner_token_hash or str(metadata.get("batch_id") or "") != batch_id:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed


def _cleanup_batch_references() -> None:
    references_dir = app_config.DATA_DIR / "batch_references"
    if not references_dir.exists():
        return
    try:
        protected = {str(job.get("reference_id") or "") for job in list_batch_jobs(None, active_only=True, limit=1000)}
    except Exception:
        protected = set()
    cutoff = time.time() - MAX_BATCH_REFERENCE_AGE_SECONDS
    for path in references_dir.iterdir():
        try:
            if path.name not in protected and path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
        except OSError:
            continue


def _save_batch_reference_bundle(owner_token_hash: str, batch_id: str, uploads: list[UploadFile]) -> dict:
    _cleanup_batch_references()
    reference_id = secrets.token_hex(16)
    target_dir = _batch_reference_path(reference_id)
    target_dir.mkdir(parents=True, exist_ok=False)
    saved: list[str] = []
    original_names: list[str] = []
    try:
        for index, upload in enumerate(uploads, start=1):
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in IMAGE_MAGIC:
                raise HTTPException(status_code=400, detail="unsupported image type")
            filename = f"{index:02d}{suffix}"
            _save_uploaded_image(upload, target_dir / filename)
            saved.append(filename)
            original_names.append(_reference_image_name(upload.filename, index, suffix))
        metadata = {
            "id": reference_id,
            "owner_token_hash": owner_token_hash,
            "batch_id": str(batch_id or "")[:100],
            "images": saved,
            "original_names": original_names,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        (target_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        return metadata
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise


def _batch_reference_bundle(reference_id: str, owner_token_hash: str, batch_id: str) -> tuple[list[Path], list[str]]:
    try:
        target_dir = _batch_reference_path(reference_id)
        metadata = json.loads((target_dir / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="批量共用参考图已失效，请重新上传")
    if str(metadata.get("owner_token_hash") or "") != owner_token_hash or str(metadata.get("batch_id") or "") != batch_id:
        raise HTTPException(status_code=400, detail="批量共用参考图不可用，请重新上传")
    paths = [target_dir / str(name) for name in metadata.get("images", []) if str(name)]
    if not paths or any(not path.is_file() for path in paths):
        raise HTTPException(status_code=400, detail="批量共用参考图不完整，请重新上传")
    raw_names = metadata.get("original_names") if isinstance(metadata.get("original_names"), list) else []
    names = [
        _reference_image_name(raw_names[index] if index < len(raw_names) else "", index + 1, path.suffix)
        for index, path in enumerate(paths)
    ]
    return paths, names


def _batch_reference_paths(reference_id: str, owner_token_hash: str, batch_id: str) -> list[Path]:
    return _batch_reference_bundle(reference_id, owner_token_hash, batch_id)[0]


def _delete_batch_reference_bundle(reference_id: str, owner_token_hash: str) -> bool:
    try:
        target_dir = _batch_reference_path(reference_id)
        metadata = json.loads((target_dir / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if str(metadata.get("owner_token_hash") or "") != owner_token_hash:
        return False
    shutil.rmtree(target_dir, ignore_errors=True)
    return True
_RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
if count >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local retry = 1
  if oldest[2] then retry = math.max(1, math.ceil((tonumber(oldest[2]) + window - now) / 1000)) end
  return {0, retry}
end
redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, window)
return {1, 0}
"""
quota_reset_task = None
account_cleanup_task = None
task_cache_cleanup_task = None
proxy_health_task = None
resource_alert_task = None
LOCAL_TZ = timezone(timedelta(hours=8))


def _redis_rate_limit(key: str, limit: int, window: int) -> tuple[bool, int] | None:
    try:
        from .task_queue import get_task_queue

        client = getattr(get_task_queue(), "client", None)
        if client is None:
            return None
        now_ms = int(time.time() * 1000)
        digest = hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()
        member = f"{now_ms}:{secrets.token_hex(6)}"
        result = client.eval(_RATE_LIMIT_SCRIPT, 1, f"dola:request-rate:{digest}", now_ms, window * 1000, limit, member)
        return bool(int(result[0])), max(0, int(result[1]))
    except Exception:
        return None


def _memory_rate_limit(key: str, limit: int, window: int) -> tuple[bool, int]:
    now = time.monotonic()
    with _RATE_LOCK:
        recent = [stamp for stamp in _RATE_BUCKETS.get(key, []) if now - stamp < window]
        if len(recent) >= limit:
            return False, max(1, int(window - (now - recent[0])))
        recent.append(now)
        _RATE_BUCKETS[key] = recent
        if len(_RATE_BUCKETS) > _RATE_BUCKET_LIMIT:
            for stale_key in [item for item, stamps in _RATE_BUCKETS.items() if not stamps or now - stamps[-1] >= window]:
                _RATE_BUCKETS.pop(stale_key, None)
            while len(_RATE_BUCKETS) > _RATE_BUCKET_LIMIT:
                _RATE_BUCKETS.pop(next(iter(_RATE_BUCKETS)))
    return True, 0


async def _rate_limit(request: Request, scope: str, limit: int, window: int, identity: str = "") -> None:
    key = f"{scope}:{request.client.host if request.client else 'unknown'}:{identity}"
    result = await asyncio.to_thread(_redis_rate_limit, key, limit, window)
    allowed, retry_after = result if result is not None else _memory_rate_limit(key, limit, window)
    if not allowed:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试", headers={"Retry-After": str(retry_after or 1)})


def _idempotency_key(value: str | None) -> str:
    key = str(value or "").strip()
    if key and (len(key) > 128 or any(ord(char) < 33 or ord(char) > 126 for char in key)):
        raise HTTPException(status_code=400, detail="invalid Idempotency-Key")
    return key


def _transaction_user_id(access: AccessContext) -> str:
    if not access.is_temp:
        return ""
    try:
        return str(user_identity_by_token_hash(access.token_hash).get("id") or "")
    except KeyError:
        return ""


async def _storage_call(function, *args, attempts: int = 3, **kwargs):
    attempts = max(1, int(attempts))
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except Exception as exc:
            if attempt >= attempts or not is_transient_postgres_error(exc):
                raise
            logger.warning(
                "transient storage operation failed (operation=%s attempt=%s/%s error=%s)",
                getattr(function, "__name__", type(function).__name__),
                attempt,
                attempts,
                type(exc).__name__,
            )
            await asyncio.sleep(0.15 * (2 ** (attempt - 1)))


async def _record_activity_safe(user_id: str, action: str, title: str, **kwargs) -> None:
    if not str(user_id or "").strip():
        return
    try:
        await asyncio.to_thread(record_activity, user_id, action, title, **kwargs)
    except Exception:
        logger.exception("user activity write failed (user_id=%s action=%s)", user_id, action)


async def _record_admin_action_safe(action: str, title: str, **kwargs) -> None:
    try:
        await asyncio.to_thread(record_admin_action, action, title, **kwargs)
    except Exception:
        logger.exception("admin audit write failed (action=%s)", action)


def _request_client_key(request: Request) -> str:
    return str(request.client.host if request.client else "unknown")[:80]


def _request_fingerprint(route: str, owner: str, payload: dict) -> str:
    raw = json.dumps({"route": route, "owner": owner, "payload": payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def next_quota_reset_at() -> str:
    now = datetime.now(LOCAL_TZ)
    tomorrow = now.date() + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=LOCAL_TZ).isoformat()


async def account_quota_reset_loop() -> None:
    import asyncio

    while True:
        reset_daily_account_quotas_if_needed()
        now = datetime.now(LOCAL_TZ)
        tomorrow = now.date() + timedelta(days=1)
        next_reset = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=LOCAL_TZ)
        await asyncio.sleep(max(1, (next_reset - now).total_seconds()))


async def account_flagged_cleanup_loop() -> None:
    while True:
        now = datetime.now(LOCAL_TZ)
        if now.hour == 23:
            try:
                result = await asyncio.to_thread(cleanup_flagged_accounts, now)
                if result.get("removed"):
                    _clear_account_list_cache()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduled flagged account cleanup failed")
            await asyncio.sleep(60)
            continue
        next_run = datetime(now.year, now.month, now.day, 23, tzinfo=LOCAL_TZ)
        if next_run <= now:
            next_run += timedelta(days=1)
        await asyncio.sleep(max(1, (next_run - now).total_seconds()))


async def task_cache_cleanup_loop() -> None:
    import asyncio

    while True:
        try:
            settings = load_settings()
            await asyncio.to_thread(cleanup_terminal_tasks_before_local_day, 1, active_task_ids())
            expired_batches = await asyncio.to_thread(cleanup_batch_history, settings.batch_history_retention_days, 5000)
            for job in expired_batches:
                job_id = str(job.get("id") or "")
                if job_id:
                    try:
                        await asyncio.to_thread(shutil.rmtree, _batch_job_assets_path(job_id), True)
                    except ValueError:
                        pass
            await asyncio.to_thread(_cleanup_batch_references)
            await asyncio.to_thread(_cleanup_batch_asset_uploads)
            await asyncio.to_thread(prune_admin_actions, 90, 10_000)
            await asyncio.to_thread(archive_old_transactions, 365, 5000)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduled data retention cleanup failed")
        await asyncio.sleep(6 * 60 * 60)


async def resource_alert_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(collect_resource_snapshot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("resource alert collection failed")
        await asyncio.sleep(60)


async def refresh_proxy_health_once() -> dict[str, object]:
    settings = load_settings()
    if not settings.proxy_enabled:
        return {"checked": False, "switched": False}
    if settings.proxy_source == "account":
        pool = await _measure_account_proxies([], settings)
        available = [item for item in pool["proxies"] if item.get("latency_status") == "available"]
        return {"checked": True, "switched": False, "eligible_count": len(pool["proxies"]), "available_count": len(available)}
    if not settings.proxy_auto_select or not settings.proxy_subscription_url:
        return {"checked": False, "switched": False}
    nodes = await fetch_subscription_node_list(
        settings.proxy_subscription_url,
        timeout_seconds=settings.proxy_api_timeout_seconds,
        refresh_seconds=settings.proxy_subscription_refresh_seconds,
        force=True,
    )
    countries = set(settings.proxy_auto_countries)
    if not countries:
        raise RuntimeError("自动选择节点前请至少勾选一个国家")
    eligible = tuple(node for node in nodes if node.country in countries)
    if not eligible:
        raise RuntimeError("所选国家没有可用节点")
    await measure_node_delays(eligible, settings.proxy_subscription_url, settings.proxy_api_timeout_seconds)
    threshold = settings.proxy_latency_threshold_ms
    measured = [
        (int(payload["latency_ms"]), node)
        for node in eligible
        if (payload := node_payload(node)).get("latency_ms") is not None
        and int(payload["latency_ms"]) <= threshold
    ]
    if not measured:
        raise RuntimeError("所选国家没有低于高延迟阈值的可用节点")
    best_delay, best = min(measured, key=lambda item: item[0])
    current = next((item for item in measured if item[1].id == settings.proxy_selected_node), None)
    should_switch = current is None or best_delay + 50 < current[0]
    if should_switch:
        await activate_mihomo_node(
            best,
            settings.proxy_subscription_url,
            settings.proxy_api_timeout_seconds,
            settings.proxy_subscription_refresh_seconds,
        )
        update_config({"proxy_selected_node": best.id})
    return {
        "checked": True,
        "switched": should_switch,
        "selected_node": best.id if should_switch else current[1].id,
        "latency_ms": best_delay if should_switch else current[0],
        "eligible_count": len(eligible),
    }


async def proxy_health_loop() -> None:
    await asyncio.sleep(15)
    while True:
        try:
            await refresh_proxy_health_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        refreshed_at = time.monotonic()
        while True:
            interval = load_settings().proxy_health_refresh_seconds
            remaining = interval - (time.monotonic() - refreshed_at)
            if remaining <= 0:
                break
            await asyncio.sleep(min(5.0, remaining))


def _batch_job_is_active(job_id: str, owner_token_hash: str) -> bool:
    job = get_batch_job(job_id, owner_token_hash)
    return bool(job and str(job.get("status") or "") in {"queued", "running"} and not job.get("canceled_at"))


async def _create_scheduled_batch_task(claim: dict[str, object]) -> str:
    assert create_sem is not None
    job = dict(claim.get("job") or {})
    row = dict(claim.get("row") or {})
    job_id = str(job.get("id") or "")
    owner_hash = str(job.get("owner_token_hash") or "")
    row_index = max(1, int(row.get("index") or 0))
    access = await _storage_call(get_temp_context_by_hash, owner_hash)
    if not access or not access.is_temp or not user_token_is_enabled(owner_hash):
        raise ValueError("用户账号不可用")
    if await _storage_call(sync_user_membership_by_token_hash, owner_hash):
        access = await _storage_call(get_temp_context_by_hash, owner_hash)
    if not access or not _batch_job_is_active(job_id, owner_hash):
        raise HTTPException(status_code=409, detail="批量提交已停止")

    prompt = repair_text(str(row.get("prompt") or "").strip())
    ratio = str(job.get("ratio") or DEFAULT_RATIO).strip()
    platform, model = validate_task_platform_model(
        str(job.get("platform") or "dola"),
        str(job.get("model") or "Seedance 2.0"),
    )
    duration = validate_task_duration(platform, model, int(job.get("duration") or 0) or None)
    if not prompt or ratio not in VALID_RATIOS:
        raise ValueError("批量任务参数无效")
    shared_reference_paths: list[Path] = []
    shared_reference_names: list[str] = []
    reference_id = str(job.get("reference_id") or "")
    reference_count = max(0, int(job.get("reference_count") or 0))
    reference_is_real_person = bool(job.get("reference_is_real_person"))
    if reference_id:
        shared_reference_paths, shared_reference_names = await asyncio.to_thread(
            _batch_reference_bundle,
            reference_id,
            owner_hash,
            str(job.get("reference_batch_id") or job_id),
        )
        if reference_count <= 0 or len(shared_reference_paths) < reference_count:
            raise ValueError("批量共用参考图数量无效，请重新上传")
        shared_reference_paths = shared_reference_paths[:reference_count]
        shared_reference_names = shared_reference_names[:reference_count]
    assets_root = _batch_job_assets_path(job_id)
    row_reference_paths: list[Path] = []
    for name in row.get("image_files", []):
        candidate = assets_root / Path(str(name)).name
        if not candidate.is_file():
            raise ValueError("批量任务参考图不完整，请重新上传")
        row_reference_paths.append(candidate)
    raw_row_reference_names = row.get("image_names") if isinstance(row.get("image_names"), list) else []
    row_reference_names = [
        _reference_image_name(raw_row_reference_names[index] if index < len(raw_row_reference_names) else "", index + 1, path.suffix)
        for index, path in enumerate(row_reference_paths)
    ]
    if len(shared_reference_paths) + len(row_reference_paths) > load_settings().max_image_count:
        raise ValueError("每条任务最多添加 9 张参考图")

    key = f"batch-job-{job_id}-{row_index:06d}"
    fingerprint = _request_fingerprint(
        "batch-jobs",
        owner_hash,
        {"job_id": job_id, "row_index": row_index, "prompt": prompt, "ratio": ratio, "duration": duration, "platform": platform, "model": model, "reference_is_real_person": reference_is_real_person, "images": [path.name for path in row_reference_paths]},
    )
    meta: dict[str, object] | None = None
    reserved_access: AccessContext | None = None
    created = False
    resumed_initializing = False
    async with _owner_create_semaphore(access), create_sem:
        await asyncio.to_thread(_admit_task_creation)
        try:
            meta, created = await _storage_call(
                find_or_create_task,
                prompt,
                ratio,
                owner_hash,
                platform,
                model,
                "video",
                key,
                fingerprint,
                "batch-jobs",
                duration,
                job_id,
                row_index,
                int(row.get("sheet_row") or 0),
            )
            resumed_initializing = not created and str(meta.get("status") or "") == "initializing"
            if not created and not resumed_initializing:
                return str(meta["id"])
            base_cost_units = model_cost_units(platform, model, "video", duration)
            discount_units = await _storage_call(task_discount_units_by_token_hash, owner_hash, platform, model)
            cost_units = max(1, base_cost_units - discount_units)
            user_id = await _storage_call(_transaction_user_id, access)
            reserved_access = await _storage_call(reserve_temp_quota, access, str(meta["id"]), cost_units, user_id=user_id)
            if not _batch_job_is_active(job_id, owner_hash):
                raise HTTPException(status_code=409, detail="批量提交已停止")
            reservation = await _storage_call(get_temp_reservation, owner_hash, str(meta["id"]))
            charged_units = int(reservation.get("units") or 0)
            if user_id and reservation:
                free_used = bool(reservation.get("free"))
                await _storage_call(
                    record_transaction,
                    user_id,
                    "video_quota_consume" if free_used else "consume",
                    0 if free_used else -charged_units,
                    "视频额度任务消费" if free_used else "视频任务消费",
                    balance_units=reserved_access.credit_units,
                    video_quota_change=-1 if free_used else 0,
                    video_quota_balance=reserved_access.free_remaining,
                    reference_id=str(meta["id"]),
                    detail=f"任务 ID：{meta['id']}\n{PLATFORM_LABELS.get(platform, platform)} / {model}",
                    transaction_id=f"task-{str(meta['id'])[:27]}",
                )
        except Exception:
            if meta and (created or resumed_initializing):
                await _storage_call(refund_temp_quota_hash, owner_hash, str(meta["id"]), attempts=2)
                await _storage_call(delete_task, str(meta["id"]), attempts=2)
            raise

        saved_paths: list[Path] = []
        try:
            for index, source in enumerate([*shared_reference_paths, *row_reference_paths], start=1):
                target = images_dir(str(meta["id"])) / f"{index:02d}{source.suffix.lower()}"
                await asyncio.to_thread(shutil.copy2, source, target)
                saved_paths.append(target)
            await _storage_call(
                set_task_images,
                str(meta["id"]),
                saved_paths,
                [*shared_reference_names, *row_reference_names],
            )
            await _storage_call(update_meta, str(meta["id"]), reference_is_real_person=reference_is_real_person)
            if not _batch_job_is_active(job_id, owner_hash):
                raise HTTPException(status_code=409, detail="批量提交已停止")
            await _storage_call(finalize_task_creation, str(meta["id"]))
        except Exception:
            if reserved_access:
                await _storage_call(refund_temp_quota_hash, owner_hash, str(meta["id"]), attempts=2)
            await _storage_call(delete_task, str(meta["id"]), attempts=2)
            raise
        if user_id:
            await _record_activity_safe(
                user_id,
                "task_submit",
                "提交视频生成任务",
                reference_id=str(meta["id"]),
                detail=f"{model} / {ratio} / 多任务第 {row_index} 条",
            )
        return str(meta["id"])


async def _reconcile_persistent_batch_jobs(jobs: list[dict[str, object]]) -> None:
    all_task_ids = list(dict.fromkeys(
        str(row.get("task_id") or "")
        for job in jobs
        for row in job.get("rows", [])
        if isinstance(row, dict)
        and str(row.get("task_id") or "")
        and str(row.get("status") or "") not in {"completed", "failed", "canceled"}
    ))
    states = await asyncio.to_thread(task_states, all_task_ids) if all_task_ids else []
    all_payloads = {
        task_id: {
            "status": str(meta.get("status") or ""),
            "error": str(meta.get("error") or ""),
            "video_url": _task_video_url(meta, result),
        }
        for task_id, meta, result in states
    }
    for job in jobs:
        job_id = str(job.get("id") or "")
        task_ids = [
            str(row.get("task_id") or "")
            for row in job.get("rows", [])
            if isinstance(row, dict)
            and str(row.get("task_id") or "")
            and str(row.get("status") or "") not in {"completed", "failed", "canceled"}
        ]
        if not task_ids:
            continue
        payloads = {task_id: all_payloads[task_id] for task_id in task_ids if task_id in all_payloads}
        needs_update = any(
            payload.get("video_url") or str(payload.get("status") or "") in {"failed", "canceled"}
            for payload in payloads.values()
        )
        if not needs_update:
            continue
        updated = await asyncio.to_thread(reconcile_batch_job, job_id, payloads)
        if str(updated.get("status") or "") in {"completed", "canceled"}:
            await asyncio.to_thread(shutil.rmtree, _batch_job_assets_path(job_id), True)


async def batch_scheduler_tick() -> bool:
    global _BATCH_RECONCILE_AT, _BATCH_RECOVER_AT
    coordinator = batch_coordinator()
    with coordinator.lease(120) as acquired:
        if not acquired:
            return False
        if time.monotonic() >= _BATCH_RECOVER_AT:
            await asyncio.to_thread(recover_stale_creating_rows)
            _BATCH_RECOVER_AT = time.monotonic() + 30.0
        jobs = await asyncio.to_thread(list_batch_jobs, None, active_only=True, limit=1000)
        if not jobs:
            return False
        if time.monotonic() >= _BATCH_RECONCILE_AT:
            await _reconcile_persistent_batch_jobs(jobs)
            _BATCH_RECONCILE_AT = time.monotonic() + 1.0
            jobs = await asyncio.to_thread(list_batch_jobs, None, active_only=True, limit=1000)
        active_rows = await asyncio.to_thread(list_task_metas_by_statuses, {"pending", "running", "submitted"})
        global_capacity = BROWSER_SUBMISSION_CONCURRENCY
        if len(active_rows) >= global_capacity:
            return False
        owner_limits: dict[str, int] = {}
        access_cache: dict[str, AccessContext | None] = {}
        for job in jobs:
            if not any(str(row.get("status") or "") == "queued" for row in job.get("rows", []) if isinstance(row, dict)):
                continue
            owner_hash = str(job.get("owner_token_hash") or "")
            if owner_hash not in access_cache:
                access_cache[owner_hash] = await _storage_call(get_temp_context_by_hash, owner_hash)
            access = access_cache[owner_hash]
            if not access or not user_token_is_enabled(owner_hash):
                continue
            owner_limit = min(max(1, int(job.get("concurrency") or 1)), max(1, int(access.concurrency or 1)))
            owner_limits[owner_hash] = max(owner_limits.get(owner_hash, 0), owner_limit)
        fair_limits = fair_owner_capacity_limits(owner_limits, global_capacity)
        eligible: set[str] = set()
        for owner_hash, fair_limit in fair_limits.items():
            owner_active = await _storage_call(active_task_count_for_owner, owner_hash)
            if owner_active < fair_limit:
                eligible.add(owner_hash)
        owner = await asyncio.to_thread(coordinator.next_owner, eligible)
        if not owner:
            return False
        claim = await asyncio.to_thread(claim_next_batch_row, owner)
        if not claim:
            return False
        job_id = str(dict(claim.get("job") or {}).get("id") or "")
        row_index = int(dict(claim.get("row") or {}).get("index") or 0)
        try:
            task_id = await _create_scheduled_batch_task(claim)
            await asyncio.to_thread(finish_batch_row_creation, job_id, row_index, task_id)
        except (QuotaExceeded, ValueError) as exc:
            await asyncio.to_thread(fail_or_requeue_batch_row, job_id, row_index, str(exc), retry=False)
        except HTTPException as exc:
            retry = int(exc.status_code) >= 500
            await asyncio.to_thread(fail_or_requeue_batch_row, job_id, row_index, str(exc.detail), retry=retry)
        except Exception as exc:
            logger.exception("persistent batch row creation failed (job_id=%s row=%s)", job_id, row_index)
            await asyncio.to_thread(fail_or_requeue_batch_row, job_id, row_index, "任务创建暂时繁忙，请稍后重试", retry=True)
        return True


async def batch_scheduler_loop() -> None:
    await asyncio.sleep(1)
    while True:
        try:
            scheduled = await batch_scheduler_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("persistent batch scheduler tick failed")
            scheduled = False
        await asyncio.sleep(0.15 if scheduled else 1.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    global create_sem, query_sem, list_sem, delete_sem, quota_reset_task, account_cleanup_task, task_cache_cleanup_task, proxy_health_task, resource_alert_task, batch_scheduler_task, _ACCOUNT_LIST_MAINTENANCE_AT
    with _RATE_LOCK:
        _RATE_BUCKETS.clear()
    clear_registration_security_state()
    _OWNER_CREATE_SEMAPHORES.clear()
    startup_config = ensure_config()
    validate_startup_credentials(startup_config)
    if postgres_enabled():
        ensure_postgres_schema()
    if int(startup_config.get("account_quota_policy_version") or 0) < 1:
        sync_account_default_quotas(load_settings().account_default_quotas)
        update_config({"account_quota_policy_version": 1})
    migrate_ten_second_accounts_to_api()
    purge_legacy_cards()
    running_marker = DATA_DIR / ".service-running"
    running_marker.parent.mkdir(parents=True, exist_ok=True)
    running_marker.write_text(str(time.time()), encoding="utf-8")
    repair_registered_user_tokens()
    for stale_task in fail_initializing_tasks():
        refund_temp_quota_once(str(stale_task.get("id") or ""), str(stale_task.get("owner_token_hash") or ""))
    cleanup_flagged_accounts()
    reset_daily_account_quotas_if_needed()
    reconcile_account_quotas()
    _clear_account_list_cache()
    _ACCOUNT_LIST_MAINTENANCE_AT = time.monotonic() + ACCOUNT_LIST_MAINTENANCE_SECONDS
    create_sem = asyncio.Semaphore(4)
    query_sem = asyncio.Semaphore(5)
    list_sem = asyncio.Semaphore(8)
    delete_sem = asyncio.Semaphore(1)
    quota_reset_task = asyncio.create_task(account_quota_reset_loop())
    account_cleanup_task = asyncio.create_task(account_flagged_cleanup_loop())
    task_cache_cleanup_task = asyncio.create_task(task_cache_cleanup_loop())
    proxy_health_task = asyncio.create_task(proxy_health_loop())
    resource_alert_task = asyncio.create_task(resource_alert_loop())
    batch_scheduler_task = asyncio.create_task(batch_scheduler_loop())
    try:
        yield
    finally:
        running_marker.unlink(missing_ok=True)
        if quota_reset_task:
            quota_reset_task.cancel()
            with suppress(asyncio.CancelledError):
                await quota_reset_task
        if account_cleanup_task:
            account_cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await account_cleanup_task
        if task_cache_cleanup_task:
            task_cache_cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await task_cache_cleanup_task
        if proxy_health_task:
            proxy_health_task.cancel()
            with suppress(asyncio.CancelledError):
                await proxy_health_task
        if resource_alert_task:
            resource_alert_task.cancel()
            with suppress(asyncio.CancelledError):
                await resource_alert_task
        if batch_scheduler_task:
            batch_scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await batch_scheduler_task
        _OWNER_CREATE_SEMAPHORES.clear()


app = FastAPI(title="Fetch Task Service", version=__version__, lifespan=lifespan)
ADMIN_DIR = Path(__file__).resolve().parent / "admin"
BLOCKED_PROBE_SUFFIXES = {".asp", ".aspx", ".bak", ".cgi", ".env", ".ini", ".php", ".sql"}


def _is_sensitive_probe_path(path: str) -> bool:
    normalized = str(path or "").lower()
    segments = tuple(segment for segment in normalized.split("/") if segment)
    if any(segment.startswith(".") and segment != ".well-known" for segment in segments):
        return True
    return any(normalized.endswith(suffix) for suffix in BLOCKED_PROBE_SUFFIXES)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = JSONResponse(status_code=404, content={"detail": "Not Found"}) if _is_sensitive_probe_path(request.url.path) else await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https:; media-src 'self' blob: https:; connect-src 'self' https:; "
        "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    if _request_is_secure(request):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response

if ADMIN_DIR.exists():
    app.mount("/admin/assets", StaticFiles(directory=ADMIN_DIR), name="admin-assets")


@app.get("/health/live")
async def health_live():
    return {"ok": True, "version": __version__}


def _request_is_secure(request: Request) -> bool:
    forwarded = str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


def _set_client_session_cookie(response: JSONResponse, request: Request, token_hash: str, previous_session: str = "") -> None:
    if previous_session:
        delete_client_session(previous_session)
    response.set_cookie(
        CLIENT_SESSION_COOKIE_NAME,
        create_client_session(token_hash),
        max_age=CLIENT_SESSION_TTL_SECONDS,
        httponly=True,
        secure=_request_is_secure(request),
        samesite="strict",
        path="/",
    )


def _clear_client_session_cookie(response: JSONResponse, request: Request) -> None:
    response.delete_cookie(
        CLIENT_SESSION_COOKIE_NAME,
        path="/",
        secure=_request_is_secure(request),
        httponly=True,
        samesite="strict",
    )


def _validate_cookie_request(request: Request) -> None:
    if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return
    fetch_site = str(request.headers.get("sec-fetch-site") or "").strip().lower()
    if fetch_site == "cross-site":
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    origin = str(request.headers.get("origin") or "").strip().rstrip("/").lower()
    if not origin:
        return
    scheme = "https" if _request_is_secure(request) else request.url.scheme
    expected_origin = f"{scheme}://{request.headers.get('host') or request.url.netloc}".rstrip("/").lower()
    if not hmac.compare_digest(origin, expected_origin):
        raise HTTPException(status_code=403, detail="cross-site request rejected")


async def require_token(
    request: Request,
    x_api_token: Annotated[str | None, Header(alias="X-API-Token")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> AccessContext:
    configured = load_settings().api_token
    supplied = x_api_token or ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if configured and supplied == configured:
        return AccessContext(token_hash=hash_token(supplied), is_admin=True, is_temp=False)
    settings = load_settings()
    def resolve_temp_context() -> AccessContext | None:
        temp_context = get_temp_context(supplied)
        if temp_context and sync_user_membership_by_token_hash(temp_context.token_hash):
            temp_context = get_temp_context(supplied)
        if temp_context and user_token_is_enabled(temp_context.token_hash):
            touch_user_by_token(supplied)
            return temp_context
        return None

    temp_context = await asyncio.to_thread(resolve_temp_context)
    if temp_context:
        return temp_context
    if supplied:
        raise HTTPException(status_code=403, detail="forbidden")
    async def resolve_client_cookie() -> AccessContext | None:
        client_token_hash = await asyncio.to_thread(client_session_token_hash, request.cookies.get(CLIENT_SESSION_COOKIE_NAME, ""))
        if not client_token_hash:
            return None

        def resolve_client_session() -> AccessContext | None:
            context = get_temp_context_by_hash(client_token_hash)
            if context and sync_user_membership_by_token_hash(context.token_hash):
                context = get_temp_context_by_hash(client_token_hash)
            if context and user_token_is_enabled(context.token_hash):
                touch_user_by_token_hash(context.token_hash)
                return context
            return None

        return await asyncio.to_thread(resolve_client_session)

    portal_hint = str(request.headers.get("x-dola-portal") or "").strip().lower()
    prefer_client = portal_hint == "client" or request.url.path.startswith(("/auth/client", "/auth/access-state"))
    if prefer_client:
        client_context = await resolve_client_cookie()
        if client_context:
            _validate_cookie_request(request)
            return client_context
    session_owner = session_username(request.cookies.get(SESSION_COOKIE_NAME, ""))
    if session_owner and hmac.compare_digest(session_owner, settings.admin_username):
        _validate_cookie_request(request)
        return AccessContext(token_hash=hash_token(f"admin:{session_owner}"), is_admin=True, is_temp=False)
    if not prefer_client:
        client_context = await resolve_client_cookie()
        if client_context:
            _validate_cookie_request(request)
            return client_context
    raise HTTPException(status_code=403, detail="forbidden")


async def require_admin(access: Annotated[AccessContext, Depends(require_token)]) -> AccessContext:
    if not access.is_admin:
        raise HTTPException(status_code=403, detail="forbidden")
    return access


async def require_account_access(
    request: Request,
    x_account_access_key: Annotated[str | None, Header(alias="X-Account-Access-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    supplied = str(x_account_access_key or "").strip()
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    identity = hashlib.sha256(supplied.encode("utf-8")).hexdigest()[:16] if supplied else "missing"
    if not await asyncio.to_thread(verify_account_access_key, supplied):
        await _rate_limit(request, "account-access-auth", 20, 60, identity)
        raise HTTPException(status_code=403, detail="访问密钥无效或已停用")
    await _rate_limit(request, "account-access", 240, 60, identity)
    return identity


class OpenAIAPIError(Exception):
    def __init__(self, status_code: int, message: str, error_type: str, param: str | None = None, code: str | None = None, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.param = param
        self.code = code
        self.headers = headers or {}


@app.exception_handler(OpenAIAPIError)
async def openai_error_handler(_request: Request, exc: OpenAIAPIError):
    headers = dict(exc.headers)
    if exc.status_code == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(status_code=exc.status_code, headers=headers, content={"error": {"message": exc.message, "type": exc.error_type, "param": exc.param, "code": exc.code}})


async def require_openai_token(authorization: Annotated[str | None, Header()] = None) -> AccessContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise OpenAIAPIError(401, "Missing bearer token", "authentication_error", code="invalid_api_key")
    supplied = authorization[7:].strip()
    configured = load_settings().api_token
    if configured and supplied == configured:
        return AccessContext(token_hash=hash_token(supplied), is_admin=True, is_temp=False)
    def resolve_temp_context() -> AccessContext | None:
        context = get_temp_context(supplied)
        if context and sync_user_membership_by_token_hash(context.token_hash):
            context = get_temp_context(supplied)
        if context and user_token_is_enabled(context.token_hash):
            touch_user_by_token(supplied)
            return context
        return None

    context = await asyncio.to_thread(resolve_temp_context)
    if context:
        return context
    raise OpenAIAPIError(401, "Invalid API key", "authentication_error", code="invalid_api_key")


class OpenAIMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    content: str


class OpenAIChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    messages: list[OpenAIMessage]
    stream: bool = False
    n: int = 1
    ratio: str = DEFAULT_RATIO
    task_type: str = "video"
    duration: int | None = None


class BulkTaskRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_ids: list[str] = Field(default_factory=list)
    retry_all: bool = False
    q: str = ""
    platform: str = ""


class BatchRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_indices: list[int] = Field(default_factory=list)
    retry_all: bool = False


async def require_temp(access: Annotated[AccessContext, Depends(require_token)]) -> AccessContext:
    if not access.is_temp:
        raise HTTPException(status_code=403, detail="forbidden")
    return access


def _json(data: dict | list, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=data, status_code=status_code)


def _health_payload(access: AccessContext) -> dict:
    settings = load_settings()
    from .task_queue import get_task_queue

    queue_health = get_task_queue().health()
    platform_guard = PlatformGuard(getattr(get_task_queue(), "client", None))
    effective_workers, resource_health = adaptive_worker_limit(BROWSER_SUBMISSION_CONCURRENCY, BROWSER_SUBMISSION_CONCURRENCY)
    resource_health = {**resource_health, "effective_workers": effective_workers, "browser_pool_capacity": BROWSER_SUBMISSION_CONCURRENCY}
    browser_error = ""
    try:
        browser_path = resolve_browser_executable(settings.browser_executable_path)
    except Exception as exc:
        browser_path = None
        browser_error = str(exc)[:500]
    browser_ok = bool(browser_path)
    data = {
        "ok": True,
        "version": __version__,
        "status": "healthy" if queue_health["ok"] and browser_ok else "degraded",
        "role": "admin" if access.is_admin else "client",
        "browser_workers": BROWSER_SUBMISSION_CONCURRENCY,
        "active": sorted(active_task_ids()),
        "components": {
            "queue": {key: value for key, value in queue_health.items() if key != "error"},
            "browser": {
                "ok": browser_ok,
                "process_limit": BROWSER_POOL_PROCESSES,
                "contexts_per_process": BROWSER_CONTEXTS_PER_PROCESS,
                "submission_capacity": BROWSER_SUBMISSION_CONCURRENCY,
            },
            "resources": resource_health,
            "platforms": {platform: platform_guard.snapshot(platform) for platform in PLATFORM_LABELS},
        },
    }
    if access.is_admin:
        monitoring = latest_resource_snapshot()
        data["admin_username"] = settings.admin_username
        data["remote_generation_limit"] = 0
        try:
            data["remote_generation_active"] = len(list_task_metas_by_statuses({"submitted"}, platform="dola"))
        except Exception:
            data["remote_generation_active"] = 0
        data["components"]["queue"]["error"] = queue_health.get("error", "")
        data["components"]["browser"]["executable_path"] = browser_path or ""
        data["components"]["browser"]["error"] = browser_error
        data["components"]["monitoring"] = monitoring
        if monitoring.get("level") == "critical":
            data["status"] = "degraded"
    if access.is_temp:
        data.update(_client_access_payload(access))
        data["browser_workers"] = access.concurrency
    return data


def _client_access_payload(access: AccessContext) -> dict:
    balance = user_balance_by_token_hash(access.token_hash)
    try:
        user_name = str(user_identity_by_token_hash(access.token_hash).get("username") or "")
    except KeyError:
        user_name = temp_token_remarks().get(access.token_hash, "")
    return {
        "quota": {
            "limit": access.limit,
            "used": access.used,
            "remaining": access.remaining,
            **balance,
        },
        "token_concurrency": access.concurrency,
        "active_task_count": active_task_count_for_owner(access.token_hash),
        "remote_generation_limit": access.remote_generation_limit,
        "task_retention_days": access.task_retention_days,
        "billing_priority": access.billing_priority,
        "model_discounts": user_model_discounts_by_token_hash(access.token_hash),
        "user_name": user_name,
    }


def _admit_task_creation() -> None:
    from .task_queue import get_task_queue

    if task_submission_paused():
        raise HTTPException(status_code=503, detail="任务发布已暂停", headers={"Retry-After": "30"})
    queue_health = get_task_queue().health()
    if not queue_health.get("ok"):
        raise HTTPException(status_code=503, detail="任务队列暂不可用", headers={"Retry-After": "5"})
    admission = queue_admission(queue_health)
    if not admission.allowed:
        raise HTTPException(status_code=503, detail="任务队列繁忙，请稍后重试", headers={"Retry-After": str(admission.retry_after)})


def _refund_canceled_task(meta: dict[str, Any]) -> None:
    task_id = str(meta.get("id") or "")
    if not task_id:
        return
    result = load_result(task_id)
    account_id = str(result.get("account_id") or "")
    account = account_for_current_task(task_id)
    if not account_id and account:
        account_id = str(account.get("id") or "")
    if account_id:
        clear_account_current_task(account_id, task_id)
        charge_id = str(result.get("account_quota_charge_id") or (account or {}).get("current_quota_charge_id") or "")
        refund_account_quota_once(task_id, account_id, charge_id)
    refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))


def _frontend_safe_retry_text(value: str) -> str:
    import re

    text = str(value or "")
    if re.search(r"service[ _-]*frequent|risk check:\s*service_frequent|710022002|当前服务访问频繁|服务访问频繁", text, flags=re.IGNORECASE):
        return "服务繁忙正在重试！"
    return text


def _task_has_service_frequent(meta: dict[str, Any]) -> bool:
    values = [str(meta.get("error") or ""), str(meta.get("status_reason") or "")]
    values.extend(str(item.get("reason") or "") for item in meta.get("attempt_history") or [] if isinstance(item, dict))
    return any(_frontend_safe_retry_text(value) != value for value in values)


def _client_safe_text(value: str, model: str, *, terminal: bool = False) -> str:
    import re

    replacement = str(model or "当前模型")
    raw_text = str(value or "")
    text = _frontend_safe_retry_text(raw_text)
    if text != raw_text:
        return text
    if re.search(r"generating videos longer than\s*10 seconds.*not supported|视频.{0,20}超过\s*10\s*秒.{0,20}不支持", text, flags=re.IGNORECASE):
        return "生成接口繁忙请稍后重试！"
    if re.search(r"游客模式|请登录后再试|登录后再试", text):
        return "生成失败，请重试！" if terminal else "正在重试中，请稍等！"
    if re.search(r"710022004|rate limited|slider verification|跳验证|滑块风控", text, flags=re.IGNORECASE):
        return "生成失败，请重试！" if terminal else "正在重试中，请稍等！"
    if re.search(r"生成超过\d+分钟", text):
        return "生成失败，请重试！" if terminal else "正在生成中，请稍等！"
    if re.search(r"reference image upload timed out|prepare_upload timed out", text, flags=re.IGNORECASE):
        return "参考图上传超时，请重试！" if terminal else "参考图上传超时，正在重试！"
    if re.search(r"generation acknowledgement missing|连续要求进入视频创作页面|无法直接生成[^\n\r]{0,80}(?:创作|生成)页面", text, flags=re.IGNORECASE):
        return "生成接口繁忙请稍后重试！" if terminal else "服务繁忙正在重试！"
    if "正在打开生成页面" in text:
        return "正在启动服务"
    text = text.replace("浏览器", "服务")
    if re.search(r"当前地区不可用|所在的国家/地区不可用|region restricted|country restricted", text, flags=re.IGNORECASE):
        return "生成失败，请重试！" if terminal else "正在重试中，请稍等！"
    if re.search(r"all configured proxy modes|proxy modes are temporarily|cooling down", text, flags=re.IGNORECASE):
        return "生成失败，请重试！" if terminal else "正在重试中，请稍等！"
    if re.search(
        r"Page\.(?:goto|click|evaluate|waitFor|wait_for)|execution context was destroyed|worker execution heartbeat missing|SSL:|TLSV1_|tls alert|failed to fetch|net::ERR_|ERR_PROXY|PROXY_CONNECTION|ProxyError|Target page|browser (?:timeout|closed)|playwright|Traceback|\bat\s+\S+[:(]|�|锟斤拷",
        text,
        flags=re.IGNORECASE,
    ):
        return "服务暂时异常，请重试！" if terminal else "服务连接异常，正在重试！"
    text = re.sub(r"Dola|dola|豆包|千问|qianwen|doubao|平台", replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"账号|账户|号池|换号|服务凭证", "服务", text, flags=re.IGNORECASE)
    if re.search(r"额度不足|额度已用完|额度用完了|额度已耗尽|额度耗尽了|次数不足|次数已用完|次数已耗尽|余额不足|正在切换服务重试|正在切换账号重试|多个服务额度均不足", text):
        return "生成接口繁忙请稍后重试！" if terminal else "服务繁忙正在重试！"
    return text


def _frontend_task_stats(stats: dict[str, Any], *, client: bool) -> dict[str, Any]:
    safe = dict(stats or {})
    combined: dict[str, int] = {}
    for item in safe.get("failure_reasons") or []:
        if not isinstance(item, dict):
            continue
        raw_reason = str(item.get("reason") or "未知原因")
        reason = _client_safe_text(raw_reason, "当前模型", terminal=True) if client else _frontend_safe_retry_text(raw_reason)
        combined[reason] = combined.get(reason, 0) + max(0, int(item.get("count") or 0))
    safe["failure_reasons"] = [
        {"reason": reason, "count": count}
        for reason, count in sorted(combined.items(), key=lambda item: (-item[1], item[0]))
    ]
    return safe


def _client_task(task: dict) -> dict:
    safe = dict(task)
    service_frequent = _task_has_service_frequent(safe)
    model = str(safe.get("model") or "当前模型")
    terminal = str(safe.get("status") or "") in {"failed", "canceled"}
    for key in ("error", "status_reason"):
        if key in safe:
            safe[key] = _client_safe_text(str(safe.get(key) or ""), model, terminal=terminal)
    if str(safe.get("status") or "") == "pending" and (
        int(safe.get("retry_count") or 0) > 0 or int(safe.get("infrastructure_retry_count") or 0) > 0
    ):
        safe["error"] = "服务繁忙正在重试！" if service_frequent else "正在重试中，请稍等！"
        safe["status_reason"] = "服务繁忙正在重试！" if service_frequent else str(safe.get("status_reason") or "正在重试中，请稍等！")
    for key in ("failed_account_ids", "failed_proxy_node_ids", "proxy_retry_avoid_node_id", "account_id", "owner_token_hash", "worker_id", "platform", "execution_phase", "phase_updated_at", "infrastructure_error", "attempt_history", "last_attempt_error", "last_attempt_kind", "last_attempt_at", "reference_upload_cache_bypass", "reference_face_detection_completed", "reference_face_count", "reference_face_processing_errors", "portrait_protection_retry_count", "video_hidden_for_admin", "task_hidden_for_admin", "task_hidden_for_client"):
        safe.pop(key, None)
    return safe


async def _request_payload(request: Request) -> dict[str, object]:
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid json body")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="json body must be an object")
        return {str(key): value for key, value in data.items() if value is not None}

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        return {str(key): str(value) for key, value in form.items() if value is not None}

    body = (await request.body()).decode("utf-8", errors="replace").strip()
    return {"url": body} if body else {}


def validate_task_platform_model(platform_value: str | None, model_value: str | None) -> tuple[str, str]:
    settings = load_settings()
    platform = normalize_platform(platform_value or settings.default_platform)
    model = normalize_model(model_value)
    allowed = [item for item in settings.platform_models.get(platform, []) if settings.platform_model_states.get(platform, {}).get(item, True)]
    if not allowed:
        raise HTTPException(status_code=400, detail="该平台暂无已启用模型")
    if allowed and not model:
        model = allowed[0]
    if model and model not in allowed:
        raise HTTPException(status_code=400, detail="model is not allowed for platform")
    if platform not in {DEFAULT_PLATFORM, "doubao", "qianwen"}:
        raise HTTPException(status_code=400, detail="该平台号池已隔离，网页自动化接入完成后才能生成")
    if platform == "doubao" and not model:
        model = "Seedance 2.0 Mini"
    if platform == "qianwen" and not model:
        model = "万相 2.7"
    return platform, model


def validate_task_duration(platform: str, model: str, duration_value: int | None) -> int:
    settings = load_settings()
    enabled = list(settings.model_durations.get(platform, {}).get(model, []))
    if not enabled:
        raise HTTPException(status_code=400, detail="该模型暂无已启用时长")
    if duration_value is None:
        preferred = settings.video_duration if platform == "dola" else 10
        return preferred if preferred in enabled else enabled[0]
    try:
        duration = int(duration_value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="视频时长无效") from exc
    if duration not in enabled:
        raise HTTPException(status_code=400, detail=f"该模型未开启 {duration} 秒时长")
    return duration


@app.get("/health", dependencies=[Depends(require_token)])
async def health(access: Annotated[AccessContext, Depends(require_token)]):
    return await asyncio.to_thread(_health_payload, access)


@app.get("/auth/admin", dependencies=[Depends(require_admin)])
async def admin_auth(access: Annotated[AccessContext, Depends(require_admin)]):
    return await asyncio.to_thread(_health_payload, access)


@app.post("/auth/admin/login")
async def admin_login(request: Request):
    payload = await _request_payload(request)
    await _rate_limit(request, "admin-login", 10, 60, str(payload.get("username") or "").lower())
    settings = load_settings()
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    valid_username = hmac.compare_digest(username, settings.admin_username)
    valid_password = bool(settings.admin_password_hash) and verify_password(password, settings.admin_password_hash)
    if not valid_username or not valid_password:
        raise HTTPException(status_code=401, detail="管理员账号或密码错误")
    response = JSONResponse({"ok": True, "username": settings.admin_username})
    response.set_cookie(SESSION_COOKIE_NAME, create_session(settings.admin_username), max_age=SESSION_TTL_SECONDS, httponly=True, secure=_request_is_secure(request), samesite="strict", path="/")
    return response


@app.post("/auth/admin/logout")
async def admin_logout(request: Request):
    if request.cookies.get(SESSION_COOKIE_NAME):
        _validate_cookie_request(request)
    delete_session(request.cookies.get(SESSION_COOKIE_NAME, ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", httponly=True, secure=_request_is_secure(request), samesite="strict")
    return response


@app.post("/auth/admin/password", dependencies=[Depends(require_admin)])
async def admin_change_password(request: Request):
    payload = await _request_payload(request)
    settings = load_settings()
    current_password = str(payload.get("current_password") or "")
    new_password = str(payload.get("new_password") or "")
    confirm_password = str(payload.get("confirm_password") or "")
    if not settings.admin_password_hash or not verify_password(current_password, settings.admin_password_hash):
        raise HTTPException(status_code=400, detail="当前密码错误")
    if new_password != confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")
    try:
        validate_password(new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if hmac.compare_digest(current_password, new_password):
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")
    update_config({"admin_password_hash": hash_password(new_password)})
    delete_user_sessions(settings.admin_username)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", httponly=True, secure=request.url.scheme == "https", samesite="strict")
    return response


@app.get("/auth/client", dependencies=[Depends(require_temp)])
async def client_auth(access: Annotated[AccessContext, Depends(require_temp)]):
    return await asyncio.to_thread(_health_payload, access)


@app.get("/auth/access-state", dependencies=[Depends(require_temp)])
async def client_access_state(access: Annotated[AccessContext, Depends(require_temp)]):
    return await asyncio.to_thread(_client_access_payload, access)


@app.patch("/auth/billing-priority", dependencies=[Depends(require_temp)])
async def client_billing_priority(request: Request, access: Annotated[AccessContext, Depends(require_temp)]):
    payload = await _request_payload(request)
    try:
        token = set_temp_billing_priority(access.token_hash, payload.get("priority", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    refreshed = get_temp_context_by_hash(access.token_hash)
    if not refreshed:
        raise HTTPException(status_code=404, detail="用户 Token 不存在")
    try:
        user = user_identity_by_token_hash(access.token_hash)
        await _record_activity_safe(str(user.get("id") or ""), "billing_priority", "修改优先扣除设置", detail=str(token["billing_priority"]))
    except KeyError:
        pass
    return {"ok": True, "billing_priority": token["billing_priority"], **_client_access_payload(refreshed)}


@app.get("/points/packages", dependencies=[Depends(require_temp)])
async def points_packages():
    return {"packages": list_packages(), "payment_enabled": True, "payment_url": DEFAULT_PAYMENT_URL}


@app.post("/points/redeem", dependencies=[Depends(require_temp)])
async def points_redeem(request: Request, access: Annotated[AccessContext, Depends(require_temp)]):
    payload = await _request_payload(request)
    try:
        user = user_identity_by_token_hash(access.token_hash)
        result = redeem_card(payload.get("code", ""), str(user.get("id") or ""), access.token_hash, str(user.get("username") or ""))
        card = result["card"]
        balance = result["balance"]
        record_transaction(
            str(user.get("id") or ""),
            "redeem",
            int(card.get("points_units") or 0),
            "积分充值",
            balance_units=int(balance.get("credit_units") or 0),
            video_quota_balance=int(balance.get("free_remaining") or 0),
            reference_id=str(card.get("id") or ""),
        )
        await _record_activity_safe(str(user.get("id") or ""), "points_redeem", "使用卡密充值积分", reference_id=str(card.get("id") or ""), detail=f"充值 {card.get('points', 0)} 积分")
        return {"ok": True, "points": card.get("points", 0), "balance": balance}
    except KeyError:
        raise HTTPException(status_code=404, detail="卡密不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/points/transactions", dependencies=[Depends(require_temp)])
async def point_transactions(access: Annotated[AccessContext, Depends(require_temp)], page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100)):
    try:
        user = user_identity_by_token_hash(access.token_hash)
        return list_transactions(str(user.get("id") or ""), page, page_size, include_hidden=False)
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在或已停用")


@app.get("/memberships", dependencies=[Depends(require_temp)])
async def memberships():
    return {"packages": list_memberships()}


@app.post("/memberships/{package_id}/purchase", dependencies=[Depends(require_temp)])
async def purchase_membership(package_id: str, access: Annotated[AccessContext, Depends(require_temp)]):
    try:
        package = get_membership(package_id)
        user = user_identity_by_token_hash(access.token_hash)
        result = purchase_user_membership(str(user.get("id") or ""), package)
        balance = result["balance"]
        record_transaction(
            str(user.get("id") or ""),
            "membership_purchase",
            -points_to_units(package.get("points_cost")),
            f"购买会员：{package.get('name')}",
            balance_units=int(balance.get("credit_units") or 0),
            video_quota_change=int(package.get("bonus_free_uses") or 0),
            video_quota_balance=int(balance.get("free_remaining") or 0),
            reference_id=str(package.get("id") or ""),
            detail=f"有效期 {package.get('duration_days')} 天 / 并发 {package.get('concurrency')} / 赠送视频额度 {package.get('bonus_free_uses')}",
        )
        await _record_activity_safe(str(user.get("id") or ""), "membership_purchase", "购买会员套餐", reference_id=str(package.get("id") or ""), detail=str(package.get("name") or ""))
        return {"ok": True, "package": package, **result}
    except KeyError:
        raise HTTPException(status_code=404, detail="会员套餐或用户不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/admin/points/packages", dependencies=[Depends(require_admin)])
async def admin_points_packages():
    return {"packages": list_packages(include_disabled=True)}


@app.post("/admin/points/packages", dependencies=[Depends(require_admin)], status_code=201)
async def admin_create_points_package(request: Request):
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return {"ok": True, "package": create_package(payload)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.patch("/admin/points/packages/{package_id}", dependencies=[Depends(require_admin)])
async def admin_update_points_package(package_id: str, request: Request):
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return {"ok": True, "package": update_package(package_id, payload)}
    except KeyError:
        raise HTTPException(status_code=404, detail="套餐不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/admin/points/packages/{package_id}", dependencies=[Depends(require_admin)])
async def admin_disable_points_package(package_id: str):
    try:
        return {"ok": True, "package": disable_package(package_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="套餐不存在")


@app.get("/admin/point-cards", dependencies=[Depends(require_admin)])
async def admin_point_cards(limit: int = Query(500, ge=1, le=2000), status: str = "", q: str = ""):
    rows = list_cards(limit)
    usernames = {str(item.get("id") or ""): str(item.get("username") or "") for item in list_users(list_temp_tokens())}
    for item in rows:
        item["redeemed_username"] = str(item.get("redeemed_username") or usernames.get(str(item.get("redeemed_by") or ""), ""))
    normalized_status = str(status or "").strip().lower()
    if normalized_status in {"unused", "redeemed"}:
        rows = [item for item in rows if item.get("status") == normalized_status]
    query = str(q or "").strip().casefold()
    if query:
        normalized_code = "".join(character for character in query.upper() if character.isalnum())
        query_digest = hashlib.sha256(normalized_code.encode("ascii")).hexdigest() if len(normalized_code) >= 12 else ""
        rows = [item for item in rows if query_digest == str(item.get("code_hash") or "") or query in " ".join((str(item.get("code") or ""), str(item.get("code_hint") or ""), str(item.get("redeemed_username") or ""), str(item.get("note") or ""))).casefold()]
    return {"cards": rows, "total": len(rows)}


@app.post("/admin/point-cards", dependencies=[Depends(require_admin)], status_code=201)
async def admin_generate_point_cards(request: Request):
    payload = await _request_payload(request)
    try:
        cards = generate_cards(payload.get("points"), int(payload.get("count") or 1), payload.get("note", ""))
        return {"ok": True, "cards": cards, "count": len(cards)}
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/admin/point-cards/delete", dependencies=[Depends(require_admin)])
async def admin_delete_point_cards(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求内容无效")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求内容必须为对象")
    card_ids = payload.get("ids") or []
    if not isinstance(card_ids, list):
        raise HTTPException(status_code=400, detail="卡密 ID 必须为列表")
    try:
        deleted = delete_cards(card_ids, payload.get("status", ""))
        return {"ok": True, "deleted": deleted}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/admin/memberships", dependencies=[Depends(require_admin)])
async def admin_memberships():
    return {"packages": list_memberships(include_disabled=True)}


@app.post("/admin/memberships", dependencies=[Depends(require_admin)], status_code=201)
async def admin_create_membership(request: Request):
    try:
        return {"ok": True, "package": create_membership(await request.json())}
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.patch("/admin/memberships/{package_id}", dependencies=[Depends(require_admin)])
async def admin_update_membership(package_id: str, request: Request):
    try:
        return {"ok": True, "package": update_membership(package_id, await request.json())}
    except KeyError:
        raise HTTPException(status_code=404, detail="会员套餐不存在")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/admin/memberships/{package_id}", dependencies=[Depends(require_admin)])
async def admin_disable_membership(package_id: str):
    try:
        return {"ok": True, "package": disable_membership(package_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="会员套餐不存在")


@app.post("/auth/register")
async def client_register(request: Request):
    client_key = _request_client_key(request)
    settings = load_settings()
    registration_security_enabled = settings.registration_abuse_detection_enabled
    if registration_security_enabled:
        blocked_for = await asyncio.to_thread(registration_block_retry_after, client_key)
        if blocked_for > 0:
            raise HTTPException(
                status_code=429,
                detail="连续异常注册次数过多，已暂时拦截，请稍后重试",
                headers={"Retry-After": str(blocked_for)},
            )
    payload = await _request_payload(request)
    await _rate_limit(request, "register", 5, 60)
    invitation_reservation = None
    try:
        if payload.get("password") != payload.get("confirm_password"):
            raise ValueError("两次输入的密码不一致")
        invitation_code = str(payload.get("invitation_code") or "").strip()
        if invitation_registration_required() and not invitation_code:
            raise ValueError("请输入邀请码")
        if invitation_code:
            invitation_reservation = reserve_invitation_code(invitation_code)
        email = ""
        if settings.registration_email_verification_enabled:
            email = consume_registration_code(payload.get("email", ""), payload.get("email_code", ""), settings)
        registered = register_user(
            payload.get("username", ""),
            payload.get("password", ""),
            email,
            str((invitation_reservation or {}).get("code") or ""),
        )
        identity = user_identity_by_token_hash(hash_token(str(registered.get("token") or "")))
        if invitation_reservation:
            complete_invitation_reservation(
                str(invitation_reservation.get("reservation_id") or ""),
                str(identity.get("id") or ""),
                str(identity.get("username") or ""),
            )
            invitation_reservation = None
        record_transaction(
            str(identity.get("id") or ""),
            "video_quota_credit",
            0,
            "注册赠送视频额度",
            balance_units=0,
            video_quota_change=1,
            video_quota_balance=1,
        )
        await _record_activity_safe(str(identity.get("id") or ""), "register", "注册账号")
        if registration_security_enabled:
            await asyncio.to_thread(reset_registration_failures, client_key)
        response = JSONResponse({"ok": True, **registered})
        _set_client_session_cookie(response, request, hash_token(str(registered.get("token") or "")))
        return response
    except ValueError as exc:
        if invitation_reservation:
            release_invitation_reservation(str(invitation_reservation.get("reservation_id") or ""))
        if not registration_security_enabled:
            raise HTTPException(status_code=400, detail=str(exc))
        failure = await asyncio.to_thread(record_registration_failure, client_key)
        retry_after = int(failure.get("retry_after") or 0)
        if failure.get("blocked_now"):
            await _record_admin_action_safe(
                "registration_block",
                "异常注册已临时拦截",
                detail=f"连续 {int(failure.get('failures') or 0)} 次注册异常，临时拦截 15 分钟",
                actor="系统",
                ip_address=client_key,
            )
        if retry_after > 0:
            raise HTTPException(
                status_code=429,
                detail="连续异常注册次数过多，已暂时拦截，请稍后重试",
                headers={"Retry-After": str(retry_after)},
            )
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        if invitation_reservation:
            release_invitation_reservation(str(invitation_reservation.get("reservation_id") or ""))
        raise


@app.post("/auth/register/email-code")
async def client_registration_email_code(request: Request):
    import asyncio

    payload = await _request_payload(request)
    settings = load_settings()
    try:
        email = validate_allowed_email(payload.get("email", ""), settings)
        await _rate_limit(request, "registration-email-code-ip", 5, 600)
        await _rate_limit(request, "registration-email-code-address", 3, 600, hashlib.sha256(email.encode("utf-8")).hexdigest())
        await asyncio.to_thread(send_registration_code, email, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (OSError, RuntimeError, smtplib.SMTPException):
        raise HTTPException(status_code=503, detail="验证码发送失败，请联系管理员检查邮箱配置")
    return {"ok": True, "detail": "验证码已发送"}


@app.get("/auth/register/email-domains")
async def client_registration_email_domains():
    settings = load_settings()
    return {
        "enabled": settings.registration_email_verification_enabled,
        "domains": [f"@{item}" for item in settings.registration_email_domains],
        "invitation_required": invitation_registration_required(),
    }


@app.post("/auth/login")
async def client_login(request: Request):
    import asyncio

    payload = await _request_payload(request)
    identifier = payload.get("identifier") or payload.get("username") or ""
    await _rate_limit(request, "client-login-ip", 200, 60)
    await _rate_limit(request, "client-login-identifier", 20, 60, str(identifier).strip().casefold())
    result = await asyncio.to_thread(login_user, identifier, payload.get("password", ""))
    if not result:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    try:
        identity = await asyncio.to_thread(user_identity_by_token_hash, hash_token(str(result.get("token") or "")))
        await _record_activity_safe(
            str(identity.get("id") or ""),
            "login",
            "登录用户端",
            detail=f"IP：{request.client.host if request.client else 'unknown'}",
        )
    except KeyError:
        pass
    response = JSONResponse({"ok": True, **result})
    _set_client_session_cookie(response, request, hash_token(str(result.get("token") or "")), request.cookies.get(CLIENT_SESSION_COOKIE_NAME, ""))
    return response


@app.post("/auth/session", dependencies=[Depends(require_temp)])
async def client_session_upgrade(request: Request, access: Annotated[AccessContext, Depends(require_temp)]):
    response = JSONResponse({"ok": True})
    _set_client_session_cookie(response, request, access.token_hash, request.cookies.get(CLIENT_SESSION_COOKIE_NAME, ""))
    return response


@app.post("/auth/logout")
async def client_logout(request: Request):
    if request.cookies.get(CLIENT_SESSION_COOKIE_NAME):
        _validate_cookie_request(request)
    delete_client_session(request.cookies.get(CLIENT_SESSION_COOKIE_NAME, ""))
    response = JSONResponse({"ok": True})
    _clear_client_session_cookie(response, request)
    return response


@app.post("/auth/password/forgot-code")
async def client_forgot_password_code(request: Request):
    import asyncio

    payload = await _request_payload(request)
    settings = load_settings()
    email = str(payload.get("email") or "").strip().lower()
    generic_detail = "如果该邮箱已绑定账号，验证码将发送到邮箱"
    try:
        email = normalize_email(email)
        await _rate_limit(request, "reset-password-code-ip", 5, 600)
        await _rate_limit(request, "reset-password-code-address", 3, 600, hashlib.sha256(email.encode("utf-8")).hexdigest())
        if has_verified_enabled_email(email):
            await asyncio.to_thread(send_registration_code, email, settings, "reset_password", "", True)
    except (OSError, RuntimeError, smtplib.SMTPException):
        raise HTTPException(status_code=503, detail="验证码发送失败，请稍后重试")
    except ValueError:
        pass
    return {"ok": True, "detail": generic_detail}


@app.post("/auth/password/reset")
async def client_reset_password(request: Request):
    payload = await _request_payload(request)
    settings = load_settings()
    email = str(payload.get("email") or "").strip().lower()
    new_password = str(payload.get("new_password") or "")
    if new_password != str(payload.get("confirm_password") or ""):
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")
    try:
        email = consume_registration_code(email, payload.get("email_code", ""), settings, "reset_password", "", True)
        result = reset_user_password_by_email(email, new_password)
    except KeyError:
        raise HTTPException(status_code=400, detail="邮箱验证码错误或账号不可用")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    old_token_hash = result.pop("_old_token_hash", "")
    migrate_task_owner(old_token_hash, hash_token(result["token"]))
    return {"ok": True, **result}


@app.post("/auth/token/refresh", dependencies=[Depends(require_temp)])
async def client_token_refresh(request: Request, access: Annotated[AccessContext, Depends(require_temp)]):
    try:
        result = rotate_user_token_by_hash(access.token_hash)
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在或已停用")
    migrate_task_owner(access.token_hash, hash_token(result["token"]))
    refreshed = get_temp_context(result["token"])
    payload = _health_payload(refreshed) if refreshed else {}
    response = JSONResponse({"ok": True, **result, **payload})
    _set_client_session_cookie(response, request, hash_token(result["token"]), request.cookies.get(CLIENT_SESSION_COOKIE_NAME, ""))
    return response


@app.post("/auth/password", dependencies=[Depends(require_temp)])
async def client_change_password(request: Request, access: Annotated[AccessContext, Depends(require_temp)]):
    body = await _request_payload(request)
    new_password = str(body.get("new_password") or "")
    if new_password != str(body.get("confirm_password") or ""):
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")
    try:
        result = change_user_password_by_token_hash(access.token_hash, str(body.get("current_password") or ""), new_password)
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在或已停用")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    new_token_hash = hash_token(result["token"])
    migrate_task_owner(access.token_hash, new_token_hash)
    try:
        identity = user_identity_by_token_hash(new_token_hash)
        await _record_activity_safe(str(identity.get("id") or ""), "password_change", "修改登录密码")
    except KeyError:
        pass
    refreshed = get_temp_context(result["token"])
    payload = _health_payload(refreshed) if refreshed else {}
    response = JSONResponse({"ok": True, **result, **payload})
    _set_client_session_cookie(response, request, new_token_hash, request.cookies.get(CLIENT_SESSION_COOKIE_NAME, ""))
    return response


@app.get("/auth/profile", dependencies=[Depends(require_temp)])
async def client_profile(access: Annotated[AccessContext, Depends(require_temp)]):
    try:
        return await asyncio.to_thread(user_profile_by_token_hash, access.token_hash)
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在或已停用")


@app.post("/auth/email/code", dependencies=[Depends(require_temp)])
async def client_email_code(request: Request, access: Annotated[AccessContext, Depends(require_temp)]):
    import asyncio

    payload = await _request_payload(request)
    settings = load_settings()
    try:
        email = validate_allowed_email(payload.get("email", ""), settings)
        await _rate_limit(request, "change-email-code", 3, 600, access.token_hash)
        await asyncio.to_thread(send_registration_code, email, settings, "change_email", access.token_hash)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (OSError, RuntimeError, smtplib.SMTPException):
        raise HTTPException(status_code=503, detail="验证码发送失败，请稍后重试")
    return {"ok": True, "detail": "验证码已发送"}


@app.patch("/auth/email", dependencies=[Depends(require_temp)])
async def client_change_email(request: Request, access: Annotated[AccessContext, Depends(require_temp)]):
    payload = await _request_payload(request)
    settings = load_settings()
    try:
        email = consume_registration_code(payload.get("email", ""), payload.get("email_code", ""), settings, "change_email", access.token_hash)
        result = change_user_email_by_token_hash(access.token_hash, email)
        identity = user_identity_by_token_hash(access.token_hash)
        await _record_activity_safe(str(identity.get("id") or ""), "email_change", "修改绑定邮箱", detail=email)
        return {"ok": True, **result}
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在或已停用")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/feedback", dependencies=[Depends(require_temp)], status_code=201)
async def client_create_feedback(request: Request, access: Annotated[AccessContext, Depends(require_temp)]):
    payload = await _request_payload(request)
    await _rate_limit(request, "feedback-user", 10, 3600, access.token_hash)
    try:
        user = user_identity_by_token_hash(access.token_hash)
        return {"ok": True, "feedback": create_feedback(user, payload.get("category", "其他"), payload.get("content", ""), payload.get("contact", ""), payload.get("source_page", ""))}
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在或已停用")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/feedback", dependencies=[Depends(require_temp)])
async def client_feedback(access: Annotated[AccessContext, Depends(require_temp)]):
    try:
        user = user_identity_by_token_hash(access.token_hash)
        rows = list_feedback_for_user(str(user.get("id") or ""))
        return {"feedback": rows, "total": len(rows)}
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在或已停用")


@app.get("/notifications", dependencies=[Depends(require_temp)])
async def client_notifications(access: Annotated[AccessContext, Depends(require_temp)]):
    try:
        user = user_identity_by_token_hash(access.token_hash)
        rows = list_notifications_for_user(str(user.get("id") or ""))
        return {"notifications": rows, "total": len(rows), "unread": sum(not item.get("read_at") for item in rows)}
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在或已停用")


@app.patch("/notifications/{notification_id}/read", dependencies=[Depends(require_temp)])
async def client_notification_read(notification_id: str, access: Annotated[AccessContext, Depends(require_temp)]):
    try:
        user = user_identity_by_token_hash(access.token_hash)
        return {"ok": True, "notification": mark_notification_read(notification_id, str(user.get("id") or ""))}
    except KeyError:
        raise HTTPException(status_code=404, detail="通知不存在")


@app.post("/notifications/read-all", dependencies=[Depends(require_temp)])
async def client_notifications_read_all(access: Annotated[AccessContext, Depends(require_temp)]):
    try:
        user = user_identity_by_token_hash(access.token_hash)
        return {"ok": True, "updated": mark_all_notifications_read(str(user.get("id") or ""))}
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在或已停用")


@app.get("/announcements", dependencies=[Depends(require_temp)])
async def client_announcements(access: Annotated[AccessContext, Depends(require_temp)]):
    try:
        user = user_identity_by_token_hash(access.token_hash)
        rows = list_announcements(str(user.get("id") or ""))
        return {"announcements": rows, "total": len(rows), "unseen": sum(not item.get("seen") for item in rows)}
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在或已停用")


@app.patch("/announcements/{announcement_id}/seen", dependencies=[Depends(require_temp)])
async def client_announcement_seen(announcement_id: str, access: Annotated[AccessContext, Depends(require_temp)]):
    try:
        user = user_identity_by_token_hash(access.token_hash)
        return {"ok": True, "announcement": mark_announcement_seen(announcement_id, str(user.get("id") or ""))}
    except KeyError:
        raise HTTPException(status_code=404, detail="公告不存在")


@app.get("/admin/feedback", dependencies=[Depends(require_admin)])
async def admin_feedback(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), status: str = "", q: str = ""):
    return list_feedback(page, page_size, status, q)


@app.patch("/admin/feedback/{feedback_id}", dependencies=[Depends(require_admin)])
async def admin_update_feedback(feedback_id: str, request: Request):
    payload = await _request_payload(request)
    try:
        return {"ok": True, "feedback": update_feedback(feedback_id, str(payload.get("status") or "pending"), payload.get("admin_note", ""))}
    except KeyError:
        raise HTTPException(status_code=404, detail="反馈不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/admin/feedback/{feedback_id}", dependencies=[Depends(require_admin)])
async def admin_delete_feedback(feedback_id: str):
    try:
        return {"ok": True, "feedback": delete_feedback(feedback_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="反馈不存在")


@app.get("/admin/notifications", dependencies=[Depends(require_admin)])
async def admin_notifications(limit: int = Query(200, ge=1, le=1000)):
    rows = list_admin_notifications(limit)
    return {"notifications": rows, "total": len(rows)}


@app.delete("/admin/notifications/{notification_id}", dependencies=[Depends(require_admin)])
async def admin_delete_notification(notification_id: str):
    try:
        return {"ok": True, "notification": delete_notification(notification_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="通知不存在")


@app.get("/admin/announcements", dependencies=[Depends(require_admin)])
async def admin_announcements():
    return {"announcements": list_announcements(include_disabled=True)}


@app.post("/admin/announcements", dependencies=[Depends(require_admin)], status_code=201)
async def admin_create_announcement(request: Request):
    payload = await _request_payload(request)
    try:
        lock_screen = str(payload.get("lock_screen", "false")).lower() in {"1", "true", "yes", "on"}
        return {"ok": True, "announcement": create_announcement(payload.get("title", ""), payload.get("content", ""), payload.get("level", "large"), lock_screen)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.patch("/admin/announcements/{announcement_id}", dependencies=[Depends(require_admin)])
async def admin_update_announcement(announcement_id: str, request: Request):
    payload = await _request_payload(request)
    try:
        enabled = str(payload["enabled"]).lower() in {"1", "true", "yes", "on"} if "enabled" in payload else None
        lock_screen = str(payload["lock_screen"]).lower() in {"1", "true", "yes", "on"} if "lock_screen" in payload else None
        return {"ok": True, "announcement": update_announcement(announcement_id, enabled=enabled, lock_screen=lock_screen)}
    except KeyError:
        raise HTTPException(status_code=404, detail="公告不存在")


@app.delete("/admin/announcements/{announcement_id}", dependencies=[Depends(require_admin)])
async def admin_delete_announcement(announcement_id: str):
    try:
        return {"ok": True, "announcement": delete_announcement(announcement_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="公告不存在")


@app.get("/admin/notification-recipients", dependencies=[Depends(require_admin)])
async def admin_notification_recipients():
    rows = list_users(list_temp_tokens())
    return {
        "users": [
            {"id": item["id"], "username": item["username"], "email": item.get("email", ""), "enabled": item.get("enabled", True)}
            for item in rows
        ]
    }


@app.post("/admin/notifications", dependencies=[Depends(require_admin)], status_code=201)
async def admin_create_notifications(request: Request):
    payload = await request.json()
    user_ids = payload.get("user_ids") if isinstance(payload, dict) else []
    if not isinstance(user_ids, list):
        raise HTTPException(status_code=400, detail="user_ids must be a list")
    selected = {str(item or "") for item in user_ids if str(item or "")}
    recipients = [item for item in list_users(list_temp_tokens()) if str(item.get("id") or "") in selected]
    if len(recipients) != len(selected):
        raise HTTPException(status_code=400, detail="所选用户包含无效账号")
    try:
        return {"ok": True, **create_notifications(recipients, payload.get("title", ""), payload.get("content", ""))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/users", dependencies=[Depends(require_admin)])
async def users_list(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), q: str = ""):
    rows = list_users(list_temp_tokens())
    query = str(q or "").strip().casefold()
    if query:
        rows = [item for item in rows if query in {str(item.get("username") or "").casefold(), str(item.get("email") or "").casefold(), str(item.get("id") or "").casefold()} or query in str(item.get("username") or "").casefold() or query in str(item.get("email") or "").casefold()]
        rows.sort(key=lambda item: (query not in {str(item.get("username") or "").casefold(), str(item.get("email") or "").casefold(), str(item.get("id") or "").casefold()}, str(item.get("username") or "").casefold()))
    total = len(rows)
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = min(page, total_pages)
    start = (current_page - 1) * page_size
    return {"users": rows[start:start + page_size], "online": sum(bool(item.get("online")) for item in rows), "total": total, "page": current_page, "page_size": page_size, "total_pages": total_pages}


@app.post("/users", dependencies=[Depends(require_admin)], status_code=201)
async def users_create(request: Request):
    payload = await _request_payload(request)
    password = str(payload.get("password") or "")
    if password != str(payload.get("confirm_password") or ""):
        raise HTTPException(status_code=400, detail="两次输入的密码不一致")
    try:
        registered = await asyncio.to_thread(register_user, payload.get("username", ""), password)
        identity = await asyncio.to_thread(user_identity_by_token_hash, hash_token(str(registered.get("token") or "")))
        record_transaction(
            str(identity.get("id") or ""),
            "video_quota_credit",
            0,
            "管理员创建用户赠送视频额度",
            balance_units=0,
            video_quota_change=1,
            video_quota_balance=1,
        )
        await _record_activity_safe(
            str(identity.get("id") or ""),
            "admin_create_user",
            "管理员创建用户",
            detail="未绑定邮箱",
            actor="admin",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    created = next(
        (item for item in list_users(list_temp_tokens()) if str(item.get("id") or "") == str(identity.get("id") or "")),
        None,
    )
    return {"ok": True, "user": created or identity}


@app.get("/users/{user_id}/details", dependencies=[Depends(require_admin)])
async def user_details(user_id: str):
    user = next((item for item in list_users(list_temp_tokens()) if str(item.get("id") or "") == str(user_id)), None)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    transactions = await asyncio.to_thread(list_transactions, user_id, 1, 100)
    activities = await asyncio.to_thread(list_activity, user_id, 1, 100)
    owner_hash = hash_token(str(user.get("token") or "")) if user.get("token") else ""
    tasks = await asyncio.to_thread(list_tasks, owner_hash) if owner_hash else []
    model_discounts = await asyncio.to_thread(user_model_discounts_by_token_hash, owner_hash) if owner_hash else {}
    settings = load_settings()
    model_discount_catalog = []
    for platform in PLATFORM_LABELS:
        models = []
        platform_discounts = model_discounts.get(platform, {})
        for model in settings.platform_models.get(platform, []):
            discount = next((value for name, value in platform_discounts.items() if name.casefold() == model.casefold()), 0)
            models.append({
                "name": model,
                "enabled": settings.platform_model_states.get(platform, {}).get(model, True),
                "discount": discount,
                "duration_costs": {
                    str(duration): model_cost_points(platform, model, duration=duration)
                    for duration in PLATFORM_VIDEO_DURATIONS[platform]
                },
            })
        if models:
            model_discount_catalog.append({"id": platform, "label": PLATFORM_LABELS.get(platform, platform), "models": models})
    task_summary = {
        "total": len(tasks),
        "success": sum(str(item.get("status") or "") == "success" for item in tasks),
        "failed": sum(str(item.get("status") or "") in {"failed", "canceled"} for item in tasks),
        "active": sum(str(item.get("status") or "") in {"pending", "running", "submitted"} for item in tasks),
        "today_success": sum(str(item.get("status") or "") == "success" and item.get("completed_today") is True for item in tasks),
        "today_failed": sum(str(item.get("status") or "") == "failed" and item.get("completed_today") is True for item in tasks),
    }
    return {
        "user": user,
        "transactions": transactions,
        "activities": activities,
        "task_summary": task_summary,
        "model_discounts": model_discounts,
        "model_discount_catalog": model_discount_catalog,
    }


@app.put("/users/{user_id}/model-discounts", dependencies=[Depends(require_admin)])
async def users_set_model_discounts(user_id: str, request: Request):
    payload = await _request_payload(request)
    raw_discounts = payload.get("discounts")
    if not isinstance(raw_discounts, dict):
        raise HTTPException(status_code=400, detail="模型减免配置格式无效")
    settings = load_settings()
    normalized: dict[str, dict[str, int | float]] = {}
    try:
        for raw_platform, raw_models in raw_discounts.items():
            platform = str(raw_platform or "").strip().lower()
            if platform not in PLATFORM_LABELS or not isinstance(raw_models, dict):
                raise ValueError("模型减免平台无效")
            configured_models = settings.platform_models.get(platform, [])
            canonical_models = {item.casefold(): item for item in configured_models}
            platform_values: dict[str, int | float] = {}
            for raw_model, raw_discount in raw_models.items():
                model = canonical_models.get(str(raw_model or "").strip().casefold())
                if not model:
                    raise ValueError(f"{PLATFORM_LABELS.get(platform, platform)} 模型不存在")
                units = nonnegative_points_to_units(raw_discount)
                if units > 0:
                    platform_values[model] = units_to_points(units)
            if platform_values:
                normalized[platform] = platform_values
        saved = await asyncio.to_thread(set_user_model_discounts, user_id, normalized)
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    configured_count = sum(len(models) for models in saved.values())
    await _record_activity_safe(
        user_id,
        "admin_model_discount",
        "管理员调整单模型积分减免",
        detail=f"已设置 {configured_count} 个模型的单条视频积分减免",
        actor="admin",
    )
    return {"ok": True, "discounts": saved}


@app.post("/users/{user_id}/points", dependencies=[Depends(require_admin)])
async def users_add_points(user_id: str, request: Request):
    payload = await _request_payload(request)
    balance_type = str(payload.get("balance_type") or "points").strip().lower()
    if balance_type not in {"points", "video_quota"}:
        raise HTTPException(status_code=400, detail="充值类型无效")
    try:
        if balance_type == "video_quota":
            raw_amount = payload.get("amount")
            amount = int(raw_amount)
            if isinstance(raw_amount, bool) or amount <= 0 or isinstance(raw_amount, float) and not raw_amount.is_integer():
                raise ValueError("视频额度充值数量必须是正整数")
            credited = adjust_user_video_quota(user_id, amount)
            user = next(item for item in list_users(list_temp_tokens()) if str(item.get("id") or "") == user_id)
            record_transaction(
                user_id,
                "admin_video_quota_credit",
                0,
                "管理员充值视频额度",
                balance_units=points_to_units(user.get("points") or 0) if float(user.get("points") or 0) > 0 else 0,
                video_quota_change=amount,
                video_quota_balance=int(user.get("free_remaining") or 0),
            )
            await _record_activity_safe(user_id, "admin_video_quota_credit", "管理员充值视频额度", detail=f"增加 {amount} 次视频额度", actor="admin")
            return {"ok": True, "balance_type": balance_type, **credited}
        credited = add_user_points(user_id, payload.get("amount"), list_temp_tokens())
        user = next(item for item in list_users(list_temp_tokens()) if str(item.get("id") or "") == user_id)
        record_transaction(
            user_id,
            "admin_credit",
            points_to_units(payload.get("amount")),
            "管理员充值",
            balance_units=points_to_units(user.get("points") or 0) if float(user.get("points") or 0) > 0 else 0,
            video_quota_balance=int(user.get("free_remaining") or 0),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await _record_activity_safe(user_id, "admin_credit", "管理员充值积分", detail=f"增加 {payload.get('amount')} 积分", actor="admin")
    return {"ok": True, "balance_type": balance_type, **credited}


@app.post("/users/{user_id}/points/deduct", dependencies=[Depends(require_admin)])
async def users_deduct_points(user_id: str, request: Request):
    payload = await _request_payload(request)
    balance_type = str(payload.get("balance_type") or "points").strip().lower()
    visible_to_client = str(payload.get("visible_to_client", "true")).strip().lower() in {"1", "true", "yes", "on"}
    if balance_type not in {"points", "video_quota"}:
        raise HTTPException(status_code=400, detail="扣除类型无效")
    try:
        if balance_type == "video_quota":
            raw_amount = payload.get("amount")
            amount = int(raw_amount)
            if isinstance(raw_amount, bool) or amount <= 0 or isinstance(raw_amount, float) and not raw_amount.is_integer():
                raise ValueError("视频额度扣除数量必须是正整数")
            deducted = adjust_user_video_quota(user_id, -amount)
            user = next(item for item in list_users(list_temp_tokens()) if str(item.get("id") or "") == user_id)
            record_transaction(
                user_id,
                "admin_video_quota_deduct",
                0,
                "管理员扣除视频额度",
                balance_units=points_to_units(user.get("points") or 0) if float(user.get("points") or 0) > 0 else 0,
                video_quota_change=-amount,
                video_quota_balance=int(user.get("free_remaining") or 0),
                visible_to_client=visible_to_client,
            )
            visibility_detail = "用户端显示" if visible_to_client else "用户端隐藏"
            await _record_activity_safe(user_id, "admin_video_quota_deduct", "管理员扣除视频额度", detail=f"扣除 {amount} 次视频额度 / {visibility_detail}", actor="admin")
            return {"ok": True, "balance_type": balance_type, "visible_to_client": visible_to_client, **deducted}
        deduct_user_points(user_id, payload.get("amount"))
        user = next(item for item in list_users(list_temp_tokens()) if str(item.get("id") or "") == user_id)
        record_transaction(
            user_id,
            "admin_deduct",
            -points_to_units(payload.get("amount")),
            "管理员扣除",
            balance_units=points_to_units(user.get("points") or 0) if float(user.get("points") or 0) > 0 else 0,
            video_quota_balance=int(user.get("free_remaining") or 0),
            visible_to_client=visible_to_client,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    visibility_detail = "用户端显示" if visible_to_client else "用户端隐藏"
    await _record_activity_safe(user_id, "admin_deduct", "管理员扣除积分", detail=f"扣除 {payload.get('amount')} 积分 / {visibility_detail}", actor="admin")
    return {"ok": True, "balance_type": balance_type, "visible_to_client": visible_to_client}


@app.patch("/users/{user_id}", dependencies=[Depends(require_admin)])
async def users_update(user_id: str, request: Request):
    payload = await _request_payload(request)
    try:
        if "enabled" in payload:
            set_user_enabled(user_id, str(payload["enabled"]).lower() in {"1", "true", "yes", "on"})
        if "concurrency" in payload:
            concurrency = int(payload["concurrency"])
            if concurrency < 1 or concurrency > 100:
                raise ValueError("并发数量需为1-100")
            set_user_concurrency(user_id, concurrency)
        if "remote_generation_limit" in payload:
            remote_generation_limit = int(payload["remote_generation_limit"])
            if remote_generation_limit < 1 or remote_generation_limit > 999:
                raise ValueError("单用户远端上限需为1-999")
            set_user_remote_generation_limit(user_id, remote_generation_limit)
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if "concurrency" in payload:
        await _record_activity_safe(user_id, "admin_concurrency", "管理员调整并发", detail=f"并发调整为 {payload['concurrency']}", actor="admin")
    if "enabled" in payload:
        await _record_activity_safe(user_id, "admin_status", "管理员调整账号状态", detail="启用" if str(payload["enabled"]).lower() in {"1", "true", "yes", "on"} else "停用", actor="admin")
    if "remote_generation_limit" in payload:
        await _record_activity_safe(user_id, "admin_remote_limit", "管理员调整远端上限", detail=f"远端生成上限调整为 {payload['remote_generation_limit']}", actor="admin")
    return {"ok": True}


@app.delete("/users/{user_id}", dependencies=[Depends(require_admin)])
async def users_delete(user_id: str):
    try:
        delete_user(user_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True}


@app.post("/users/{user_id}/status", dependencies=[Depends(require_admin)])
async def users_status(user_id: str, request: Request):
    payload = await _request_payload(request)
    enabled = str(payload.get("enabled") or "").lower() in {"1", "true", "yes", "on"}
    try:
        set_user_enabled(user_id, enabled)
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在")
    await _record_activity_safe(
        user_id,
        "admin_status",
        "管理员调整账号状态",
        detail="启用" if enabled else "停用",
        actor="admin",
    )
    return {"ok": True}


@app.post("/users/{user_id}/delete", dependencies=[Depends(require_admin)])
async def users_delete_action(user_id: str):
    try:
        delete_user(user_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True}


@app.get("/admin", include_in_schema=False)
@app.get("/admin/", include_in_schema=False)
async def admin_panel():
    index_path = ADMIN_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="admin panel not found")
    return FileResponse(index_path, headers={"Cache-Control": "no-store"})


@app.get("/client", include_in_schema=False)
@app.get("/client/", include_in_schema=False)
async def client_panel():
    index_path = ADMIN_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="client panel not found")
    return FileResponse(index_path, headers={"Cache-Control": "no-store"})


def _masked_proxy_username(username: str) -> str:
    value = str(username or "")
    if not value:
        return ""
    return f"{value[:3]}***{value[-2:]}" if len(value) > 5 else "***"


def _proxy_config_payload(settings) -> dict:
    account_pool = list_account_proxies(settings)
    return {
        "proxy_api_url": settings.proxy_api_url,
        "proxy_api_scheme": settings.proxy_api_scheme,
        "proxy_api_timeout_seconds": settings.proxy_api_timeout_seconds,
        "proxy_source": settings.proxy_source,
        "platform_proxy_sources": settings.platform_proxy_sources,
        "platform_proxy_random": settings.platform_proxy_random,
        "proxy_subscription_configured": bool(settings.proxy_subscription_url),
        "proxy_subscription_scheme": settings.proxy_subscription_scheme,
        "proxy_subscription_refresh_seconds": settings.proxy_subscription_refresh_seconds,
        "proxy_account_configured": bool(account_pool["proxies"]),
        "proxy_account_count": len(account_pool["proxies"]),
        "proxy_account_scheme": settings.proxy_account_scheme,
        "proxy_account_host": settings.proxy_account_host,
        "proxy_account_port": settings.proxy_account_port,
        "proxy_account_username_masked": _masked_proxy_username(settings.proxy_account_username),
        "proxy_enabled": settings.proxy_enabled,
        "proxy_auto_select": settings.proxy_auto_select,
        "proxy_selected_node": settings.proxy_selected_node,
        "proxy_auto_countries": settings.proxy_auto_countries,
        "proxy_latency_threshold_ms": settings.proxy_latency_threshold_ms,
        "proxy_health_refresh_seconds": settings.proxy_health_refresh_seconds,
    }


@app.get("/config/proxy-api", dependencies=[Depends(require_admin)])
async def proxy_api_config():
    return _proxy_config_payload(load_settings())


@app.post("/config/proxy-api/test", dependencies=[Depends(require_admin)])
async def test_proxy_api_config(request: Request):
    payload = await _request_payload(request)
    settings = load_settings()
    try:
        api_url = validate_proxy_api_url(payload.get("proxy_api_url", settings.proxy_api_url))
        proxy_scheme = validate_proxy_api_scheme(payload.get("proxy_api_scheme", settings.proxy_api_scheme))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not api_url:
        raise HTTPException(status_code=400, detail="proxy_api_url is required")
    try:
        proxy = await fetch_proxy_from_api(
            api_url,
            timeout_seconds=settings.proxy_api_timeout_seconds,
            scheme=proxy_scheme,
        )
        available, latency_ms = await probe_dola_proxy(
            str(proxy.get("server") or ""),
            min(12.0, float(settings.proxy_api_timeout_seconds)),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"proxy test failed: {str(exc)[:300]}") from exc
    if not available:
        raise HTTPException(status_code=422, detail="proxy extracted successfully but cannot connect to Dola")
    return {
        "ok": True,
        "proxy_host_port": str(proxy.get("host_port") or ""),
        "proxy_scheme": proxy_scheme,
        "latency_ms": latency_ms,
    }


def _api_proxy_pool_snapshot() -> dict[str, object]:
    path = app_config.DATA_DIR / ".worker-health.json"
    try:
        health = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {"endpoints": 0, "active": 0, "capacity": 0, "slots": [], "last_error": ""}
    snapshot = health.get("api_proxy_pool") if isinstance(health, dict) else None
    if not isinstance(snapshot, dict):
        return {"endpoints": 0, "active": 0, "capacity": 0, "slots": [], "last_error": ""}
    slots = snapshot.get("slots") if isinstance(snapshot.get("slots"), list) else []
    return {
        "endpoints": max(0, int(snapshot.get("endpoints") or 0)),
        "active": max(0, int(snapshot.get("active") or 0)),
        "capacity": max(0, int(snapshot.get("capacity") or 0)),
        "contexts_per_endpoint": max(1, int(snapshot.get("contexts_per_endpoint") or 1)),
        "last_error": str(snapshot.get("last_error") or "")[:500],
        "slots": [
            {
                "id": str(item.get("id") or ""),
                "host_port": str(item.get("host_port") or ""),
                "active": max(0, int(item.get("active") or 0)),
                "total_leases": max(0, int(item.get("total_leases") or 0)),
                "last_leased_at": max(0.0, float(item.get("last_leased_at") or 0.0)),
                "state": str(item.get("state") or "idle"),
            }
            for item in slots
            if isinstance(item, dict) and str(item.get("host_port") or "")
        ],
    }


def _platform_proxy_source(settings, platform: str = "") -> tuple[str, str]:
    normalized = str(platform or "").strip().lower()
    if not normalized:
        return str(settings.proxy_source or "direct"), ""
    if normalized not in {"dola", "doubao", "qianwen"}:
        raise HTTPException(status_code=400, detail="platform must be one of dola, doubao, qianwen")
    source = str(settings.platform_proxy_sources.get(normalized) or settings.proxy_source or "direct").strip().lower()
    return source if source in {"subscription", "account", "api", "direct"} else "direct", normalized


@app.get("/config/proxy-nodes", dependencies=[Depends(require_admin)])
async def proxy_nodes(refresh: bool = False, platform: str = ""):
    settings = load_settings()
    source, normalized_platform = _platform_proxy_source(settings, platform)
    if source == "account":
        pool = list_account_proxies(settings)
        return {
            "source": "account",
            "platform": normalized_platform,
            "nodes": pool["proxies"],
            "enabled": settings.proxy_enabled,
            "auto_select": pool["rotation_enabled"],
            "selected_node": pool["selected_ids"][0] if len(pool["selected_ids"]) == 1 else "",
            "selected_ids": pool["selected_ids"],
            "auto_countries": [],
            "latency_threshold_ms": settings.proxy_latency_threshold_ms,
            "health_refresh_seconds": settings.proxy_health_refresh_seconds,
        }
    if source == "api":
        pool = _api_proxy_pool_snapshot()
        return {
            "source": "api",
            "platform": normalized_platform,
            "nodes": pool["slots"],
            "pool": pool,
            "enabled": settings.proxy_enabled,
            "auto_select": False,
            "selected_node": "",
            "selected_ids": [],
            "auto_countries": [],
            "latency_threshold_ms": settings.proxy_latency_threshold_ms,
            "health_refresh_seconds": settings.proxy_health_refresh_seconds,
        }
    if source != "subscription":
        return {"source": source, "platform": normalized_platform, "nodes": [], "enabled": settings.proxy_enabled, "auto_select": False, "selected_node": "", "selected_ids": [], "auto_countries": [], "latency_threshold_ms": settings.proxy_latency_threshold_ms, "health_refresh_seconds": settings.proxy_health_refresh_seconds}
    if not settings.proxy_subscription_url:
        return {"source": "subscription", "platform": normalized_platform, "nodes": [], "enabled": settings.proxy_enabled, "auto_select": settings.proxy_auto_select, "selected_node": "", "selected_ids": [], "auto_countries": settings.proxy_auto_countries, "latency_threshold_ms": settings.proxy_latency_threshold_ms, "health_refresh_seconds": settings.proxy_health_refresh_seconds}
    try:
        nodes = await fetch_subscription_node_list(
            settings.proxy_subscription_url,
            timeout_seconds=settings.proxy_api_timeout_seconds,
            refresh_seconds=settings.proxy_subscription_refresh_seconds,
            force=refresh,
        )
        if refresh:
            await rebuild_mihomo_from_snapshot(
                settings.proxy_subscription_url,
                nodes,
                settings.proxy_api_timeout_seconds,
                settings.proxy_subscription_refresh_seconds,
            )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    payloads = [node_payload(node, settings.proxy_selected_node) for node in nodes]
    visible = [
        payload for payload in payloads
        if payload.get("latency_status") != "unavailable"
        and (payload.get("latency_ms") is None or int(payload["latency_ms"]) <= settings.proxy_latency_threshold_ms)
    ]
    return {
        "source": "subscription",
        "platform": normalized_platform,
        "nodes": visible,
        "filtered_count": len(payloads) - len(visible),
        "enabled": settings.proxy_enabled,
        "auto_select": settings.proxy_auto_select,
        "selected_node": settings.proxy_selected_node,
        "selected_ids": [settings.proxy_selected_node] if settings.proxy_selected_node else [],
        "auto_countries": settings.proxy_auto_countries,
        "latency_threshold_ms": settings.proxy_latency_threshold_ms,
        "health_refresh_seconds": settings.proxy_health_refresh_seconds,
    }


async def _measure_account_proxies(proxy_ids: list[str], settings) -> dict:
    entries = account_proxy_entries(proxy_ids, settings)
    if proxy_ids and len(entries) != len(set(proxy_ids)):
        raise HTTPException(status_code=404, detail="authenticated proxy not found")
    semaphore = asyncio.Semaphore(32)

    async def measure(entry: dict) -> tuple[str, tuple[bool, int | None]]:
        async with semaphore:
            result = await probe_dola_proxy(account_proxy_url(entry), min(5.0, float(settings.proxy_api_timeout_seconds)))
            return str(entry["id"]), result

    measured = await asyncio.gather(*(measure(entry) for entry in entries))
    return update_account_proxy_latencies(dict(measured), settings)


@app.post("/config/proxy-nodes/latency", dependencies=[Depends(require_admin)])
async def proxy_node_latency(request: Request):
    settings = load_settings()
    payload = await _request_payload(request)
    source, normalized_platform = _platform_proxy_source(settings, str(payload.get("platform") or ""))
    if source == "account":
        raw_ids = payload.get("proxy_ids", [])
        if not isinstance(raw_ids, list):
            raise HTTPException(status_code=400, detail="proxy_ids must be an array")
        pool = await _measure_account_proxies([str(item) for item in raw_ids if str(item)], settings)
        return {"source": "account", "platform": normalized_platform, "nodes": pool["proxies"], "selected_ids": pool["selected_ids"], "auto_select": pool["rotation_enabled"]}
    if source != "subscription":
        raise HTTPException(status_code=409, detail=f"{source} proxy source does not support latency measurement")
    if not settings.proxy_subscription_url:
        raise HTTPException(status_code=409, detail="proxy subscription is not configured")
    try:
        nodes = await fetch_subscription_node_list(
            settings.proxy_subscription_url,
            timeout_seconds=settings.proxy_api_timeout_seconds,
            refresh_seconds=settings.proxy_subscription_refresh_seconds,
        )
        delays = await measure_node_delays(nodes, settings.proxy_subscription_url, settings.proxy_api_timeout_seconds)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    threshold = settings.proxy_latency_threshold_ms
    visible_nodes = [node for node in nodes if delays.get(node.id) is not None and int(delays[node.id]) <= threshold]
    countries = set(settings.proxy_auto_countries)
    selectable = [node for node in visible_nodes if countries and node.country in countries]
    selected_node = settings.proxy_selected_node
    if settings.proxy_auto_select:
        selected_node = min(selectable, key=lambda node: int(delays[node.id])).id if selectable else ""
        if selected_node != settings.proxy_selected_node:
            update_config({"proxy_selected_node": selected_node})
    payloads = []
    for node in visible_nodes:
        item = node_payload(node, selected_node)
        item.update({
            "latency_ms": int(delays[node.id]),
            "latency_measured": True,
            "latency_cached": False,
            "latency_status": "available",
        })
        payloads.append(item)
    return {
        "source": "subscription",
        "platform": normalized_platform,
        "nodes": payloads,
        "filtered_count": len(nodes) - len(visible_nodes),
        "selected_node": selected_node,
        "selected_ids": [selected_node] if selected_node else [],
        "auto_select": settings.proxy_auto_select,
        "latency_threshold_ms": threshold,
    }


@app.post("/config/proxy-nodes/select", dependencies=[Depends(require_admin)])
async def select_proxy_node(request: Request):
    payload = await _request_payload(request)
    node_id = str(payload.get("node_id") or "").strip()
    settings = load_settings()
    source, normalized_platform = _platform_proxy_source(settings, str(payload.get("platform") or ""))
    if source == "account":
        raw_ids = payload.get("node_ids", [node_id] if node_id else [])
        if not isinstance(raw_ids, list):
            raise HTTPException(status_code=400, detail="node_ids must be an array")
        try:
            pool = select_account_proxies(raw_ids, bool(payload.get("rotation_enabled", len(raw_ids) > 1)), settings)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "source": "account", "platform": normalized_platform, "selected_node": pool["selected_ids"][0] if len(pool["selected_ids"]) == 1 else "", "selected_ids": pool["selected_ids"], "auto_select": pool["rotation_enabled"], "nodes": pool["proxies"]}
    if source != "subscription":
        raise HTTPException(status_code=409, detail=f"{source} proxy source does not support node selection")
    if not settings.proxy_subscription_url:
        raise HTTPException(status_code=409, detail="proxy subscription is not configured")
    try:
        nodes = await fetch_subscription_node_list(
            settings.proxy_subscription_url,
            timeout_seconds=settings.proxy_api_timeout_seconds,
            refresh_seconds=settings.proxy_subscription_refresh_seconds,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    selected = next((node for node in nodes if node.id == node_id), None)
    if selected is None:
        raise HTTPException(status_code=404, detail="proxy node not found")
    try:
        await activate_mihomo_node(
            selected,
            settings.proxy_subscription_url,
            settings.proxy_api_timeout_seconds,
            settings.proxy_subscription_refresh_seconds,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    update_config({"proxy_selected_node": selected.id, "proxy_auto_select": False})
    return {"ok": True, "source": "subscription", "platform": normalized_platform, "selected_node": selected.id, "node": node_payload(selected, selected.id)}


@app.post("/config/account-proxies/import", dependencies=[Depends(require_admin)])
async def import_account_proxy_list(request: Request):
    payload = await _request_payload(request)
    try:
        result = import_account_proxies(str(payload.get("text") or ""), load_settings())
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    measured = await _measure_account_proxies(list(result.get("added_ids", [])), load_settings()) if result.get("added_ids") else {
        "proxies": result["proxies"],
        "selected_ids": result["selected_ids"],
        "rotation_enabled": result["rotation_enabled"],
    }
    return {"ok": True, "added": result["added"], "duplicates": result["duplicates"], **measured}


@app.post("/config/account-proxies/action", dependencies=[Depends(require_admin)])
async def account_proxy_action(request: Request):
    payload = await _request_payload(request)
    action = str(payload.get("action") or "").strip().lower()
    raw_ids = payload.get("proxy_ids", [])
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="proxy_ids must be an array")
    proxy_ids = [str(item) for item in raw_ids if str(item)]
    try:
        if action == "delete":
            result = delete_account_proxies(proxy_ids, load_settings())
        elif action == "enable":
            result = set_account_proxies_enabled(proxy_ids, True, load_settings())
        elif action == "disable":
            result = set_account_proxies_enabled(proxy_ids, False, load_settings())
        else:
            raise ValueError("unsupported account proxy action")
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **result}


@app.get("/admin/repository-update", dependencies=[Depends(require_admin)])
async def repository_update_status():
    try:
        return await asyncio.to_thread(repository_status, ADMIN_DIR.parent.parent)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/admin/repository-update", dependencies=[Depends(require_admin)])
async def repository_update_action():
    try:
        return await asyncio.to_thread(update_repository, ADMIN_DIR.parent.parent)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="repository update timed out")
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/config/registration-email", dependencies=[Depends(require_admin)])
async def registration_email_config():
    settings = load_settings()
    return {
        "enabled": settings.registration_email_verification_enabled,
        "domains": list(settings.registration_email_domains),
        "smtp_host": settings.registration_smtp_host,
        "smtp_port": settings.registration_smtp_port,
        "smtp_username": settings.registration_smtp_username,
        "authorization_code_configured": bool(settings.registration_smtp_authorization_code),
        "sender_name": settings.registration_email_sender_name,
        "code_ttl_minutes": settings.registration_email_code_ttl_minutes,
    }


@app.get("/admin/invitation-codes", dependencies=[Depends(require_admin)])
async def admin_invitation_codes(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=10, le=100),
    q: str = Query("", max_length=80),
    usage: str = Query("all"),
):
    try:
        return await asyncio.to_thread(invitation_state, None, page, page_size, q, usage)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/admin/invitation-codes", dependencies=[Depends(require_admin)], status_code=201)
async def admin_generate_invitation_codes(request: Request):
    payload = await _request_payload(request)
    try:
        count = int(payload.get("count") or 1)
        length = int(payload.get("length") or 7)
        note = str(payload.get("note") or "").strip()[:120]
        cards = await asyncio.to_thread(generate_invitation_codes, count, length, note)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await _record_admin_action_safe(
        "invitation_generate",
        "生成邀请码",
        detail=f"生成 {len(cards)} 个 {length} 位长期邀请码" + (f"，备注：{note}" if note else ""),
        actor=load_settings().admin_username,
        ip_address=_request_client_key(request),
    )
    return {"ok": True, "count": len(cards), "generated": cards, **invitation_state()}


@app.patch("/admin/invitation-codes/{code_id}/note", dependencies=[Depends(require_admin)])
async def admin_update_invitation_code(code_id: str, request: Request):
    payload = await _request_payload(request)
    note = str(payload.get("note") or "").strip()[:120]
    updated = await asyncio.to_thread(update_invitation_code_note, code_id, note)
    if not updated:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    await _record_admin_action_safe(
        "invitation_update",
        "修改邀请码备注",
        detail=f"{updated.get('code') or ''}：{note or '清空备注'}",
        actor=load_settings().admin_username,
        ip_address=_request_client_key(request),
        reference_id=code_id,
    )
    return {"ok": True, "code": updated, **invitation_state()}


@app.patch("/admin/invitation-codes/settings", dependencies=[Depends(require_admin)])
async def admin_invitation_code_settings(request: Request):
    payload = await _request_payload(request)
    raw_required = payload.get("required")
    if not isinstance(raw_required, bool):
        raise HTTPException(status_code=400, detail="required must be a boolean")
    state = await asyncio.to_thread(set_invitation_registration_required, raw_required)
    await _record_admin_action_safe(
        "invitation_setting",
        "修改邀请码注册设置",
        detail="启用强制邀请码注册" if raw_required else "关闭强制邀请码注册",
        actor=load_settings().admin_username,
        ip_address=_request_client_key(request),
    )
    return {"ok": True, **state}


@app.delete("/admin/invitation-codes/{code_id}", dependencies=[Depends(require_admin)])
async def admin_delete_invitation_code(code_id: str, request: Request):
    try:
        deleted = await asyncio.to_thread(delete_invitation_code, code_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    await _record_admin_action_safe(
        "invitation_delete",
        "删除邀请码",
        detail=str(deleted.get("code") or ""),
        actor=load_settings().admin_username,
        ip_address=_request_client_key(request),
        reference_id=code_id,
    )
    return {"ok": True, **invitation_state()}


@app.get("/admin/audit-logs", dependencies=[Depends(require_admin)])
async def admin_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=10, le=100),
    q: str = Query("", max_length=100),
    action: str = Query("all", max_length=60),
):
    return await asyncio.to_thread(list_admin_actions, page, page_size, q, action)


@app.get("/config/workers", dependencies=[Depends(require_token)])
async def workers_config():
    settings = load_settings()
    effective_workers, resources = adaptive_worker_limit(BROWSER_SUBMISSION_CONCURRENCY, BROWSER_SUBMISSION_CONCURRENCY)
    return {
        "browser_workers": BROWSER_SUBMISSION_CONCURRENCY,
        "max_effective_workers": BROWSER_SUBMISSION_CONCURRENCY,
        "effective_browser_workers": effective_workers,
        "capacity_limit": resources["capacity_limit"],
        "browser_pool_processes": BROWSER_POOL_PROCESSES,
        "browser_contexts_per_process": BROWSER_CONTEXTS_PER_PROCESS,
        "submission_concurrency": BROWSER_SUBMISSION_CONCURRENCY,
        "remote_generation_limit": settings.remote_generation_limit,
    }


@app.get("/config/runtime", dependencies=[Depends(require_admin)])
async def runtime_config():
    settings = load_settings()
    return {
        "dola_submit_interval_seconds": settings.dola_submit_interval_seconds,
        "dola_global_submit_interval_seconds": settings.dola_global_submit_interval_seconds,
        "task_retry_limit": settings.task_retry_limit,
        "doubao_submit_retry_limit": settings.doubao_submit_retry_limit,
        "batch_history_retention_days": settings.batch_history_retention_days,
    }


@app.get("/admin/task-pause", dependencies=[Depends(require_admin)])
async def admin_task_pause_state():
    runtime = await asyncio.to_thread(load_runtime)
    return {
        "paused": bool(runtime.get("task_submission_paused", False)),
        "paused_at": str(runtime.get("task_submission_paused_at") or ""),
    }


@app.post("/admin/task-pause", dependencies=[Depends(require_admin)])
async def update_admin_task_pause(request: Request):
    payload = await _request_payload(request)
    if not isinstance(payload.get("paused"), bool):
        raise HTTPException(status_code=400, detail="paused must be a boolean")
    paused = bool(payload["paused"])
    runtime = await asyncio.to_thread(set_task_submission_paused, paused)
    canceled_metas = await asyncio.to_thread(cancel_pending_tasks) if paused else []
    canceled_batch_rows = 0
    if paused:
        active_batch_jobs = await asyncio.to_thread(list_batch_jobs, None, active_only=True, limit=1000)
        for job in active_batch_jobs:
            queued_rows = sum(
                str(row.get("status") or "") in {"queued", "creating"} and not str(row.get("task_id") or "")
                for row in job.get("rows", [])
                if isinstance(row, dict)
            )
            if not queued_rows:
                continue
            await asyncio.to_thread(
                cancel_persistent_batch_job,
                str(job.get("id") or ""),
                str(job.get("owner_token_hash") or ""),
                "管理员暂停任务发布",
            )
            canceled_batch_rows += queued_rows
    for meta in canceled_metas:
        await asyncio.to_thread(_refund_canceled_task, meta)
    await _record_admin_action_safe(
        "task_pause" if paused else "task_resume",
        "暂停任务发布" if paused else "恢复任务发布",
        detail=f"取消排队任务 {len(canceled_metas) + canceled_batch_rows} 条" if paused else "已恢复接收和发布任务",
        actor=load_settings().admin_username,
        ip_address=_request_client_key(request),
    )
    return {
        "ok": True,
        "paused": paused,
        "paused_at": str(runtime.get("task_submission_paused_at") or ""),
        "canceled_pending": len(canceled_metas),
        "canceled_batch_rows": canceled_batch_rows,
    }


@app.get("/config/registration-security", dependencies=[Depends(require_admin)])
async def registration_security_config():
    return {"enabled": load_settings().registration_abuse_detection_enabled}


@app.post("/config/runtime", dependencies=[Depends(require_admin)])
async def update_runtime_config(request: Request):
    payload = await _request_payload(request)
    settings = load_settings()
    try:
        submit_interval = float(payload.get("dola_submit_interval_seconds", settings.dola_submit_interval_seconds))
        global_submit_interval = float(payload.get(
            "dola_global_submit_interval_seconds",
            payload.get("dola_exit_submit_interval_seconds", settings.dola_global_submit_interval_seconds),
        ))
        task_retry_limit = int(payload.get("task_retry_limit", settings.task_retry_limit))
        doubao_submit_retry_limit = int(payload.get("doubao_submit_retry_limit", settings.doubao_submit_retry_limit))
        batch_history_retention_days = int(payload.get("batch_history_retention_days", settings.batch_history_retention_days))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="runtime retention values must be numbers")
    if submit_interval < 1 or submit_interval > 5:
        raise HTTPException(status_code=400, detail="dola_submit_interval_seconds must be between 1 and 5")
    if global_submit_interval < 3 or global_submit_interval > 30:
        raise HTTPException(status_code=400, detail="dola_global_submit_interval_seconds must be between 3 and 30")
    if task_retry_limit < 0 or task_retry_limit > 10:
        raise HTTPException(status_code=400, detail="task_retry_limit must be between 0 and 10")
    if doubao_submit_retry_limit < 0 or doubao_submit_retry_limit > 10:
        raise HTTPException(status_code=400, detail="doubao_submit_retry_limit must be between 0 and 10")
    if batch_history_retention_days < 7 or batch_history_retention_days > 30:
        raise HTTPException(status_code=400, detail="batch_history_retention_days must be between 7 and 30")
    submit_interval = round(submit_interval, 1)
    update_config({
        "dola_submit_interval_seconds": submit_interval,
        "dola_global_submit_interval_seconds": round(global_submit_interval, 1),
        "task_retry_limit": task_retry_limit,
        "doubao_submit_retry_limit": doubao_submit_retry_limit,
        "batch_history_retention_days": batch_history_retention_days,
    })
    refreshed = load_settings()
    return {
        "ok": True,
        "dola_submit_interval_seconds": refreshed.dola_submit_interval_seconds,
        "dola_global_submit_interval_seconds": refreshed.dola_global_submit_interval_seconds,
        "task_retry_limit": refreshed.task_retry_limit,
        "doubao_submit_retry_limit": refreshed.doubao_submit_retry_limit,
        "batch_history_retention_days": refreshed.batch_history_retention_days,
    }


@app.get("/config/platforms", dependencies=[Depends(require_token)])
async def platforms_config():
    settings = load_settings()
    return {
        "default_platform": settings.default_platform,
        "platforms": [
            {
                "id": platform,
                "label": PLATFORM_LABELS.get(platform, platform),
                "models": [
                    model
                    for model in settings.platform_models.get(platform, [])
                    if settings.platform_model_states.get(platform, {}).get(model, True)
                    and settings.model_durations.get(platform, {}).get(model, [])
                ],
                "model_costs": {model: model_cost_points(platform, model) for model in settings.platform_models.get(platform, [])},
                "model_durations": {model: settings.model_durations.get(platform, {}).get(model, []) for model in settings.platform_models.get(platform, [])},
                "model_duration_costs": {
                    model: {
                        str(duration): model_cost_points(platform, model, duration=duration)
                        for duration in PLATFORM_VIDEO_DURATIONS[platform]
                    }
                    for model in settings.platform_models.get(platform, [])
                },
                "supported_durations": list(PLATFORM_VIDEO_DURATIONS[platform]),
                "all_models": [
                    {
                        "name": model,
                        "enabled": settings.platform_model_states.get(platform, {}).get(model, True),
                        "cost": model_cost_points(platform, model),
                        "durations": settings.model_durations.get(platform, {}).get(model, []),
                        "duration_costs": {
                            str(duration): model_cost_points(platform, model, duration=duration)
                            for duration in PLATFORM_VIDEO_DURATIONS[platform]
                        },
                    }
                    for model in settings.platform_models.get(platform, [])
                ],
                "enabled": platform in {DEFAULT_PLATFORM, "doubao", "qianwen"},
            }
            for platform in PLATFORM_LABELS
        ],
    }


@app.get("/v1/models")
async def openai_models(_access: Annotated[AccessContext, Depends(require_openai_token)]):
    settings = load_settings()
    data = []
    for platform in PLATFORM_LABELS:
        for model in settings.platform_models.get(platform, []):
            if settings.platform_model_states.get(platform, {}).get(model, True) and settings.model_durations.get(platform, {}).get(model, []):
                data.append({"id": f"{platform}:{model}", "object": "model", "created": 0, "owned_by": platform})
    return {"object": "list", "data": data}


def _video_ratio_from_size(value: object) -> str:
    normalized = str(value or "").strip().lower().replace("×", "x")
    if "x" not in normalized:
        return ""
    try:
        width, height = (int(part) for part in normalized.split("x", 1))
    except (TypeError, ValueError):
        return ""
    if width <= 0 or height <= 0:
        return ""
    ratios = {
        "9:16": 9 / 16,
        "16:9": 16 / 9,
        "1:1": 1.0,
        "3:4": 3 / 4,
        "4:3": 4 / 3,
        "21:9": 21 / 9,
    }
    actual = width / height
    return min(ratios, key=lambda ratio: abs(ratios[ratio] - actual))


def _decode_video_data_image(value: str, index: int) -> tuple[str, bytes]:
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\r\n]+)", str(value or "").strip(), flags=re.IGNORECASE)
    if not match:
        raise OpenAIAPIError(400, "Reference images must use multipart upload or a base64 data URL", "invalid_request_error", "content", "invalid_image")
    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[match.group(1).lower()]
    try:
        encoded = re.sub(r"\s+", "", match.group(2))
        data = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise OpenAIAPIError(400, "Invalid reference image data", "invalid_request_error", "content", "invalid_image") from exc
    if len(data) > MAX_UPLOAD_BYTES:
        raise OpenAIAPIError(413, "Reference image is too large", "invalid_request_error", "content", "image_too_large")
    return f"reference-{index}{suffix}", data


async def _parse_openai_video_request(request: Request) -> tuple[dict[str, object], list[tuple[str, bytes]]]:
    content_type = str(request.headers.get("content-type") or "").lower()
    images: list[tuple[str, bytes]] = []
    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        payload: dict[str, object] = {}
        for key, value in form.multi_items():
            if hasattr(value, "filename") and hasattr(value, "read"):
                filename = Path(str(value.filename or f"reference-{len(images) + 1}.png")).name
                data = await value.read(MAX_UPLOAD_BYTES + 1)
                await value.close()
                if len(data) > MAX_UPLOAD_BYTES:
                    raise OpenAIAPIError(413, "Reference image is too large", "invalid_request_error", key, "image_too_large")
                images.append((filename, data))
            else:
                payload[str(key)] = str(value)
    else:
        try:
            payload = await request.json()
        except Exception as exc:
            raise OpenAIAPIError(400, "A JSON or multipart body is required", "invalid_request_error", code="invalid_json") from exc
        if not isinstance(payload, dict):
            raise OpenAIAPIError(400, "Request body must be an object", "invalid_request_error", code="invalid_json")

    content = payload.get("content")
    text_parts: list[str] = []
    image_values: list[str] = []
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").lower()
            if item_type in {"text", "input_text"} and str(item.get("text") or "").strip():
                text_parts.append(str(item.get("text") or ""))
            if item_type in {"image_url", "input_image", "image"}:
                image_value = item.get("image_url") or item.get("image") or item.get("url")
                if isinstance(image_value, dict):
                    image_value = image_value.get("url")
                if str(image_value or "").strip():
                    image_values.append(str(image_value))
    for key in ("image_url", "input_reference", "reference_image"):
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("url")
        if isinstance(value, str) and value.strip():
            image_values.append(value)
    for image_value in image_values:
        images.append(_decode_video_data_image(image_value, len(images) + 1))

    prompt = str(payload.get("prompt") or "").strip() or "\n".join(part.strip() for part in text_parts if part.strip())
    ratio = str(payload.get("ratio") or "").strip() or _video_ratio_from_size(payload.get("size") or payload.get("resolution")) or DEFAULT_RATIO
    raw_duration = payload.get("duration") if payload.get("duration") is not None else payload.get("seconds")
    try:
        duration = int(raw_duration) if raw_duration not in {None, ""} else None
    except (TypeError, ValueError) as exc:
        raise OpenAIAPIError(400, "Invalid duration", "invalid_request_error", "duration", "invalid_value") from exc
    normalized = {
        "model": str(payload.get("model") or "").strip(),
        "prompt": repair_text(prompt),
        "ratio": ratio,
        "duration": duration,
        "reference_is_real_person": str(payload.get("reference_is_real_person") or "false").lower() in {"1", "true", "yes", "on"},
    }
    return normalized, images


async def _create_compatible_video_task(
    request: Request,
    access: AccessContext,
    payload: dict[str, object],
    images: list[tuple[str, bytes]],
    idempotency_key: str | None,
) -> tuple[dict, bool]:
    assert create_sem is not None
    async with _owner_create_semaphore(access), create_sem:
        try:
            await asyncio.to_thread(_admit_task_creation)
        except HTTPException as exc:
            raise OpenAIAPIError(exc.status_code, "Service temporarily overloaded", "server_error", code="service_unavailable", headers=exc.headers)
        prompt = str(payload.get("prompt") or "").strip()
        model_id = str(payload.get("model") or "").strip()
        ratio = str(payload.get("ratio") or DEFAULT_RATIO).strip()
        if not prompt:
            raise OpenAIAPIError(400, "A non-empty prompt is required", "invalid_request_error", "prompt", "missing_prompt")
        if len(prompt.encode("utf-8")) > 8192:
            raise OpenAIAPIError(400, "Prompt is too long", "invalid_request_error", "prompt", "string_too_long")
        platform, separator, model = model_id.partition(":")
        if not separator:
            raise OpenAIAPIError(404, f"The model '{model_id}' does not exist", "invalid_request_error", "model", "model_not_found")
        try:
            platform, model = validate_task_platform_model(platform, model)
        except (ValueError, HTTPException):
            raise OpenAIAPIError(404, f"The model '{model_id}' does not exist or is disabled", "invalid_request_error", "model", "model_not_found")
        if ratio not in VALID_RATIOS:
            raise OpenAIAPIError(400, "Invalid ratio", "invalid_request_error", "ratio", "invalid_value")
        try:
            duration = validate_task_duration(platform, model, payload.get("duration"))
        except HTTPException as exc:
            raise OpenAIAPIError(400, str(exc.detail), "invalid_request_error", "duration", "invalid_value")
        if len(images) > load_settings().max_image_count:
            raise OpenAIAPIError(400, "Too many reference images", "invalid_request_error", "input_reference", "array_too_long")
        for filename, data in images:
            suffix = Path(filename).suffix.lower()
            if suffix not in ALLOWED_IMAGE_SUFFIXES or not any(data.startswith(magic) for magic in IMAGE_MAGIC[suffix]):
                raise OpenAIAPIError(400, "Invalid reference image", "invalid_request_error", "input_reference", "invalid_image")
        try:
            await _rate_limit(request, "openai-video-task", 30, 60, access.token_hash)
            key = _idempotency_key(idempotency_key)
        except HTTPException as exc:
            error_type = "rate_limit_error" if exc.status_code == 429 else "invalid_request_error"
            error_code = "rate_limit_exceeded" if exc.status_code == 429 else "invalid_request"
            raise OpenAIAPIError(exc.status_code, str(exc.detail), error_type, code=error_code, headers=exc.headers) from exc
        fingerprint = _request_fingerprint("openai-video", access.token_hash, {
            "prompt": prompt,
            "ratio": ratio,
            "duration": duration,
            "platform": platform,
            "model": model,
            "images": [{"name": name, "sha256": hashlib.sha256(data).hexdigest()} for name, data in images],
        })
        meta: dict | None = None
        reserved_access = None
        reservation: dict = {}
        try:
            if key:
                meta, created = await _storage_call(
                    find_or_create_task,
                    prompt,
                    ratio,
                    access.token_hash if access.is_temp else "",
                    platform,
                    model,
                    "video",
                    key,
                    fingerprint,
                    "openai-video",
                    duration,
                )
            else:
                meta = await _storage_call(
                    create_task,
                    prompt,
                    ratio,
                    owner_token_hash=access.token_hash if access.is_temp else "",
                    platform=platform,
                    model=model,
                    task_type="video",
                    enqueue=False,
                    duration=duration,
                )
                created = True
            resumed_initializing = not created and str(meta.get("status") or "") == "initializing"
            if not created and not resumed_initializing:
                return meta, False
            queued_for_concurrency = access.is_temp and await _storage_call(active_task_count_for_owner, access.token_hash) >= access.concurrency
            base_cost_units = model_cost_units(platform, model, "video", duration)
            discount_units = await _storage_call(task_discount_units_by_token_hash, access.token_hash, platform, model) if access.is_temp else 0
            cost_units = max(1, base_cost_units - discount_units)
            user_id = await _storage_call(_transaction_user_id, access)
            reserved_access = await _storage_call(reserve_temp_quota, access, str(meta["id"]), cost_units, user_id=user_id)
            reservation = await _storage_call(get_temp_reservation, access.token_hash, str(meta["id"])) if access.is_temp else {}
            if user_id and reservation:
                charged_units = int(reservation.get("units") or 0)
                free_used = bool(reservation.get("free"))
                await _storage_call(
                    record_transaction,
                    user_id,
                    "video_quota_consume" if free_used else "consume",
                    0 if free_used else -charged_units,
                    "视频额度任务消费" if free_used else "视频任务消费",
                    balance_units=reserved_access.credit_units,
                    video_quota_change=-1 if free_used else 0,
                    video_quota_balance=reserved_access.free_remaining,
                    reference_id=str(meta["id"]),
                    detail=f"任务 ID：{meta['id']}\n{PLATFORM_LABELS.get(platform, platform)} / {model}",
                    transaction_id=f"task-{str(meta['id'])[:27]}",
                )
            saved_paths: list[Path] = []
            saved_names: list[str] = []
            for index, (filename, data) in enumerate(images, start=1):
                suffix = Path(filename).suffix.lower()
                target = images_dir(str(meta["id"])) / f"{index:02d}{suffix}"
                await asyncio.to_thread(_save_image_bytes, data, filename, target)
                saved_paths.append(target)
                saved_names.append(_reference_image_name(filename, index, suffix))
            await _storage_call(set_task_images, str(meta["id"]), saved_paths, saved_names)
            await _storage_call(
                update_meta,
                str(meta["id"]),
                reference_is_real_person=bool(payload.get("reference_is_real_person")),
                openai_video_queued_for_concurrency=bool(queued_for_concurrency),
            )
            await _storage_call(finalize_task_creation, str(meta["id"]))
            if user_id:
                await _record_activity_safe(
                    user_id,
                    "task_submit",
                    "通过 API 提交视频生成任务",
                    reference_id=str(meta["id"]),
                    detail=f"{model} / {ratio}",
                )
            meta = await _storage_call(get_meta, str(meta["id"]))
            meta["queued_for_concurrency"] = bool(queued_for_concurrency)
            return meta, True
        except ValueError as exc:
            raise OpenAIAPIError(409, str(exc), "invalid_request_error", "Idempotency-Key", "idempotency_conflict") from exc
        except OpenAIAPIError:
            if meta:
                await _storage_call(refund_temp_quota_hash, access.token_hash, str(meta["id"]), attempts=2)
                await _storage_call(delete_task, str(meta["id"]), attempts=2)
            raise
        except QuotaExceeded:
            if meta:
                await _storage_call(delete_task, str(meta["id"]), attempts=2)
            raise OpenAIAPIError(429, "You exceeded your current quota", "insufficient_quota", code="insufficient_quota")
        except HTTPException as exc:
            if meta:
                await _storage_call(refund_temp_quota_hash, access.token_hash, str(meta["id"]), attempts=2)
                await _storage_call(delete_task, str(meta["id"]), attempts=2)
            raise OpenAIAPIError(exc.status_code, str(exc.detail), "invalid_request_error", code="invalid_request")
        except Exception as exc:
            if meta:
                await _storage_call(refund_temp_quota_hash, access.token_hash, str(meta["id"]), attempts=2)
                await _storage_call(delete_task, str(meta["id"]), attempts=2)
            logger.exception("OpenAI-compatible video task creation failed (error_type=%s)", type(exc).__name__)
            raise OpenAIAPIError(500, "Failed to create video task", "server_error", code="internal_error") from exc


def _external_base_url(request: Request) -> str:
    scheme = str(request.headers.get("x-forwarded-proto") or request.url.scheme).split(",", 1)[0].strip()
    host = str(request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",", 1)[0].strip()
    return f"{scheme}://{host}".rstrip("/")


def _compatible_video_response(request: Request, meta: dict, *, vendor: bool = False, result: dict | None = None) -> dict:
    task_id = str(meta.get("id") or "")
    internal_status = str(meta.get("status") or "pending")
    if vendor:
        status = {"pending": "queued", "running": "running", "submitted": "running", "success": "succeeded", "failed": "failed", "canceled": "cancelled"}.get(internal_status, "queued")
    else:
        status = {"pending": "queued", "running": "in_progress", "submitted": "in_progress", "success": "completed", "failed": "failed", "canceled": "cancelled"}.get(internal_status, "queued")
    base_url = _external_base_url(request)
    video_url = f"{base_url}/tasks/{task_id}/video" if internal_status == "success" else ""
    created_at = str(meta.get("created_at") or "")
    payload = {
        "id": task_id,
        "object": "video",
        "model": f"{meta.get('platform') or 'dola'}:{meta.get('model') or ''}",
        "status": status,
        "created_at": created_at,
        "progress": 100 if internal_status == "success" else 0,
        "ratio": str(meta.get("ratio") or DEFAULT_RATIO),
        "seconds": int(meta.get("duration") or 10),
        "video_url": video_url,
        "result_endpoint": f"{base_url}/v1/videos/{task_id}",
        "content_endpoint": f"{base_url}/v1/videos/{task_id}/content",
        "queued_for_concurrency": bool(meta.get("openai_video_queued_for_concurrency")),
    }
    if vendor:
        payload["content"] = {"video_url": video_url} if video_url else {}
    if internal_status == "failed":
        detail = str((result or {}).get("text") or meta.get("error") or "Video generation failed")
        payload["error"] = {"message": detail, "code": "generation_failed"}
    return payload


@app.post("/v1/videos")
@app.post("/v1/videos/generations")
@app.post("/v1/video/generations")
@app.post("/api/v3/contents/generations/tasks")
@app.post("/v1/api/v3/contents/generations/tasks")
async def openai_video_create(
    request: Request,
    access: Annotated[AccessContext, Depends(require_openai_token)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    payload, images = await _parse_openai_video_request(request)
    meta, _created = await _create_compatible_video_task(request, access, payload, images, idempotency_key)
    return _compatible_video_response(request, meta, vendor="/api/v3/" in request.url.path)


@app.get("/v1/videos/{task_id}")
@app.get("/v1/videos/generations/{task_id}")
@app.get("/v1/video/generations/{task_id}")
@app.get("/api/v3/contents/generations/tasks/{task_id}")
@app.get("/v1/api/v3/contents/generations/tasks/{task_id}")
async def openai_video_status(
    request: Request,
    access: Annotated[AccessContext, Depends(require_openai_token)],
    task_id: str,
):
    try:
        validate_task_id(task_id)
        meta = await _storage_call(get_meta, task_id)
    except (ValueError, FileNotFoundError):
        raise OpenAIAPIError(404, "Video task not found", "invalid_request_error", "id", "not_found")
    if access.is_temp and str(meta.get("owner_token_hash") or "") != access.token_hash:
        raise OpenAIAPIError(404, "Video task not found", "invalid_request_error", "id", "not_found")
    result = await query_task(task_id)
    meta = await _storage_call(get_meta, task_id)
    return _compatible_video_response(request, meta, vendor="/api/v3/" in request.url.path, result=result)


@app.get("/v1/videos/{task_id}/content")
async def openai_video_content(
    request: Request,
    access: Annotated[AccessContext, Depends(require_openai_token)],
    task_id: str,
):
    try:
        return await task_video(request, access, task_id)
    except HTTPException as exc:
        code = "not_found" if exc.status_code == 404 else "video_unavailable"
        raise OpenAIAPIError(exc.status_code, str(exc.detail), "invalid_request_error", "id", code, headers=exc.headers) from exc


@app.delete("/v1/videos/{task_id}")
@app.delete("/api/v3/contents/generations/tasks/{task_id}")
@app.delete("/v1/api/v3/contents/generations/tasks/{task_id}")
async def openai_video_cancel(
    access: Annotated[AccessContext, Depends(require_openai_token)],
    task_id: str,
):
    try:
        return await remove_task(access, task_id)
    except HTTPException as exc:
        code = "not_found" if exc.status_code == 404 else "invalid_request"
        raise OpenAIAPIError(exc.status_code, str(exc.detail), "invalid_request_error", "id", code, headers=exc.headers) from exc


@app.post("/v1/chat/completions")
async def openai_chat_completions(
    payload: OpenAIChatRequest,
    access: Annotated[AccessContext, Depends(require_openai_token)],
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    try:
        _admit_task_creation()
    except HTTPException as exc:
        raise OpenAIAPIError(exc.status_code, "Service temporarily overloaded", "server_error", code="service_unavailable", headers=exc.headers)
    if payload.stream:
        raise OpenAIAPIError(400, "Streaming is not supported", "invalid_request_error", "stream", "unsupported_value")
    if payload.n != 1:
        raise OpenAIAPIError(400, "Only n=1 is supported", "invalid_request_error", "n", "unsupported_value")
    if len(payload.messages) > 100:
        raise OpenAIAPIError(400, "Too many messages", "invalid_request_error", "messages", "array_too_long")
    prompt = next((item.content.strip() for item in reversed(payload.messages) if item.role == "user" and item.content.strip()), "")
    if not prompt:
        raise OpenAIAPIError(400, "A non-empty user message is required", "invalid_request_error", "messages", "missing_user_message")
    if len(prompt.encode("utf-8")) > 8192:
        raise OpenAIAPIError(400, "Prompt is too long", "invalid_request_error", "messages", "string_too_long")
    platform, separator, model = payload.model.partition(":")
    if not separator or not platform or not model:
        raise OpenAIAPIError(404, f"The model '{payload.model}' does not exist", "invalid_request_error", "model", "model_not_found")
    try:
        platform, model = validate_task_platform_model(platform, model)
    except (ValueError, HTTPException):
        raise OpenAIAPIError(404, f"The model '{payload.model}' does not exist or is disabled", "invalid_request_error", "model", "model_not_found")
    if payload.ratio not in VALID_RATIOS:
        raise OpenAIAPIError(400, "Invalid ratio", "invalid_request_error", "ratio", "invalid_value")
    if platform == "qianwen" and payload.task_type != "video":
        raise OpenAIAPIError(400, "Qianwen only supports video tasks", "invalid_request_error", "task_type", "unsupported_value")
    try:
        duration = validate_task_duration(platform, model, payload.duration)
    except HTTPException as exc:
        raise OpenAIAPIError(400, str(exc.detail), "invalid_request_error", "duration", "invalid_value")
    task_type = "video"
    await _rate_limit(request, "openai-task", 30, 60, access.token_hash)
    key = _idempotency_key(idempotency_key)
    fingerprint = _request_fingerprint("openai", access.token_hash, {"prompt": repair_text(prompt), "ratio": payload.ratio, "duration": duration, "platform": platform, "model": model, "task_type": task_type})
    try:
        if key:
            meta, created = find_or_create_task(repair_text(prompt), payload.ratio, access.token_hash if access.is_temp else "", platform, model, task_type, key, fingerprint, "openai", duration)
        else:
            meta, created = create_task(repair_text(prompt), payload.ratio, owner_token_hash=access.token_hash if access.is_temp else "", platform=platform, model=model, task_type=task_type, enqueue=False, duration=duration), True
        if not created:
            task_id = str(meta["id"])
            content = json.dumps({"task_id": task_id, "status": str(meta.get("status") or "submitted"), "result_endpoint": f"/tasks/{task_id}"}, ensure_ascii=False)
            return {"id": f"chatcmpl-{task_id}", "object": "chat.completion", "created": int(time.time()), "model": payload.model, "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
        queued_for_concurrency = False
        if access.is_temp:
            access = get_temp_context_by_hash(access.token_hash) or access
            queued_for_concurrency = active_task_count_for_owner(access.token_hash) >= access.concurrency
        base_cost_units = model_cost_units(platform, model, task_type, duration)
        discount_units = task_discount_units_by_token_hash(access.token_hash, platform, model) if access.is_temp else 0
        cost_units = max(1, base_cost_units - discount_units)
        user_id = _transaction_user_id(access)
        reserved_access = reserve_temp_quota(access, str(meta["id"]), cost_units, user_id=user_id)
        reservation = get_temp_reservation(access.token_hash, str(meta["id"])) if access.is_temp else {}
        charged_units = int(reservation.get("units") or 0)
        if user_id and reservation:
            free_used = bool(reservation.get("free"))
            record_transaction(
                user_id,
                "video_quota_consume" if free_used else "consume",
                0 if free_used else -charged_units,
                "视频额度任务消费" if free_used else "视频任务消费",
                balance_units=reserved_access.credit_units,
                video_quota_change=-1 if free_used else 0,
                video_quota_balance=reserved_access.free_remaining,
                reference_id=str(meta["id"]),
                detail=f"任务 ID：{meta['id']}\n{PLATFORM_LABELS.get(platform, platform)} / {model}",
            )
        finalize_task_creation(str(meta["id"]))
    except ValueError as exc:
        raise OpenAIAPIError(409, str(exc), "invalid_request_error", "Idempotency-Key", "idempotency_conflict")
    except OpenAIAPIError:
        if "meta" in locals():
            refund_temp_quota_hash(access.token_hash, str(meta["id"]))
            delete_task(str(meta["id"]))
        raise
    except QuotaExceeded:
        if "meta" in locals():
            delete_task(str(meta["id"]))
        raise OpenAIAPIError(429, "You exceeded your current quota", "insufficient_quota", code="insufficient_quota")
    except Exception:
        if "meta" in locals():
            refund_temp_quota_hash(access.token_hash, str(meta["id"]))
            delete_task(str(meta["id"]))
        raise OpenAIAPIError(500, "Failed to create task", "server_error", code="internal_error")
    task_id = str(meta["id"])
    content = json.dumps({"task_id": task_id, "status": "pending", "queued_for_concurrency": queued_for_concurrency, "result_endpoint": f"/tasks/{task_id}"}, ensure_ascii=False)
    return {
        "id": f"chatcmpl-{task_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/config/platforms", dependencies=[Depends(require_admin)])
async def update_platforms_config(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body is required")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    raw_platforms = payload.get("platforms")
    if not isinstance(raw_platforms, list):
        raise HTTPException(status_code=400, detail="platforms is required")
    models_by_platform: dict[str, list[str]] = {platform: [] for platform in PLATFORM_LABELS}
    states_by_platform: dict[str, dict[str, bool]] = {platform: {} for platform in PLATFORM_LABELS}
    costs_by_platform: dict[str, dict[str, int | float]] = {platform: {} for platform in PLATFORM_LABELS}
    durations_by_platform: dict[str, dict[str, list[int]]] = {platform: {} for platform in PLATFORM_LABELS}
    duration_costs_by_platform: dict[str, dict[str, dict[str, int | float]]] = {platform: {} for platform in PLATFORM_LABELS}
    for item in raw_platforms:
        if not isinstance(item, dict):
            continue
        try:
            platform = normalize_platform(str(item.get("id") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        raw_models = item.get("models")
        if not isinstance(raw_models, list):
            raise HTTPException(status_code=400, detail=f"models is required for {platform}")
        seen: set[str] = set()
        for raw_model in raw_models:
            if isinstance(raw_model, dict):
                model = normalize_model(str(raw_model.get("name") or ""))
                enabled = bool(raw_model.get("enabled", True))
                try:
                    cost = units_to_points(points_to_units(raw_model.get("cost", 1)))
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=f"{platform} {model or '模型'}: {exc}")
                raw_durations = raw_model.get("durations", PLATFORM_VIDEO_DURATIONS[platform])
                if not isinstance(raw_durations, list):
                    raise HTTPException(status_code=400, detail=f"{platform} {model or '模型'}: 时长配置无效")
                unsupported = [value for value in raw_durations if not isinstance(value, int) or value not in PLATFORM_VIDEO_DURATIONS[platform]]
                if unsupported:
                    raise HTTPException(status_code=400, detail=f"{platform} {model or '模型'}: 包含不支持的时长")
                durations = [value for value in PLATFORM_VIDEO_DURATIONS[platform] if value in raw_durations]
                raw_duration_costs = raw_model.get("duration_costs") if isinstance(raw_model.get("duration_costs"), dict) else {}
                duration_costs: dict[str, int | float] = {}
                for duration in PLATFORM_VIDEO_DURATIONS[platform]:
                    try:
                        duration_costs[str(duration)] = units_to_points(points_to_units(raw_duration_costs.get(str(duration), cost)))
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=f"{platform} {model or '模型'} {duration} 秒: {exc}")
            else:
                model = normalize_model(str(raw_model or ""))
                enabled = True
                cost = model_cost_points(platform, model)
                durations = list(PLATFORM_VIDEO_DURATIONS[platform])
                duration_costs = {str(duration): model_cost_points(platform, model, duration=duration) for duration in durations}
            if not model or model in seen:
                continue
            seen.add(model)
            models_by_platform[platform].append(model)
            states_by_platform[platform][model] = enabled
            costs_by_platform[platform][model] = cost
            durations_by_platform[platform][model] = durations
            duration_costs_by_platform[platform][model] = duration_costs
    default_platform = str(payload.get("default_platform") or load_settings().default_platform)
    try:
        default_platform = normalize_platform(default_platform)
        update_config({"default_platform": default_platform, "platform_models": models_by_platform, "platform_model_states": states_by_platform, "model_costs": costs_by_platform, "model_durations": durations_by_platform, "model_duration_costs": duration_costs_by_platform})
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return await platforms_config()


@app.post("/config/platforms/qianwen/sync", dependencies=[Depends(require_admin)])
async def sync_qianwen_models():
    try:
        discovered = await fetch_qianwen_video_models()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"千问模型获取失败：{str(exc)[:200]}")
    if not discovered:
        raise HTTPException(status_code=502, detail="未获取到千问可用视频模型")
    settings = load_settings()
    existing = settings.platform_models.get("qianwen", [])
    models = discovered + [item for item in existing if item not in discovered]
    models_by_platform = {platform: list(settings.platform_models.get(platform, [])) for platform in PLATFORM_LABELS}
    states_by_platform = {platform: dict(settings.platform_model_states.get(platform, {})) for platform in PLATFORM_LABELS}
    models_by_platform["qianwen"] = models
    qianwen_states = states_by_platform.setdefault("qianwen", {})
    for model in discovered:
        qianwen_states.setdefault(model, True)
    update_config({"platform_models": models_by_platform, "platform_model_states": states_by_platform})
    response = await platforms_config()
    response["discovered"] = discovered
    return response


@app.post("/config/platforms/{platform}/sync", dependencies=[Depends(require_admin)])
async def sync_platform_models(platform: str):
    try:
        platform = normalize_platform(platform)
        discovered = await (fetch_qianwen_video_models() if platform == "qianwen" else fetch_platform_video_models(platform))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{platform} 模型获取失败：{str(exc)[:200]}")
    if not discovered:
        raise HTTPException(status_code=502, detail=f"未获取到 {platform} 可用视频模型")
    settings = load_settings()
    models_by_platform = {item: list(settings.platform_models.get(item, [])) for item in PLATFORM_LABELS}
    states_by_platform = {item: dict(settings.platform_model_states.get(item, {})) for item in PLATFORM_LABELS}
    existing = models_by_platform[platform]
    models_by_platform[platform] = discovered + [item for item in existing if item not in discovered]
    for model in discovered:
        states_by_platform[platform].setdefault(model, True)
    update_config({"platform_models": models_by_platform, "platform_model_states": states_by_platform})
    response = await platforms_config()
    response["discovered"] = discovered
    response["synced_platform"] = platform
    return response


@app.post("/config/workers", dependencies=[Depends(require_token)])
async def update_workers_config(
    access: Annotated[AccessContext, Depends(require_token)],
    request: Request,
    browser_workers: Annotated[int | None, Query()] = None,
):
    if access.is_temp:
        raise HTTPException(status_code=403, detail="forbidden")
    payload = await _request_payload(request)
    raw_workers = payload.get("browser_workers") or payload.get("workers") or browser_workers or BROWSER_SUBMISSION_CONCURRENCY
    raw_capacity = payload.get("max_effective_workers") or payload.get("capacity_limit") or BROWSER_SUBMISSION_CONCURRENCY
    try:
        workers = int(raw_workers)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="browser_workers must be an integer")
    if workers < 1 or workers > 999:
        raise HTTPException(status_code=400, detail="browser_workers must be between 1 and 999")
    try:
        capacity = int(raw_capacity) if raw_capacity is not None else load_settings().max_effective_workers
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="max_effective_workers must be an integer")
    if capacity < 1 or capacity > 999:
        raise HTTPException(status_code=400, detail="max_effective_workers must be between 1 and 999")
    try:
        update_config({"browser_workers": BROWSER_SUBMISSION_CONCURRENCY, "max_effective_workers": BROWSER_SUBMISSION_CONCURRENCY, "remote_generation_limit": 0})
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    settings = load_settings()
    effective_workers, resources = adaptive_worker_limit(BROWSER_SUBMISSION_CONCURRENCY, BROWSER_SUBMISSION_CONCURRENCY)
    return {
        "ok": True,
        "browser_workers": BROWSER_SUBMISSION_CONCURRENCY,
        "max_effective_workers": BROWSER_SUBMISSION_CONCURRENCY,
        "effective_browser_workers": effective_workers,
        "capacity_limit": resources["capacity_limit"],
        "browser_pool_processes": BROWSER_POOL_PROCESSES,
        "browser_contexts_per_process": BROWSER_CONTEXTS_PER_PROCESS,
        "submission_concurrency": BROWSER_SUBMISSION_CONCURRENCY,
        "remote_generation_limit": 0,
    }


@app.get("/config/account-access", dependencies=[Depends(require_admin)])
async def account_access_config():
    return {
        **account_access_status(),
        "groups_endpoint": "/account-access/groups",
        "accounts_endpoint": "/account-access/accounts",
    }


@app.post("/config/account-access/rotate", dependencies=[Depends(require_admin)])
async def account_access_rotate(request: Request):
    raw_key, key_status = await asyncio.to_thread(generate_account_access_key)
    await _record_admin_action_safe(
        "account_access_rotate",
        "生成账号访问密钥",
        detail=f"新密钥：{key_status.get('hint') or '-'}；旧密钥已立即失效",
        ip_address=_request_client_key(request),
    )
    return {"ok": True, "key": raw_key, **key_status}


@app.patch("/config/account-access", dependencies=[Depends(require_admin)])
async def account_access_update(request: Request):
    payload = await _request_payload(request)
    if "enabled" not in payload:
        raise HTTPException(status_code=400, detail="enabled is required")
    enabled = str(payload.get("enabled") or "").strip().lower() in {"1", "true", "yes", "y", "on"}
    try:
        key_status = await asyncio.to_thread(set_account_access_enabled, enabled)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await _record_admin_action_safe(
        "account_access_setting",
        "启用账号访问密钥" if enabled else "停用账号访问密钥",
        detail=f"密钥：{key_status.get('hint') or '-'}",
        ip_address=_request_client_key(request),
    )
    return {"ok": True, **key_status}


@app.delete("/config/account-access", dependencies=[Depends(require_admin)])
async def account_access_revoke(request: Request):
    previous = account_access_status()
    key_status = await asyncio.to_thread(revoke_account_access_key)
    await _record_admin_action_safe(
        "account_access_revoke",
        "撤销账号访问密钥",
        detail=f"已撤销密钥：{previous.get('hint') or '-'}",
        ip_address=_request_client_key(request),
    )
    return {"ok": True, **key_status}


def _account_access_public_account(account: dict) -> dict:
    return {
        key: account.get(key)
        for key in (
            "id", "platform", "account_source", "name", "enabled", "account_status", "status_reason",
            "quota_limit", "quota_used", "quota_remaining", "quota_reset_date",
            "created_at", "updated_at",
        )
    }


@app.get("/account-access/groups")
async def account_access_groups(
    _access_key: Annotated[str, Depends(require_account_access)],
):
    rows = await asyncio.to_thread(list_accounts)
    groups = []
    for group_id, label in PLATFORM_LABELS.items():
        members = [item for item in rows if str(item.get("platform") or DEFAULT_PLATFORM) == group_id]
        groups.append(
            {
                "id": group_id,
                "name": label,
                "account_count": len(members),
                "enabled_count": sum(item.get("enabled") is not False for item in members),
            }
        )
    return {"groups": groups}


@app.get("/account-access/accounts")
async def account_access_accounts(
    _access_key: Annotated[str, Depends(require_account_access)],
    group: str = Query(DEFAULT_PLATFORM),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=200),
):
    try:
        platform = normalize_platform(group)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    rows = await asyncio.to_thread(list_accounts, platform=platform)
    total = len(rows)
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = min(page, total_pages)
    start = (current_page - 1) * page_size
    return {
        "accounts": [_account_access_public_account(item) for item in rows[start:start + page_size]],
        "group": platform,
        "total": total,
        "page": current_page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@app.post("/account-access/accounts", status_code=201)
async def account_access_create_account(
    request: Request,
    _access_key: Annotated[str, Depends(require_account_access)],
):
    await _rate_limit(request, "account-access-create", 60, 60, _access_key)
    payload = await _request_payload(request)
    allowed_fields = {"group", "platform", "name", "cookie_data", "cookies", "cookie", "quota_limit", "enabled"}
    unknown = sorted(set(payload) - allowed_fields)
    if unknown:
        raise HTTPException(status_code=400, detail=f"不支持的字段：{', '.join(unknown)}")
    try:
        platform = normalize_platform(payload.get("group") or payload.get("platform") or DEFAULT_PLATFORM)
        raw_cookie_data = payload.get("cookie_data") or payload.get("cookies") or payload.get("cookie") or ""
        cookie_data = json.dumps(raw_cookie_data, ensure_ascii=False) if isinstance(raw_cookie_data, (dict, list)) else str(raw_cookie_data)
        default_quota_limit = load_settings().account_default_quotas[platform]
        quota_limit = int(payload.get("quota_limit") if payload.get("quota_limit") not in {None, ""} else default_quota_limit)
        if quota_limit < 0 or quota_limit > 1_000_000:
            raise ValueError("账号额度上限需为 0-1000000")
        enabled = str(payload.get("enabled") if payload.get("enabled") is not None else "true").lower() not in {"0", "false", "no", "off"}
        account = await asyncio.to_thread(
            add_account,
            payload.get("name") or "",
            cookie_data,
            enabled,
            quota_limit,
            platform,
            "api",
        )
        _clear_account_list_cache()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await _record_admin_action_safe(
        "account_access_create",
        "访问密钥添加账号",
        detail=f"分组：{PLATFORM_LABELS.get(platform, platform)}；名称：{account.get('name') or '-'}；额度上限：{account.get('quota_limit')}",
        actor="账号访问密钥",
        ip_address=_request_client_key(request),
        reference_id=str(account.get("id") or ""),
    )
    return {"ok": True, "account": _account_access_public_account(account)}


@app.get("/config/account-quotas", dependencies=[Depends(require_admin)])
async def account_quota_config():
    settings = load_settings()
    return {
        "default_quotas": settings.account_default_quotas,
        "quota_costs": {
            platform: {
                model: {str(duration): cost for duration, cost in costs.items()}
                for model, costs in platform_costs.items()
            }
            for platform, platform_costs in settings.account_quota_costs.items()
        },
        "platforms": [
            {
                "id": platform,
                "name": PLATFORM_LABELS[platform],
                "models": [
                    {"name": model, "durations": list(PLATFORM_VIDEO_DURATIONS[platform])}
                    for model in settings.platform_models.get(platform, [])
                ],
            }
            for platform in PLATFORM_LABELS
        ],
    }


@app.post("/config/account-quotas", dependencies=[Depends(require_admin)])
async def update_account_quota_config(request: Request):
    payload = await _request_payload(request)
    settings = load_settings()
    raw_defaults = payload.get("default_quotas")
    raw_costs = payload.get("quota_costs")
    if not isinstance(raw_defaults, dict) or not isinstance(raw_costs, dict):
        raise HTTPException(status_code=400, detail="default_quotas and quota_costs are required")

    defaults: dict[str, int] = {}
    costs: dict[str, dict[str, dict[str, int]]] = {}
    try:
        for platform in PLATFORM_LABELS:
            value = int(raw_defaults.get(platform, settings.account_default_quotas[platform]))
            if value < 0 or value > 1_000_000:
                raise ValueError(f"{platform} 默认额度需为 0-1000000")
            defaults[platform] = value
            platform_payload = raw_costs.get(platform, {})
            if not isinstance(platform_payload, dict):
                raise ValueError(f"{platform} 额度消耗配置无效")
            costs[platform] = {}
            for model in settings.platform_models.get(platform, []):
                model_payload = platform_payload.get(model, {})
                if not isinstance(model_payload, dict):
                    raise ValueError(f"{platform} {model} 额度消耗配置无效")
                costs[platform][model] = {}
                for duration in PLATFORM_VIDEO_DURATIONS[platform]:
                    fallback = settings.account_quota_costs.get(platform, {}).get(model, {}).get(duration, 1)
                    cost = int(model_payload.get(str(duration), model_payload.get(duration, fallback)))
                    if cost < 1 or cost > 1000:
                        raise ValueError(f"{platform} {model} {duration}秒消耗额度需为 1-1000")
                    costs[platform][model][str(duration)] = cost
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    update_config({"account_default_quotas": defaults, "account_quota_costs": costs, "account_quota_policy_version": 1})
    synced = await asyncio.to_thread(sync_account_default_quotas, defaults)
    _clear_account_list_cache()
    await _record_admin_action_safe(
        "account_quota_setting",
        "修改账号额度规则",
        detail="；".join(f"{PLATFORM_LABELS[platform]} 默认 {defaults[platform]}，同步 {synced.get(platform, 0)} 个账号" for platform in PLATFORM_LABELS),
        ip_address=_request_client_key(request),
    )
    return {**(await account_quota_config()), "ok": True, "synced_accounts": synced}


@app.patch("/account-access/accounts/{account_id}")
async def account_access_update_account(
    account_id: str,
    request: Request,
    _access_key: Annotated[str, Depends(require_account_access)],
):
    await _rate_limit(request, "account-access-update", 120, 60, _access_key)
    payload = await _request_payload(request)
    unknown = sorted(set(payload) - {"name", "quota_limit"})
    if unknown:
        raise HTTPException(status_code=400, detail=f"此密钥不能修改字段：{', '.join(unknown)}")
    if not payload:
        raise HTTPException(status_code=400, detail="至少需要提供账号名称或额度上限")
    try:
        account = await asyncio.to_thread(
            update_account_details,
            account_id,
            name=payload.get("name") if "name" in payload else None,
            quota_limit=payload.get("quota_limit") if "quota_limit" in payload else None,
        )
        _clear_account_list_cache()
    except KeyError:
        raise HTTPException(status_code=404, detail="account not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    changed = []
    if "name" in payload:
        changed.append(f"名称：{account.get('name') or '-'}")
    if "quota_limit" in payload:
        changed.append(f"额度上限：{account.get('quota_limit')}")
    await _record_admin_action_safe(
        "account_access_update",
        "访问密钥修改账号",
        detail="；".join(changed),
        actor="账号访问密钥",
        ip_address=_request_client_key(request),
        reference_id=str(account.get("id") or ""),
    )
    return {"ok": True, "account": _account_access_public_account(account)}


@app.get("/accounts", dependencies=[Depends(require_admin)])
async def accounts_list(
    page: int | None = Query(None, ge=1),
    page_size: int | None = Query(None, ge=1, le=100),
    q: str | None = Query(None, max_length=200),
    platform: str | None = Query(None),
    status: str | None = Query(None),
):
    return await asyncio.to_thread(_accounts_list_payload, page, page_size, q, platform, status)


@app.get("/accounts/deletion-history", dependencies=[Depends(require_admin)])
async def accounts_deletion_history(limit: int = Query(90, ge=1, le=180)):
    days = await asyncio.to_thread(list_account_deletion_history, limit)
    return {
        "days": days,
        "retention_days": 180,
        "cleanup_time": "23:00",
        "timezone": "Asia/Shanghai",
    }


def _run_account_list_maintenance() -> None:
    global _ACCOUNT_LIST_MAINTENANCE_AT

    now = time.monotonic()
    if now < _ACCOUNT_LIST_MAINTENANCE_AT or not _ACCOUNT_LIST_MAINTENANCE_LOCK.acquire(blocking=False):
        return
    try:
        if time.monotonic() < _ACCOUNT_LIST_MAINTENANCE_AT:
            return
        reset_daily_account_quotas_if_needed()
        reconcile_account_quotas()
        _ACCOUNT_LIST_MAINTENANCE_AT = time.monotonic() + ACCOUNT_LIST_MAINTENANCE_SECONDS
    except Exception:
        _ACCOUNT_LIST_MAINTENANCE_AT = time.monotonic() + 5.0
        raise
    finally:
        _ACCOUNT_LIST_MAINTENANCE_LOCK.release()


def _clear_account_list_cache() -> None:
    global _ACCOUNT_LIST_IN_FLIGHT

    with _ACCOUNT_LIST_CACHE_LOCK:
        _ACCOUNT_LIST_IN_FLIGHT = None


def _build_account_list_snapshot() -> dict:
    _run_account_list_maintenance()
    accounts = list_accounts()
    current_task_ids = [str(item.get("current_task_id") or "") for item in accounts if item.get("current_task_id")]
    current_tasks = {
        task_id: (meta, result)
        for task_id, meta, result in task_states(current_task_ids)
    }
    active_by_account = account_active_tasks()
    normalized_accounts = []
    for account in accounts:
        current_task_id = str(account.get("current_task_id") or "")
        current_meta, current_result = current_tasks.get(current_task_id, ({}, {}))
        current_status = str(current_meta.get("status") or "")
        keep_current = current_status == "running" or (current_status == "success" and not current_result.get("decoded_main_url"))
        if current_task_id and not keep_current:
            clear_account_current_task(str(account.get("id") or ""), current_task_id)
            account = {**account, "current_task_id": "", "current_worker_id": "", "current_started_at": ""}
        account_active = active_by_account.get(str(account.get("id") or ""), [])
        if account_active:
            first = account_active[0]
            account = {
                **account,
                "current_task_id": str(first.get("task_id") or ""),
                "current_worker_id": str(first.get("worker_id") or ""),
                "active_tasks": account_active,
                "active_task_count": len(account_active),
            }
        else:
            account = {**account, "active_tasks": [], "active_task_count": 0}
        normalized_accounts.append(account)
    total_limit = sum(max(0, int(item.get("quota_limit") or 0)) for item in normalized_accounts)
    total_used = sum(max(0, int(item.get("quota_used") or 0)) for item in normalized_accounts)
    unlimited_count = sum(1 for item in normalized_accounts if not int(item.get("quota_limit") or 0))
    return {
        "accounts": normalized_accounts,
        "quota_summary": {
            "total_limit": total_limit,
            "total_used": total_used,
            "total_remaining": max(0, total_limit - total_used),
            "unlimited_count": unlimited_count,
        },
        "next_quota_reset_at": next_quota_reset_at(),
    }


def _account_list_snapshot() -> dict:
    global _ACCOUNT_LIST_IN_FLIGHT

    with _ACCOUNT_LIST_CACHE_LOCK:
        current = _ACCOUNT_LIST_IN_FLIGHT
        owns_build = current is None
        if owns_build:
            current = Future()
            _ACCOUNT_LIST_IN_FLIGHT = current
    assert current is not None
    if not owns_build:
        return copy.deepcopy(current.result(timeout=120))
    try:
        snapshot = _build_account_list_snapshot()
        current.set_result(snapshot)
        return copy.deepcopy(snapshot)
    except BaseException as exc:
        current.set_exception(exc)
        raise
    finally:
        with _ACCOUNT_LIST_CACHE_LOCK:
            if _ACCOUNT_LIST_IN_FLIGHT is current:
                _ACCOUNT_LIST_IN_FLIGHT = None


def _accounts_list_payload(
    page: int | None,
    page_size: int | None,
    q: str | None,
    platform: str | None,
    status: str | None,
) -> dict:
    response = _account_list_snapshot()
    accounts = response["accounts"]
    if page is None and page_size is None and q is None and platform is None and status is None:
        return response
    selected_platform = str(platform or "").strip().lower()
    if selected_platform and selected_platform != "all":
        try:
            selected_platform = normalize_platform(selected_platform)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    else:
        selected_platform = ""
    platform_accounts = [item for item in accounts if not selected_platform or str(item.get("platform") or DEFAULT_PLATFORM) == selected_platform]
    selected_status = str(status or "").strip().lower()
    if selected_status not in {"", "all", "normal", "api", "ten_second", "slider_verification", "abnormal", "disabled"}:
        raise HTTPException(status_code=422, detail="账号状态筛选无效")
    if selected_status in {"", "all"}:
        status_accounts = platform_accounts
    elif selected_status == "normal":
        status_accounts = [item for item in platform_accounts if item.get("enabled") is not False and str(item.get("account_status") or "normal") == "normal"]
    elif selected_status == "api":
        status_accounts = [item for item in platform_accounts if str(item.get("account_source") or "admin") == "api"]
    elif selected_status == "ten_second":
        status_accounts = [item for item in platform_accounts if item.get("ten_second_only")]
    elif selected_status == "slider_verification":
        status_accounts = [item for item in platform_accounts if item.get("account_status") == "slider_verification"]
    elif selected_status == "abnormal":
        status_accounts = [item for item in platform_accounts if item.get("account_status") == "abnormal"]
    else:
        status_accounts = [item for item in platform_accounts if item.get("enabled") is False and str(item.get("account_status") or "normal") == "normal"]
    keyword = str(q or "").strip().lower()
    filtered = [
        item for item in status_accounts
        if not keyword or any(
            keyword in str(value or "").lower()
            for value in (
                item.get("id"), item.get("name"), item.get("account_source"), item.get("account_status"), item.get("status_reason"),
                item.get("current_task_id"), item.get("current_worker_id"), item.get("last_used_worker_id"),
            )
        )
    ]
    effective_page_size = page_size or 20
    total = len(filtered)
    total_pages = max(1, (total + effective_page_size - 1) // effective_page_size)
    current_page = min(page or 1, total_pages)
    start = (current_page - 1) * effective_page_size
    response.update(
        accounts=filtered[start:start + effective_page_size],
        total=total,
        page=current_page,
        page_size=effective_page_size,
        total_pages=total_pages,
        stats={
            "total": len(platform_accounts),
            "normal": sum(item.get("enabled") is not False and str(item.get("account_status") or "normal") == "normal" for item in platform_accounts),
            "api": sum(str(item.get("account_source") or "admin") == "api" for item in platform_accounts),
            "ten_second": sum(bool(item.get("ten_second_only")) for item in platform_accounts),
            "slider_verification": sum(item.get("account_status") == "slider_verification" for item in platform_accounts),
            "abnormal": sum(item.get("account_status") == "abnormal" for item in platform_accounts),
            "disabled": sum(item.get("enabled") is False for item in platform_accounts),
            "by_platform": {
                item: sum(str(account.get("platform") or DEFAULT_PLATFORM) == item for account in accounts)
                for item in PLATFORM_LABELS
            },
        },
    )
    return response


def _available_generation_accounts(platform: str = DEFAULT_PLATFORM, duration: int = 0) -> list[dict[str, object]]:
    target_platform = normalize_platform(platform)
    available: list[dict[str, object]] = []
    for account in _account_list_snapshot()["accounts"]:
        quota_remaining = account.get("quota_remaining")
        if (
            str(account.get("platform") or DEFAULT_PLATFORM) != target_platform
            or account.get("enabled") is False
            or str(account.get("account_status") or "normal") != "normal"
            or int(account.get("cookie_count") or 0) <= 0
            or str(account.get("current_task_id") or "")
            or int(account.get("active_task_count") or 0) > 0
            or (quota_remaining is not None and int(quota_remaining) <= 0)
            or (target_platform == "dola" and bool(account.get("ten_second_only")) and (int(duration or 0) <= 0 or int(duration or 0) > 10))
        ):
            continue
        available.append({
            "id": str(account.get("id") or ""),
            "name": str(account.get("name") or ""),
            "platform": target_platform,
            "quota_limit": max(0, int(account.get("quota_limit") or 0)),
            "quota_used": max(0, int(account.get("quota_used") or 0)),
            "quota_remaining": int(quota_remaining) if quota_remaining is not None else None,
            "ten_second_only": bool(account.get("ten_second_only")),
        })
    available.sort(
        key=lambda item: (
            item["quota_remaining"] is not None,
            -int(item["quota_remaining"] or 0),
            str(item["name"]),
            str(item["id"]),
        )
    )
    return available


@app.get("/accounts/available", dependencies=[Depends(require_admin)])
async def available_generation_accounts(platform: str = Query(DEFAULT_PLATFORM), duration: int = Query(0, ge=0, le=60)):
    try:
        accounts = await asyncio.to_thread(_available_generation_accounts, platform, duration)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"accounts": accounts, "total": len(accounts)}


@app.post("/accounts", dependencies=[Depends(require_admin)])
async def accounts_create(request: Request):
    import asyncio

    payload = await _request_payload(request)
    cookie_data = payload.get("cookie_data") or payload.get("cookies") or payload.get("cookie") or ""
    bulk = str(payload.get("bulk") or "").strip().lower() in {"1", "true", "yes", "y", "on"}
    try:
        platform = normalize_platform(payload.get("platform") or DEFAULT_PLATFORM)
        account_source = "api" if str(payload.get("account_source") or "admin").strip().lower() == "api" else "admin"
        default_quota_limit = load_settings().account_default_quotas[platform]
        quota_limit = int(payload.get("quota_limit") if payload.get("quota_limit") not in {None, ""} else default_quota_limit)
        enabled = str(payload.get("enabled") if payload.get("enabled") is not None else "true").lower() not in {"0", "false", "no", "off"}
        if bulk:
            result = await asyncio.to_thread(add_accounts_bulk_result, cookie_data, quota_limit, enabled, platform, account_source)
            _clear_account_list_cache()
            return {"ok": True, **result}
        account = await asyncio.to_thread(
            add_account,
            payload.get("name") or "",
            cookie_data,
            enabled,
            quota_limit,
            platform,
            account_source,
        )
        _clear_account_list_cache()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "account": account}


@app.patch("/accounts/{account_id}", dependencies=[Depends(require_admin)])
@app.put("/accounts/{account_id}", dependencies=[Depends(require_admin)])
async def accounts_update(account_id: str, request: Request):
    payload = await _request_payload(request)
    try:
        if "reset_quota" in payload and str(payload.get("reset_quota") or "").strip().lower() in {"1", "true", "yes", "y", "on"}:
            account = await asyncio.to_thread(reset_account_quota, account_id)
        elif "quota_limit" in payload:
            account = await asyncio.to_thread(update_account_quota, account_id, int(payload.get("quota_limit") or 0))
        elif "enabled" in payload:
            enabled = str(payload.get("enabled") or "").strip().lower() in {"1", "true", "yes", "y", "on"}
            account = await asyncio.to_thread(set_account_enabled, account_id, enabled)
        else:
            raise HTTPException(status_code=400, detail="enabled or quota_limit is required")
    except KeyError:
        raise HTTPException(status_code=404, detail="account not found")
    except ValueError:
        raise HTTPException(status_code=400, detail="quota_limit must be an integer")
    _clear_account_list_cache()
    return {"ok": True, "account": account}


@app.delete("/accounts/{account_id}", dependencies=[Depends(require_admin)])
async def accounts_delete(account_id: str):
    if not await asyncio.to_thread(delete_account, account_id):
        raise HTTPException(status_code=404, detail="account not found")
    _clear_account_list_cache()
    return {"ok": True}


@app.get("/admin/data-backup", dependencies=[Depends(require_admin)])
async def admin_data_backup():
    try:
        archive = await asyncio.to_thread(create_backup)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=f"备份创建失败：{exc}") from exc
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="dola-user-account-backup-{stamp}.zip"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/admin/data-restore", dependencies=[Depends(require_admin)])
async def admin_data_restore(
    upload: UploadFile = File(...),
    confirm: bool = Form(False),
):
    if not confirm:
        raise HTTPException(status_code=400, detail="恢复前必须确认将覆盖用户、额度和账号数据")
    active = await asyncio.to_thread(
        list_task_metas_by_statuses,
        {"pending", "running", "submitted"},
        limit=2000,
    )
    if active:
        raise HTTPException(status_code=409, detail=f"当前有 {len(active)} 个生成中任务，请等待任务完成后再恢复数据")
    raw = await upload.read(MAX_BACKUP_BYTES + 1)
    try:
        result = await asyncio.to_thread(restore_backup, raw)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=f"恢复失败：{exc}") from exc
    record_admin_action("data_restore", "恢复用户与账号数据", detail=json.dumps(result, ensure_ascii=False))
    return {"ok": True, "restored": result}


@app.post("/admin/disk-cleanup", dependencies=[Depends(require_admin)])
async def admin_disk_cleanup():
    before = shutil.disk_usage(DATA_DIR)
    active = await asyncio.to_thread(active_task_ids)
    result = await asyncio.to_thread(cleanup_terminal_tasks_before_local_day, 1, active)
    after = shutil.disk_usage(DATA_DIR)
    released = max(0, int(after.free) - int(before.free))
    payload = {
        **result,
        "released_bytes": released,
        "disk_before": {"total_bytes": int(before.total), "used_bytes": int(before.used), "free_bytes": int(before.free)},
        "disk_after": {"total_bytes": int(after.total), "used_bytes": int(after.used), "free_bytes": int(after.free)},
    }
    record_admin_action(
        "disk_cleanup",
        "清理历史任务文件",
        detail=json.dumps({"deleted": result["deleted"], "cutoff_local": result["cutoff_local"], "released_bytes": released}, ensure_ascii=False),
    )
    return {"ok": True, **payload}


@app.post("/accounts/{account_id}/delete", dependencies=[Depends(require_admin)])
async def accounts_delete_action(account_id: str):
    if not await asyncio.to_thread(delete_account, account_id):
        raise HTTPException(status_code=404, detail="account not found")
    _clear_account_list_cache()
    return {"ok": True}


@app.patch("/config/proxy-api", dependencies=[Depends(require_admin)])
@app.put("/config/proxy-api", dependencies=[Depends(require_admin)])
@app.post("/config/proxy-api", dependencies=[Depends(require_admin)])
async def update_proxy_api_config(
    request: Request,
    url: Annotated[str | None, Query()] = None,
    proxy_api_url: Annotated[str | None, Query()] = None,
    scheme: Annotated[str | None, Query()] = None,
    proxy_api_scheme: Annotated[str | None, Query()] = None,
):
    payload = await _request_payload(request)
    current = load_settings()
    if "proxy_api_url" in payload:
        next_url = payload.get("proxy_api_url")
    elif "url" in payload:
        next_url = payload.get("url")
    else:
        next_url = proxy_api_url if proxy_api_url is not None else url
    next_scheme = payload.get("proxy_api_scheme") or payload.get("scheme") or proxy_api_scheme or scheme
    try:
        updates = {}
        if "proxy_source" in payload:
            proxy_source = str(payload.get("proxy_source") or "").strip().lower()
            if proxy_source not in {"subscription", "account", "api", "direct"}:
                raise ValueError("proxy_source must be one of subscription, account, api, direct")
            updates["proxy_source"] = proxy_source
        if "platform_proxy_sources" in payload:
            raw_sources = payload.get("platform_proxy_sources")
            if not isinstance(raw_sources, dict):
                raise ValueError("platform_proxy_sources must be an object")
            platform_sources = dict(current.platform_proxy_sources)
            for platform in PLATFORM_LABELS:
                if platform not in raw_sources:
                    continue
                source = str(raw_sources.get(platform) or "").strip().lower()
                if source not in {"subscription", "account", "api", "direct"}:
                    raise ValueError(f"invalid proxy source for {platform}")
                platform_sources[platform] = source
            updates["platform_proxy_sources"] = platform_sources
        if "platform_proxy_random" in payload:
            raw_random = payload.get("platform_proxy_random")
            if not isinstance(raw_random, dict):
                raise ValueError("platform_proxy_random must be an object")
            platform_random = dict(current.platform_proxy_random)
            for platform in PLATFORM_LABELS:
                if platform in raw_random:
                    platform_random[platform] = str(raw_random.get(platform)).strip().lower() in {"1", "true", "yes", "on"}
            updates["platform_proxy_random"] = platform_random
        if next_url is not None:
            updates["proxy_api_url"] = validate_proxy_api_url(next_url)
        if next_scheme:
            updates["proxy_api_scheme"] = validate_proxy_api_scheme(next_scheme)
        if "proxy_subscription_url" in payload:
            updates["proxy_subscription_url"] = validate_proxy_api_url(payload.get("proxy_subscription_url"))
        if "proxy_subscription_scheme" in payload:
            updates["proxy_subscription_scheme"] = validate_proxy_api_scheme(payload.get("proxy_subscription_scheme"))
        if "proxy_subscription_refresh_seconds" in payload:
            refresh_seconds = int(payload.get("proxy_subscription_refresh_seconds"))
            if refresh_seconds < 60 or refresh_seconds > 86400:
                raise ValueError("proxy_subscription_refresh_seconds must be between 60 and 86400")
            updates["proxy_subscription_refresh_seconds"] = refresh_seconds
        if str(payload.get("proxy_account_clear") or "").strip().lower() in {"1", "true", "yes", "on"}:
            updates.update({
                "proxy_account_host": "",
                "proxy_account_port": 0,
                "proxy_account_username": "",
                "proxy_account_password": "",
            })
        else:
            if "proxy_account_scheme" in payload:
                updates["proxy_account_scheme"] = validate_proxy_api_scheme(payload.get("proxy_account_scheme"))
            if "proxy_account_host" in payload and str(payload.get("proxy_account_host") or "").strip():
                updates["proxy_account_host"] = validate_proxy_account_host(payload.get("proxy_account_host"))
            if "proxy_account_port" in payload and str(payload.get("proxy_account_port") or "").strip():
                account_port = int(payload.get("proxy_account_port"))
                if account_port < 1 or account_port > 65535:
                    raise ValueError("proxy_account_port must be between 1 and 65535")
                updates["proxy_account_port"] = account_port
            if "proxy_account_username" in payload and str(payload.get("proxy_account_username") or "").strip():
                account_username = str(payload.get("proxy_account_username") or "").strip()
                if len(account_username) > 300 or any(char in account_username for char in "\r\n\0"):
                    raise ValueError("proxy_account_username is invalid")
                updates["proxy_account_username"] = account_username
            if "proxy_account_password" in payload and str(payload.get("proxy_account_password") or ""):
                account_password = str(payload.get("proxy_account_password") or "")
                if len(account_password) > 500 or any(char in account_password for char in "\r\n\0"):
                    raise ValueError("proxy_account_password is invalid")
                updates["proxy_account_password"] = account_password
        if "proxy_enabled" in payload:
            updates["proxy_enabled"] = str(payload.get("proxy_enabled")).strip().lower() in {"1", "true", "yes", "on"}
        if "proxy_auto_select" in payload:
            updates["proxy_auto_select"] = str(payload.get("proxy_auto_select")).strip().lower() in {"1", "true", "yes", "on"}
        if "proxy_selected_node" in payload:
            updates["proxy_selected_node"] = str(payload.get("proxy_selected_node") or "").strip()[:200]
        if "proxy_auto_countries" in payload:
            raw_countries = payload.get("proxy_auto_countries")
            if not isinstance(raw_countries, list):
                raise ValueError("proxy_auto_countries must be an array")
            updates["proxy_auto_countries"] = list(dict.fromkeys(
                str(item).strip()[:40] for item in raw_countries if str(item or "").strip()
            ))
        if "proxy_latency_threshold_ms" in payload:
            threshold = int(payload.get("proxy_latency_threshold_ms"))
            if threshold < 100 or threshold > 5000:
                raise ValueError("proxy_latency_threshold_ms must be between 100 and 5000")
            updates["proxy_latency_threshold_ms"] = threshold
        if "proxy_health_refresh_seconds" in payload:
            refresh_seconds = int(payload.get("proxy_health_refresh_seconds"))
            if refresh_seconds < 60 or refresh_seconds > 86400:
                raise ValueError("proxy_health_refresh_seconds must be between 60 and 86400")
            updates["proxy_health_refresh_seconds"] = refresh_seconds
        if "proxy_source" not in payload and current.proxy_source == "direct":
            if str(updates.get("proxy_subscription_url") or ""):
                updates["proxy_source"] = "subscription"
            elif str(updates.get("proxy_account_host") or ""):
                updates["proxy_source"] = "account"
            elif str(updates.get("proxy_api_url") or ""):
                updates["proxy_source"] = "api"
        if not updates:
            raise ValueError("proxy configuration is required")
        selected_source = str(updates.get("proxy_source", current.proxy_source))
        if selected_source == "account":
            account_values = {
                "host": updates.get("proxy_account_host", current.proxy_account_host),
                "port": updates.get("proxy_account_port", current.proxy_account_port),
                "username": updates.get("proxy_account_username", current.proxy_account_username),
                "password": updates.get("proxy_account_password", current.proxy_account_password),
            }
            account_fields_submitted = any(key in payload for key in ("proxy_account_host", "proxy_account_port", "proxy_account_username", "proxy_account_password"))
            if account_fields_submitted and not all(account_values.values()):
                raise ValueError("authenticated proxy host, port, username and password are required")
        if selected_source == "subscription" and not str(updates.get("proxy_subscription_url", current.proxy_subscription_url)):
            raise ValueError("proxy subscription is not configured")
        if selected_source == "api" and not str(updates.get("proxy_api_url", current.proxy_api_url)):
            raise ValueError("proxy api is not configured")
        platform_sources = updates.get("platform_proxy_sources", current.platform_proxy_sources)
        configured_sources = {
            "subscription": bool(str(updates.get("proxy_subscription_url", current.proxy_subscription_url))),
            "account": bool(list_account_proxies(current)["proxies"]),
            "api": bool(str(updates.get("proxy_api_url", current.proxy_api_url))),
            "direct": True,
        }
        for platform, source in platform_sources.items():
            if not configured_sources.get(str(source), False):
                raise ValueError(f"{PLATFORM_LABELS.get(platform, platform)} proxy source is not configured")
        update_config(updates)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    settings = load_settings()
    return {"ok": True, **_proxy_config_payload(settings)}


@app.post("/config/registration-email", dependencies=[Depends(require_admin)])
async def update_registration_email_config(request: Request):
    payload = await _request_payload(request)
    try:
        current = load_settings()
        enabled = str(payload.get("enabled", True)).lower() in {"1", "true", "yes", "on"}
        raw_domains = payload.get("domains", list(current.registration_email_domains))
        domains = normalize_domains(raw_domains) if raw_domains else list(current.registration_email_domains)
        port = int(payload.get("smtp_port") or 465)
        ttl = int(payload.get("code_ttl_minutes") or 10)
        host = str(payload.get("smtp_host") or "smtp.qq.com").strip().lower()
        username = str(payload.get("smtp_username") or current.registration_smtp_username).strip().lower()
        if enabled and host != "smtp.qq.com":
            raise ValueError("当前仅支持 QQ 邮箱 SMTP 接口 smtp.qq.com")
        if enabled and port != 465:
            raise ValueError("QQ 邮箱 SSL SMTP 端口必须为 465")
        if enabled and username and not username.endswith("@qq.com"):
            raise ValueError("SMTP 发件账号必须是 QQ 邮箱")
        if enabled and (ttl < 3 or ttl > 30):
            raise ValueError("验证码有效期需为3-30分钟")
        updates = {
            "registration_email_verification_enabled": enabled,
            "registration_email_domains": domains,
            "registration_smtp_host": host,
            "registration_smtp_port": port,
            "registration_smtp_username": username,
            "registration_email_sender_name": str(payload.get("sender_name") or "视频生成服务").strip()[:80],
            "registration_email_code_ttl_minutes": ttl,
        }
        authorization_code = str(payload.get("authorization_code") or "").strip()
        if authorization_code:
            updates["registration_smtp_authorization_code"] = authorization_code
        update_config(updates)
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return await registration_email_config()


@app.post("/config/registration-security", dependencies=[Depends(require_admin)])
async def update_registration_security_config(request: Request):
    payload = await _request_payload(request)
    enabled = str(payload.get("enabled", False)).strip().lower() in {"1", "true", "yes", "on"}
    update_config({"registration_abuse_detection_enabled": enabled})
    await _record_admin_action_safe(
        "registration_security_setting",
        "异常注册检测设置",
        detail="已开启" if enabled else "已关闭",
        actor=load_settings().admin_username,
        ip_address=_request_client_key(request),
    )
    return {"ok": True, "enabled": enabled}


@app.get("/temp-tokens", dependencies=[Depends(require_admin)])
async def temp_tokens_list():
    return {"tokens": list_temp_tokens()}


@app.post("/temp-tokens", dependencies=[Depends(require_admin)])
async def temp_tokens_create(request: Request):
    payload = await _request_payload(request)
    try:
        count = int(payload.get("count") or payload.get("num") or 1)
        limit = int(payload.get("limit") or 100)
        concurrency = int(payload.get("concurrency") or 1)
        task_retention_days = int(payload.get("task_retention_days") or 7)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="count, limit, concurrency and task_retention_days must be integers")
    if concurrency < 1 or concurrency > 100:
        raise HTTPException(status_code=400, detail="concurrency must be between 1 and 100")
    if task_retention_days < 1 or task_retention_days > 15:
        raise HTTPException(status_code=400, detail="task_retention_days must be between 1 and 15")
    return {"tokens": create_temp_tokens(count, limit, concurrency, str(payload.get("remark") or payload.get("note") or ""), task_retention_days)}


@app.patch("/temp-tokens/{token_id}", dependencies=[Depends(require_admin)])
@app.put("/temp-tokens/{token_id}", dependencies=[Depends(require_admin)])
async def temp_tokens_update(token_id: str, request: Request):
    payload = await _request_payload(request)
    if "limit" not in payload and "concurrency" not in payload and "remark" not in payload and "note" not in payload and "task_retention_days" not in payload:
        raise HTTPException(status_code=400, detail="limit, concurrency, task_retention_days or remark is required")
    try:
        requested_concurrency = int(payload["concurrency"]) if "concurrency" in payload else None
        token = update_temp_token(
            token_id,
            limit=int(payload["limit"]) if "limit" in payload else None,
            concurrency=None,
            task_retention_days=int(payload["task_retention_days"]) if "task_retention_days" in payload else None,
            remark=str(payload.get("remark") if "remark" in payload else payload.get("note")) if "remark" in payload or "note" in payload else None,
        )
        if requested_concurrency is not None:
            effective = set_user_concurrency_by_token_hash(token_id, requested_concurrency)
            token = update_temp_token(token_id, concurrency=requested_concurrency) if effective is None else next(item for item in list_temp_tokens() if item["id"] == token_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="token not found")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="limit, concurrency and task_retention_days must be integers")
    return {"ok": True, "token": token}


@app.delete("/temp-tokens/{token_id}", dependencies=[Depends(require_admin)])
async def temp_tokens_delete(token_id: str):
    if not delete_temp_token(token_id):
        raise HTTPException(status_code=404, detail="token not found")
    return {"ok": True}


@app.post("/temp-tokens/{token_id}/delete", dependencies=[Depends(require_admin)])
async def temp_tokens_delete_action(token_id: str):
    if not delete_temp_token(token_id):
        raise HTTPException(status_code=404, detail="token not found")
    return {"ok": True}


@app.post("/batch-prompts/references", dependencies=[Depends(require_temp)])
async def create_batch_references(
    request: Request,
    access: Annotated[AccessContext, Depends(require_temp)],
    batch_id: Annotated[str, Form()],
    images: Annotated[list[UploadFile] | None, File(alias="images")] = None,
):
    normalized_batch_id = str(batch_id or "").strip()[:100]
    uploads = [item for item in (images or []) if item and item.filename]
    if not normalized_batch_id:
        raise HTTPException(status_code=400, detail="批次 ID 不能为空")
    if not uploads:
        raise HTTPException(status_code=400, detail="请选择共用参考图")
    if len(uploads) > load_settings().max_image_count:
        raise HTTPException(status_code=400, detail="too many images")
    await _rate_limit(request, "batch-reference-upload", 30, 60, access.token_hash)
    metadata = await asyncio.to_thread(_save_batch_reference_bundle, access.token_hash, normalized_batch_id, uploads)
    return {"ok": True, "reference_id": metadata["id"], "image_count": len(metadata["images"])}


@app.delete("/batch-prompts/references/{reference_id}", dependencies=[Depends(require_temp)])
async def delete_batch_references(reference_id: str, access: Annotated[AccessContext, Depends(require_temp)]):
    deleted = await asyncio.to_thread(_delete_batch_reference_bundle, reference_id, access.token_hash)
    return {"ok": True, "deleted": deleted}


@app.post("/batch-prompts/job-assets", dependencies=[Depends(require_temp)])
async def upload_batch_job_assets(
    request: Request,
    access: Annotated[AccessContext, Depends(require_temp)],
    batch_id: Annotated[str, Form()],
    manifest: Annotated[str, Form()],
    upload_id: Annotated[str, Form()] = "",
    images: Annotated[list[UploadFile] | None, File(alias="images")] = None,
):
    normalized_batch_id = str(batch_id or "").strip()[:100]
    if not normalized_batch_id:
        raise HTTPException(status_code=400, detail="批次 ID 不能为空")
    _ensure_batch_active(access, normalized_batch_id)
    try:
        entries = json.loads(str(manifest or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="批量参考图分片参数无效") from exc
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise HTTPException(status_code=400, detail="批量参考图分片参数无效")
    uploads = [item for item in (images or []) if item and item.filename]
    await _rate_limit(request, "batch-job-asset-upload", 240, 60, access.token_hash)
    result = await asyncio.to_thread(
        _save_batch_asset_chunk,
        access.token_hash,
        normalized_batch_id,
        str(upload_id or ""),
        entries,
        uploads,
    )
    return {
        "ok": True,
        "upload_id": result["id"],
        "uploaded_count": result["uploaded_count"],
        "total_bytes": result["total_bytes"],
    }


@app.post("/batch-prompts/jobs", dependencies=[Depends(require_temp)])
async def create_persistent_batch_job(
    request: Request,
    access: Annotated[AccessContext, Depends(require_temp)],
    manifest: Annotated[str, Form()],
    images: Annotated[list[UploadFile] | None, File(alias="images")] = None,
):
    await _rate_limit(request, "batch-job-create", 10, 60, access.token_hash)
    try:
        payload = json.loads(str(manifest or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="批量生成设置无效") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise HTTPException(status_code=400, detail="批量生成设置无效")
    raw_rows = payload["rows"]
    if not raw_rows or len(raw_rows) > 2000:
        raise HTTPException(status_code=400, detail="每个批次需包含 1 至 2000 条提示词")
    ratio = str(payload.get("ratio") or DEFAULT_RATIO).strip()
    if ratio not in VALID_RATIOS:
        raise HTTPException(status_code=400, detail="invalid ratio")
    platform, model = validate_task_platform_model(
        str(payload.get("platform") or "dola"),
        str(payload.get("model") or ""),
    )
    duration = validate_task_duration(platform, model, payload.get("duration"))
    try:
        concurrency = max(1, min(int(access.concurrency or 1), int(payload.get("concurrency") or access.concurrency or 1)))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="生成并发无效") from exc
    reference_id = str(payload.get("reference_id") or "").strip()
    asset_upload_id = str(payload.get("asset_upload_id") or "").strip().lower()
    reference_count = max(0, min(load_settings().max_image_count, int(payload.get("reference_count") or 0)))
    reference_batch_id = str(payload.get("reference_batch_id") or "").strip()[:100]
    reference_is_real_person = payload.get("reference_is_real_person") is True
    _ensure_batch_active(access, reference_batch_id)
    if reference_id:
        shared_paths = await asyncio.to_thread(_batch_reference_paths, reference_id, access.token_hash, reference_batch_id)
        if reference_count <= 0 or len(shared_paths) < reference_count:
            raise HTTPException(status_code=400, detail="批量共用参考图数量无效，请重新上传")
    elif reference_count:
        raise HTTPException(status_code=400, detail="批量共用参考图参数无效")

    normalized_rows: list[dict[str, object]] = []
    expected_images = 0
    for index, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="批量提示词格式无效")
        prompt = repair_text(str(raw.get("prompt") or "").strip())
        if not prompt:
            raise HTTPException(status_code=400, detail=f"第 {index} 条提示词为空")
        image_count = max(0, int(raw.get("image_count") or 0))
        if reference_count + image_count > load_settings().max_image_count:
            raise HTTPException(status_code=400, detail=f"第 {index} 条任务最多添加 {load_settings().max_image_count} 张参考图")
        expected_images += image_count
        normalized_rows.append({
            "client_index": max(0, int(raw.get("client_index") or index - 1)),
            "sheet_row": max(1, int(raw.get("sheet_row") or index)),
            "prompt": prompt,
            "image_count": image_count,
            "image_files": [],
            "image_names": [],
        })
    uploads = [item for item in (images or []) if item and item.filename]
    if asset_upload_id and uploads:
        raise HTTPException(status_code=400, detail="批量参考图不能同时使用分片和直接上传")
    if not asset_upload_id and len(uploads) != expected_images:
        raise HTTPException(status_code=400, detail="批量任务参考图上传不完整，请重新提交")

    job_id = secrets.token_hex(16)
    assets_root = _batch_job_assets_path(job_id)
    assets_consumed = False
    upload_cursor = 0
    try:
        if asset_upload_id:
            await asyncio.to_thread(
                _consume_batch_asset_upload,
                asset_upload_id,
                access.token_hash,
                reference_batch_id,
                normalized_rows,
                assets_root,
            )
            assets_consumed = True
        else:
            assets_root.mkdir(parents=True, exist_ok=False)
            for row_index, row in enumerate(normalized_rows, start=1):
                _ensure_batch_active(access, reference_batch_id)
                saved_names: list[str] = []
                original_names: list[str] = []
                for image_index in range(int(row["image_count"])):
                    upload = uploads[upload_cursor]
                    upload_cursor += 1
                    suffix = Path(upload.filename or "").suffix.lower()
                    if suffix not in IMAGE_MAGIC:
                        raise HTTPException(status_code=400, detail="unsupported image type")
                    filename = f"{row_index:06d}-{image_index + 1:02d}{suffix}"
                    await asyncio.to_thread(_save_uploaded_image, upload, assets_root / filename)
                    saved_names.append(filename)
                    original_names.append(_reference_image_name(upload.filename, image_index + 1, suffix))
                row["image_files"] = saved_names
                row["image_names"] = original_names
        _ensure_batch_active(access, reference_batch_id)
        job = await _storage_call(
            create_batch_job,
            access.token_hash,
            normalized_rows,
            ratio=ratio,
            concurrency=concurrency,
            platform=platform,
            model=model,
            duration=duration,
            reference_id=reference_id,
            reference_count=reference_count,
            reference_batch_id=reference_batch_id,
            reference_is_real_person=reference_is_real_person,
            job_id=job_id,
        )
        if reference_batch_id and _batch_is_canceled(access, reference_batch_id):
            job = await asyncio.to_thread(cancel_persistent_batch_job, job_id, access.token_hash)
    except Exception:
        if assets_consumed:
            await asyncio.to_thread(_restore_batch_asset_upload, asset_upload_id, assets_root)
        else:
            shutil.rmtree(assets_root, ignore_errors=True)
        raise
    return JSONResponse(status_code=201, content={"job": public_batch_job(job)})


@app.get("/batch-prompts/jobs/current", dependencies=[Depends(require_temp)])
async def current_persistent_batch_job(access: Annotated[AccessContext, Depends(require_temp)]):
    jobs = await asyncio.to_thread(list_batch_jobs, access.token_hash, active_only=True, limit=1)
    return {"job": public_batch_job(jobs[0]) if jobs else None}


@app.get("/batch-prompts/jobs/{job_id}", dependencies=[Depends(require_temp)])
async def persistent_batch_job_status(
    job_id: str,
    access: Annotated[AccessContext, Depends(require_temp)],
    since_revision: int | None = Query(None, ge=0),
):
    job = await asyncio.to_thread(get_batch_job, job_id, access.token_hash)
    if not job:
        raise HTTPException(status_code=404, detail="批次不存在")
    task_ids = [
        str(row.get("task_id") or "")
        for row in job.get("rows", [])
        if isinstance(row, dict)
        and str(row.get("task_id") or "")
        and str(row.get("status") or "") not in {"completed", "failed", "canceled"}
    ]
    if task_ids:
        states = await asyncio.to_thread(task_states, task_ids, access.token_hash)
        payloads = {
            task_id: {
                "status": str(meta.get("status") or ""),
                "error": _client_safe_text(
                    str(meta.get("error") or ""),
                    str(meta.get("model") or "当前模型"),
                    terminal=str(meta.get("status") or "") in {"failed", "canceled"},
                ),
                "video_url": _task_video_url(meta, result),
            }
            for task_id, meta, result in states
        }
        job = await asyncio.to_thread(reconcile_batch_job, job_id, payloads)
    return {"job": public_batch_job(job, since_revision=since_revision)}


@app.post("/batch-prompts/jobs/{job_id}/retry", dependencies=[Depends(require_temp)])
async def retry_failed_persistent_batch_rows(
    job_id: str,
    payload: BatchRetryRequest,
    request: Request,
    access: Annotated[AccessContext, Depends(require_temp)],
):
    await _rate_limit(request, "batch-failed-retry", 5, 60, access.token_hash)
    job = await asyncio.to_thread(get_batch_job, job_id, access.token_hash)
    if not job:
        raise HTTPException(status_code=404, detail="batch job not found")
    rows = [dict(row) for row in job.get("rows", []) if isinstance(row, dict)]
    selected_indices = {int(index) for index in payload.row_indices if int(index) > 0}
    if not payload.retry_all and not selected_indices:
        raise HTTPException(status_code=400, detail="select failed batch tasks to retry")
    candidates = [
        row for row in rows
        if str(row.get("status") or "") == "failed"
        and (payload.retry_all or int(row.get("index") or 0) in selected_indices)
    ]
    if not candidates:
        raise HTTPException(status_code=409, detail="no failed batch tasks are available for retry")
    if len(candidates) > 1000:
        raise HTTPException(status_code=400, detail="retry at most 1000 tasks at once")

    release_started_at = datetime.now(timezone.utc)
    release_interval_seconds = max(1.0, min(5.0, float(load_settings().dola_submit_interval_seconds)))
    created: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []
    for release_index, row in enumerate(candidates):
        task_id = str(row.get("task_id") or "")
        try:
            validate_task_id(task_id)
            original = await asyncio.to_thread(get_meta, task_id)
            if str(original.get("owner_token_hash") or "") != access.token_hash:
                raise HTTPException(status_code=404, detail="task not found")
            if str(original.get("status") or "") != "failed":
                skipped.append({"row_index": int(row.get("index") or 0), "reason": "only failed tasks can be retried"})
                continue
            available_at = release_started_at + timedelta(seconds=release_index * release_interval_seconds)
            retry_result = await _create_manual_retry_task(task_id, original, access, available_at=available_at)
            created.append({"source": row, "retry_id": retry_result["id"], "available_at": available_at.isoformat()})
        except (ValueError, FileNotFoundError):
            failed.append({"row_index": int(row.get("index") or 0), "reason": "task not found"})
        except HTTPException as exc:
            failed.append({"row_index": int(row.get("index") or 0), "reason": str(exc.detail)})
        except Exception as exc:
            logger.exception("batch retry failed for job %s row %s", job_id, row.get("index"))
            failed.append({"row_index": int(row.get("index") or 0), "reason": f"{type(exc).__name__}: {exc}"})
    if not created:
        raise HTTPException(status_code=409, detail="no selected batch tasks could be retried")
    retry_job = await asyncio.to_thread(create_batch_retry_job, access.token_hash, job, created)
    return {
        "ok": not failed,
        "job": public_batch_job(retry_job),
        "requested": len(candidates),
        "created": len(created),
        "skipped": skipped,
        "failed": failed,
        "release_interval_seconds": release_interval_seconds,
    }


@app.post("/batch-prompts/{batch_id}/cancel", dependencies=[Depends(require_token)])
async def cancel_batch_prompt_submission(
    batch_id: str,
    access: Annotated[AccessContext, Depends(require_token)],
):
    normalized = str(batch_id or "").strip()[:100]
    if not normalized:
        raise HTTPException(status_code=400, detail="batch id is required")
    if access.is_temp:
        existing = await asyncio.to_thread(get_batch_job, normalized, access.token_hash)
        if existing:
            canceled = await asyncio.to_thread(cancel_persistent_batch_job, normalized, access.token_hash)
            return {"ok": True, "batch_id": normalized, "job": public_batch_job(canceled)}
    _set_batch_canceled(access, normalized)
    if access.is_temp:
        await asyncio.to_thread(_delete_batch_asset_uploads, access.token_hash, normalized)
    return {"ok": True, "batch_id": normalized}


@app.post("/tasks", dependencies=[Depends(require_token)])
async def submit_task(
    request: Request,
    access: Annotated[AccessContext, Depends(require_token)],
    prompt: Annotated[str, Form()],
    ratio: Annotated[str, Form()] = DEFAULT_RATIO,
    platform: Annotated[str, Form()] = DEFAULT_PLATFORM,
    model: Annotated[str, Form()] = "",
    task_type: Annotated[str, Form()] = "video",
    duration: Annotated[int | None, Form()] = None,
    batch: Annotated[bool, Form()] = False,
    batch_id: Annotated[str, Form()] = "",
    batch_index: Annotated[int, Form()] = 0,
    batch_row: Annotated[int, Form()] = 0,
    batch_reference_task_id: Annotated[str, Form()] = "",
    batch_reference_id: Annotated[str, Form()] = "",
    batch_reference_image_count: Annotated[int, Form()] = 0,
    reference_is_real_person: Annotated[bool, Form()] = False,
    preferred_account_id: Annotated[str, Form()] = "",
    images: Annotated[list[UploadFile] | None, File(alias="images")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    assert create_sem is not None
    async with _owner_create_semaphore(access), create_sem:
        await asyncio.to_thread(_admit_task_creation)
        prompt = repair_text((prompt or "").strip())
        ratio = (ratio or DEFAULT_RATIO).strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        if ratio not in VALID_RATIOS:
            raise HTTPException(status_code=400, detail="invalid ratio")
        batch_id = str(batch_id or "").strip()[:100] if batch else ""
        batch_index = max(0, min(1000, int(batch_index or 0))) if batch else 0
        batch_row = max(0, min(1_000_000, int(batch_row or 0))) if batch else 0
        batch_reference_task_id = str(batch_reference_task_id or "").strip() if batch else ""
        batch_reference_id = str(batch_reference_id or "").strip() if batch else ""
        batch_reference_image_count = max(0, min(load_settings().max_image_count, int(batch_reference_image_count or 0))) if batch else 0
        _ensure_batch_active(access, batch_id)
        platform, model = validate_task_platform_model(platform, model)
        duration = validate_task_duration(platform, model, duration)
        preferred_account_id = str(preferred_account_id or "").strip().lower()[:64]
        if preferred_account_id:
            if not access.is_admin:
                raise HTTPException(status_code=403, detail="only administrators can select a generation account")
            selectable_accounts = await asyncio.to_thread(_available_generation_accounts, platform, duration)
            if not any(str(item.get("id") or "") == preferred_account_id for item in selectable_accounts):
                raise HTTPException(status_code=409, detail="selected account is no longer available")
        if platform == "qianwen" and task_type != "video":
            raise HTTPException(status_code=400, detail="千问当前仅支持视频任务")
        uploads = [item for item in (images or []) if item and item.filename]
        if len(uploads) + batch_reference_image_count > load_settings().max_image_count:
            raise HTTPException(status_code=400, detail="too many images")
        shared_reference_paths: list[Path] = []
        shared_reference_names: list[str] = []
        if batch_reference_id:
            shared_reference_paths, shared_reference_names = await asyncio.to_thread(
                _batch_reference_bundle,
                batch_reference_id,
                access.token_hash,
                batch_id,
            )
            if batch_reference_image_count <= 0 or len(shared_reference_paths) < batch_reference_image_count:
                raise HTTPException(status_code=400, detail="批量共用参考图数量无效，请重新上传")
            shared_reference_paths = shared_reference_paths[:batch_reference_image_count]
            shared_reference_names = shared_reference_names[:batch_reference_image_count]
        elif batch_reference_task_id:
            try:
                validate_task_id(batch_reference_task_id)
                reference_meta = await asyncio.to_thread(get_meta, batch_reference_task_id)
                reference_paths = await asyncio.to_thread(task_image_paths, batch_reference_task_id)
            except (ValueError, FileNotFoundError):
                raise HTTPException(status_code=400, detail="批量共用参考图已失效，请重新提交")
            if (
                not access.is_temp
                or str(reference_meta.get("owner_token_hash") or "") != access.token_hash
                or str(reference_meta.get("batch_id") or "") != batch_id
                or batch_reference_image_count <= 0
                or len(reference_paths) < batch_reference_image_count
            ):
                raise HTTPException(status_code=400, detail="批量共用参考图不可用，请重新提交")
            shared_reference_paths = reference_paths[:batch_reference_image_count]
            raw_reference_names = reference_meta.get("reference_image_names") if isinstance(reference_meta.get("reference_image_names"), list) else []
            shared_reference_names = [
                _reference_image_name(raw_reference_names[index] if index < len(raw_reference_names) else "", index + 1, path.suffix)
                for index, path in enumerate(shared_reference_paths)
            ]
        elif batch_reference_image_count:
            raise HTTPException(status_code=400, detail="批量共用参考图参数无效")
        await _rate_limit(request, "task-create-batch" if batch else "task-create", 2400 if batch else 30, 60, access.token_hash)
        key = _idempotency_key(idempotency_key)
        fingerprint = _request_fingerprint("tasks", access.token_hash, {"prompt": prompt, "ratio": ratio, "duration": duration or 0, "platform": platform, "model": model, "task_type": task_type, "batch_id": batch_id, "batch_index": batch_index, "batch_row": batch_row, "batch_reference_id": batch_reference_id, "batch_reference_task_id": batch_reference_task_id, "batch_reference_image_count": batch_reference_image_count, "reference_is_real_person": reference_is_real_person, "preferred_account_id": preferred_account_id, "images": [Path(item.filename or "").name for item in uploads]})

        try:
            if key:
                meta, created = await _storage_call(
                    find_or_create_task,
                    prompt,
                    ratio,
                    access.token_hash if access.is_temp else "",
                    platform,
                    model,
                    task_type,
                    key,
                    fingerprint,
                    "tasks",
                    duration,
                    batch_id,
                    batch_index,
                    batch_row,
                )
            else:
                meta = await asyncio.to_thread(
                    create_task,
                    prompt,
                    ratio,
                    owner_token_hash=access.token_hash if access.is_temp else "",
                    platform=platform,
                    model=model,
                    task_type=task_type,
                    enqueue=False,
                    duration=duration,
                    batch_id=batch_id,
                    batch_index=batch_index,
                    batch_row=batch_row,
                )
                created = True
            resumed_initializing = not created and str(meta.get("status") or "") == "initializing"
            if not created and not resumed_initializing:
                return {"id": meta["id"], "replayed": True, "image_count": int(meta.get("image_count") or 0)}
            queued_for_concurrency = False
            if access.is_temp:
                queued_for_concurrency = await _storage_call(active_task_count_for_owner, access.token_hash) >= access.concurrency
            base_cost_units = model_cost_units(platform, model, task_type, duration)
            discount_units = await _storage_call(task_discount_units_by_token_hash, access.token_hash, platform, model) if access.is_temp else 0
            cost_units = max(1, base_cost_units - discount_units)
            user_id = await _storage_call(_transaction_user_id, access)
            reserved_access = await _storage_call(reserve_temp_quota, access, str(meta["id"]), cost_units, user_id=user_id)
            _ensure_batch_active(access, batch_id)
            reservation = await _storage_call(get_temp_reservation, access.token_hash, str(meta["id"])) if access.is_temp else {}
            charged_units = int(reservation.get("units") or 0)
            if user_id and reservation:
                free_used = bool(reservation.get("free"))
                await _storage_call(
                    record_transaction,
                    user_id,
                    "video_quota_consume" if free_used else "consume",
                    0 if free_used else -charged_units,
                    "视频额度任务消费" if free_used else "视频任务消费",
                    balance_units=reserved_access.credit_units,
                    video_quota_change=-1 if free_used else 0,
                    video_quota_balance=reserved_access.free_remaining,
                    reference_id=str(meta["id"]),
                    detail=f"任务 ID：{meta['id']}\n{PLATFORM_LABELS.get(platform, platform)} / {model}",
                    transaction_id=f"task-{str(meta['id'])[:27]}",
                )
        except HTTPException:
            if "meta" in locals():
                await _storage_call(refund_temp_quota_hash, access.token_hash, str(meta["id"]), attempts=2)
                await _storage_call(delete_task, str(meta["id"]), attempts=2)
            raise
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except QuotaExceeded as exc:
            if "meta" in locals():
                await asyncio.to_thread(delete_task, str(meta["id"]))
            raise HTTPException(status_code=429, detail=str(exc))
        except Exception as exc:
            if "meta" in locals():
                await asyncio.to_thread(refund_temp_quota_hash, access.token_hash, str(meta["id"]))
                await asyncio.to_thread(delete_task, str(meta["id"]))
            logger.exception(
                "task creation failed before upload (task_id=%s batch=%s batch_id=%s batch_index=%s error_type=%s)",
                str(locals().get("meta", {}).get("id") or ""),
                batch,
                batch_id,
                batch_index,
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail="任务创建暂时繁忙，请稍后重试", headers={"Retry-After": "2"}) from exc
        saved_paths: list[Path] = []
        saved_reference_names: list[str] = []
        try:
            for index, source in enumerate(shared_reference_paths, start=1):
                target = images_dir(meta["id"]) / f"{index:02d}{source.suffix.lower()}"
                await asyncio.to_thread(shutil.copy2, source, target)
                saved_paths.append(target)
                saved_reference_names.append(
                    _reference_image_name(
                        shared_reference_names[index - 1] if index <= len(shared_reference_names) else "",
                        index,
                        source.suffix,
                    )
                )
            for index, upload in enumerate(uploads, start=len(saved_paths) + 1):
                filename = Path(upload.filename or f"image_{index}.png").name
                suffix = Path(filename).suffix.lower() or ".png"
                target = images_dir(meta["id"]) / f"{index:02d}{suffix}"
                await asyncio.to_thread(_save_uploaded_image, upload, target)
                saved_paths.append(target)
                saved_reference_names.append(_reference_image_name(upload.filename, index, suffix))
            await _storage_call(set_task_images, meta["id"], saved_paths, saved_reference_names)
            await _storage_call(
                update_meta,
                str(meta["id"]),
                reference_is_real_person=bool(reference_is_real_person),
                preferred_account_id=preferred_account_id,
            )
            _ensure_batch_active(access, batch_id)
            await _storage_call(finalize_task_creation, str(meta["id"]))
        except HTTPException:
            if reserved_access:
                await asyncio.to_thread(refund_temp_quota_hash, reserved_access.token_hash, str(meta["id"]))
            await asyncio.to_thread(delete_task, meta["id"])
            raise
        except Exception as exc:
            if reserved_access:
                await asyncio.to_thread(refund_temp_quota_hash, reserved_access.token_hash, str(meta["id"]))
            await asyncio.to_thread(delete_task, meta["id"])
            logger.exception(
                "task creation failed while saving uploads (task_id=%s batch=%s batch_id=%s batch_index=%s error_type=%s)",
                str(meta.get("id") or ""),
                batch,
                batch_id,
                batch_index,
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail="任务创建暂时繁忙，请稍后重试", headers={"Retry-After": "2"}) from exc
        if user_id:
            await _record_activity_safe(
                user_id,
                "task_submit",
                "提交视频生成任务",
                reference_id=str(meta["id"]),
                detail=f"{model} / {ratio}{' / 多任务第 ' + str(batch_index) + ' 条' if batch else ''}",
            )
        response = {"id": meta["id"], "queued_for_concurrency": queued_for_concurrency, "image_count": len(saved_paths), "preferred_account_id": preferred_account_id}
        if resumed_initializing:
            response["replayed"] = True
        if reserved_access and reserved_access.is_temp:
            try:
                balance = await _storage_call(user_balance_by_token_hash, reserved_access.token_hash)
            except Exception:
                logger.exception("task created but balance refresh failed (task_id=%s)", str(meta["id"]))
                balance = {
                    "free_remaining": reserved_access.free_remaining,
                    "points": units_to_points(reserved_access.credit_units),
                }
            response["quota"] = {
                "limit": reserved_access.limit,
                "used": reserved_access.used,
                "remaining": reserved_access.remaining,
                **balance,
            }
            response["token_concurrency"] = reserved_access.concurrency
            response["billing"] = {
                "free_used": bool(reservation.get("free")),
                "points_used": units_to_points(int(reservation.get("units") or 0)),
            }
        return response


@app.post("/batch-prompts/parse")
async def parse_batch_prompts(
    request: Request,
    access: Annotated[AccessContext, Depends(require_temp)],
    spreadsheet: Annotated[UploadFile, File()],
):
    await _rate_limit(request, "batch-prompt-parse", 10, 60, access.token_hash)
    filename = Path(spreadsheet.filename or "").name
    try:
        data = await spreadsheet.read(MAX_SPREADSHEET_BYTES + 1)
    finally:
        await spreadsheet.close()
    try:
        prompts = await asyncio.to_thread(parse_spreadsheet, filename, data)
    except SpreadsheetImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "filename": filename,
        "count": len(prompts),
        "prompts": prompts,
        "supported_extensions": sorted(SUPPORTED_SPREADSHEET_SUFFIXES),
    }


@app.post("/batch-prompts/status")
async def batch_prompt_status(
    body: dict,
    access: Annotated[AccessContext, Depends(require_temp)],
):
    raw_ids = body.get("task_ids") if isinstance(body, dict) else []
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="task_ids must be a list")
    task_ids = list(dict.fromkeys(str(task_id or "").strip() for task_id in raw_ids if str(task_id or "").strip()))
    if len(task_ids) > 2000:
        raise HTTPException(status_code=400, detail="一次最多刷新 2000 条任务")
    invalid = [task_id for task_id in task_ids if len(task_id) != 32]
    if invalid:
        raise HTTPException(status_code=400, detail="invalid task id")

    rows = await asyncio.to_thread(task_states, task_ids, access.token_hash)
    states = []
    for task_id, meta, result in rows:
        status = str(meta.get("status") or "")
        url = _task_video_url(meta, result)
        if url:
            code = "2"
            text = "视频生成成功"
        elif status in {"failed", "canceled"}:
            code = "0"
            text = _client_safe_text(
                str(meta.get("error") or ("用户取消生成" if status == "canceled" else "生成失败")),
                str(meta.get("model") or "当前模型"),
                terminal=True,
            )
        elif status == "pending" and (
            int(meta.get("retry_count") or 0) > 0 or int(meta.get("infrastructure_retry_count") or 0) > 0
        ):
            code = "1"
            text = "服务繁忙正在重试！" if _task_has_service_frequent(meta) else "正在重试中，请稍等！"
        else:
            code = "1"
            text = _client_safe_text(str(meta.get("status_reason") or meta.get("queue_reason") or meta.get("error") or ""), str(meta.get("model") or "当前模型"))
        states.append({"id": task_id, "status": status, "code": code, "text": text, "url": url})
    return {"tasks": states}


@app.get("/tasks", dependencies=[Depends(require_token)])
async def all_tasks(
    access: Annotated[AccessContext, Depends(require_token)],
    page: int | None = Query(None, ge=1),
    page_size: int | None = Query(None, ge=1, le=100),
    q: str | None = Query(None, max_length=200),
    status: str | None = Query(None),
    platform: str | None = Query(None),
):
    assert list_sem is not None
    async with list_sem:
        owner = access.token_hash if access.is_temp else None
        if page is None and page_size is None and q is None and status is None and platform is None:
            tasks = await asyncio.to_thread(
                lambda: list_tasks(owner_token_hash=owner, owner_remarks=temp_token_remarks())
            )
            if access.is_temp:
                tasks = [item for item in tasks if not item.get("task_hidden_for_client")]
                tasks = [_client_task(item) for item in tasks]
            else:
                tasks = [item for item in tasks if not item.get("task_hidden_for_admin")]
            return {"tasks": tasks}
        effective_page_size = page_size or 50
        result = await asyncio.to_thread(
            lambda: list_tasks_page(
                owner_token_hash=owner,
                owner_remarks=temp_token_remarks(),
                audience="client" if access.is_temp else "admin",
                page=page or 1,
                page_size=effective_page_size,
                keyword=str(q or "").strip(),
                status=str(status or "").strip().lower(),
                platform=str(platform or "").strip().lower(),
            )
        )
        tasks = result["items"]
        if access.is_temp:
            tasks = [_client_task(item) for item in tasks]
        return {
            "tasks": tasks,
            "total": result["total"],
            "page": result["page"],
            "page_size": result["page_size"],
            "total_pages": result["total_pages"],
            "stats": _frontend_task_stats(result["stats"], client=access.is_temp),
        }


@app.delete("/tasks", dependencies=[Depends(require_token)])
async def clear_tasks(access: Annotated[AccessContext, Depends(require_token)]):
    assert delete_sem is not None
    async with delete_sem:
        owner = access.token_hash if access.is_temp else None
        audience = "client" if access.is_temp else "admin"
        hidden = 0
        skipped: list[str] = []
        active = active_task_ids()
        for item in list_tasks(owner_token_hash=owner):
            task_id = str(item.get("id") or "")
            status = str(item.get("status") or "")
            if task_id in active or status in {"pending", "running", "submitted"} or (status == "success" and not task_has_video(task_id)):
                skipped.append(task_id)
                continue
            set_task_hidden(task_id, audience, True)
            hidden += 1
        return {"ok": True, "deleted": hidden, "hidden": hidden, "skipped": skipped}


@app.delete("/tasks-failed", dependencies=[Depends(require_token)])
async def clear_failed_tasks(access: Annotated[AccessContext, Depends(require_token)]):
    assert delete_sem is not None
    async with delete_sem:
        owner = access.token_hash if access.is_temp else None
        hidden = 0
        audience = "client" if access.is_temp else "admin"
        removable_statuses = {"failed", "canceled"}
        for item in list_tasks(owner_token_hash=owner):
            task_id = str(item.get("id") or "")
            if str(item.get("status") or "") not in removable_statuses:
                continue
            set_task_hidden(task_id, audience, True)
            hidden += 1
        return {"ok": True, "deleted": hidden, "hidden": hidden}


@app.get("/tasks/{task_id}", dependencies=[Depends(require_token)])
async def task_result(access: Annotated[AccessContext, Depends(require_token)], task_id: str):
    assert query_sem is not None
    async with query_sem:
        try:
            validate_task_id(task_id)
            meta = get_meta(task_id)
        except (ValueError, FileNotFoundError):
            raise HTTPException(status_code=404, detail="task not found")
        if access.is_temp and str(meta.get("owner_token_hash") or "") != access.token_hash:
            raise HTTPException(status_code=404, detail="task not found")
        audience = "client" if access.is_temp else "admin"
        if bool(meta.get(f"task_hidden_for_{audience}", False)):
            raise HTTPException(status_code=404, detail="task not found")
        result = await query_task(task_id)
        if access.is_temp:
            result = dict(result)
            result["text"] = _client_safe_text(
                str(result.get("text") or ""),
                str(meta.get("model") or "当前模型"),
                terminal=str(result.get("code") or "") == "0",
            )
        return result


@app.get("/tasks/{task_id}/video", dependencies=[Depends(require_token)])
async def task_video(
    request: Request,
    access: Annotated[AccessContext, Depends(require_token)],
    task_id: str,
    download: bool = False,
):
    try:
        validate_task_id(task_id)
        meta = await asyncio.to_thread(get_meta, task_id)
        result = await asyncio.to_thread(load_result, task_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="task not found")
    if access.is_temp and str(meta.get("owner_token_hash") or "") != access.token_hash:
        raise HTTPException(status_code=404, detail="task not found")
    url = _validate_video_url(_task_video_url(meta, result))
    headers = {
        "Accept": "video/*,*/*;q=0.8",
        "Accept-Encoding": "identity",
        "Referer": _video_referer(str(meta.get("platform") or "dola")),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    }
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=False, trust_env=False)
    response = None

    async def close_upstream() -> None:
        try:
            if response is not None:
                await response.aclose()
        finally:
            await client.aclose()

    try:
        current_url = url
        for _ in range(6):
            response = await client.send(client.build_request("GET", current_url, headers=headers), stream=True)
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            location = response.headers.get("location", "")
            await response.aclose()
            response = None
            if not location:
                break
            current_url = _validate_video_url(urljoin(current_url, location))
        if response is None or response.status_code not in {200, 206}:
            status = response.status_code if response is not None else 502
            raise HTTPException(status_code=502, detail=f"video upstream returned HTTP {status}")
    except HTTPException:
        await close_upstream()
        raise
    except httpx.HTTPError as exc:
        await close_upstream()
        raise HTTPException(status_code=502, detail="video upstream unavailable") from exc
    except BaseException:
        await close_upstream()
        raise

    outgoing_headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in {"accept-ranges", "content-length", "content-range", "content-type", "etag", "last-modified"}
    }
    outgoing_headers.setdefault("Accept-Ranges", "bytes")
    outgoing_headers["Cache-Control"] = "private, max-age=300"
    download_name = _video_download_filename(meta, task_id)
    disposition = "attachment" if download else "inline"
    outgoing_headers["Content-Disposition"] = (
        f'{disposition}; filename="{task_id}.mp4"; filename*=UTF-8\'\'{quote(download_name, safe="")}'
    )

    async def stream_video():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await close_upstream()

    return StreamingResponse(
        stream_video(),
        status_code=response.status_code,
        media_type=response.headers.get("content-type") or "video/mp4",
        headers=outgoing_headers,
    )


@app.get("/tasks/{task_id}/references/{image_index}", dependencies=[Depends(require_token)])
async def task_reference_image(
    access: Annotated[AccessContext, Depends(require_token)],
    task_id: str,
    image_index: int,
):
    try:
        validate_task_id(task_id)
        meta = await asyncio.to_thread(get_meta, task_id)
        paths = await asyncio.to_thread(task_reference_display_paths, task_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="task not found")
    if access.is_temp and str(meta.get("owner_token_hash") or "") != access.token_hash:
        raise HTTPException(status_code=404, detail="task not found")
    audience = "client" if access.is_temp else "admin"
    if bool(meta.get(f"task_hidden_for_{audience}", False)):
        raise HTTPException(status_code=404, detail="task not found")
    if image_index < 1 or image_index > len(paths):
        raise HTTPException(status_code=404, detail="reference image not found")
    path = paths[image_index - 1]
    media_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    media_type = media_types.get(path.suffix.lower())
    if not media_type:
        raise HTTPException(status_code=404, detail="reference image not found")
    source_name = _reference_image_name(
        (meta.get("reference_image_names") or [])[image_index - 1]
        if isinstance(meta.get("reference_image_names"), list) and image_index <= len(meta.get("reference_image_names") or [])
        else "",
        image_index,
        path.suffix,
    )
    if Path(source_name).suffix.lower() != path.suffix.lower():
        source_name = f"{Path(source_name).stem or f'reference-{image_index}'}{path.suffix.lower()}"
    return FileResponse(
        path,
        media_type=media_type,
        filename=source_name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


async def _retry_access_for_owner(owner_hash: str, fallback: AccessContext) -> AccessContext:
    if owner_hash:
        owner_access = await asyncio.to_thread(get_temp_context_by_hash, owner_hash)
        if owner_access is None:
            raise HTTPException(status_code=409, detail="任务所属用户已失效")
        return owner_access
    return fallback


def _manual_retry_priority(task_id: str) -> tuple[str, str, str]:
    try:
        meta = get_meta(task_id)
    except (ValueError, FileNotFoundError):
        return ("9999", "9999", task_id)
    return (
        str(meta.get("queue_priority_at") or meta.get("created_at") or meta.get("queued_at") or ""),
        str(meta.get("created_at") or ""),
        task_id,
    )


async def _create_manual_retry_task(
    task_id: str,
    original: dict,
    retry_access: AccessContext,
    *,
    available_at: datetime | None = None,
) -> dict:
    owner_hash = str(original.get("owner_token_hash") or "")
    platform = str(original.get("platform") or DEFAULT_PLATFORM)
    model = str(original.get("model") or "")
    task_type = str(original.get("task_type") or "video")
    prompt = str(original.get("prompt") or "").strip()
    ratio = str(original.get("ratio") or DEFAULT_RATIO)
    duration = int(original.get("duration") or 0) or None
    platform, model = validate_task_platform_model(platform, model)
    duration = validate_task_duration(platform, model, duration)
    reserved_access = None
    reservation: dict = {}
    retry_meta: dict | None = None
    try:
        retry_meta = await asyncio.to_thread(
            create_task,
            prompt,
            ratio,
            owner_token_hash=owner_hash,
            platform=platform,
            model=model,
            task_type=task_type,
            enqueue=False,
            duration=duration,
        )
        if retry_access.is_temp:
            base_cost_units = model_cost_units(platform, model, task_type, duration)
            discount_units = await asyncio.to_thread(task_discount_units_by_token_hash, owner_hash, platform, model)
            cost_units = max(1, base_cost_units - discount_units)
            user_id = _transaction_user_id(retry_access)
            reserved_access = await asyncio.to_thread(reserve_temp_quota, retry_access, str(retry_meta["id"]), cost_units, user_id=user_id)
            reservation = await asyncio.to_thread(get_temp_reservation, owner_hash, str(retry_meta["id"]))
            if user_id and reservation:
                free_used = bool(reservation.get("free"))
                charged_units = int(reservation.get("units") or 0)
                await asyncio.to_thread(
                    record_transaction,
                    user_id,
                    "video_quota_consume" if free_used else "consume",
                    0 if free_used else -charged_units,
                    "视频额度任务消费" if free_used else "视频任务消费",
                    balance_units=reserved_access.credit_units,
                    video_quota_change=-1 if free_used else 0,
                    video_quota_balance=reserved_access.free_remaining,
                    reference_id=str(retry_meta["id"]),
                    detail=f"重试任务 ID：{retry_meta['id']}\n原任务 ID：{task_id}",
                )
        source_images = await asyncio.to_thread(task_image_paths, task_id)
        copied_images: list[Path] = []
        for index, source in enumerate(source_images, start=1):
            target = images_dir(str(retry_meta["id"])) / f"{index:02d}{source.suffix.lower()}"
            await asyncio.to_thread(shutil.copy2, source, target)
            copied_images.append(target)
        original_reference_names = original.get("reference_image_names") if isinstance(original.get("reference_image_names"), list) else []
        retry_reference_names = [
            _reference_image_name(
                original_reference_names[index] if index < len(original_reference_names) else "",
                index + 1,
                source.suffix,
            )
            for index, source in enumerate(source_images)
        ]
        await asyncio.to_thread(set_task_images, str(retry_meta["id"]), copied_images, retry_reference_names)
        retry_updates: dict[str, object] = {
            "retry_of_task_id": task_id,
            "reference_is_real_person": bool(original.get("reference_is_real_person")),
            "queue_priority_at": str(original.get("queue_priority_at") or original.get("created_at") or retry_meta.get("created_at") or ""),
        }
        if available_at is not None:
            retry_updates.update(
                next_attempt_at=available_at.isoformat(),
                queue_reason="批量重试任务按顺序排队",
                queue_category="bulk_retry_pacing",
                status_reason="等待按提交间隔执行",
            )
        await asyncio.to_thread(update_meta, str(retry_meta["id"]), **retry_updates)
        await asyncio.to_thread(finalize_task_creation, str(retry_meta["id"]))
    except QuotaExceeded as exc:
        if retry_meta:
            await asyncio.to_thread(delete_task, str(retry_meta["id"]))
        raise HTTPException(status_code=429, detail=str(exc))
    except Exception:
        if retry_meta:
            if reserved_access:
                await asyncio.to_thread(refund_temp_quota_hash, owner_hash, str(retry_meta["id"]))
            await asyncio.to_thread(delete_task, str(retry_meta["id"]))
        raise
    return {
        "ok": True,
        "id": str(retry_meta["id"]),
        "retry_of": task_id,
        "billing": {
            "free_used": bool(reservation.get("free")),
            "points_used": units_to_points(int(reservation.get("units") or 0)),
        } if retry_access.is_temp else None,
    }


@app.post("/tasks/bulk-retry", dependencies=[Depends(require_admin)])
async def bulk_retry_failed_tasks(
    request: Request,
    payload: BulkTaskRetryRequest,
    access: Annotated[AccessContext, Depends(require_admin)],
):
    await _rate_limit(request, "task-bulk-retry", 5, 60, "admin")
    limit = 1000
    matched_total = 0
    truncated = False
    if payload.retry_all:
        result = await asyncio.to_thread(
            lambda: list_tasks_page(
                owner_token_hash=None,
                owner_remarks=temp_token_remarks(),
                audience="admin",
                page=1,
                page_size=limit,
                keyword=str(payload.q or "").strip()[:200],
                status="failed",
                platform=str(payload.platform or "").strip().lower(),
            )
        )
        task_ids = [str(item.get("id") or "") for item in result["items"]]
        matched_total = int(result["total"])
        truncated = matched_total > len(task_ids)
        if truncated:
            raise HTTPException(status_code=409, detail=f"筛选结果超过 {limit} 个，请增加搜索条件后分组重试")
    else:
        task_ids = list(dict.fromkeys(str(task_id or "").strip() for task_id in payload.task_ids))
        task_ids = [task_id for task_id in task_ids if task_id]
        matched_total = len(task_ids)
        if not task_ids:
            raise HTTPException(status_code=400, detail="请选择要重试的失败任务")
        if len(task_ids) > limit:
            raise HTTPException(status_code=400, detail=f"单次最多重试 {limit} 个任务")

    task_ids = await asyncio.to_thread(lambda: sorted(task_ids, key=_manual_retry_priority))
    release_started_at = datetime.now(timezone.utc)
    release_interval_seconds = max(1.0, min(5.0, float(load_settings().dola_submit_interval_seconds)))
    release_index = 0

    created: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    for task_id in task_ids:
        try:
            validate_task_id(task_id)
            original = await asyncio.to_thread(get_meta, task_id)
            if bool(original.get("task_hidden_for_admin", False)):
                raise HTTPException(status_code=404, detail="task not found")
            if str(original.get("status") or "") != "failed":
                skipped.append({"id": task_id, "reason": "仅失败任务可以批量重试"})
                continue
            retry_access = await _retry_access_for_owner(str(original.get("owner_token_hash") or ""), access)
            available_at = release_started_at + timedelta(seconds=release_index * release_interval_seconds)
            release_index += 1
            retry_result = await _create_manual_retry_task(task_id, original, retry_access, available_at=available_at)
            created.append({"id": task_id, "retry_id": retry_result["id"], "available_at": available_at.isoformat()})
        except (ValueError, FileNotFoundError):
            failed.append({"id": task_id, "reason": "任务不存在"})
        except HTTPException as exc:
            failed.append({"id": task_id, "reason": str(exc.detail)})
        except Exception as exc:
            logger.exception("bulk retry failed for task %s", task_id)
            failed.append({"id": task_id, "reason": f"{type(exc).__name__}: {exc}"})

    return {
        "ok": not failed,
        "requested": len(task_ids),
        "matched_total": matched_total,
        "truncated": truncated,
        "created": len(created),
        "skipped": len(skipped),
        "failed": len(failed),
        "release_interval_seconds": release_interval_seconds,
        "results": {"created": created, "skipped": skipped, "failed": failed},
    }


@app.post("/tasks/{task_id}/retry", dependencies=[Depends(require_token)])
async def retry_completed_task(request: Request, access: Annotated[AccessContext, Depends(require_token)], task_id: str):
    try:
        validate_task_id(task_id)
        original = await asyncio.to_thread(get_meta, task_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="task not found")
    owner_hash = str(original.get("owner_token_hash") or "")
    if access.is_temp and owner_hash != access.token_hash:
        raise HTTPException(status_code=404, detail="task not found")
    audience = "client" if access.is_temp else "admin"
    if bool(original.get(f"task_hidden_for_{audience}", False)):
        raise HTTPException(status_code=404, detail="task not found")
    if str(original.get("status") or "") not in {"success", "failed"}:
        raise HTTPException(status_code=409, detail="仅成功或失败任务可以重新生成")
    await _rate_limit(request, "task-retry", 30, 60, owner_hash or "admin")
    retry_access = await _retry_access_for_owner(owner_hash, access)
    return await _create_manual_retry_task(task_id, original, retry_access)


@app.post("/tasks/{task_id}/video-visibility", dependencies=[Depends(require_token)])
async def task_video_visibility(request: Request, access: Annotated[AccessContext, Depends(require_token)], task_id: str):
    try:
        validate_task_id(task_id)
        meta = get_meta(task_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="task not found")
    if access.is_temp and str(meta.get("owner_token_hash") or "") != access.token_hash:
        raise HTTPException(status_code=404, detail="task not found")
    body = await _request_payload(request)
    hidden = str(body.get("hidden") or "true").lower() in {"1", "true", "yes", "on"}
    audience = "client" if access.is_temp else "admin"
    set_task_video_hidden(task_id, audience, hidden)
    return {"ok": True, "hidden": hidden, "audience": audience}


@app.delete("/tasks/{task_id}", dependencies=[Depends(require_token)])
async def remove_task(access: Annotated[AccessContext, Depends(require_token)], task_id: str):
    assert delete_sem is not None
    async with delete_sem:
        try:
            validate_task_id(task_id)
            meta = get_meta(task_id)
        except (ValueError, FileNotFoundError):
            raise HTTPException(status_code=404, detail="task not found")
        if access.is_temp and str(meta.get("owner_token_hash") or "") != access.token_hash:
            raise HTTPException(status_code=404, detail="task not found")
        activity_user_id = _transaction_user_id(access)
        status = str(meta.get("status") or "")
        if not access.is_admin and (status == "submitted" or str(meta.get("submit_phase") or "") in {"committing", "submitted"}):
            return {"ok": False, "cancelable": False, "detail": "已提交生成，无法取消"}
        if status in {"pending", "running", "submitted"}:
            cancel_reason = "管理员取消生成" if access.is_admin else "用户取消生成"
            canceled, canceled_meta = request_task_cancel(task_id, cancel_reason, allow_submitted=access.is_admin)
            if not canceled:
                return {"ok": False, "cancelable": False, "detail": "任务正在提交平台，无法取消"}
            await asyncio.to_thread(_refund_canceled_task, canceled_meta)
            await _record_activity_safe(activity_user_id, "task_cancel", "取消视频生成任务", reference_id=task_id)
            return {"ok": True, "canceled": True}
        audience = "client" if access.is_temp else "admin"
        set_task_hidden(task_id, audience, True)
        await _record_activity_safe(activity_user_id, "task_delete", "删除任务记录", reference_id=task_id)
        return {"ok": True, "deleted": True, "hidden": True, "audience": audience}
