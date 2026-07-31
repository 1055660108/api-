from __future__ import annotations

import asyncio
import re
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .accounts import disable_account_for_login, set_account_cooldown, update_account_cookies
from .browser_runtime import BROWSER_EXTRA_HTTP_HEADERS, BROWSER_INIT_SCRIPT, BROWSER_USER_AGENT, BrowserContextLease, ReusableBrowserPool, bounded_cleanup, resolve_browser_executable, safe_close
from .config import DOUBAO_STATES_DIR, ensure_dirs, load_settings
from .store import begin_task_submission, clear_transient_result, is_task_canceled, mark_pending, mark_submitted, mark_success, release_task_submission, save_result, set_execution_phase, task_exists
from .profile_lock import account_profile_lock


DOUBAO_URL = "https://www.doubao.com/chat/"
VIDEO_URL_RE = re.compile(r'https?://[^"\\\s]+(?:mime_type=video_mp4|\.mp4(?:\?[^"\\\s]*)?)', re.IGNORECASE)
REGION_RESTRICTION_MARKERS = (
    "doubao-region-ban",
    "当前地区暂不支持",
    "所在地区暂不支持",
    "not available in your region",
    "region is not supported",
)
DOUBAO_MODEL_CODES = {
    "Seedance 2.0 Mini": "seedance_v2.0_mini",
    "Seedance 2.0 Fast": "seedance_v2.0",
}


