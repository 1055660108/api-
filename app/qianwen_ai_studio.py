from __future__ import annotations

import asyncio
import uuid
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from playwright.async_api import async_playwright

from .accounts import local_today, merge_account_cookies, sync_qianwen_ai_studio_credit
from .browser_runtime import resolve_browser_executable
from .browser_live_view import TaskBrowserLiveView
from .config import load_settings
from .profile_lock import account_profile_lock
from .proxy_manager import acquire_dola_subscription_proxy, release_dola_subscription_proxy
from .store import begin_task_submission, clear_transient_result, is_task_canceled, save_result, task_exists


QIANWEN_AI_STUDIO_ORIGIN = "https://create.qianwen.com"
QIANWEN_AI_STUDIO_API = "https://ai-studio-create.qianwen.com/api/web"
QIANWEN_AI_STUDIO_SUBMIT_URL = f"{QIANWEN_AI_STUDIO_API}/ai/video/function"
QIANWEN_AI_STUDIO_QUERY_URL = f"{QIANWEN_AI_STUDIO_API}/assets/v1/batch/get"
QIANWEN_AI_STUDIO_PAGE_URL = f"{QIANWEN_AI_STUDIO_ORIGIN}/r/ai-studio-pc/main/gen?tab=video"
QIANWEN_AI_STUDIO_CREDIT_INFO_URL = f"{QIANWEN_AI_STUDIO_API}/credit/info"
QIANWEN_AI_STUDIO_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
QIANWEN_AI_STUDIO_CONNECT_ATTEMPTS = 3
QIANWEN_AI_STUDIO_CONNECT_BACKOFF_SECONDS = 0.5
QIANWEN_AI_STUDIO_CREDIT_COUNTRIES = ("台湾", "香港")
QIANWEN_AI_STUDIO_CREDIT_SYNC_CONCURRENCY = 4
_CREDIT_SYNC_SEMAPHORE = asyncio.Semaphore(QIANWEN_AI_STUDIO_CREDIT_SYNC_CONCURRENCY)

QIANWEN_AI_STUDIO_MODELS: dict[str, dict[str, str]] = {
    "HappyHorse 1.1": {"root_model": "happyhorse11", "scene": "hh11_t2v"},
    "万相 2.7": {"root_model": "wan27", "scene": "wan27_t2v"},
    "万相 2.6": {"root_model": "wan26", "scene": "wan26_t2v"},
}


async def acquire_qianwen_ai_studio_credit_proxy(settings: Any) -> dict[str, str]:
    if not settings.proxy_enabled or not settings.proxy_subscription_url:
        raise RuntimeError("qianwen AI Studio credit proxy unavailable")
    return await acquire_dola_subscription_proxy(
        settings.proxy_subscription_url,
        timeout_seconds=settings.proxy_api_timeout_seconds,
        scheme=settings.proxy_subscription_scheme,
        refresh_seconds=settings.proxy_subscription_refresh_seconds,
        auto_select=False,
        selected_countries=QIANWEN_AI_STUDIO_CREDIT_COUNTRIES,
        latency_threshold_ms=max(5000, int(settings.proxy_latency_threshold_ms)),
        random_select=True,
    )


def qianwen_ai_studio_model(model: str) -> dict[str, str] | None:
    expected = str(model or "").strip().casefold()
    for name, config in QIANWEN_AI_STUDIO_MODELS.items():
        if name.casefold() == expected:
            return {"name": name, **config}
    return None


def qianwen_ai_studio_cookie_header(cookies: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{item['name']}={item['value']}"
        for item in cookies
        if isinstance(item, dict) and item.get("name") and item.get("value") is not None
    )


def _cookie_value(cookies: list[dict[str, Any]], name: str) -> str:
    for item in cookies:
        if isinstance(item, dict) and str(item.get("name") or "").replace("\\_", "_") == name:
            return str(item.get("value") or "")
    return ""


def _missing_ticket_message(value: Any) -> bool:
    text = str(value or "").replace("\\_", "_").casefold()
    return "tongyi_sso_ticket" in text and any(marker in text for marker in ("empty", "missing", "required", "cannot be empty", "不能为空", "缺失"))


