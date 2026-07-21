from __future__ import annotations

import asyncio
import json
import hmac
import hashlib
import smtplib
import subprocess
import threading
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from .admin_auth import SESSION_COOKIE_NAME, SESSION_TTL_SECONDS, create_session, delete_session, delete_user_sessions, hash_password, session_username, validate_password, verify_password
from .accounts import account_for_current_task, add_account, add_accounts_bulk_result, clear_account_current_task, delete_account, list_accounts, reconcile_account_quotas, refund_account_quota, reset_account_quota, reset_daily_account_quotas_if_needed, set_account_enabled, update_account_quota
from .billing import model_cost_points, model_cost_units, points_to_units, units_to_points
from .browser_runtime import resolve_browser_executable
from .config import (
    DATA_DIR,
    DEFAULT_RATIO,
    VALID_RATIOS,
    ensure_config,
    load_settings,
    update_config,
    validate_proxy_api_scheme,
    validate_proxy_api_url,
    validate_startup_credentials,
)
from .email_verification import consume_registration_code, normalize_domains, normalize_email, send_registration_code, validate_allowed_email
from .feedback import create_feedback, list_feedback, list_feedback_for_user, update_feedback
from .notifications import create_announcement, create_notifications, list_admin_notifications, list_announcements, list_notifications_for_user, mark_all_notifications_read, mark_announcement_seen, mark_notification_read, update_announcement
from .platforms import DEFAULT_PLATFORM, PLATFORM_LABELS, normalize_model, normalize_platform
from .query import query_task
from .qianwen_models import fetch_qianwen_video_models
from .platform_model_sync import fetch_platform_video_models
from .proxy_manager import activate_mihomo_node, fetch_subscription_node_list, measure_node_delays, node_payload, rebuild_mihomo_from_snapshot
from .resilience import PlatformGuard, adaptive_worker_limit, queue_admission
from .repository_update import repository_status, update_repository
from .postgres import ensure_schema as ensure_postgres_schema
from .postgres import enabled as postgres_enabled
from .package_catalog import create_package, disable_package, list_packages, update_package
from .membership_catalog import DEFAULT_PAYMENT_URL, create_membership, disable_membership, get_membership, list_memberships, update_membership
from .point_cards import generate_cards, list_cards, redeem_card
from .point_transactions import list_transactions, record_transaction
from .store import (
    active_task_ids,
    active_task_count_for_owner,
    account_active_tasks,
    cleanup_expired_task_cache,
    create_task,
    finalize_task_creation,
    find_or_create_task,
    active_task_count_for_owner,
    load_result,
    delete_task,
    get_meta,
    images_dir,
    mark_cancel_requested,
    mark_failed,
    migrate_task_owner,
    list_tasks,
    set_task_video_hidden,
    set_task_hidden,
    set_task_images,
    request_task_cancel,
    task_has_video,
    validate_task_id,
)
from .temp_access import (
    AccessContext,
    QuotaExceeded,
    create_temp_tokens,
    delete_temp_token,
    get_temp_context,
    hash_token,
    list_temp_tokens,
    refund_temp_quota_hash,
    reserve_temp_quota,
    temp_token_retention_days,
    temp_token_remarks,
    update_temp_token,
)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
IMAGE_MAGIC = {
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".webp": (b"RIFF",),
}


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
from .textfix import repair_text
from .version import __version__
from .worker import refund_account_quota_once, refund_temp_quota_once
from .users import add_user_points, change_user_email_by_token_hash, change_user_password_by_token_hash, deduct_user_points, delete_user, has_verified_enabled_email, list_users, login_user, purchase_user_membership, register_user, repair_registered_user_tokens, reset_user_password_by_email, rotate_user_token_by_hash, set_user_concurrency, set_user_enabled, sync_user_membership_by_token_hash, touch_user_by_token, user_balance_by_token_hash, user_identity_by_token_hash, user_profile_by_token_hash, user_token_is_enabled


create_sem = None
query_sem = None
list_sem = None
delete_sem = None
_RATE_LOCK = threading.RLock()
_RATE_BUCKETS: dict[str, list[float]] = {}
quota_reset_task = None
task_cache_cleanup_task = None
LOCAL_TZ = timezone(timedelta(hours=8))


def _rate_limit(request: Request, scope: str, limit: int, window: int, identity: str = "") -> None:
    key = f"{scope}:{request.client.host if request.client else 'unknown'}:{identity}"
    now = time.monotonic()
    with _RATE_LOCK:
        recent = [stamp for stamp in _RATE_BUCKETS.get(key, []) if now - stamp < window]
        if len(recent) >= limit:
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试", headers={"Retry-After": str(window)})
        recent.append(now)
        _RATE_BUCKETS[key] = recent


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