DOUBAO_SUBMIT_SCRIPT = r"""
async ({prompt, ratio, model, duration}) => {
  function uuid() {
    return crypto.randomUUID ? crypto.randomUUID() :
      "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {
        const r = Math.random() * 16 | 0;
        const v = c === "x" ? r : (r & 0x3 | 0x8);
        return v.toString(16);
      });
  }
  function randomDigits(len) {
    let out = "";
    for (let i = 0; i < len; i += 1) out += String(Math.floor(Math.random() * 10));
    return out.replace(/^0/, "1");
  }
  function cookieValue(name) {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = document.cookie.match(new RegExp(`(?:^|; )${escaped}=([^;]*)`));
    return match ? decodeURIComponent(match[1]) : "";
  }
  function storageFind(regex) {
    for (const store of [localStorage, sessionStorage]) {
      for (let index = 0; index < store.length; index += 1) {
        const key = store.key(index);
        const value = store.getItem(key) || "";
        if (regex.test(key) && value && value.length < 100) return value;
      }
    }
    return "";
  }
  function trySign(url) {
    const signers = [window.byted_acrawler, window.bytedAcrawler, window.__acrawler, window.ABogus].filter(Boolean);
    for (const signer of signers) {
      try {
        if (typeof signer.sign !== "function") continue;
        const signed = signer.sign({url});
        if (typeof signed === "string" && signed) return signed;
        if (signed && typeof signed === "object") {
          if (typeof signed.a_bogus === "string") return signed.a_bogus;
          if (typeof signed.aBogus === "string") return signed.aBogus;
          if (typeof signed.url === "string") {
            const value = new URL(signed.url, location.origin).searchParams.get("a_bogus");
            if (value) return value;
          }
        }
      } catch (_) {}
    }
    return "";
  }
  function extract(patterns, text) {
    for (const pattern of patterns) {
      const match = text.match(pattern);
      if (match) return match[1];
    }
    return "";
  }

  const localConversationId = `local_${randomDigits(16)}`;
  const uniqueKey = uuid();
  const webId = (storageFind(/web_id|tea_uuid|device_id/i).match(/\d{15,24}/) || [])[0]
    || `${Date.now()}${randomDigits(6)}`;
  const fp = cookieValue("s_v_web_id") || storageFind(/s_v_web_id|fp|verify/i) || `verify_${randomDigits(12)}`;
  const region = cookieValue("flow_user_country") || "JP";
  const params = new URLSearchParams({
    aid: "497858",
    device_id: webId,
    device_platform: "web",
    doubao_device_platform: "web",
    doubao_pc_version: "3.29.10",
    fp,
    language: "zh",
    pc_version: "3.29.10",
    pkg_type: "release_version",
    real_aid: "497858",
    region,
    samantha_web: "1",
    sys_region: region,
    tea_uuid: webId,
    tz_name: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
    "use-olympus-account": "1",
    version_code: "20800",
    web_id: webId,
    web_platform: "browser",
    web_tab_id: uuid()
  });
  const msToken = cookieValue("msToken") || storageFind(/mstoken/i);
  if (msToken) params.set("msToken", msToken);
  let requestUrl = `${location.origin}/chat/completion?${params.toString()}`;
  const aBogus = trySign(requestUrl);
  if (aBogus) {
    params.set("a_bogus", aBogus);
    requestUrl = `${location.origin}/chat/completion?${params.toString()}`;
  }

  const seconds = Math.max(1, Number(duration) || 10);
  const payload = {
    client_meta: {
      local_conversation_id: localConversationId,
      conversation_id: "",
      bot_id: "7338286299411103781",
      last_section_id: "",
      last_message_index: null
    },
    messages: [{
      local_message_id: uuid(),
      content_block: [{
        block_type: 10000,
        content: {
          text_block: {text: `生成视频：${prompt}，${seconds}s`, icon_url: "", icon_url_dark: "", summary: ""},
          pc_event_block: ""
        },
        block_id: uuid(),
        parent_id: "",
        meta_info: [],
        append_fields: []
      }],
      message_status: 0
    }],
    option: {
      send_message_scene: "",
      create_time_ms: Date.now(),
      collect_id: "",
      is_audio: false,
      answer_with_suggest: false,
      tts_switch: false,
      need_deep_think: 0,
      click_clear_context: false,
      from_suggest: false,
      is_regen: false,
      is_replace: false,
      is_from_click_option: false,
      is_from_click_softlink: false,
      disable_sse_cache: false,
      select_text_action: "",
      is_select_text: false,
      resend_for_regen: false,
      scene_type: 0,
      unique_key: uniqueKey,
      start_seq: 0,
      need_create_conversation: true,
      conversation_init_option: {need_ack_conversation: true},
      regen_query_id: [],
      edit_query_id: [],
      regen_instruction: "",
      no_replace_for_regen: false,
      message_from: 0,
      shared_app_name: "",
      shared_app_id: "",
      sse_recv_event_options: {support_chunk_delta: true},
      is_ai_playground: false,
      is_old_user: true,
      recovery_option: {
        is_recovery: false,
        req_create_time_sec: Math.floor(Date.now() / 1000),
        append_sse_event_scene: 0
      },
      message_storage_type: 0
    },
    chat_ability: {
      ability_type: 17,
      ability_param: JSON.stringify({ratio: ratio || "auto", model, duration: seconds})
    },
    user_context: [],
    ext: {
      answer_with_suggest: "0",
      sub_conv_firstmet_type: "1",
      collection_id: "",
      conversation_init_option: JSON.stringify({need_ack_conversation: true}),
      commerce_credit_config_enable: "0"
    }
  };
  history.pushState({}, "", `/chat/${localConversationId}`);
  const response = await fetch(requestUrl, {
    method: "POST",
    credentials: "include",
    headers: {
      accept: "*/*",
      "agw-js-conv": "str, str",
      "content-type": "application/json",
      "last-event-id": "undefined"
    },
    body: JSON.stringify(payload)
  });
  let text = "";
  let timedOut = false;
  const reader = response.body && response.body.getReader ? response.body.getReader() : null;
  if (reader) {
    const decoder = new TextDecoder("utf-8");
    const deadline = Date.now() + 90000;
    for (;;) {
      const remain = Math.max(1, deadline - Date.now());
      const item = await Promise.race([
        reader.read(),
        new Promise(resolve => setTimeout(() => resolve({timeout: true}), remain))
      ]);
      if (item.timeout) {
        timedOut = true;
        break;
      }
      const {done, value} = item;
      if (done) break;
      text += decoder.decode(value, {stream: true});
      if (text.includes("710022002") || text.includes("710022004") || text.includes("STREAM_ERROR") || text.includes("SSE_REPLY_END")) break;
    }
    try { await reader.cancel(); } catch (_) {}
    try { text += decoder.decode(); } catch (_) {}
  } else {
    text = await response.text();
  }
  const decodedText = text.replace(/\\u0026/g, "&").replace(/\\\//g, "/");
  const conversationId = extract([
    /"(?:conversation_id|conversationId|conversationID|conv_id|convId)"\s*:\s*"?(\d{15,24})"?/,
    /(?:conversation_id|conversationId|conversationID|conv_id|convId)(?:\\?"|)\s*[:=]\s*(?:\\?")?(\d{15,24})/
  ], text);
  const videoUrl = extract([
    /(https?:\/\/[^"\\\s]+(?:mime_type=video_mp4|\.mp4(?:\?[^"\\\s]*)?))/i
  ], decodedText);
  const preview = text.length <= 6000 ? text : `${text.slice(0, 3000)}\n...[truncated]...\n${text.slice(-3000)}`;
  return {
    ok: response.ok,
    status: response.status,
    response_preview: preview,
    conversation_id: conversationId,
    local_conversation_id: localConversationId,
    video_url: videoUrl,
    accepted: text.includes("SSE_REPLY_END") && !text.includes("STREAM_ERROR"),
    service_frequent: text.includes("710022002") || text.includes("当前服务访问频繁") || text.includes("服务访问频繁"),
    slider_verification: text.includes("710022004") || text.includes('"type":"verify"') || text.includes('"verify_scene":"doubao_message_web"'),
    stream_error: text.includes("STREAM_ERROR"),
    sse_timed_out: timedOut
  };
}
"""