def _httpx_proxy_url(proxy_config: dict[str, str] | None) -> str:
    if not proxy_config or not proxy_config.get("server"):
        return ""
    server = str(proxy_config["server"])
    username = str(proxy_config.get("username") or "")
    password = str(proxy_config.get("password") or "")
    if not username:
        return server
    parsed = urlsplit(server if "://" in server else f"http://{server}")
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return urlunsplit((parsed.scheme or "http", f"{auth}{host}", parsed.path, parsed.query, parsed.fragment))


def _request_headers(cookie_header: str, xsrf_token: str = "") -> dict[str, str]:
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "cookie": cookie_header,
        "origin": QIANWEN_AI_STUDIO_ORIGIN,
        "referer": f"{QIANWEN_AI_STUDIO_ORIGIN}/r/ai-studio-pc/main/gen?tab=video",
        "user-agent": QIANWEN_AI_STUDIO_UA,
    }
    if xsrf_token:
        headers["x-xsrf-token"] = xsrf_token
    return headers


def parse_qianwen_ai_studio_credit(payload: Any) -> dict[str, int] | None:
    root = payload if isinstance(payload, dict) else {}
    data = root.get("data") if isinstance(root.get("data"), dict) else {}
    try:
        code = int(root.get("code") or 0)
    except (TypeError, ValueError):
        return None
    if code != 0 or "totalAmount" not in data:
        return None
    try:
        return {
            "total_amount": max(0, int(data.get("totalAmount") or 0)),
            "sign_in_amount": max(0, int(data.get("signIn") or 0)),
        }
    except (TypeError, ValueError):
        return None


def qianwen_ai_studio_submission_payload(
    prompt: str,
    model: str,
    ratio: str,
    duration: int,
    *,
    req_id: str = "",
    chid: str = "",
) -> tuple[dict[str, Any], str]:
    model_config = qianwen_ai_studio_model(model)
    if model_config is None:
        raise ValueError(f"千问 AI Studio 暂不支持 {model} 文生视频")
    normalized_ratio = str(ratio or "").strip()
    if normalized_ratio not in {"16:9", "9:16", "1:1", "4:3", "3:4"}:
        raise ValueError(f"千问 AI Studio 不支持画面比例 {normalized_ratio}")
    normalized_duration = int(duration or 10)
    if normalized_duration not in {5, 10, 15}:
        raise ValueError(f"千问 AI Studio 不支持 {normalized_duration} 秒")
    request_id = req_id or uuid.uuid4().hex
    payload = {
        "model": model_config["root_model"],
        "rootModel": model_config["root_model"],
        "prompt": str(prompt or "").strip(),
        "originPrompt": str(prompt or "").strip(),
        "scene": "gen_video",
        "genMode": "vid_gen",
        "params": {
            "size": normalized_ratio,
            "resolution": "720P",
            "duration": normalized_duration,
            "attachmentType": 0,
            "attachments": [],
        },
        "req_id": request_id,
        "chid": chid or uuid.uuid4().hex,
    }
    return payload, model_config["scene"]


def parse_qianwen_ai_studio_result(payload: Any) -> dict[str, Any]:
    root = payload if isinstance(payload, dict) else {}
    data = root.get("data") if isinstance(root.get("data"), dict) else {}
    items = data.get("list") if isinstance(data.get("list"), list) else []
    item = items[0] if items and isinstance(items[0], dict) else {}
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    extra = content.get("extra") if isinstance(content.get("extra"), dict) else {}
    videos = extra.get("result_videos") if isinstance(extra.get("result_videos"), list) else []
    if not videos and isinstance(data.get("resultVideos"), list):
        videos = data.get("resultVideos") or []
    candidates: list[tuple[int, str, str]] = []
    for video in videos:
        if not isinstance(video, dict):
            continue
        for key, score in (("url", 500), ("cdn_url", 400), ("cdnUrl", 400), ("download_url", 300), ("downloadUrl", 300), ("play_url", 200), ("playUrl", 200)):
            value = str(video.get(key) or "").strip()
            if value:
                candidates.append((score, key, value.replace("http://", "https://", 1)))
    candidates.sort(reverse=True)
    status = content.get("status", data.get("status", ""))
    error_text = str(content.get("error_msg") or data.get("errorMsg") or root.get("msg") or "").strip()
    quota_insufficient = any(marker in error_text.lower() for marker in ("额度不足", "额度已用完", "credit not enough", "insufficient credit"))
    failed = status in {3, 4, "failed", "auditFailed"}
    failed = failed or any(marker in error_text.lower() for marker in ("失败", "违规", "审核", "failed", "audit"))
    if candidates:
        state = "succeeded"
    elif quota_insufficient:
        state = "quota_insufficient"
    elif failed:
        state = "failed"
    else:
        state = "generating"
    return {
        "state": state,
        "video_url": candidates[0][2] if candidates else "",
        "video_source": candidates[0][1] if candidates else "",
        "status": status,
        "text": error_text,
        "task_id": str(content.get("task_id") or data.get("taskId") or ""),
        "model": str(extra.get("model_name") or extra.get("model") or ""),
        "duration": int((extra.get("params") or {}).get("duration") or 0) if isinstance(extra.get("params"), dict) else 0,
        "ratio": str((extra.get("params") or {}).get("size") or "") if isinstance(extra.get("params"), dict) else "",
        "quota_insufficient": quota_insufficient,
    }