async def task_cache_cleanup_loop() -> None:
    import asyncio

    while True:
        settings = load_settings()
        cleanup_expired_task_cache(settings.task_cache_retention_days, active_task_ids(), temp_token_retention_days())
        await asyncio.sleep(6 * 60 * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    global create_sem, query_sem, list_sem, delete_sem, quota_reset_task, task_cache_cleanup_task
    with _RATE_LOCK:
        _RATE_BUCKETS.clear()
    validate_startup_credentials(ensure_config())
    if postgres_enabled():
        ensure_postgres_schema()
    running_marker = DATA_DIR / ".service-running"
    running_marker.parent.mkdir(parents=True, exist_ok=True)
    running_marker.write_text(str(time.time()), encoding="utf-8")
    repair_registered_user_tokens()
    reset_daily_account_quotas_if_needed()
    reconcile_account_quotas()
    create_sem = asyncio.Semaphore(2)
    query_sem = asyncio.Semaphore(5)
    list_sem = asyncio.Semaphore(1)
    delete_sem = asyncio.Semaphore(1)
    quota_reset_task = asyncio.create_task(account_quota_reset_loop())
    task_cache_cleanup_task = asyncio.create_task(task_cache_cleanup_loop())
    try:
        yield
    finally:
        running_marker.unlink(missing_ok=True)
        if quota_reset_task:
            quota_reset_task.cancel()
            with suppress(asyncio.CancelledError):
                await quota_reset_task
        if task_cache_cleanup_task:
            task_cache_cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await task_cache_cleanup_task


app = FastAPI(title="Fetch Task Service", version=__version__, lifespan=lifespan)
ADMIN_DIR = Path(__file__).resolve().parent / "admin"

if ADMIN_DIR.exists():
    app.mount("/admin/assets", StaticFiles(directory=ADMIN_DIR), name="admin-assets")


@app.get("/health/live")
async def health_live():
    return {"ok": True, "version": __version__}


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
    temp_context = get_temp_context(supplied)
    if temp_context:
        if sync_user_membership_by_token_hash(temp_context.token_hash):
            temp_context = get_temp_context(supplied)
    if temp_context and user_token_is_enabled(temp_context.token_hash):
        touch_user_by_token(supplied)
        return temp_context
    session_owner = session_username(request.cookies.get(SESSION_COOKIE_NAME, ""))
    if session_owner and hmac.compare_digest(session_owner, settings.admin_username):
        return AccessContext(token_hash=hash_token(f"admin:{session_owner}"), is_admin=True, is_temp=False)
    raise HTTPException(status_code=403, detail="forbidden")


async def require_admin(access: Annotated[AccessContext, Depends(require_token)]) -> AccessContext:
    if not access.is_admin:
        raise HTTPException(status_code=403, detail="forbidden")
    return access


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
    context = get_temp_context(supplied)
    if context and sync_user_membership_by_token_hash(context.token_hash):
        context = get_temp_context(supplied)
    if context and user_token_is_enabled(context.token_hash):
        touch_user_by_token(supplied)
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
    _, resource_health = adaptive_worker_limit(settings.browser_workers)
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
        "browser_workers": settings.browser_workers,
        "active": sorted(active_task_ids()),
        "components": {
            "queue": {key: value for key, value in queue_health.items() if key != "error"},
            "browser": {"ok": browser_ok},
            "resources": resource_health,
            "platforms": {platform: platform_guard.snapshot(platform) for platform in PLATFORM_LABELS},
        },
    }
    if access.is_admin:
        data["admin_username"] = settings.admin_username
        data["components"]["queue"]["error"] = queue_health.get("error", "")
        data["components"]["browser"]["executable_path"] = browser_path or ""
        data["components"]["browser"]["error"] = browser_error
    if access.is_temp:
        balance = user_balance_by_token_hash(access.token_hash, list_temp_tokens())
        data["quota"] = {
            "limit": access.limit,
            "used": access.used,
            "remaining": access.remaining,
            **balance,
        }
        data["browser_workers"] = access.concurrency
        data["token_concurrency"] = access.concurrency
        data["task_retention_days"] = access.task_retention_days
        data["user_name"] = temp_token_remarks().get(access.token_hash, "")
    return data


def _admit_task_creation() -> None:
    from .task_queue import get_task_queue

    queue_health = get_task_queue().health()
    if not queue_health.get("ok"):
        raise HTTPException(status_code=503, detail="任务队列暂不可用", headers={"Retry-After": "5"})
    admission = queue_admission(queue_health)
    if not admission.allowed:
        raise HTTPException(status_code=503, detail="任务队列繁忙，请稍后重试", headers={"Retry-After": str(admission.retry_after)})


def _client_safe_text(value: str, model: str) -> str:
    import re

    replacement = str(model or "当前模型")
    text = str(value or "")
    text = re.sub(r"Dola|dola|豆包|千问|qianwen|doubao|平台", replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"账号|账户|号池|换号|服务凭证", "服务", text, flags=re.IGNORECASE)
    if re.search(r"额度不足|额度已用完|次数不足|次数已用完|余额不足|正在切换服务重试|正在切换账号重试|多个服务额度均不足", text):
        return "生成异常请重试！"
    return text