class DoubaoVideoAutomation:
    def __init__(
        self,
        task_id: str,
        prompt: str,
        ratio: str,
        model: str,
        duration: int = 10,
        account: dict[str, Any] | None = None,
        proxy_session: Any | None = None,
        browser_pool: ReusableBrowserPool | None = None,
    ):
        self.task_id = task_id
        self.prompt = prompt
        self.ratio = ratio
        self.model = model
        self.duration = max(1, int(duration or 10))
        self.account = account or {}
        self.proxy_session = proxy_session
        self.browser_pool = browser_pool
        self.settings = load_settings()
        ensure_dirs()
        self.state_path = DOUBAO_STATES_DIR / f"{str(self.account.get('id') or 'unknown')}.json"

    async def _refresh_cookies(self, context) -> None:
        account_id = str(self.account.get("id") or "")
        if not account_id:
            return
        cookies = await context.cookies(["https://www.doubao.com"])
        if cookies:
            update_account_cookies(account_id, cookies)
        await context.storage_state(path=str(self.state_path))

    async def run(self) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(self._run_once(), timeout=max(self.settings.task_timeout_seconds, 600))
        except asyncio.TimeoutError:
            if task_exists(self.task_id):
                mark_pending(self.task_id, "doubao browser timeout")
            return {"success": False, "retryable": True, "reason": "doubao browser timeout"}
        except Exception as exc:
            if all(hasattr(exc, name) for name in ("retry_after", "queue_reason", "queue_category")):
                return {
                    "success": False,
                    "retryable": True,
                    "reason": str(exc),
                    "infrastructure_fault": True,
                    "defer_only": True,
                    "retry_after": max(1, int(exc.retry_after)),
                    "defer_reason": str(exc.queue_reason),
                    "defer_category": str(exc.queue_category),
                }
            reason = str(exc)[:500]
            if task_exists(self.task_id):
                mark_pending(self.task_id, reason)
            return {
                "success": False,
                "retryable": True,
                "reason": reason,
                "infrastructure_fault": self.proxy_session is not None,
            }

    async def _run_once(self) -> dict[str, Any]:
        if not task_exists(self.task_id):
            return {"success": True, "retryable": False, "reason": ""}
        clear_transient_result(self.task_id)
        if not self.account:
            return {"success": False, "retryable": True, "reason": "no doubao account available"}
        lock = await account_profile_lock("doubao", str(self.account.get("id") or ""))
        async with lock:
            return await self._run_profile()

    async def _run_profile(self) -> dict[str, Any]:
        proxy_config = None
        proxy_acquired = False
        try:
            self._set_phase("connecting_node", "正在连接豆包生成节点")
            if self.proxy_session is not None:
                proxy_config = await self.proxy_session.acquire_browser_proxy()
                proxy_acquired = True
            if task_exists(self.task_id) and is_task_canceled(self.task_id):
                return {"success": False, "retryable": False, "reason": "用户取消生成"}
            return await self._run_browser(proxy_config)
        except Exception:
            if proxy_acquired:
                self.proxy_session.mark_browser_proxy_unavailable(reason="doubao_browser_failure")
            raise
        finally:
            if self.proxy_session is not None:
                await self.proxy_session.release_browser_proxy()

    async def _record_diagnostic(self, page, category: str, body: str = "") -> None:
        if not task_exists(self.task_id):
            return
        try:
            title = str(await page.title())[:300]
        except Exception:
            title = ""
        if not body:
            try:
                body = str(await page.locator("body").inner_text())
            except Exception:
                body = ""
        excerpt = re.sub(r"\s+", " ", body).strip()[:1200]
        save_result(
            self.task_id,
            extra={
                "doubao_diagnostic_category": category,
                "doubao_page_url": str(page.url or "")[:1000],
                "doubao_page_title": title,
                "doubao_page_excerpt": excerpt,
            },
        )

    def _set_phase(self, phase: str, status_reason: str) -> None:
        if task_exists(self.task_id):
            set_execution_phase(self.task_id, phase, status_reason)

    async def _observe_service_frequent(
        self,
        page: Page,
        *,
        seconds: float = 15.0,
    ) -> str:
        self._set_phase("checking_account_risk", "正在确认豆包账号登录状态")
        deadline = asyncio.get_running_loop().time() + max(0.0, seconds)
        while True:
            try:
                body = await page.locator("body").inner_text()
                if await self._login_required(page, body):
                    save_result(self.task_id, extra={"doubao_service_frequent_state": "login_invalid"})
                    return "login_invalid"
            except Exception as exc:
                save_result(self.task_id, extra={"doubao_service_frequent_check_error": str(exc)[:300]})

            if asyncio.get_running_loop().time() >= deadline:
                save_result(self.task_id, extra={"doubao_service_frequent_state": "service_frequent"})
                return "service_frequent"
            await page.wait_for_timeout(500)

    @staticmethod
    def _is_region_restricted(page_url: str, body: str) -> bool:
        haystack = f"{page_url}\n{body[:3000]}".lower()
        return any(marker.lower() in haystack for marker in REGION_RESTRICTION_MARKERS)

    @staticmethod
    async def _login_required(page, body: str) -> bool:
        user_visible = await page.get_by_text(re.compile(r"^用户\d+$")).count()
        if user_visible:
            return False
        login_button = page.get_by_role("button", name=re.compile(r"^登录(?:豆包)?$"))
        login_link = page.get_by_role("link", name=re.compile(r"^登录(?:豆包)?$"))
        if await login_button.count() or await login_link.count():
            return True
        return any(marker in body[:2000] for marker in ("扫码登录", "手机号登录", "登录豆包"))

    async def _run_browser(self, proxy_config: dict[str, str] | None) -> dict[str, Any]:
        runtime = self.browser_pool.playwright_context() if self.browser_pool is not None else async_playwright()
        async with runtime as playwright:
            browser: Browser | None = None
            context: BrowserContext | None = None
            lease: BrowserContextLease | None = None
            page = None
            try:
                self._set_phase("starting_browser", "正在启动豆包生成环境")
                executable_path = resolve_browser_executable(self.settings.browser_executable_path)
                browser_args = ["--disable-dev-shm-usage", "--no-sandbox", "--disable-blink-features=AutomationControlled"]
                context_options: dict[str, Any] = {
                    "locale": "zh-CN",
                    "viewport": {"width": 1365, "height": 900},
                    "user_agent": BROWSER_USER_AGENT,
                    "extra_http_headers": BROWSER_EXTRA_HTTP_HEADERS,
                    "accept_downloads": False,
                }
                if self.state_path.is_file():
                    context_options["storage_state"] = str(self.state_path)
                if self.browser_pool is not None:
                    self._set_phase("allocating_browser", "正在分配豆包浏览器资源")
                    lease = await self.browser_pool.acquire_context(
                        executable_path=executable_path,
                        headless=self.settings.headless,
                        proxy=proxy_config,
                        browser_args=browser_args,
                        context_options=context_options,
                    )
                    browser = lease.browser
                    context = lease.context
                else:
                    self._set_phase("launching_browser", "正在启动豆包浏览器")
                    browser = await playwright.chromium.launch(
                        headless=self.settings.headless,
                        executable_path=executable_path,
                        proxy=proxy_config,
                        args=browser_args,
                    )
                    context = await browser.new_context(**context_options)
                await context.add_init_script(BROWSER_INIT_SCRIPT)
                page = context.pages[0] if context.pages else await context.new_page()
                cookies = [dict(item) for item in self.account.get("cookies") or [] if isinstance(item, dict) and item.get("name")]
                if cookies:
                    await context.add_cookies(cookies)
                self._set_phase("opening_generation_page", "正在打开豆包生成页面")
                await page.goto(DOUBAO_URL, wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(5000)
                body = await page.locator("body").inner_text()
                if self._is_region_restricted(str(page.url), body):
                    await self._record_diagnostic(page, "doubao_region_restricted", body)
                    if self.proxy_session is not None:
                        self.proxy_session.mark_browser_proxy_unavailable(reason="doubao_region_restricted")
                    return {
                        "success": False,
                        "retryable": True,
                        "reason": "doubao region restricted",
                        "infrastructure_fault": True,
                    }
                if await self._login_required(page, body):
                    await self._record_diagnostic(page, "doubao_login_invalid", body)
                    disable_account_for_login(str(self.account.get("id") or ""), "豆包登录状态失效，请重新导入 Cookie")
                    return {
                        "success": False,
                        "retryable": True,
                        "reason": "doubao account not logged in",
                        "account_fault": True,
                        "account_login_invalid": True,
                        "switch_account": True,
                    }
                await self._refresh_cookies(context)
                model_code = DOUBAO_MODEL_CODES.get(self.model)
                if not model_code:
                    return {"success": False, "retryable": False, "reason": "doubao model unavailable"}
                if not begin_task_submission(self.task_id):
                    canceled = is_task_canceled(self.task_id)
                    return {"success": False, "retryable": not canceled, "reason": "用户取消生成" if canceled else "任务提交状态已变化，正在重试"}
                self._set_phase("submitting_request", "正在提交豆包生成请求")
                completion_result = await page.evaluate(
                    DOUBAO_SUBMIT_SCRIPT,
                    {
                        "prompt": self.prompt,
                        "ratio": self.ratio or "auto",
                        "model": model_code,
                        "duration": self.duration,
                    },
                )
                if not isinstance(completion_result, dict):
                    release_task_submission(self.task_id)
                    return {"success": False, "retryable": True, "reason": "doubao submission returned an invalid response"}

                error = ""
                category = ""
                if completion_result.get("service_frequent"):
                    error = "doubao service frequent"
                    category = "service_frequent"
                    set_account_cooldown(str(self.account.get("id") or ""), 1800, "豆包当前服务访问频繁")
                elif completion_result.get("slider_verification"):
                    error = "doubao verification required"
                    category = "slider_verification"
                elif completion_result.get("stream_error"):
                    error = "doubao submit rejected"
                    category = "submit_rejected"
                    set_account_cooldown(str(self.account.get("id") or ""), 1800, "豆包提交被拒绝")
                elif not completion_result.get("ok"):
                    error = f"doubao submit http {int(completion_result.get('status') or 0)}"
                    category = "http_error"
                elif completion_result.get("sse_timed_out") and not completion_result.get("accepted"):
                    error = "doubao submit not confirmed"
                    category = "confirmation_timeout"

                if category == "service_frequent":
                    risk_state = await self._observe_service_frequent(page)
                    if risk_state == "login_invalid":
                        error = "doubao account not logged in"
                        category = "login_invalid"
                await self._refresh_cookies(context)
                self._set_phase("submission_received", "豆包生成请求已接收，正在确认状态")
                if error:
                    release_task_submission(self.task_id)
                    save_result(self.task_id, extra={
                        "doubao_submit_error_category": category,
                        "doubao_submission_response_preview": str(completion_result.get("response_preview") or "")[:6000],
                    })
                    if category == "service_frequent":
                        if self.proxy_session is not None:
                            self.proxy_session.mark_browser_proxy_unavailable(reason="doubao_service_frequent")
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": error,
                            "infrastructure_fault": True,
                        }
                    if category == "slider_verification":
                        set_account_cooldown(str(self.account.get("id") or ""), 86400, "豆包触发网页人机验证")
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": error,
                            "account_fault": True,
                            "account_slider_verification": True,
                            "switch_account": True,
                        }
                    if category == "login_invalid":
                        disable_account_for_login(str(self.account.get("id") or ""), "豆包登录状态失效，请重新导入 Cookie")
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": error,
                            "account_fault": True,
                            "account_login_invalid": True,
                            "switch_account": True,
                        }
                    return {"success": False, "retryable": True, "reason": error}
                if not completion_result.get("accepted") and not completion_result.get("video_url"):
                    release_task_submission(self.task_id)
                    return {"success": False, "retryable": True, "reason": "doubao submit not accepted"}
                mark_submitted(self.task_id)
                save_result(
                    self.task_id,
                    extra={
                        "platform": "doubao",
                        "model": self.model,
                        "account_id": str(self.account.get("id") or ""),
                        "account_name": str(self.account.get("name") or ""),
                        "account_quota_charge_id": str(self.account.get("quota_charge_id") or ""),
                        "doubao_page_url": page.url,
                        "doubao_submit_confirmed": bool(completion_result.get("accepted")),
                        "doubao_conversation_id": str(completion_result.get("conversation_id") or ""),
                    },
                )
                conversation_id = str(completion_result.get("conversation_id") or "")
                if conversation_id:
                    try:
                        await page.goto(f"https://www.doubao.com/chat/{conversation_id}", wait_until="domcontentloaded", timeout=90000)
                        await page.wait_for_timeout(3000)
                    except Exception as exc:
                        save_result(self.task_id, extra={"doubao_conversation_open_error": str(exc)[:500]})
                deadline = asyncio.get_running_loop().time() + 240
                self._set_phase("waiting_result", "豆包正在生成视频")
                while asyncio.get_running_loop().time() < deadline:
                    if completion_result.get("video_url"):
                        url = str(completion_result.get("video_url") or "")
                        await self._refresh_cookies(context)
                        save_result(self.task_id, extra={"decoded_main_url": url, "doubao_page_url": page.url})
                        mark_success(self.task_id)
                        return {"success": True, "retryable": False, "reason": ""}
                    videos = page.locator("video")
                    count = await videos.count()
                    for index in range(count):
                        src = str(await videos.nth(index).get_attribute("src") or "")
                        if src.startswith("http"):
                            await self._refresh_cookies(context)
                            save_result(self.task_id, extra={"decoded_main_url": src, "doubao_page_url": page.url})
                            mark_success(self.task_id)
                            return {"success": True, "retryable": False, "reason": ""}
                    links = await page.locator('a[href*="video"],a[href$=".mp4"],a[download]').evaluate_all("els => els.map(e => e.href).filter(Boolean)")
                    for url in links:
                        if str(url).startswith("http"):
                            await self._refresh_cookies(context)
                            save_result(self.task_id, extra={"decoded_main_url": str(url), "doubao_page_url": page.url})
                            mark_success(self.task_id)
                            return {"success": True, "retryable": False, "reason": ""}
                    text = await page.locator("body").inner_text()
                    if any(marker in text[-1500:] for marker in ("生成失败", "无法生成", "内容违规")):
                        return {"success": False, "retryable": True, "reason": "doubao generation failed"}
                    await page.wait_for_timeout(10000)
                await self._refresh_cookies(context)
                return {"success": False, "retryable": True, "reason": "doubao video result timeout"}
            finally:
                if lease is not None:
                    await bounded_cleanup(lease.release())
                else:
                    await bounded_cleanup(safe_close(context))
                    await bounded_cleanup(safe_close(browser))