async def fetch_qianwen_ai_studio_result(
    cookie_header: str,
    record_id: str,
    scene: str,
    *,
    xsrf_token: str = "",
    proxy_server: str = "",
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "headers": _request_headers(cookie_header, xsrf_token),
        "follow_redirects": True,
        "timeout": httpx.Timeout(45.0, connect=15.0),
        "trust_env": False,
    }
    if proxy_server:
        options["proxy"] = proxy_server
    payload = {
        "items": [{"recordId": str(record_id or ""), "scene": str(scene or "")}],
        "req_id": uuid.uuid4().hex,
        "chid": uuid.uuid4().hex,
    }
    async with httpx.AsyncClient(**options) as client:
        response = await client.post(QIANWEN_AI_STUDIO_QUERY_URL, json=payload)
        response.raise_for_status()
        parsed = parse_qianwen_ai_studio_result(response.json())
        parsed["http_status"] = response.status_code
        return parsed


class QianwenAIStudioAutomation:
    def __init__(
        self,
        task_id: str,
        prompt: str,
        ratio: str,
        model: str,
        duration: int,
        *,
        account: dict[str, Any],
        proxy_session: Any | None = None,
        submission_pacer: Any | None = None,
    ) -> None:
        self.task_id = task_id
        self.prompt = prompt
        self.ratio = ratio
        self.model = model
        self.duration = max(1, int(duration or 10))
        self.account = account
        self.proxy_session = proxy_session
        self.submission_pacer = submission_pacer
        self.settings = load_settings()

    def _failure(self, reason: str, *, retryable: bool = True, **extra: Any) -> dict[str, Any]:
        return {"success": False, "retryable": retryable, "reason": reason, **extra}

    async def run(self) -> dict[str, Any]:
        if not task_exists(self.task_id):
            return {"success": True, "retryable": False, "reason": ""}
        clear_transient_result(self.task_id)
        account_id = str(self.account.get("id") or "")
        lock = await account_profile_lock("qianwen", account_id)
        async with lock:
            return await self._run_locked()

    async def _sync_daily_credit(
        self,
        cookies: list[dict[str, Any]],
        _submission_proxy_config: dict[str, str] | None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        if not force and str(self.account.get("qianwen_ai_studio_credit_sync_date") or "") == local_today():
            return {"ok": True, "skipped": True}
        credit_event = asyncio.Event()
        credit_payload: dict[str, int] = {}
        response_tasks: set[asyncio.Task[Any]] = set()
        browser = None
        context = None
        credit_proxy: dict[str, str] | None = None
        live_view = TaskBrowserLiveView(self.task_id, "qianwen")

        async def inspect_credit_response(response) -> None:
            if "/api/web/credit/info" not in str(response.url):
                return
            try:
                parsed = parse_qianwen_ai_studio_credit(await response.json())
            except Exception:
                parsed = None
            if parsed is not None:
                credit_payload.update(parsed)
                credit_event.set()

        try:
            async with _CREDIT_SYNC_SEMAPHORE:
                credit_proxy = await acquire_qianwen_ai_studio_credit_proxy(self.settings)
                async with async_playwright() as playwright:
                    launch_options: dict[str, Any] = {
                        "headless": self.settings.headless,
                        "executable_path": resolve_browser_executable(self.settings.browser_executable_path),
                        "args": ["--disable-dev-shm-usage", "--no-sandbox", "--disable-blink-features=AutomationControlled"],
                    }
                    if credit_proxy.get("server"):
                        launch_options["proxy"] = credit_proxy
                    browser = await playwright.chromium.launch(**launch_options)
                    context = await browser.new_context(
                        locale="zh-CN",
                        user_agent=QIANWEN_AI_STUDIO_UA,
                        viewport={"width": 1365, "height": 900},
                    )
                    normalized_cookies: list[dict[str, Any]] = []
                    for raw in cookies:
                        if not isinstance(raw, dict) or not raw.get("name"):
                            continue
                        item = dict(raw)
                        item["domain"] = ".qianwen.com"
                        item.pop("url", None)
                        normalized_cookies.append(item)
                    await context.add_cookies(normalized_cookies)
                    page = await context.new_page()
                    if task_exists(self.task_id):
                        await live_view.start(page)

                    def response_handler(response) -> None:
                        task = asyncio.create_task(inspect_credit_response(response))
                        response_tasks.add(task)
                        task.add_done_callback(response_tasks.discard)

                    page.on("response", response_handler)
                    await page.goto(QIANWEN_AI_STUDIO_PAGE_URL, wait_until="domcontentloaded", timeout=120000)
                    try:
                        await asyncio.wait_for(credit_event.wait(), timeout=20)
                    except asyncio.TimeoutError:
                        await page.reload(wait_until="domcontentloaded", timeout=120000)
                        try:
                            await asyncio.wait_for(credit_event.wait(), timeout=20)
                        except asyncio.TimeoutError:
                            pass
                    await page.wait_for_timeout(1500)
                    refreshed_cookies = await context.cookies([QIANWEN_AI_STUDIO_ORIGIN, "https://www.qianwen.com/"])
                    page.remove_listener("response", response_handler)
                    if response_tasks:
                        await asyncio.gather(*list(response_tasks), return_exceptions=True)
                    if refreshed_cookies and "total_amount" in credit_payload:
                        merge_account_cookies(str(self.account.get("id") or ""), refreshed_cookies)
                    await context.close()
                    context = None
                    await browser.close()
                    browser = None
            if "total_amount" not in credit_payload:
                return {"ok": False, "reason": "qianwen AI Studio daily credit unavailable"}
            synced = sync_qianwen_ai_studio_credit(
                str(self.account.get("id") or ""),
                credit_payload["total_amount"],
                sign_in_amount=credit_payload.get("sign_in_amount", 0),
                pending_charge_id=str(self.account.get("quota_charge_id") or ""),
            )
            self.account.update(synced)
            return {"ok": True, **credit_payload, "remaining": synced.get("qianwen_ai_studio_quota_remaining")}
        except Exception as exc:
            return {"ok": False, "reason": f"qianwen AI Studio daily credit sync failed: {str(exc)[:300]}"}
        finally:
            await live_view.stop()
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
            if credit_proxy is not None:
                try:
                    await release_dola_subscription_proxy(credit_proxy)
                except Exception:
                    pass

    async def _run_locked(self) -> dict[str, Any]:
        cookies = [dict(item) for item in self.account.get("cookies") or [] if isinstance(item, dict)]
        cookie_header = str(self.account.get("cookie_header") or "") or qianwen_ai_studio_cookie_header(cookies)
        if not cookie_header:
            return self._failure("千问 AI Studio Cookie 为空", account_login_invalid=True, account_fault=True, switch_account=True)
        if not _cookie_value(cookies, "tongyi_sso_ticket").strip():
            return self._failure(
                "用户未登录错误, cookie中tongyi_sso_ticket不能为空",
                account_login_invalid=True,
                account_fault=True,
                switch_account=True,
            )
        try:
            payload, scene = qianwen_ai_studio_submission_payload(
                self.prompt,
                self.model,
                self.ratio,
                self.duration,
            )
        except ValueError as exc:
            return self._failure(str(exc), retryable=False)
        proxy_config = None
        try:
            if self.proxy_session is not None:
                proxy_config = await self.proxy_session.acquire_browser_proxy()
            credit = await self._sync_daily_credit(cookies, proxy_config)
            if not credit.get("ok"):
                return self._failure(str(credit.get("reason") or "qianwen AI Studio daily credit sync failed"), infrastructure_fault=True)
            if not credit.get("skipped") and int(credit.get("total_amount") or 0) < int(self.account.get("quota_cost") or 1):
                return self._failure(
                    "鍗冮棶 AI Studio 棰濆害涓嶈冻",
                    account_fault=True,
                    account_quota_insufficient=True,
                    switch_account=True,
                )
            options: dict[str, Any] = {
                "headers": _request_headers(cookie_header, _cookie_value(cookies, "XSRF-TOKEN")),
                "follow_redirects": True,
                "timeout": httpx.Timeout(60.0, connect=20.0),
                "trust_env": False,
            }
            proxy_url = _httpx_proxy_url(proxy_config)
            if proxy_url:
                options["proxy"] = proxy_url
            if self.submission_pacer is not None:
                await self.submission_pacer()
            if not begin_task_submission(self.task_id):
                canceled = is_task_canceled(self.task_id)
                return self._failure("用户取消生成" if canceled else "任务提交状态已变化，正在重试", retryable=not canceled)
            if self.submission_pacer is not None:
                await self.submission_pacer()
            if task_exists(self.task_id) and is_task_canceled(self.task_id):
                return self._failure("用户取消生成", retryable=False)
            async with httpx.AsyncClient(**options) as client:
                for attempt in range(1, QIANWEN_AI_STUDIO_CONNECT_ATTEMPTS + 1):
                    try:
                        response = await client.post(QIANWEN_AI_STUDIO_SUBMIT_URL, json=payload)
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        if attempt >= QIANWEN_AI_STUDIO_CONNECT_ATTEMPTS:
                            raise
                        await asyncio.sleep(QIANWEN_AI_STUDIO_CONNECT_BACKOFF_SECONDS * attempt)
                response.raise_for_status()
                body = response.json()
            code = int(body.get("code") or 0) if isinstance(body, dict) else -1
            data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else {}
            record_id = str(data.get("recordId") or "")
            message = str(body.get("msg") or body.get("message") or "") if isinstance(body, dict) else ""
            if _missing_ticket_message(message):
                return self._failure(
                    "用户未登录错误, cookie中tongyi_sso_ticket不能为空",
                    account_login_invalid=True,
                    account_fault=True,
                    switch_account=True,
                )
            if code == 15001:
                return self._failure(
                    "千问 AI Studio 额度不足",
                    account_fault=True,
                    account_quota_insufficient=True,
                    switch_account=True,
                )
            if code != 0 or not record_id:
                message = message or "千问 AI Studio 提交失败"
                retryable = code not in {1018}
                return self._failure(message[:500], retryable=retryable, account_fault=code in {401, 403}, switch_account=code in {401, 403, 1003, 429})
            save_result(
                self.task_id,
                cookie_string=cookie_header,
                cookies=cookies,
                extra={
                    "platform": "qianwen",
                    "model": self.model,
                    "account_id": str(self.account.get("id") or ""),
                    "account_name": str(self.account.get("name") or ""),
                    "account_quota_charge_id": str(self.account.get("quota_charge_id") or ""),
                    "account_quota_bucket": "qianwen_ai_studio",
                    "qianwen_submit_confirmed": True,
                    "qianwen_result_chain": "ai_studio",
                    "qianwen_ai_studio_record_id": record_id,
                    "qianwen_ai_studio_scene": scene,
                    "qianwen_ai_studio_req_id": str(payload.get("req_id") or ""),
                    "qianwen_actual_model": self.model,
                    "qianwen_actual_duration": self.duration,
                    "qianwen_actual_ratio": self.ratio,
                },
            )
            return {
                "success": False,
                "retryable": True,
                "reason": "qianwen AI Studio generation submitted",
                "submitted": True,
                "keep_account_claimed": True,
                "account_fault": False,
            }
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if self.proxy_session is not None:
                self.proxy_session.mark_browser_proxy_unavailable(reason="qianwen_ai_studio_transport_failure")
            return self._failure(f"千问 AI Studio 连接异常：{str(exc)[:300]}", infrastructure_fault=True)
        except (ValueError, TypeError) as exc:
            return self._failure(f"千问 AI Studio 返回异常：{str(exc)[:300]}")
        finally:
            if self.proxy_session is not None:
                await self.proxy_session.release_browser_proxy()