def _client_task(task: dict) -> dict:
    safe = dict(task)
    model = str(safe.get("model") or "当前模型")
    for key in ("error", "status_reason"):
        if key in safe:
            safe[key] = _client_safe_text(str(safe.get(key) or ""), model)
    for key in ("failed_account_ids", "account_id", "owner_token_hash", "worker_id", "platform", "video_hidden_for_admin", "task_hidden_for_admin", "task_hidden_for_client"):
        safe.pop(key, None)
    return safe


async def _request_payload(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid json body")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="json body must be an object")
        return {str(key): str(value) for key, value in data.items() if value is not None}

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


@app.get("/health", dependencies=[Depends(require_token)])
async def health(access: Annotated[AccessContext, Depends(require_token)]):
    return _health_payload(access)


@app.get("/auth/admin", dependencies=[Depends(require_admin)])
async def admin_auth(access: Annotated[AccessContext, Depends(require_admin)]):
    return _health_payload(access)


@app.post("/auth/admin/login")
async def admin_login(request: Request):
    payload = await _request_payload(request)
    _rate_limit(request, "admin-login", 10, 60, str(payload.get("username") or "").lower())
    settings = load_settings()
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    valid_username = hmac.compare_digest(username, settings.admin_username)
    valid_password = bool(settings.admin_password_hash) and verify_password(password, settings.admin_password_hash)
    if not valid_username or not valid_password:
        raise HTTPException(status_code=401, detail="管理员账号或密码错误")
    response = JSONResponse({"ok": True, "username": settings.admin_username})
    response.set_cookie(SESSION_COOKIE_NAME, create_session(settings.admin_username), max_age=SESSION_TTL_SECONDS, httponly=True, secure=request.url.scheme == "https", samesite="strict", path="/")
    return response


@app.post("/auth/admin/logout")
async def admin_logout(request: Request):
    delete_session(request.cookies.get(SESSION_COOKIE_NAME, ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", httponly=True, secure=request.url.scheme == "https", samesite="strict")
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
    return _health_payload(access)


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
            "卡密兑换",
            balance_units=int(balance.get("credit_units") or 0),
            reference_id=str(card.get("id") or ""),
        )
        return {"ok": True, "points": card.get("points", 0), "balance": balance}
    except KeyError:
        raise HTTPException(status_code=404, detail="卡密不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/points/transactions", dependencies=[Depends(require_temp)])
async def point_transactions(access: Annotated[AccessContext, Depends(require_temp)], page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100)):
    try:
        user = user_identity_by_token_hash(access.token_hash)
        return list_transactions(str(user.get("id") or ""), page, page_size)
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
            reference_id=str(package.get("id") or ""),
            detail=f"有效期 {package.get('duration_days')} 天 / 并发 {package.get('concurrency')} / 赠送视频额度 {package.get('bonus_free_uses')}",
        )
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
    payload = await _request_payload(request)
    _rate_limit(request, "register", 5, 60)
    if payload.get("password") != payload.get("confirm_password"):
        raise HTTPException(status_code=400, detail="两次输入的密码不一致")
    try:
        settings = load_settings()
        email = ""
        if settings.registration_email_verification_enabled:
            email = consume_registration_code(payload.get("email", ""), payload.get("email_code", ""), settings)
        return {"ok": True, **register_user(payload.get("username", ""), payload.get("password", ""), email)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/auth/register/email-code")
async def client_registration_email_code(request: Request):
    import asyncio

    payload = await _request_payload(request)
    settings = load_settings()
    try:
        email = validate_allowed_email(payload.get("email", ""), settings)
        _rate_limit(request, "registration-email-code-ip", 5, 600)
        _rate_limit(request, "registration-email-code-address", 3, 600, hashlib.sha256(email.encode("utf-8")).hexdigest())
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
    }


@app.post("/auth/login")
async def client_login(request: Request):
    payload = await _request_payload(request)
    identifier = payload.get("identifier") or payload.get("username") or ""
    _rate_limit(request, "client-login-ip", 60, 60)
    _rate_limit(request, "client-login-identifier", 20, 60, str(identifier).strip().casefold())
    result = login_user(identifier, payload.get("password", ""))
    if not result:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"ok": True, **result}


@app.post("/auth/password/forgot-code")
async def client_forgot_password_code(request: Request):
    import asyncio

    payload = await _request_payload(request)
    settings = load_settings()
    email = str(payload.get("email") or "").strip().lower()
    generic_detail = "如果该邮箱已绑定账号，验证码将发送到邮箱"
    try:
        email = normalize_email(email)
        _rate_limit(request, "reset-password-code-ip", 5, 600)
        _rate_limit(request, "reset-password-code-address", 3, 600, hashlib.sha256(email.encode("utf-8")).hexdigest())
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
async def client_token_refresh(access: Annotated[AccessContext, Depends(require_temp)]):
    try:
        result = rotate_user_token_by_hash(access.token_hash)
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在或已停用")
    migrate_task_owner(access.token_hash, hash_token(result["token"]))
    refreshed = get_temp_context(result["token"])
    payload = _health_payload(refreshed) if refreshed else {}
    return {"ok": True, **result, **payload}


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
    refreshed = get_temp_context(result["token"])
    payload = _health_payload(refreshed) if refreshed else {}
    return {"ok": True, **result, **payload}


@app.get("/auth/profile", dependencies=[Depends(require_temp)])
async def client_profile(access: Annotated[AccessContext, Depends(require_temp)]):
    try:
        return user_profile_by_token_hash(access.token_hash)
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在或已停用")


@app.post("/auth/email/code", dependencies=[Depends(require_temp)])
async def client_email_code(request: Request, access: Annotated[AccessContext, Depends(require_temp)]):
    import asyncio

    payload = await _request_payload(request)
    settings = load_settings()
    try:
        email = validate_allowed_email(payload.get("email", ""), settings)
        _rate_limit(request, "change-email-code", 3, 600, access.token_hash)
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
        return {"ok": True, **change_user_email_by_token_hash(access.token_hash, email)}
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在或已停用")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/feedback", dependencies=[Depends(require_temp)], status_code=201)
async def client_create_feedback(request: Request, access: Annotated[AccessContext, Depends(require_temp)]):
    payload = await _request_payload(request)
    _rate_limit(request, "feedback-user", 10, 3600, access.token_hash)
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


@app.get("/admin/notifications", dependencies=[Depends(require_admin)])
async def admin_notifications(limit: int = Query(200, ge=1, le=1000)):
    rows = list_admin_notifications(limit)
    return {"notifications": rows, "total": len(rows)}


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


@app.post("/users/{user_id}/points", dependencies=[Depends(require_admin)])
async def users_add_points(user_id: str, request: Request):
    payload = await _request_payload(request)
    try:
        credited = add_user_points(user_id, payload.get("amount"), list_temp_tokens())
        user = next(item for item in list_users(list_temp_tokens()) if str(item.get("id") or "") == user_id)
        record_transaction(
            user_id,
            "admin_credit",
            points_to_units(payload.get("amount")),
            "管理员充值",
            balance_units=points_to_units(user.get("points") or 0) if float(user.get("points") or 0) > 0 else 0,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **credited}


@app.post("/users/{user_id}/points/deduct", dependencies=[Depends(require_admin)])
async def users_deduct_points(user_id: str, request: Request):
    payload = await _request_payload(request)
    try:
        deduct_user_points(user_id, payload.get("amount"))
        user = next(item for item in list_users(list_temp_tokens()) if str(item.get("id") or "") == user_id)
        record_transaction(
            user_id,
            "admin_deduct",
            -points_to_units(payload.get("amount")),
            "管理员扣除",
            balance_units=points_to_units(user.get("points") or 0) if float(user.get("points") or 0) > 0 else 0,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


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
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
    try:
        set_user_enabled(user_id, str(payload.get("enabled") or "").lower() in {"1", "true", "yes", "on"})
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在")
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


@app.get("/config/proxy-api", dependencies=[Depends(require_admin)])
async def proxy_api_config():
    settings = load_settings()
    return {
        "proxy_api_url": settings.proxy_api_url,
        "proxy_api_scheme": settings.proxy_api_scheme,
        "proxy_api_timeout_seconds": settings.proxy_api_timeout_seconds,
        "proxy_subscription_configured": bool(settings.proxy_subscription_url),
        "proxy_subscription_scheme": settings.proxy_subscription_scheme,
        "proxy_subscription_refresh_seconds": settings.proxy_subscription_refresh_seconds,
        "proxy_enabled": settings.proxy_enabled,
        "proxy_auto_select": settings.proxy_auto_select,
        "proxy_selected_node": settings.proxy_selected_node,
    }


@app.get("/config/proxy-nodes", dependencies=[Depends(require_admin)])
async def proxy_nodes(refresh: bool = False):
    settings = load_settings()
    if not settings.proxy_subscription_url:
        return {"nodes": [], "enabled": settings.proxy_enabled, "auto_select": settings.proxy_auto_select, "selected_node": ""}
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
    return {
        "nodes": [node_payload(node, settings.proxy_selected_node) for node in nodes],
        "enabled": settings.proxy_enabled,
        "auto_select": settings.proxy_auto_select,
        "selected_node": settings.proxy_selected_node,
    }


@app.post("/config/proxy-nodes/latency", dependencies=[Depends(require_admin)])
async def proxy_node_latency():
    settings = load_settings()
    if not settings.proxy_subscription_url:
        raise HTTPException(status_code=409, detail="proxy subscription is not configured")
    try:
        nodes = await fetch_subscription_node_list(
            settings.proxy_subscription_url,
            timeout_seconds=settings.proxy_api_timeout_seconds,
            refresh_seconds=settings.proxy_subscription_refresh_seconds,
        )
        await measure_node_delays(nodes, settings.proxy_subscription_url, settings.proxy_api_timeout_seconds)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"nodes": [node_payload(node, settings.proxy_selected_node) for node in nodes]}


@app.post("/config/proxy-nodes/select", dependencies=[Depends(require_admin)])
async def select_proxy_node(request: Request):
    payload = await _request_payload(request)
    node_id = str(payload.get("node_id") or "").strip()
    settings = load_settings()
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
    return {"ok": True, "selected_node": selected.id, "node": node_payload(selected, selected.id)}


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


@app.get("/config/workers", dependencies=[Depends(require_token)])
async def workers_config():
    settings = load_settings()
    return {"browser_workers": settings.browser_workers}


@app.get("/config/platforms", dependencies=[Depends(require_token)])
async def platforms_config():
    settings = load_settings()
    return {
        "default_platform": settings.default_platform,
        "platforms": [
            {
                "id": platform,
                "label": PLATFORM_LABELS.get(platform, platform),
                "models": [model for model in settings.platform_models.get(platform, []) if settings.platform_model_states.get(platform, {}).get(model, True)],
                "model_costs": {model: model_cost_points(platform, model) for model in settings.platform_models.get(platform, [])},
                "all_models": [
                    {
                        "name": model,
                        "enabled": settings.platform_model_states.get(platform, {}).get(model, True),
                        "cost": model_cost_points(platform, model),
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
            if settings.platform_model_states.get(platform, {}).get(model, True):
                data.append({"id": f"{platform}:{model}", "object": "model", "created": 0, "owned_by": platform})
    return {"object": "list", "data": data}


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
    task_type = "video"
    if access.is_temp and active_task_count_for_owner(access.token_hash) >= access.concurrency:
        raise OpenAIAPIError(429, "Concurrency limit exceeded", "rate_limit_error", code="rate_limit_exceeded")
    _rate_limit(request, "openai-task", 30, 60, access.token_hash)
    key = _idempotency_key(idempotency_key)
    fingerprint = _request_fingerprint("openai", access.token_hash, {"prompt": repair_text(prompt), "ratio": payload.ratio, "platform": platform, "model": model, "task_type": task_type})
    try:
        if key:
            meta, created = find_or_create_task(repair_text(prompt), payload.ratio, access.token_hash if access.is_temp else "", platform, model, task_type, key, fingerprint, "openai")
        else:
            meta, created = create_task(repair_text(prompt), payload.ratio, owner_token_hash=access.token_hash if access.is_temp else "", platform=platform, model=model, task_type=task_type, enqueue=False), True
        if not created:
            task_id = str(meta["id"])
            content = json.dumps({"task_id": task_id, "status": str(meta.get("status") or "submitted"), "result_endpoint": f"/tasks/{task_id}"}, ensure_ascii=False)
            return {"id": f"chatcmpl-{task_id}", "object": "chat.completion", "created": int(time.time()), "model": payload.model, "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
        cost_units = model_cost_units(platform, model, task_type)
        user_id = _transaction_user_id(access)
        charged = access.is_temp and access.free_remaining <= 0
        reserved_access = reserve_temp_quota(access, str(meta["id"]), cost_units, user_id=user_id)
        if charged and user_id:
            record_transaction(
                user_id,
                "consume",
                -cost_units,
                "视频任务消费",
                balance_units=reserved_access.credit_units,
                reference_id=str(meta["id"]),
                detail=f"任务 ID：{meta['id']}\n{PLATFORM_LABELS.get(platform, platform)} / {model}",
            )
        finalize_task_creation(str(meta["id"]))
    except ValueError as exc:
        raise OpenAIAPIError(409, str(exc), "invalid_request_error", "Idempotency-Key", "idempotency_conflict")
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
    content = json.dumps({"task_id": task_id, "status": "submitted", "result_endpoint": f"/tasks/{task_id}"}, ensure_ascii=False)
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
            else:
                model = normalize_model(str(raw_model or ""))
                enabled = True
                cost = model_cost_points(platform, model)
            if not model or model in seen:
                continue
            seen.add(model)
            models_by_platform[platform].append(model)
            states_by_platform[platform][model] = enabled
            costs_by_platform[platform][model] = cost
    default_platform = str(payload.get("default_platform") or load_settings().default_platform)
    try:
        default_platform = normalize_platform(default_platform)
        update_config({"default_platform": default_platform, "platform_models": models_by_platform, "platform_model_states": states_by_platform, "model_costs": costs_by_platform})
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
    raw_workers = payload.get("browser_workers") or payload.get("workers") or browser_workers
    if raw_workers is None:
        raise HTTPException(status_code=400, detail="browser_workers is required")
    try:
        workers = int(raw_workers)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="browser_workers must be an integer")
    if workers < 1 or workers > 100:
        raise HTTPException(status_code=400, detail="browser_workers must be between 1 and 100")
    try:
        update_config({"browser_workers": workers})
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    settings = load_settings()
    return {"ok": True, "browser_workers": settings.browser_workers}


@app.get("/accounts", dependencies=[Depends(require_admin)])
async def accounts_list(
    page: int | None = Query(None, ge=1),
    page_size: int | None = Query(None, ge=1, le=100),
    q: str | None = Query(None, max_length=200),
    platform: str | None = Query(None),
):
    reset_daily_account_quotas_if_needed()
    reconcile_account_quotas()
    task_statuses = {item["id"]: str(item.get("status") or "") for item in list_tasks()}
    active_by_account = account_active_tasks()
    accounts = []
    for account in list_accounts():
        current_task_id = str(account.get("current_task_id") or "")
        current_status = task_statuses.get(current_task_id, "")
        current_result = load_result(current_task_id) if current_task_id and current_task_id in task_statuses else {}
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
        accounts.append(account)
    total_limit = sum(max(0, int(item.get("quota_limit") or 0)) for item in accounts)
    total_used = sum(max(0, int(item.get("quota_used") or 0)) for item in accounts)
    unlimited_count = sum(1 for item in accounts if not int(item.get("quota_limit") or 0))
    response = {
        "accounts": accounts,
        "quota_summary": {
            "total_limit": total_limit,
            "total_used": total_used,
            "total_remaining": max(0, total_limit - total_used),
            "unlimited_count": unlimited_count,
        },
        "next_quota_reset_at": next_quota_reset_at(),
    }
    if page is None and page_size is None and q is None and platform is None:
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
    keyword = str(q or "").strip().lower()
    filtered = [
        item for item in platform_accounts
        if not keyword or any(
            keyword in str(value or "").lower()
            for value in (
                item.get("id"), item.get("name"), item.get("account_status"), item.get("status_reason"),
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
            "normal": sum(item.get("enabled") is not False and item.get("account_status") != "abnormal" for item in platform_accounts),
            "abnormal": sum(item.get("account_status") == "abnormal" for item in platform_accounts),
            "disabled": sum(item.get("enabled") is False for item in platform_accounts),
            "by_platform": {
                item: sum(str(account.get("platform") or DEFAULT_PLATFORM) == item for account in accounts)
                for item in PLATFORM_LABELS
            },
        },
    )
    return response


@app.post("/accounts", dependencies=[Depends(require_admin)])
async def accounts_create(request: Request):
    import asyncio

    payload = await _request_payload(request)
    cookie_data = payload.get("cookie_data") or payload.get("cookies") or payload.get("cookie") or ""
    bulk = str(payload.get("bulk") or "").strip().lower() in {"1", "true", "yes", "y", "on"}
    try:
        platform = normalize_platform(payload.get("platform") or DEFAULT_PLATFORM)
        default_quota_limit = 1 if platform == "dola" else 5 if platform == "qianwen" else 2
        quota_limit = int(payload.get("quota_limit") if payload.get("quota_limit") not in {None, ""} else default_quota_limit)
        enabled = str(payload.get("enabled") or "true").lower() not in {"0", "false", "no", "off"}
        if bulk:
            result = await asyncio.to_thread(add_accounts_bulk_result, cookie_data, quota_limit, enabled, platform)
            return {"ok": True, **result}
        account = add_account(payload.get("name") or "", cookie_data, enabled=enabled, quota_limit=quota_limit, platform=platform)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "account": account}


@app.patch("/accounts/{account_id}", dependencies=[Depends(require_admin)])
@app.put("/accounts/{account_id}", dependencies=[Depends(require_admin)])
async def accounts_update(account_id: str, request: Request):
    payload = await _request_payload(request)
    try:
        if "reset_quota" in payload and str(payload.get("reset_quota") or "").strip().lower() in {"1", "true", "yes", "y", "on"}:
            account = reset_account_quota(account_id)
        elif "quota_limit" in payload:
            account = update_account_quota(account_id, int(payload.get("quota_limit") or 0))
        elif "enabled" in payload:
            enabled = str(payload.get("enabled") or "").strip().lower() in {"1", "true", "yes", "y", "on"}
            account = set_account_enabled(account_id, enabled)
        else:
            raise HTTPException(status_code=400, detail="enabled or quota_limit is required")
    except KeyError:
        raise HTTPException(status_code=404, detail="account not found")
    except ValueError:
        raise HTTPException(status_code=400, detail="quota_limit must be an integer")
    return {"ok": True, "account": account}


@app.delete("/accounts/{account_id}", dependencies=[Depends(require_admin)])
async def accounts_delete(account_id: str):
    if not delete_account(account_id):
        raise HTTPException(status_code=404, detail="account not found")
    return {"ok": True}


@app.post("/accounts/{account_id}/delete", dependencies=[Depends(require_admin)])
async def accounts_delete_action(account_id: str):
    if not delete_account(account_id):
        raise HTTPException(status_code=404, detail="account not found")
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
    if "proxy_api_url" in payload:
        next_url = payload.get("proxy_api_url")
    elif "url" in payload:
        next_url = payload.get("url")
    else:
        next_url = proxy_api_url if proxy_api_url is not None else url
    next_scheme = payload.get("proxy_api_scheme") or payload.get("scheme") or proxy_api_scheme or scheme
    try:
        updates = {}
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
        if "proxy_enabled" in payload:
            updates["proxy_enabled"] = str(payload.get("proxy_enabled")).strip().lower() in {"1", "true", "yes", "on"}
        if "proxy_auto_select" in payload:
            updates["proxy_auto_select"] = str(payload.get("proxy_auto_select")).strip().lower() in {"1", "true", "yes", "on"}
        if "proxy_selected_node" in payload:
            updates["proxy_selected_node"] = str(payload.get("proxy_selected_node") or "").strip()[:200]
        if not updates:
            raise ValueError("proxy configuration is required")
        update_config(updates)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    settings = load_settings()
    return {
        "ok": True,
        "proxy_api_url": settings.proxy_api_url,
        "proxy_api_scheme": settings.proxy_api_scheme,
        "proxy_api_timeout_seconds": settings.proxy_api_timeout_seconds,
        "proxy_subscription_configured": bool(settings.proxy_subscription_url),
        "proxy_subscription_scheme": settings.proxy_subscription_scheme,
        "proxy_subscription_refresh_seconds": settings.proxy_subscription_refresh_seconds,
        "proxy_enabled": settings.proxy_enabled,
        "proxy_auto_select": settings.proxy_auto_select,
        "proxy_selected_node": settings.proxy_selected_node,
    }


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
        token = update_temp_token(
            token_id,
            limit=int(payload["limit"]) if "limit" in payload else None,
            concurrency=int(payload["concurrency"]) if "concurrency" in payload else None,
            task_retention_days=int(payload["task_retention_days"]) if "task_retention_days" in payload else None,
            remark=str(payload.get("remark") if "remark" in payload else payload.get("note")) if "remark" in payload or "note" in payload else None,
        )
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


@app.post("/tasks", dependencies=[Depends(require_token)])
async def submit_task(
    request: Request,
    access: Annotated[AccessContext, Depends(require_token)],
    prompt: Annotated[str, Form()],
    ratio: Annotated[str, Form()] = DEFAULT_RATIO,
    platform: Annotated[str, Form()] = DEFAULT_PLATFORM,
    model: Annotated[str, Form()] = "",
    task_type: Annotated[str, Form()] = "video",
    images: Annotated[list[UploadFile] | None, File(alias="images")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    assert create_sem is not None
    async with create_sem:
        _admit_task_creation()
        prompt = repair_text((prompt or "").strip())
        ratio = (ratio or DEFAULT_RATIO).strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        if ratio not in VALID_RATIOS:
            raise HTTPException(status_code=400, detail="invalid ratio")
        platform, model = validate_task_platform_model(platform, model)
        if platform == "qianwen" and task_type != "video":
            raise HTTPException(status_code=400, detail="千问当前仅支持视频任务")
        uploads = [item for item in (images or []) if item and item.filename]
        if len(uploads) > load_settings().max_image_count:
            raise HTTPException(status_code=400, detail="too many images")
        _rate_limit(request, "task-create", 30, 60, access.token_hash)
        key = _idempotency_key(idempotency_key)
        fingerprint = _request_fingerprint("tasks", access.token_hash, {"prompt": prompt, "ratio": ratio, "platform": platform, "model": model, "task_type": task_type, "images": [Path(item.filename or "").name for item in uploads]})

        try:
            if key:
                meta, created = find_or_create_task(prompt, ratio, access.token_hash if access.is_temp else "", platform, model, task_type, key, fingerprint, "tasks")
            else:
                meta, created = create_task(prompt, ratio, owner_token_hash=access.token_hash if access.is_temp else "", platform=platform, model=model, task_type=task_type, enqueue=False), True
            if not created:
                return {"id": meta["id"], "replayed": True}
            if access.is_temp and active_task_count_for_owner(access.token_hash) >= access.concurrency:
                mark_failed(meta["id"], "已超出并发上限，及时联系管理员调整。")
                return {"id": meta["id"]}
            cost_units = model_cost_units(platform, model, task_type)
            user_id = _transaction_user_id(access)
            charged = access.is_temp and access.free_remaining <= 0
            reserved_access = reserve_temp_quota(access, str(meta["id"]), cost_units, user_id=user_id)
            if charged and user_id:
                record_transaction(
                    user_id,
                    "consume",
                    -cost_units,
                    "视频任务消费",
                    balance_units=reserved_access.credit_units,
                    reference_id=str(meta["id"]),
                    detail=f"任务 ID：{meta['id']}\n{PLATFORM_LABELS.get(platform, platform)} / {model}",
                )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except QuotaExceeded as exc:
            if "meta" in locals():
                delete_task(str(meta["id"]))
            raise HTTPException(status_code=429, detail=str(exc))
        except Exception:
            if "meta" in locals():
                refund_temp_quota_hash(access.token_hash, str(meta["id"]))
                delete_task(str(meta["id"]))
            raise
        saved_paths: list[Path] = []
        try:
            for index, upload in enumerate(uploads, start=1):
                filename = Path(upload.filename or f"image_{index}.png").name
                suffix = Path(filename).suffix.lower() or ".png"
                target = images_dir(meta["id"]) / f"{index:02d}{suffix}"
                _save_uploaded_image(upload, target)
                saved_paths.append(target)
            set_task_images(meta["id"], saved_paths)
            finalize_task_creation(str(meta["id"]))
        except Exception:
            if reserved_access:
                refund_temp_quota_hash(reserved_access.token_hash, str(meta["id"]))
            delete_task(meta["id"])
            raise
        response = {"id": meta["id"]}
        if reserved_access and reserved_access.is_temp:
            balance = user_balance_by_token_hash(reserved_access.token_hash, list_temp_tokens())
            response["quota"] = {
                "limit": reserved_access.limit,
                "used": reserved_access.used,
                "remaining": reserved_access.remaining,
                **balance,
            }
        return response


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
        tasks = list_tasks(owner_token_hash=owner, owner_remarks=temp_token_remarks())
        if access.is_temp:
            tasks = [item for item in tasks if not item.get("task_hidden_for_client")]
            tasks = [_client_task(item) for item in tasks]
        else:
            tasks = [item for item in tasks if not item.get("task_hidden_for_admin")]
        if page is None and page_size is None and q is None and status is None and platform is None:
            return {"tasks": tasks}
        selected_status = str(status or "").strip().lower()
        selected_platform = str(platform or "").strip().lower()
        keyword = str(q or "").strip().lower()
        filtered = [
            item for item in tasks
            if (not selected_status or selected_status == "all" or str(item.get("status") or "").lower() == selected_status)
            and (not selected_platform or selected_platform == "all" or str(item.get("platform") or "").lower() == selected_platform)
            and (
                not keyword
                or any(
                    keyword in str(value or "").lower()
                    for value in (
                        item.get("id"), item.get("prompt"), item.get("prompt_preview"), item.get("status"),
                        item.get("error"), item.get("owner_name"), item.get("model"), item.get("platform"),
                        item.get("created_at"), item.get("updated_at"),
                    )
                )
            )
        ]
        effective_page_size = page_size or 50
        total = len(filtered)
        total_pages = max(1, (total + effective_page_size - 1) // effective_page_size)
        current_page = min(page or 1, total_pages)
        start = (current_page - 1) * effective_page_size
        return {
            "tasks": filtered[start:start + effective_page_size],
            "total": total,
            "page": current_page,
            "page_size": effective_page_size,
            "total_pages": total_pages,
            "stats": {
                "total": len(tasks),
                "pending": sum(str(item.get("status") or "") == "pending" for item in tasks),
                "running": sum(str(item.get("status") or "") in {"running", "submitted"} for item in tasks),
                "success": sum(str(item.get("status") or "") == "success" for item in tasks),
                "failed": sum(str(item.get("status") or "") in {"failed", "canceled"} for item in tasks),
                "completed_today": sum(item.get("completed_today") is True for item in tasks),
            },
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
            result["text"] = _client_safe_text(str(result.get("text") or ""), str(meta.get("model") or "当前模型"))
        return result


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
        status = str(meta.get("status") or "")
        if status == "submitted" or str(meta.get("submit_phase") or "") in {"committing", "submitted"}:
            mark_cancel_requested(task_id, "已提交平台生成，无法取消")
            return {"ok": False, "cancelable": False, "detail": "已提交平台生成，无法取消"}
        if status in {"pending", "running"}:
            canceled, canceled_meta = request_task_cancel(task_id)
            if not canceled:
                return {"ok": False, "cancelable": False, "detail": "任务正在提交平台，无法取消"}
            result = load_result(task_id)
            account_id = str(result.get("account_id") or "")
            account = account_for_current_task(task_id)
            if not account_id and account:
                account_id = str(account.get("id") or "")
            if account_id:
                clear_account_current_task(account_id, task_id)
                refund_account_quota_once(task_id, account_id, str((account or {}).get("current_quota_charge_id") or ""))
            refund_temp_quota_once(task_id, str(canceled_meta.get("owner_token_hash") or ""))
            return {"ok": True, "canceled": True}
        audience = "client" if access.is_temp else "admin"
        set_task_hidden(task_id, audience, True)
        return {"ok": True, "deleted": True, "hidden": True, "audience": audience}
