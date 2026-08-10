from __future__ import annotations

import asyncio
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import string
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncContextManager, Awaitable, Callable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .account_proxies import account_browser_config, account_proxy_candidates, account_proxy_configured, account_proxy_url
from .api_proxy_pool import ApiProxyLease, ReusableApiProxyPool
from .browser_runtime import BROWSER_EXTRA_HTTP_HEADERS, BROWSER_INIT_SCRIPT, BROWSER_USER_AGENT, BrowserContextLease, ReusableBrowserPool, resolve_browser_executable, safe_close, safe_unroute_all
from .config import TARGET_URL, browser_proxy_config_for, load_settings
from .proxy_manager import (
    acquire_dola_subscription_proxy,
    acquire_authenticated_socks_proxy,
    dola_proxy_available,
    fetch_proxy_from_api,
    mark_node_unavailable,
    mark_proxy_source_available,
    mark_proxy_source_unavailable,
    node_retry_after,
    proxy_exit_identity,
    proxy_source_available,
    proxy_source_retry_after,
    record_node_doubao_success,
    record_node_doubao_verification,
    record_node_gateway_failure,
    record_node_success,
    release_dola_subscription_proxy,
    release_task_mihomo_proxy,
)
from .store import (
    begin_task_submission,
    clear_transient_result,
    load_result,
    mark_success,
    mark_submitted,
    release_task_submission,
    is_task_canceled,
    save_result,
    set_execution_phase,
    task_exists,
    task_image_paths,
    get_meta,
    update_meta,
)
from .reference_images import prepare_task_reference_images
from .slider_solver import SliderChallengeSolver, SliderSolveResult, SliderSolverSettings, find_slider_page


REGION_RESTRICTED_URL = "https://www.dola.com/security/region-restricted?source=1"
IMAGEX_REGION = "us-east-1"
IMAGEX_SERVICE = "imagex"
IMAGEX_API_VERSION = "2018-08-01"
PREPARE_UPLOAD_BODY = {"tenant_id": "5", "scene_id": "4", "resource_type": 2}
SUBMISSION_SECRET_RE = re.compile(
    r'(?i)("?(?:authorization|cookie|msToken|oauth_token(?:_v2)?|sessionid|sid_tt|sid_guard|odin_tt|passport_csrf_token(?:_default)?)"?\s*[:=]\s*"?)([^"\s,;}]+)'
)
FINAL_FAILURE_TEXT = "无法生成该视频，请尝试降低配置后重试。"
SERVICE_FREQUENT_OBSERVE_SECONDS = 15.0
SERVICE_FREQUENT_POLL_INTERVAL_MS = 500
SERVICE_FREQUENT_EVALUATE_TIMEOUT_SECONDS = 3.0
SERVICE_FREQUENT_INSPECTION_TIMEOUT_SECONDS = 20.0
SERVICE_FREQUENT_RELOAD_TIMEOUT_SECONDS = 10.0
DOLA_SUBMIT_TIMEOUT_SECONDS = 75.0
DOLA_SUBMIT_WITH_REFERENCES_TIMEOUT_SECONDS = 135.0
SLIDER_RECOVERY_SUBMIT_ATTEMPTS = 2
SLIDER_RECOVERY_OBSERVE_SECONDS = 15.0
PROXY_TRANSPORT_COOLDOWN_SECONDS = 60

DOUBAO_PROXY_TIMEZONES = {
    "jp": "Asia/Tokyo",
    "japan": "Asia/Tokyo",
    "日本": "Asia/Tokyo",
    "hk": "Asia/Hong_Kong",
    "hong kong": "Asia/Hong_Kong",
    "香港": "Asia/Hong_Kong",
    "sg": "Asia/Singapore",
    "singapore": "Asia/Singapore",
    "新加坡": "Asia/Singapore",
    "tw": "Asia/Taipei",
    "taiwan": "Asia/Taipei",
    "台湾": "Asia/Taipei",
    "台湾省": "Asia/Taipei",
}


def doubao_proxy_timezone_id(countries: Any) -> str:
    selected = [str(item or "").strip().lower() for item in countries or () if str(item or "").strip()]
    if len(selected) != 1:
        return ""
    return DOUBAO_PROXY_TIMEZONES.get(selected[0], "")


def dola_service_frequent_abnormal_outcome(state: str) -> dict[str, Any]:
    return {
        "success": False,
        "retryable": True,
        "reason": f"service frequent (risk check: {str(state or 'service_frequent')})",
        "account_fault": True,
        "account_login_invalid": True,
        "switch_account": True,
    }


SERVICE_FREQUENT_ACCOUNT_STATE_SCRIPT = r"""
() => {
  const visible = element => {
    if (!element) return false;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  };
  const bodyText = String(document.body && document.body.textContent || "").slice(0, 12000).replace(/\s+/g, " ").trim();
  const sliderSelectors = [
    'iframe[src*="captcha"]',
    'iframe[src*="bdcaptcha"]',
    'iframe[src*="verify"]',
    '[class*="captcha"]',
    '[id*="captcha"]',
    '[class*="slide-verify"]',
    '[class*="slider-verify"]',
    '[class*="captcha-slider"]',
    '.captcha-slider-btn',
    '[class*="secsdk-captcha"]'
  ];
  const visibleSliderSelectors = sliderSelectors.filter(selector => Array.from(document.querySelectorAll(selector)).some(visible));
  const sliderSelector = visibleSliderSelectors.length > 0;
  const sliderText = /拖动滑块|向右拖动|按住滑块|完成拼图|请完成验证|安全验证|验证后继续|滑块验证/.test(bodyText);
  const loginControl = Array.from(document.querySelectorAll(
    'button,a,[role="button"],[role="link"],input[type="button"],input[type="submit"]'
  )).some(element => {
    const text = String(element.value || element.textContent || "").replace(/\s+/g, "").trim();
    return visible(element) && ["登录", "登录/注册", "注册/登录"].includes(text);
  });
  const href = String(location.href || "");
  const loginText = /游客模式|请登录后再试|登录后再试|登录状态失效|账号已退出|请重新登录/.test(bodyText);
  const loginUrl = /(?:passport|\/login(?:[/?#]|$)|login\.dola)/i.test(href);
  return {
    href,
    bodyText: bodyText.slice(0, 2000),
    sliderVerification: sliderSelector || sliderText,
    loginInvalid: loginText || loginUrl || loginControl,
    riskEvidence: sliderSelector
      ? `slider-selector:${visibleSliderSelectors.join(",")}`
      : sliderText
        ? "slider-text"
        : loginUrl
          ? "login-url"
          : loginText
            ? "login-text"
            : loginControl
              ? "login-control"
              : "none"
  };
}
"""
INFRASTRUCTURE_ERROR_MARKERS = (
    "mihomo ",
    "all connection attempts failed",
    "failed to fetch",
    "proxy connection",
    "proxy node",
    "proxy subscription",
    "connection refused",
    "connection reset",
    "connection closed",
    "server disconnected",
    "unexpected eof",
    "eof occurred in violation",
    "ssl:",
    "tlsv1_alert",
    "tls handshake",
    "net::err_proxy",
    "net::err_connection",
    "net::err_timed_out",
    "net::err_http2_protocol_error",
    "net::err_tunnel_connection_failed",
    "execution context was destroyed",
    "most likely because of a navigation",
    "cannot find context with specified id",
    "frame was detached",
    "target page, context or browser has been closed",
    "target page has been closed",
    "browser timeout",
    "submission transport timeout",
    "all configured proxy modes are unavailable",
    "all selected authenticated proxies are unavailable",
    "all eligible proxy nodes are unavailable",
)


def _submission_response_preview(value: Any) -> str:
    text = str(value or "").replace("\x00", "")[:6000]
    return SUBMISSION_SECRET_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", text)


def _environment_bool(name: str, default: bool) -> bool:
    value = str(os.environ.get(name) or "").strip().lower()
    if not value:
        return default
    return value not in {"0", "false", "no", "off"}


def _environment_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _environment_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(str(os.environ.get(name) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


PROXY_TRANSPORT_ERROR_MARKERS = (
    "mihomo ",
    "failed to fetch",
    "proxy connection",
    "proxy node",
    "proxy subscription",
    "connection refused",
    "connection reset",
    "connection closed",
    "server disconnected",
    "unexpected eof",
    "eof occurred in violation",
    "ssl:",
    "tlsv1_alert",
    "tls handshake",
    "net::err_ssl",
    "net::err_proxy",
    "net::err_connection",
    "net::err_timed_out",
    "net::err_http2_protocol_error",
    "net::err_tunnel_connection_failed",
    "submission transport timeout",
)
API_PROXY_FATAL_TRANSPORT_ERROR_MARKERS = (
    "net::err_ssl_protocol_error",
    "net::err_http2_protocol_error",
    "net::err_tunnel_connection_failed",
)
REFERENCE_UPLOAD_ERROR_MARKERS = (
    "prepare_upload",
    "applyimageupload",
    "direct image upload",
    "commitimageupload",
    "image upload",
    "upload address",
    "upload config",
)
REFERENCE_CACHE_TTL_SECONDS = max(60, min(86400, int(os.environ.get("DOLA_REFERENCE_CACHE_TTL_SECONDS") or 1800)))
REFERENCE_CACHE_MAX_ENTRIES = max(16, min(4096, int(os.environ.get("DOLA_REFERENCE_CACHE_MAX_ENTRIES") or 512)))
try:
    PREPARE_UPLOAD_TIMEOUT_SECONDS = max(
        30.0,
        min(120.0, float(os.environ.get("DOLA_PREPARE_UPLOAD_TIMEOUT_SECONDS") or 60.0)),
    )
except (TypeError, ValueError):
    PREPARE_UPLOAD_TIMEOUT_SECONDS = 60.0
REFERENCE_IMAGE_UPLOAD_TIMEOUT_SECONDS = 120.0
REFERENCE_IMAGE_UPLOAD_TRANSPORT_ATTEMPTS = 3
PREPARE_UPLOAD_TRANSPORT_ATTEMPTS = 3
REFERENCE_CACHE_WAIT_TIMEOUT_SECONDS = 130.0
API_PROXY_CANDIDATE_LIMIT = 3
API_PROXY_FETCH_LIMIT = 6
API_PROXY_FETCH_ERROR_LIMIT = 3
API_PROXY_RETRY_AFTER_SECONDS = 5
_REFERENCE_CACHE_LOCK = threading.RLock()
_REFERENCE_ATTACHMENT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_REFERENCE_UPLOADS_IN_FLIGHT: dict[str, asyncio.Future[dict[str, Any] | None]] = {}


class ProxyCoolingDownError(RuntimeError):
    def __init__(
        self,
        retry_after: int,
        reason: str = "proxy modes are temporarily cooling down",
        queue_reason: str = "生成节点冷却中，任务已自动排队",
        queue_category: str = "proxy_cooldown",
    ):
        self.retry_after = max(1, int(retry_after))
        self.queue_reason = str(queue_reason or "服务连接异常，任务已自动排队")[:120]
        self.queue_category = str(queue_category or "infrastructure")[:40]
        super().__init__(str(reason or "proxy modes are temporarily cooling down"))


class ReferenceUploadCapacityError(RuntimeError):
    def __init__(self, retry_after: int = 5):
        self.retry_after = max(1, int(retry_after))
        super().__init__("reference image upload capacity is busy")


def is_final_generation_failure(text: str) -> bool:
    return FINAL_FAILURE_TEXT in str(text or "")


def is_infrastructure_failure(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    return any(marker in normalized for marker in INFRASTRUCTURE_ERROR_MARKERS)


def exception_reason(exc: BaseException) -> str:
    detail = str(exc).strip()
    return detail[:500] if detail else type(exc).__name__


def is_proxy_transport_failure(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    return any(marker in normalized for marker in PROXY_TRANSPORT_ERROR_MARKERS)


def is_api_proxy_fatal_transport_failure(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    return any(marker in normalized for marker in API_PROXY_FATAL_TRANSPORT_ERROR_MARKERS)


def is_reference_upload_failure(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    return any(marker in normalized for marker in REFERENCE_UPLOAD_ERROR_MARKERS)


def is_reference_upload_phase(phase: str) -> bool:
    normalized = str(phase or "").strip().lower()
    return normalized == "preparing_references" or normalized == "waiting_image_upload_slot" or normalized.startswith((
        "uploading_reference_",
        "waiting_reference_",
    ))


def reference_image_cache_key(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reference_attachment_account_scope(account: dict[str, Any]) -> str:
    account_id = str(account.get("id") or "").strip()
    if not account_id:
        return ""
    cookies = account.get("cookies") if isinstance(account.get("cookies"), list) else []
    cookie_values = sorted(
        (
            str(item.get("name") or ""),
            str(item.get("value") or ""),
            str(item.get("domain") or ""),
            str(item.get("path") or ""),
        )
        for item in cookies
        if isinstance(item, dict) and item.get("name")
    )
    if not cookie_values:
        return account_id
    session_key = hashlib.sha256(json.dumps(cookie_values, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"{account_id}:{session_key}"


def reference_attachment_cache_key(account_scope: str, image_key: str) -> str:
    """Scope uploaded Dola attachments to the account that created them."""
    normalized_account = str(account_scope or "").strip()
    normalized_image = str(image_key or "").strip()
    if not normalized_account or not normalized_image:
        return ""
    return hashlib.sha256(f"{normalized_account}:{normalized_image}".encode("utf-8")).hexdigest()


def cached_reference_attachment(key: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _REFERENCE_CACHE_LOCK:
        expired = [item for item, (expires_at, _) in _REFERENCE_ATTACHMENT_CACHE.items() if expires_at <= now]
        for item in expired:
            _REFERENCE_ATTACHMENT_CACHE.pop(item, None)
        cached = _REFERENCE_ATTACHMENT_CACHE.get(str(key or ""))
        return dict(cached[1]) if cached else None


def cache_reference_attachment(key: str, attachment: dict[str, Any]) -> None:
    normalized = str(key or "")
    if not normalized or not attachment.get("uri"):
        return
    with _REFERENCE_CACHE_LOCK:
        _REFERENCE_ATTACHMENT_CACHE[normalized] = (time.monotonic() + REFERENCE_CACHE_TTL_SECONDS, dict(attachment))
        if len(_REFERENCE_ATTACHMENT_CACHE) > REFERENCE_CACHE_MAX_ENTRIES:
            oldest = sorted(_REFERENCE_ATTACHMENT_CACHE.items(), key=lambda item: item[1][0])
            for item, _ in oldest[:len(_REFERENCE_ATTACHMENT_CACHE) - REFERENCE_CACHE_MAX_ENTRIES]:
                _REFERENCE_ATTACHMENT_CACHE.pop(item, None)


def invalidate_reference_attachment_keys(keys: list[str] | tuple[str, ...]) -> None:
    with _REFERENCE_CACHE_LOCK:
        for key in keys:
            _REFERENCE_ATTACHMENT_CACHE.pop(str(key or ""), None)


def clear_reference_attachment_cache() -> None:
    with _REFERENCE_CACHE_LOCK:
        _REFERENCE_ATTACHMENT_CACHE.clear()
        _REFERENCE_UPLOADS_IN_FLIGHT.clear()


PREPARE_UPLOAD_SCRIPT = r"""
async ({body}) => {
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
    const m = document.cookie.match(new RegExp(`(?:^|; )${escaped}=([^;]*)`));
    return m ? decodeURIComponent(m[1]) : "";
  }
  function storageFind(regex) {
    const stores = [localStorage, sessionStorage];
    for (const store of stores) {
      for (let i = 0; i < store.length; i += 1) {
        const key = store.key(i);
        const value = store.getItem(key) || "";
        if (regex.test(key) && value && value.length < 100) return value;
      }
    }
    return "";
  }
  function buildQuery() {
    const fp = cookieValue("s_v_web_id") || storageFind(/s_v_web_id|fp|verify/i) || `verify_${randomDigits(12)}`;
    const id = storageFind(/web_id|tea_uuid|device_id/i).replace(/\D/g, "").slice(0, 20) || `${Date.now()}${randomDigits(6)}`;
    const region = cookieValue("flow_user_country") || "JP";
    const params = new URLSearchParams({
      aid: "495671",
      device_id: id,
      device_platform: "web",
      fp,
      language: "zh",
      pc_version: "3.23.7",
      pkg_type: "release_version",
      real_aid: "495671",
      region,
      samantha_web: "1",
      sys_region: region,
      tea_uuid: id,
      "use-olympus-account": "1",
      version_code: "20800",
      web_id: id,
      web_platform: "browser",
      web_tab_id: uuid()
    });
    const msToken = cookieValue("msToken") || storageFind(/mstoken/i);
    if (msToken) params.set("msToken", msToken);
    return params;
  }
  function trySign(url) {
    const signers = [window.byted_acrawler, window.bytedAcrawler, window.__acrawler, window.ABogus].filter(Boolean);
    for (const signer of signers) {
      try {
        if (typeof signer.sign === "function") {
          const signed = signer.sign({ url });
          if (typeof signed === "string" && signed) return signed;
          if (signed && typeof signed === "object") {
            if (typeof signed.a_bogus === "string") return signed.a_bogus;
            if (typeof signed.aBogus === "string") return signed.aBogus;
            if (typeof signed.url === "string") {
              const parsed = new URL(signed.url, location.origin);
              const v = parsed.searchParams.get("a_bogus");
              if (v) return v;
            }
          }
        }
      } catch (_) {}
    }
    return "";
  }
  const params = buildQuery();
  let requestUrl = `${location.origin}/alice/resource/prepare_upload?${params.toString()}`;
  const aBogus = trySign(requestUrl);
  if (aBogus) {
    params.set("a_bogus", aBogus);
    requestUrl = `${location.origin}/alice/resource/prepare_upload?${params.toString()}`;
  }
  let response;
  try {
    response = await fetch(requestUrl, {
      method: "POST",
      credentials: "include",
      headers: {
        "accept": "application/json, text/plain, */*",
        "agw-js-conv": "str",
        "content-type": "application/json"
      },
      body: JSON.stringify(body)
    });
  } catch (error) {
    return {
      ok: false,
      status: 0,
      text: "",
      json: null,
      networkError: true,
      errorName: String(error && error.name || "Error"),
      errorMessage: String(error && error.message || error || "Failed to fetch"),
      requestUrl,
      pageUrl: String(location.href || "")
    };
  }
  const text = await response.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch (_) {}
  return { ok: response.ok, status: response.status, text, json };
}
"""


SUBMIT_SCRIPT = r"""
async ({prompt, ratio, duration, attachments, collectionId: suppliedCollectionId, uniqueKey: suppliedUniqueKey, localConversationId: suppliedLocalConversationId}) => {
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
    const m = document.cookie.match(new RegExp(`(?:^|; )${escaped}=([^;]*)`));
    return m ? decodeURIComponent(m[1]) : "";
  }
  function storageFind(regex) {
    const stores = [localStorage, sessionStorage];
    for (const store of stores) {
      for (let i = 0; i < store.length; i += 1) {
        const key = store.key(i);
        const value = store.getItem(key) || "";
        if (regex.test(key) && value && value.length < 100) return value;
      }
    }
    return "";
  }
  function buildQuery() {
    const fp = cookieValue("s_v_web_id") || storageFind(/s_v_web_id|fp|verify/i) || `verify_${randomDigits(12)}`;
    const id = storageFind(/web_id|tea_uuid|device_id/i).replace(/\D/g, "").slice(0, 20) || `${Date.now()}${randomDigits(6)}`;
    const region = cookieValue("flow_user_country") || "JP";
    const params = new URLSearchParams({
      aid: "495671",
      device_id: id,
      device_platform: "web",
      fp,
      language: "zh",
      pc_version: "3.23.7",
      pkg_type: "release_version",
      real_aid: "495671",
      region,
      samantha_web: "1",
      sys_region: region,
      tea_uuid: id,
      "use-olympus-account": "1",
      version_code: "20800",
      web_id: id,
      web_platform: "browser",
      web_tab_id: uuid()
    });
    const msToken = cookieValue("msToken") || storageFind(/mstoken/i);
    if (msToken) params.set("msToken", msToken);
    return params;
  }
  function trySign(url) {
    const signers = [window.byted_acrawler, window.bytedAcrawler, window.__acrawler, window.ABogus].filter(Boolean);
    for (const signer of signers) {
      try {
        if (typeof signer.sign === "function") {
          const signed = signer.sign({ url });
          if (typeof signed === "string" && signed) return signed;
          if (signed && typeof signed === "object") {
            if (typeof signed.a_bogus === "string") return signed.a_bogus;
            if (typeof signed.aBogus === "string") return signed.aBogus;
            if (typeof signed.url === "string") {
              const parsed = new URL(signed.url, location.origin);
              const v = parsed.searchParams.get("a_bogus");
              if (v) return v;
            }
          }
        }
      } catch (_) {}
    }
    return "";
  }
  function extractConversationId(text) {
    if (!text) return "";
    const patterns = [
      /"(?:conversation_id|conversationId|conversationID|conv_id|convId)"\s*:\s*"?(\d{15,24})"?/,
      /(?:conversation_id|conversationId|conversationID|conv_id|convId)(?:\\?"|)\s*[:=]\s*(?:\\?")?(\d{15,24})/,
      /\/chat\/(\d{15,24})(?:\D|$)/
    ];
    for (const re of patterns) {
      const m = text.match(re);
      if (m) return m[1];
    }
    return "";
  }
  const collectionId = suppliedCollectionId || uuid();
  const uniqueKey = suppliedUniqueKey || uuid();
  function buildPayload({localConversationId}) {
    const text = `生成视频：${prompt}${ratio ? `，${ratio}` : ""}`;
    const messages = [];
    if (attachments && attachments.length) {
      messages.push({
        local_message_id: uuid(),
        content_block: [{
          block_type: 10052,
          content: {
            attachment_block: {
              attachments: attachments.map(item => ({
                type: 1,
                identifier: item.identifier || uuid(),
                image: {
                  name: item.name || "image.png",
                  uri: item.uri,
                  image_ori: {
                    url: "",
                    width: Number(item.width || 0),
                    height: Number(item.height || 0),
                    format: "",
                    url_formats: {}
                  }
                },
                parse_state: 0,
                review_state: 1,
                upload_status: 1,
                progress: 100,
                src: ""
              }))
            },
            pc_event_block: ""
          },
          block_id: uuid(),
          parent_id: "",
          meta_info: [],
          append_fields: []
        }],
        message_status: 0
      });
    }
    messages.push({
      local_message_id: uuid(),
      content_block: [{
        block_type: 10000,
        content: {
          text_block: { text, icon_url: "", icon_url_dark: "", summary: "" },
          pc_event_block: ""
        },
        block_id: uuid(),
        parent_id: "",
        meta_info: [],
        append_fields: []
      }],
      message_status: 0
    });
    const fp = cookieValue("s_v_web_id") || storageFind(/s_v_web_id|fp|verify/i) || "";
    return {
      client_meta: {
        local_conversation_id: localConversationId,
        conversation_id: "",
        bot_id: "7339470689562525703",
        last_section_id: "",
        last_message_index: null
      },
      messages,
      option: {
        send_message_scene: "",
        create_time_ms: Date.now(),
        collect_id: collectionId,
        is_audio: false,
        answer_with_suggest: false,
        tts_switch: false,
        need_deep_think: 0,
        click_clear_context: false,
        from_suggest: false,
        is_regen: false,
        is_replace: false,
        is_from_click_option: false,
        disable_sse_cache: false,
        select_text_action: "",
        is_select_text: false,
        resend_for_regen: false,
        scene_type: 0,
        unique_key: uniqueKey,
        start_seq: 0,
        need_create_conversation: true,
        conversation_init_option: { need_ack_conversation: true },
        regen_query_id: [],
        edit_query_id: [],
        regen_instruction: "",
        no_replace_for_regen: false,
        message_from: 0,
        shared_app_name: "",
        shared_app_id: "",
        sse_recv_event_options: { support_chunk_delta: true },
        is_ai_playground: false,
        is_old_user: false,
        recovery_option: {
          is_recovery: false,
          req_create_time_sec: Math.floor(Date.now() / 1000),
          append_sse_event_scene: 0
        },
        message_storage_type: 0
      },
      chat_ability: {
        ability_type: 17,
        ability_param: JSON.stringify({ ratio, model: "seedance_v2.0", duration: Number(duration) })
      },
      user_context: [],
      ext: {
        answer_with_suggest: "0",
        fp,
        sub_conv_firstmet_type: "1",
        collection_id: collectionId,
        conversation_init_option: JSON.stringify({ need_ack_conversation: true }),
        commerce_credit_config_enable: "0"
      }
    };
  }
  const localConversationId = suppliedLocalConversationId || `local_${randomDigits(16)}`;
  history.pushState({}, "", `/chat/${localConversationId}`);
  const params = buildQuery();
  let requestUrl = `${location.origin}/chat/completion?${params.toString()}`;
  const aBogus = trySign(requestUrl);
  if (aBogus) {
    params.set("a_bogus", aBogus);
    requestUrl = `${location.origin}/chat/completion?${params.toString()}`;
  }
  const response = await fetch(requestUrl, {
    method: "POST",
    credentials: "include",
    headers: {
      "accept": "*/*",
      "agw-js-conv": "str, str",
      "content-type": "application/json",
      "last-event-id": "undefined"
    },
    body: JSON.stringify(buildPayload({localConversationId}))
  });
  let text = "";
  let serviceFrequent = false;
  let sliderVerification = false;
  let timedOut = false;
  const reader = response.body && response.body.getReader ? response.body.getReader() : null;
  if (reader) {
    const decoder = new TextDecoder("utf-8");
    const deadline = Date.now() + (attachments && attachments.length ? 120000 : 60000);
    for (;;) {
      const remain = Math.max(1, deadline - Date.now());
      const timer = new Promise(resolve => setTimeout(() => resolve({timeout: true}), remain));
      const item = await Promise.race([reader.read(), timer]);
      if (item.timeout) {
        timedOut = true;
        break;
      }
      const {done, value} = item;
      if (done) break;
      const chunk = decoder.decode(value, {stream: true});
      text += chunk;
      serviceFrequent = text.includes("服务访问频繁") || text.includes("当前服务访问频繁");
      sliderVerification = text.includes("710022004") || /rate limited/i.test(text) && /(?:shark_admin|subtype\\?\"?:\\?\"?slide)/i.test(text);
      if (sliderVerification) break;
    }
    try { await reader.cancel(); } catch (_) {}
    try { text += decoder.decode(); } catch (_) {}
  } else {
    text = await response.text();
  }
  serviceFrequent = serviceFrequent || text.includes("服务访问频繁") || text.includes("当前服务访问频繁") || text.includes("710022002");
  sliderVerification = sliderVerification || text.includes("710022004") || /rate limited/i.test(text) && /(?:shark_admin|subtype\\?\"?:\\?\"?slide)/i.test(text);
  const countryRestricted = text.includes("所在的国家/地区不可用") || text.includes("country restricted");
  const conversationId = extractConversationId(text);
  const responsePreview = text.length <= 6000
    ? text
    : `${text.slice(0, 3000)}\n...[truncated]...\n${text.slice(-3000)}`;
  return {
    ok: response.ok,
    status: response.status,
    contentType: response.headers.get("content-type") || "",
    responseBytes: text.length,
    responsePreview,
    conversation_id: conversationId,
    local_conversation_id: localConversationId,
    collection_id: collectionId,
    unique_key: uniqueKey,
    submitted_with_images: Boolean(attachments && attachments.length),
    sse_timed_out: timedOut,
    slider_verification: sliderVerification,
    service_frequent: serviceFrequent,
    country_restricted: countryRestricted,
    location_href: location.href
  };
}
"""


def _random_base36(length: int = 11) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _mime_from_path(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "image/png"


def _file_extension_for_upload(path: Path) -> str:
    return path.suffix.lower() or ".png"


def _sha256_hex(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _hmac_sha256(key: str | bytes, value: str, *, hex_digest: bool = False) -> str | bytes:
    if isinstance(key, str):
        key = key.encode("utf-8")
    digest = hmac.new(key, value.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest() if hex_digest else digest.digest()


def _aws_encode(value: str) -> str:
    return quote(str(value), safe="-_.~")


def _canonical_query_string(raw_url: str) -> str:
    parsed = urlsplit(raw_url)
    pairs = [(_aws_encode(key), _aws_encode(value)) for key, value in parse_qsl(parsed.query, keep_blank_values=True)]
    pairs.sort()
    return "&".join(f"{key}={value}" for key, value in pairs)


def _amz_date_parts() -> tuple[str, str]:
    amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return amz_date, amz_date[:8]


def _normalize_upload_credentials(token: dict[str, Any]) -> dict[str, str]:
    credentials = {
        "access_key_id": token.get("access_key") or token.get("accessKeyId") or token.get("AccessKeyId") or token.get("AccessKeyID"),
        "secret_access_key": token.get("secret_key") or token.get("secretAccessKey") or token.get("SecretAccessKey"),
        "session_token": token.get("session_token") or token.get("sessionToken") or token.get("SessionToken"),
    }
    if not all(credentials.values()):
        raise RuntimeError("prepare_upload did not return complete upload credentials")
    return {key: str(value) for key, value in credentials.items()}


def _sign_imagex_request(
    *,
    method: str,
    raw_url: str,
    credentials: dict[str, str],
    body: str = "",
    include_payload_hash: bool = False,
) -> dict[str, str]:
    parsed = urlsplit(raw_url)
    amz_date, date_stamp = _amz_date_parts()
    payload_hash = _sha256_hex(body)
    canonical_headers_map = {
        "x-amz-date": amz_date,
        "x-amz-security-token": credentials["session_token"],
    }
    if include_payload_hash:
        canonical_headers_map["x-amz-content-sha256"] = payload_hash

    signed_header_names = sorted(canonical_headers_map)
    canonical_headers = "".join(
        f"{name}:{' '.join(str(canonical_headers_map[name]).strip().split())}\n"
        for name in signed_header_names
    )
    signed_headers = ";".join(signed_header_names)
    canonical_request = "\n".join(
        [
            method.upper(),
            parsed.path or "/",
            _canonical_query_string(raw_url),
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    credential_scope = f"{date_stamp}/{IMAGEX_REGION}/{IMAGEX_SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            _sha256_hex(canonical_request),
        ]
    )
    date_key = _hmac_sha256(f"AWS4{credentials['secret_access_key']}", date_stamp)
    region_key = _hmac_sha256(date_key, IMAGEX_REGION)
    service_key = _hmac_sha256(region_key, IMAGEX_SERVICE)
    signing_key = _hmac_sha256(service_key, "aws4_request")
    signature = _hmac_sha256(signing_key, string_to_sign, hex_digest=True)
    headers = {
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={credentials['access_key_id']}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "X-Amz-Date": amz_date,
        "x-amz-security-token": credentials["session_token"],
    }
    if include_payload_hash:
        headers["X-Amz-Content-Sha256"] = payload_hash
    return headers


def _json_compact(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


async def _fetch_json(client: httpx.AsyncClient, url: str, *, label: str, **kwargs: Any) -> tuple[dict[str, Any], httpx.Response]:
    response = await client.request(url=url, **kwargs)
    text = response.content.decode("utf-8-sig", errors="replace")
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"{label} failed with HTTP {response.status_code}: {text[:500]}")
    try:
        data = json.loads(text) if text else {}
    except Exception as exc:
        raise RuntimeError(f"{label} returned non-json response: {text[:500]}") from exc
    return data, response


async def _bounded_cleanup(awaitable: Awaitable[Any], timeout_seconds: float = 12.0) -> None:
    try:
        await asyncio.wait_for(awaitable, timeout=max(0.1, float(timeout_seconds)))
    except Exception:
        pass


class DolaFetchAutomation:
    def __init__(
        self,
        task_id: str,
        prompt: str,
        ratio: str,
        duration: int | None = None,
        account: dict[str, Any] | None = None,
        browser_pool: ReusableBrowserPool | None = None,
        api_proxy_pool: ReusableApiProxyPool | None = None,
        submission_pacer: Callable[[str], Awaitable[None]] | None = None,
        image_upload_slot: Callable[[], AsyncContextManager[None]] | None = None,
        prepare_upload_slot: Callable[[], AsyncContextManager[None]] | None = None,
        image_preparation_done: Callable[[], None] | None = None,
        proxy_platform: str = "dola",
    ):
        self.task_id = task_id
        self.prompt = prompt
        self.ratio = ratio
        self.duration = int(duration or 0)
        self.account = account or {}
        self.browser_pool = browser_pool
        self.api_proxy_pool = api_proxy_pool
        self.api_proxy_lease: ApiProxyLease | None = None
        self.submission_pacer = submission_pacer
        self.image_upload_slot = image_upload_slot
        self.prepare_upload_slot = prepare_upload_slot
        self.image_preparation_done = image_preparation_done
        self.proxy_platform = str(proxy_platform or "dola").strip().lower()
        if self.proxy_platform not in {"dola", "doubao", "qianwen"}:
            self.proxy_platform = "dola"
        self.settings = load_settings()
        self.uploaded_images: list[dict[str, Any]] = []
        self.proxy_node_id = ""
        self.active_proxy_source = ""
        self.active_proxy_server = ""
        self.reference_upload_stage = ""
        self.reference_upload_route_selected = False
        self.reference_upload_route_proxy = ""
        self.reference_upload_last_route = "direct"
        self.subscription_proxy: dict[str, str] | None = None
        self.account_proxy_bridge: dict[str, str] | None = None
        self.proxy_exit_id = "direct"
        self.proxy_timezone_id = ""
        self.slider_enabled = _environment_bool("DOLA_SLIDER_ENABLED", True)
        self.slider_solver = SliderChallengeSolver(
            SliderSolverSettings(
                max_attempts=_environment_int("DOLA_SLIDER_MAX_ATTEMPTS", 3, minimum=1, maximum=8),
                verify_timeout_seconds=_environment_float(
                    "DOLA_SLIDER_VERIFY_TIMEOUT_SECONDS", 5.0, minimum=1.0, maximum=30.0
                ),
                minimum_confidence=_environment_float(
                    "DOLA_SLIDER_MINIMUM_CONFIDENCE", 0.45, minimum=0.0, maximum=1.0
                ),
            )
        )

    def _task_exists(self) -> bool:
        return task_exists(self.task_id)

    def _save_result(self, **kwargs: Any) -> None:
        if self._task_exists():
            save_result(self.task_id, **kwargs)

    def _mark_success(self, *, confirmation_pending: bool = False) -> None:
        if self._task_exists():
            mark_submitted(self.task_id, result_poll_delay_seconds=15 if confirmation_pending else 45)

    def _set_phase(self, phase: str, status_reason: str) -> None:
        if self._task_exists():
            set_execution_phase(self.task_id, phase, status_reason)

    async def _resolve_slider_if_present(
        self,
        page: Page,
        context: BrowserContext,
        *,
        phase: str,
        wait_seconds: float = 0.0,
        reload_if_missing: bool = False,
    ) -> SliderSolveResult:
        if not self.slider_enabled:
            return SliderSolveResult(status="not_present", attempts=0)

        async def locate_slider(seconds: float) -> Page | None:
            deadline = asyncio.get_running_loop().time() + max(0.0, seconds)
            while True:
                target = await find_slider_page([context], self.slider_solver.settings.iframe_selector)
                if target is not None or asyncio.get_running_loop().time() >= deadline:
                    return target
                await page.wait_for_timeout(100)

        target_page = await locate_slider(wait_seconds)
        reload_error = ""
        if target_page is None and reload_if_missing:
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                reload_error = f"{type(exc).__name__}: {exc}"[:300]
            target_page = await locate_slider(max(5.0, wait_seconds))
        if target_page is None:
            if wait_seconds > 0 or reload_if_missing:
                self._save_result(
                    extra={
                        "slider_last_phase": phase,
                        "slider_last_status": "not_present",
                        "slider_last_attempts": 0,
                        "slider_last_confidence": None,
                        "slider_last_error": reload_error,
                        "slider_last_checked_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            return SliderSolveResult(status="not_present", attempts=0)

        self._set_phase("resolving_slider_verification", "正在完成滑块验证")
        result = await self.slider_solver.solve(target_page)
        self._save_result(
            extra={
                "slider_last_phase": phase,
                "slider_last_status": result.status,
                "slider_last_attempts": result.attempts,
                "slider_last_confidence": (
                    round(result.confidence, 4) if result.confidence is not None else None
                ),
                "slider_last_error": str(result.error or "")[:300],
                "slider_last_checked_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return result

    def _slider_failure_outcome(self, result: SliderSolveResult, *, phase: str) -> dict[str, Any]:
        self._mark_active_proxy_unavailable(reason="slider_verification_failed")
        self._save_result(
            extra={
                "submit_error_category": "slider_verification",
                "submit_phase": phase,
            }
        )
        return {
            "success": False,
            "retryable": True,
            "reason": result.error or "Dola slider verification failed",
            "account_fault": True,
            "account_slider_verification": True,
            "switch_account": True,
        }

    async def _submit_with_slider_recovery(
        self,
        page: Page,
        context: BrowserContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        last_result: dict[str, Any] = {}
        for attempt in range(1, SLIDER_RECOVERY_SUBMIT_ATTEMPTS + 1):
            timeout = (
                DOLA_SUBMIT_WITH_REFERENCES_TIMEOUT_SECONDS
                if payload.get("attachments")
                else DOLA_SUBMIT_TIMEOUT_SECONDS
            )
            try:
                value = await asyncio.wait_for(page.evaluate(SUBMIT_SCRIPT, payload), timeout=timeout)
            except asyncio.TimeoutError as exc:
                self._cooldown_active_proxy_transport()
                raise RuntimeError(f"Dola submission transport timeout after {timeout:g} seconds") from exc
            if not isinstance(value, dict):
                raise RuntimeError("Dola submission returned an invalid response")
            last_result = value

            account_state: dict[str, Any] | None = None
            slider_reported = bool(value.get("slider_verification"))
            if not slider_reported and value.get("service_frequent"):
                account_state = await self._inspect_service_frequent_account_state(page, context)
                value["_account_state"] = account_state
                slider_reported = account_state.get("state") == "slider_verification"
            if not slider_reported:
                return value

            slider_result = await self._resolve_slider_if_present(
                page,
                context,
                phase="submission_response",
                wait_seconds=SLIDER_RECOVERY_OBSERVE_SECONDS,
                reload_if_missing=False,
            )
            value["_slider_result"] = slider_result
            if slider_result.status != "success" or attempt >= SLIDER_RECOVERY_SUBMIT_ATTEMPTS:
                return value
            self._set_phase("retrying_after_slider", "滑块验证已完成，正在重新提交")
            await page.wait_for_timeout(300)

        return last_result

    async def _inspect_service_frequent_account_state(self, page: Page, context: BrowserContext) -> dict[str, Any]:
        self._set_phase("checking_account_risk", "正在确认账号登录和滑块状态")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + SERVICE_FREQUENT_INSPECTION_TIMEOUT_SECONDS
        page_inspection_succeeded = False

        async def inspect_pages(stage: str) -> dict[str, Any]:
            nonlocal page_inspection_succeeded
            pages = [page]
            for candidate in list(getattr(context, "pages", []) or []):
                if candidate not in pages:
                    pages.append(candidate)
            snapshots: list[dict[str, Any]] = []
            errors: list[str] = []
            for candidate in pages:
                try:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    value = await asyncio.wait_for(
                        candidate.evaluate(SERVICE_FREQUENT_ACCOUNT_STATE_SCRIPT),
                        timeout=min(SERVICE_FREQUENT_EVALUATE_TIMEOUT_SECONDS, remaining),
                    )
                    if not isinstance(value, dict):
                        raise RuntimeError("invalid inspection result")
                    value = dict(value)
                    value["inspectionStage"] = stage
                    snapshots.append(value)
                    page_inspection_succeeded = True
                except asyncio.TimeoutError:
                    errors.append(f"TimeoutError: risk inspection {stage} exceeded the per-check deadline")
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")
            selected = next((item for item in snapshots if bool(item.get("sliderVerification"))), None)
            selected = selected or next((item for item in snapshots if bool(item.get("loginInvalid"))), None)
            selected = selected or (snapshots[0] if snapshots else {
                "href": str(page.url or ""),
                "bodyText": "",
                "inspectionStage": stage,
            })
            selected["pagesChecked"] = len(pages)
            selected["inspectionFailed"] = not bool(snapshots)
            if errors:
                selected["inspectionError"] = "; ".join(errors)[:300]
            return selected

        initial = await inspect_pages("initial")
        snapshot = initial
        if not bool(initial.get("sliderVerification")) and not bool(initial.get("loginInvalid")):
            initial_signature = (
                str(initial.get("href") or ""),
                str(initial.get("bodyText") or ""),
            )
            page_changed = False
            checks = max(
                1,
                round(SERVICE_FREQUENT_OBSERVE_SECONDS * 1000 / SERVICE_FREQUENT_POLL_INTERVAL_MS),
            )
            for _ in range(checks):
                if loop.time() >= deadline:
                    break
                await asyncio.sleep(SERVICE_FREQUENT_POLL_INTERVAL_MS / 1000.0)
                snapshot = await inspect_pages("observing")
                current_signature = (
                    str(snapshot.get("href") or ""),
                    str(snapshot.get("bodyText") or ""),
                )
                page_changed = page_changed or (
                    not bool(initial.get("inspectionFailed"))
                    and not bool(snapshot.get("inspectionFailed"))
                    and current_signature != initial_signature
                )
                if bool(snapshot.get("sliderVerification")) or bool(snapshot.get("loginInvalid")):
                    break
            snapshot["pageChanged"] = page_changed

        if (
            not bool(snapshot.get("sliderVerification"))
            and not bool(snapshot.get("loginInvalid"))
            and not bool(snapshot.get("pageChanged"))
        ):
            remaining = deadline - loop.time()
            if remaining > 0:
                try:
                    await page.reload(
                        wait_until="domcontentloaded",
                        timeout=max(1, int(min(SERVICE_FREQUENT_RELOAD_TIMEOUT_SECONDS, remaining) * 1000)),
                    )
                except Exception as exc:
                    snapshot["inspectionError"] = f"reload: {type(exc).__name__}: {str(exc)[:220]}"
                remaining = deadline - loop.time()
                if remaining > 0:
                    await asyncio.sleep(min(2.0, remaining))
                    refreshed = await inspect_pages("after_reload")
                    if (
                        bool(refreshed.get("sliderVerification"))
                        or bool(refreshed.get("loginInvalid"))
                        or not bool(refreshed.get("inspectionFailed"))
                    ):
                        snapshot = refreshed
            else:
                snapshot["inspectionError"] = str(
                    snapshot.get("inspectionError") or "risk inspection exceeded the total deadline"
                )[:300]
        cookies_checked = True
        try:
            current_cookies = await asyncio.wait_for(
                context.cookies(),
                timeout=SERVICE_FREQUENT_EVALUATE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            cookies_checked = False
            current_cookies = []
            snapshot["inspectionError"] = str(
                snapshot.get("inspectionError") or "risk inspection cookie check timed out"
            )[:300]
        except Exception as exc:
            cookies_checked = False
            current_cookies = []
            snapshot["inspectionError"] = str(snapshot.get("inspectionError") or str(exc))[:300]
        auth_names = {"sessionid", "sessionid_ss", "sid_tt", "sid_guard"}
        injected_names = {
            str(item.get("name") or "")
            for item in self.account.get("cookies") or []
            if isinstance(item, dict)
        }
        current_names = {
            str(item.get("name") or "")
            for item in current_cookies
            if isinstance(item, dict)
        }
        state = "service_frequent"
        if bool(snapshot.get("sliderVerification")):
            state = "slider_verification"
        elif bool(snapshot.get("loginInvalid")) or cookies_checked and bool(injected_names & auth_names) and not bool(current_names & auth_names):
            state = "login_invalid"
        return {
            "state": state,
            "url": str(snapshot.get("href") or page.url or "")[:500],
            "page_text": _submission_response_preview(snapshot.get("bodyText"))[:1000],
            "inspection_error": str(snapshot.get("inspectionError") or "")[:300],
            "inspection_stage": str(snapshot.get("inspectionStage") or "")[:40],
            "pages_checked": max(0, int(snapshot.get("pagesChecked") or 0)),
            "evidence": str(snapshot.get("riskEvidence") or "none")[:300],
            "page_changed": bool(snapshot.get("pageChanged")),
            "inspection_transport_failed": not page_inspection_succeeded,
        }

    def _finish_image_preparation(self) -> None:
        callback = self.image_preparation_done
        self.image_preparation_done = None
        if callback is not None:
            callback()

    def _remember_failed_proxy_node(self) -> None:
        node_id = str(self.proxy_node_id or "").strip()
        if not node_id or not self._task_exists():
            return
        meta = get_meta(self.task_id)
        failed = [str(item) for item in meta.get("failed_proxy_node_ids") or [] if str(item)]
        if node_id not in failed:
            failed.append(node_id)
        update_meta(
            self.task_id,
            proxy_retry_avoid_node_id=node_id,
            failed_proxy_node_ids=failed[-20:],
        )

    def _mark_active_proxy_unavailable(
        self,
        *,
        cooldown_seconds: int | None = None,
        reason: str = "runtime_failure",
    ) -> None:
        if reason == "doubao_service_frequent" and self.proxy_node_id:
            mark_node_unavailable(
                self.proxy_node_id,
                reason=reason,
                cooldown_seconds=max(1800, int(cooldown_seconds or 0)),
            )
            return
        self._clear_doubao_pinned_proxy_node()
        if self.active_proxy_source == "api":
            self._remember_failed_proxy_node()
            if getattr(self, "api_proxy_lease", None) is not None:
                self.api_proxy_lease.invalidate()
            return
        if self.proxy_node_id:
            kwargs: dict[str, Any] = {"reason": reason}
            if cooldown_seconds is not None:
                kwargs["cooldown_seconds"] = cooldown_seconds
            mark_node_unavailable(self.proxy_node_id, **kwargs)
            self._remember_failed_proxy_node()
        if not self.proxy_node_id and self.active_proxy_source != "account":
            mark_proxy_source_unavailable(self.active_proxy_source)

    def _cooldown_active_proxy_transport(self) -> None:
        self._clear_doubao_pinned_proxy_node()
        if self.active_proxy_source == "api":
            self._remember_failed_proxy_node()
            if getattr(self, "api_proxy_lease", None) is not None:
                self.api_proxy_lease.cooldown(PROXY_TRANSPORT_COOLDOWN_SECONDS)
            return
        if self.proxy_node_id:
            mark_node_unavailable(
                self.proxy_node_id,
                reason="transport_failure",
                cooldown_seconds=PROXY_TRANSPORT_COOLDOWN_SECONDS,
            )
            self._remember_failed_proxy_node()

    def _clear_doubao_pinned_proxy_node(self) -> None:
        if getattr(self, "proxy_platform", "dola") != "doubao" or self.active_proxy_source != "subscription":
            return
        account_id = str((getattr(self, "account", {}) or {}).get("id") or "").strip()
        node_id = str(self.proxy_node_id or "").strip()
        if not account_id or not node_id:
            return
        from .accounts import clear_account_pinned_proxy_node

        clear_account_pinned_proxy_node(account_id, node_id)
        if str(self.account.get("pinned_proxy_node_id") or "").strip() == node_id:
            self.account["pinned_proxy_node_id"] = ""

    def mark_doubao_verification_proxy(self) -> int:
        if self.active_proxy_source == "api":
            if getattr(self, "api_proxy_lease", None) is not None:
                self.api_proxy_lease.cooldown(60)
            return 60
        if not self.proxy_node_id:
            return 0
        return record_node_doubao_verification(
            self.proxy_node_id,
            str((getattr(self, "account", {}) or {}).get("id") or ""),
        )

    def record_doubao_proxy_success(self) -> None:
        if self.active_proxy_source == "subscription" and self.proxy_node_id:
            record_node_doubao_success(self.proxy_node_id)

    def _discard_active_api_proxy_transport(self) -> None:
        if self.active_proxy_source != "api":
            self._cooldown_active_proxy_transport()
            return
        self._remember_failed_proxy_node()
        if getattr(self, "api_proxy_lease", None) is not None:
            self.api_proxy_lease.invalidate()

    def _handle_active_proxy_transport_failure(self, reason: str) -> None:
        if self.active_proxy_source == "api" and is_api_proxy_fatal_transport_failure(reason):
            self._discard_active_api_proxy_transport()
            return
        self._cooldown_active_proxy_transport()

    def _record_active_gateway_failure(self, status: int) -> None:
        if not self.proxy_node_id:
            return
        self._remember_failed_proxy_node()
        if self.active_proxy_source == "api":
            if getattr(self, "api_proxy_lease", None) is not None:
                self.api_proxy_lease.invalidate()
            return
        record_node_gateway_failure(self.proxy_node_id, status)

    def _active_proxy_cooldown_outcome(self) -> dict[str, Any] | None:
        if self.active_proxy_source == "api":
            return None
        retry_after = node_retry_after(self.proxy_node_id)
        if retry_after <= 0:
            return None
        self._remember_failed_proxy_node()
        return {
            "success": False,
            "retryable": True,
            "reason": "proxy node cooling down",
            "infrastructure_fault": True,
            "defer_only": True,
            "retry_after": retry_after,
        }

    async def run(self) -> dict[str, Any]:
        try:
            image_count = 0
            if self._task_exists():
                try:
                    image_count = max(0, min(9, int(get_meta(self.task_id).get("image_count") or 0)))
                except (FileNotFoundError, TypeError, ValueError):
                    image_count = 0
            timeout = max(self.settings.task_timeout_seconds, 360) + image_count * REFERENCE_IMAGE_UPLOAD_TIMEOUT_SECONDS
            return await asyncio.wait_for(self._run_once(), timeout=timeout)
        except asyncio.TimeoutError:
            if self._task_exists():
                result = load_result(self.task_id)
                conversation_id = str(result.get("conversation_id") or "").strip()
                confirmation_pending = not bool(conversation_id)
                error_category = str(result.get("submit_error_category") or "")
                submission_was_rejected = error_category in {
                    "slider_verification",
                    "service_frequent",
                    "country_restricted",
                    "region_restricted",
                } or error_category.startswith("http_")
                submission_was_received = bool(conversation_id) or (
                    not submission_was_rejected
                    and (
                        result.get("submission_ambiguous") is True
                        or str(result.get("submit_confirmation_state") or "") == "awaiting_conversation"
                    )
                )
                if submission_was_received:
                    meta = get_meta(self.task_id)
                    if str(meta.get("status") or "") == "running":
                        mark_submitted(self.task_id, result_poll_delay_seconds=5)
                    self._save_result(
                        extra={
                            "post_submission_cleanup_timeout": True,
                            "post_submission_cleanup_timeout_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    return {
                        "success": True,
                        "retryable": False,
                        "reason": "",
                        "confirmation_pending": confirmation_pending,
                    }
            self._save_result(extra={"submit_error_category": "infrastructure", "submit_phase": "browser_timeout"})
            return {"success": False, "retryable": True, "reason": "browser timeout", "infrastructure_fault": True}
        except ReferenceUploadCapacityError as exc:
            self._save_result(extra={"submit_error_category": "reference_upload_capacity", "submit_phase": "waiting_image_upload_slot"})
            return {
                "success": False,
                "retryable": True,
                "reason": str(exc),
                "infrastructure_fault": True,
                "defer_only": True,
                "retry_after": exc.retry_after,
                "defer_reason": "参考图上传繁忙，任务已自动排队",
                "defer_category": "image_upload_limit",
            }
        except ProxyCoolingDownError as exc:
            return {
                "success": False,
                "retryable": True,
                "reason": str(exc),
                "infrastructure_fault": True,
                "defer_only": True,
                "retry_after": exc.retry_after,
                "defer_reason": exc.queue_reason,
                "defer_category": exc.queue_category,
            }
        except Exception as exc:
            reason = exception_reason(exc)
            execution_phase = ""
            if self._task_exists():
                try:
                    execution_phase = str(get_meta(self.task_id).get("execution_phase") or "")
                except FileNotFoundError:
                    pass
            reference_upload_failure = is_reference_upload_failure(reason) or is_reference_upload_phase(execution_phase)
            infrastructure_fault = is_infrastructure_failure(reason) or reference_upload_failure
            upper_reason = reason.upper()
            gateway_status = next((status for status in (502, 503, 504) if f"HTTP {status}" in upper_reason), 0)
            if gateway_status:
                self._record_active_gateway_failure(gateway_status)
            if reference_upload_failure:
                reference_transport_failure = "timed out" in reason.lower() or is_proxy_transport_failure(reason)
                if reference_transport_failure and self.active_proxy_server:
                    self._handle_active_proxy_transport_failure(reason)
                self._save_result(extra={"submit_error_category": "reference_upload", "submit_phase": "uploading_references", "reference_upload_error": reason})
            elif infrastructure_fault:
                if is_proxy_transport_failure(reason):
                    self._handle_active_proxy_transport_failure(reason)
                self._save_result(extra={"submit_error_category": "infrastructure", "submit_phase": "browser_or_proxy_setup"})
                if self.active_proxy_source == "api" and is_proxy_transport_failure(reason):
                    return {
                        "success": False,
                        "retryable": True,
                        "reason": reason,
                        "infrastructure_fault": True,
                        "defer_only": True,
                        "retry_after": 3,
                        "defer_reason": "正在刷新API代理，任务已自动排队",
                        "defer_category": "proxy_refresh",
                    }
            return {"success": False, "retryable": True, "reason": reason, "infrastructure_fault": infrastructure_fault}

    async def _run_once(self) -> dict[str, Any]:
        if not self._task_exists():
            return {"success": True, "retryable": False, "reason": ""}
        clear_transient_result(self.task_id)
        self._set_phase("starting_browser", "正在启动生成环境")
        runtime = self.browser_pool.playwright_context() if self.browser_pool is not None else async_playwright()
        async with runtime as playwright:
            browser: Browser | None = None
            context: BrowserContext | None = None
            page: Page | None = None
            lease: BrowserContextLease | None = None
            try:
                executable_path = self._browser_executable_path()
                self._set_phase("connecting_node", "正在连接生成节点")
                proxy_config = await self._browser_proxy_config()
                browser_args = [
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ]
                context_options = {
                    "locale": "zh-CN",
                    "viewport": {"width": 1365, "height": 900},
                    "user_agent": BROWSER_USER_AGENT,
                    "extra_http_headers": BROWSER_EXTRA_HTTP_HEADERS,
                    "accept_downloads": False,
                }
                if self.browser_pool is not None:
                    self._set_phase("allocating_browser", "正在分配浏览器资源")
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
                    self._set_phase("launching_browser", "正在启动浏览器")
                    browser = await playwright.chromium.launch(
                        headless=self.settings.headless,
                        executable_path=executable_path,
                        proxy=proxy_config,
                        args=browser_args,
                    )
                    context = await browser.new_context(**context_options)
                await self._inject_account(context)
                await context.add_init_script(BROWSER_INIT_SCRIPT)
                page = await context.new_page()
                await self._prepare_page(page)
                self._set_phase("opening_generation_page", "正在打开生成页面")
                response = await page.goto(TARGET_URL, wait_until="commit", timeout=90000)
                if response and response.status >= 500:
                    status = int(response.status)
                    if status in {502, 503, 504}:
                        self._record_active_gateway_failure(status)
                    return {"success": False, "retryable": True, "reason": f"page load failed {status}", "infrastructure_fault": True}
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception:
                    pass
                await page.wait_for_timeout(5000)
                slider_result = await self._resolve_slider_if_present(
                    page,
                    context,
                    phase="page_navigation",
                )
                if slider_result.status == "failed":
                    return self._slider_failure_outcome(slider_result, phase="page_navigation")
                if self._is_region_restricted(page.url):
                    self._mark_active_proxy_unavailable(reason="region_restricted")
                    self._save_result(extra={"submit_error_category": "region_restricted", "submit_phase": "page_navigation"})
                    return {"success": False, "retryable": True, "reason": "region restricted", "infrastructure_fault": True}

                if cooldown := self._active_proxy_cooldown_outcome():
                    return cooldown
                self._set_phase("preparing_references", "正在准备参考图" if task_image_paths(self.task_id) else "正在准备生成请求")
                attachments = await self._upload_images_if_needed(page)
                self._finish_image_preparation()
                if cooldown := self._active_proxy_cooldown_outcome():
                    return cooldown
                if self.submission_pacer is not None:
                    self._set_phase("waiting_submit_slot", "等待当前生成出口提交时段")
                    await self.submission_pacer(self.proxy_exit_id or self.proxy_node_id or self.active_proxy_source or "direct")
                slider_result = await self._resolve_slider_if_present(
                    page,
                    context,
                    phase="before_submission",
                )
                if slider_result.status == "failed":
                    return self._slider_failure_outcome(slider_result, phase="before_submission")
                self._set_phase("submitting_request", "正在提交生成请求")
                collection_id = str(uuid.uuid4())
                unique_key = str(uuid.uuid4())
                local_conversation_id = f"local_{secrets.randbelow(9 * 10**15) + 10**15}"
                cookies = await context.cookies()
                cookie_string = "; ".join(f"{item['name']}={item['value']}" for item in cookies if item.get("name"))
                self._save_result(
                    cookie_string=cookie_string,
                    extra={
                        "account_id": str(self.account.get("id") or ""),
                        "account_name": str(self.account.get("name") or ""),
                        "account_quota_charge_id": str(self.account.get("quota_charge_id") or ""),
                        "local_conversation_id": local_conversation_id,
                        "submission_collection_id": collection_id,
                        "submission_unique_key": unique_key,
                        "submission_ambiguous": False,
                    },
                )
                if not begin_task_submission(self.task_id):
                    canceled = is_task_canceled(self.task_id)
                    return {"success": False, "retryable": not canceled, "reason": "用户取消生成" if canceled else "任务提交状态已变化，正在重试"}
                try:
                    result = await self._submit_with_slider_recovery(
                        page,
                        context,
                        {
                            "prompt": self.prompt,
                            "ratio": self.ratio,
                            "duration": self.duration or self.settings.video_duration,
                            "attachments": attachments,
                            "collectionId": collection_id,
                            "uniqueKey": unique_key,
                            "localConversationId": local_conversation_id,
                        },
                    )
                except Exception as exc:
                    reason = str(exc)[:500]
                    if is_infrastructure_failure(reason):
                        if is_api_proxy_fatal_transport_failure(reason):
                            self._discard_active_api_proxy_transport()
                        self._save_result(extra={
                            "submission_ambiguous": True,
                            "submission_ambiguous_at": datetime.now(timezone.utc).isoformat(),
                            "submit_error_category": "ambiguous_submission",
                            "submit_phase": "submitting_request",
                            "ambiguous_submission_error": reason,
                        })
                        return {"success": False, "retryable": True, "submitted": True, "reason": reason, "infrastructure_fault": True}
                    release_task_submission(self.task_id)
                    raise
                cookies = await context.cookies()
                cookie_string = "; ".join(f"{item['name']}={item['value']}" for item in cookies if item.get("name"))
                self._save_result(
                    conversation_id=str(result.get("conversation_id") or ""),
                    cookie_string=cookie_string,
                    extra={
                        "account_id": str(self.account.get("id") or ""),
                        "account_name": str(self.account.get("name") or ""),
                        "account_quota_charge_id": str(self.account.get("quota_charge_id") or ""),
                        "chat_status": result.get("status"),
                        "chat_content_type": result.get("contentType"),
                        "chat_response_bytes": int(result.get("responseBytes") or 0),
                        "submission_response_preview": (
                            ""
                            if str(result.get("conversation_id") or "")
                            else _submission_response_preview(result.get("responsePreview"))
                        ),
                        "local_conversation_id": str(result.get("local_conversation_id") or ""),
                        "submission_collection_id": str(result.get("collection_id") or ""),
                        "submission_unique_key": str(result.get("unique_key") or ""),
                        "submitted_with_images": bool(result.get("submitted_with_images")),
                        "sse_timed_out": bool(result.get("sse_timed_out")),
                        "submission_ambiguous": not bool(str(result.get("conversation_id") or "")),
                        "submission_ambiguous_at": (
                            datetime.now(timezone.utc).isoformat()
                            if not str(result.get("conversation_id") or "")
                            else ""
                        ),
                        "submit_confirmation_state": (
                            "confirmed" if str(result.get("conversation_id") or "") else "awaiting_conversation"
                        ),
                    },
                )
                self._set_phase("submission_received", "生成请求已接收，正在确认状态")
                if result.get("slider_verification"):
                    self._mark_active_proxy_unavailable(reason="slider_verification")
                    release_task_submission(self.task_id)
                    self._save_result(extra={"submit_error_category": "slider_verification", "submit_phase": "submission_response"})
                    return {
                        "success": False,
                        "retryable": True,
                        "reason": "Dola slider verification required",
                        "account_fault": True,
                        "account_slider_verification": True,
                        "switch_account": True,
                    }
                if result.get("service_frequent"):
                    account_state = result.get("_account_state")
                    if not isinstance(account_state, dict):
                        account_state = await self._inspect_service_frequent_account_state(page, context)
                    self._save_result(extra={
                        "service_frequent_account_state": account_state["state"],
                        "service_frequent_check_url": account_state["url"],
                        "service_frequent_check_text": account_state["page_text"],
                        "service_frequent_check_error": account_state["inspection_error"],
                        "service_frequent_check_stage": account_state["inspection_stage"],
                        "service_frequent_pages_checked": account_state["pages_checked"],
                        "service_frequent_check_evidence": account_state["evidence"],
                        "service_frequent_page_changed": account_state["page_changed"],
                        "service_frequent_inspection_transport_failed": bool(
                            account_state.get("inspection_transport_failed")
                        ),
                    })
                    if account_state["state"] == "slider_verification":
                        release_task_submission(self.task_id)
                        self._save_result(extra={"submit_error_category": "slider_verification", "submit_phase": "service_frequent_account_check"})
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": "Dola slider verification required",
                            "account_fault": True,
                            "account_slider_verification": True,
                            "switch_account": True,
                        }
                    if account_state["state"] == "login_invalid":
                        release_task_submission(self.task_id)
                        self._save_result(extra={"submit_error_category": "login_invalid", "submit_phase": "service_frequent_account_check"})
                        return {
                            "success": False,
                            "retryable": True,
                            "reason": "Dola login session invalid after service frequent",
                            "account_fault": True,
                            "account_login_invalid": True,
                            "switch_account": True,
                        }
                    self._remember_failed_proxy_node()
                    release_task_submission(self.task_id)
                    self._save_result(extra={"submit_error_category": "service_frequent", "submit_phase": "submission_response"})
                    return dola_service_frequent_abnormal_outcome(account_state["state"])
                if result.get("country_restricted"):
                    self._mark_active_proxy_unavailable(reason="country_restricted")
                    release_task_submission(self.task_id)
                    self._save_result(extra={"submit_error_category": "country_restricted", "submit_phase": "submission_response"})
                    return {"success": False, "retryable": True, "reason": "country restricted", "infrastructure_fault": True}
                if self._is_region_restricted(str(result.get("location_href") or page.url)):
                    self._mark_active_proxy_unavailable(reason="region_restricted")
                    release_task_submission(self.task_id)
                    self._save_result(extra={"submit_error_category": "region_restricted", "submit_phase": "submission_response"})
                    return {"success": False, "retryable": True, "reason": "region restricted", "infrastructure_fault": True}
                status = int(result.get("status") or 0)
                if not bool(result.get("ok")) or status < 200 or status >= 300:
                    release_task_submission(self.task_id)
                    reason = f"submission failed with HTTP {status or 'unknown'}"
                    if status in {502, 503, 504}:
                        self._record_active_gateway_failure(status)
                    self._save_result(extra={"submit_error_category": f"http_{status or 'unknown'}", "submit_phase": "submission_response"})
                    return {
                        "success": False,
                        "retryable": True,
                        "reason": reason,
                        "account_fault": status in {401, 403},
                        "infrastructure_fault": status >= 500,
                    }
                record_node_success(self.proxy_node_id)
                confirmation_pending = not bool(str(result.get("conversation_id") or ""))
                if self._task_exists():
                    updates: dict[str, Any] = {"proxy_retry_avoid_node_id": ""}
                    if not confirmation_pending:
                        updates.update(
                            preferred_account_id="",
                            ambiguous_proxy_retry_count=0,
                            ambiguous_proxy_avoid_node_ids=[],
                        )
                    update_meta(self.task_id, **updates)
                self._mark_success(confirmation_pending=confirmation_pending)
                return {
                    "success": True,
                    "retryable": False,
                    "reason": "",
                    "confirmation_pending": confirmation_pending,
                }
            finally:
                self._finish_image_preparation()
                await _bounded_cleanup(safe_unroute_all(page))
                if lease is not None:
                    await _bounded_cleanup(lease.release())
                else:
                    await _bounded_cleanup(safe_close(context))
                    await _bounded_cleanup(safe_close(browser))
                await self.release_browser_proxy()

    async def _api_browser_proxy_config(self, excluded_node_ids: set[str]) -> dict[str, str]:
        api_proxy_pool = getattr(self, "api_proxy_pool", None)
        if api_proxy_pool is not None:
            try:
                acquire_options: dict[str, Any] = {
                    "timeout_seconds": self.settings.proxy_api_timeout_seconds,
                    "scheme": self.settings.proxy_api_scheme,
                    "excluded_node_ids": excluded_node_ids,
                }
                if bool(getattr(self.settings, "platform_proxy_random", {}).get(self.proxy_platform)):
                    acquire_options["random_select"] = True
                lease = await api_proxy_pool.acquire(self.settings.proxy_api_url, **acquire_options)
            except Exception as exc:
                self._save_result(extra={"proxy_source": "api", "proxy_platform": self.proxy_platform, "api_proxy_last_error": str(exc)[:800]})
                raise ProxyCoolingDownError(
                    API_PROXY_RETRY_AFTER_SECONDS,
                    reason="api proxy pool is refreshing",
                    queue_reason="正在刷新API代理，任务已自动排队",
                    queue_category="proxy_refresh",
                ) from exc
            self.api_proxy_lease = lease
            self.proxy_node_id = lease.node_id
            self.active_proxy_source = "api"
            self.active_proxy_server = lease.server
            self.proxy_exit_id = await proxy_exit_identity(lease.server, lease.node_id)
            record_node_success(lease.node_id)
            mark_proxy_source_available("api")
            if excluded_node_ids and lease.node_id not in excluded_node_ids and self._task_exists():
                update_meta(self.task_id, proxy_retry_avoid_node_id="")
            self._save_result(
                extra={
                    "proxy_source": "api",
                    "proxy_platform": self.proxy_platform,
                    "proxy_server": lease.server,
                    "proxy_node_id": lease.node_id,
                    "proxy_node_name": lease.host_port,
                    "proxy_exit_id": self.proxy_exit_id,
                    "api_proxy_pool_shared": True,
                }
            )
            return browser_proxy_config_for(lease.server, default_scheme=self.settings.proxy_api_scheme)
        seen_endpoints: set[str] = set()
        rejected_endpoints: list[str] = []
        errors: list[str] = []
        fetch_count = 0
        fetch_error_count = 0
        while fetch_count < API_PROXY_FETCH_LIMIT and len(seen_endpoints) < API_PROXY_CANDIDATE_LIMIT:
            fetch_count += 1
            try:
                proxy = await fetch_proxy_from_api(
                    self.settings.proxy_api_url,
                    timeout_seconds=self.settings.proxy_api_timeout_seconds,
                    scheme=self.settings.proxy_api_scheme,
                )
            except Exception as exc:
                fetch_error_count += 1
                errors.append(f"fetch {fetch_count}: {str(exc)[:120]}")
                if fetch_error_count >= API_PROXY_FETCH_ERROR_LIMIT:
                    break
                continue
            host_port = str(proxy.get("host_port") or "").strip()
            server = str(proxy.get("server") or "").strip()
            if not host_port or not server:
                errors.append(f"fetch {fetch_count}: proxy api returned an empty endpoint")
                continue
            node_id = f"api:{host_port}"
            if node_id in seen_endpoints:
                continue
            seen_endpoints.add(node_id)
            if node_id in excluded_node_ids:
                rejected_endpoints.append(host_port)
                errors.append(f"{host_port}: excluded from the current retry")
                continue
            available = await dola_proxy_available(
                server,
                min(12.0, float(self.settings.proxy_api_timeout_seconds)),
            )
            if not available:
                rejected_endpoints.append(host_port)
                errors.append(f"{host_port}: unavailable for Dola")
                continue
            self.proxy_node_id = node_id
            self.active_proxy_source = "api"
            self.active_proxy_server = server
            self.proxy_exit_id = await proxy_exit_identity(server, node_id)
            record_node_success(node_id)
            mark_proxy_source_available("api")
            if excluded_node_ids and node_id not in excluded_node_ids and self._task_exists():
                update_meta(self.task_id, proxy_retry_avoid_node_id="")
            self._save_result(
                extra={
                    "proxy_source": "api",
                    "proxy_platform": self.proxy_platform,
                    "proxy_server": server,
                    "proxy_node_id": node_id,
                    "proxy_node_name": host_port,
                    "proxy_exit_id": self.proxy_exit_id,
                    "api_proxy_candidate_count": len(seen_endpoints),
                    "api_proxy_fetch_count": fetch_count,
                    "api_proxy_rejected_endpoints": rejected_endpoints[-3:],
                    "api_proxy_last_error": "",
                }
            )
            return browser_proxy_config_for(server, default_scheme=self.settings.proxy_api_scheme)
        self._save_result(
            extra={
                "proxy_source": "api",
                "proxy_platform": self.proxy_platform,
                "api_proxy_candidate_count": len(seen_endpoints),
                "api_proxy_fetch_count": fetch_count,
                "api_proxy_rejected_endpoints": rejected_endpoints[-3:],
                "api_proxy_last_error": "; ".join(errors[-6:])[:800],
            }
        )
        raise ProxyCoolingDownError(
            API_PROXY_RETRY_AFTER_SECONDS,
            reason="api proxy candidates unavailable, refreshing",
            queue_reason="正在刷新API代理，任务已自动排队",
            queue_category="proxy_refresh",
        )

    async def _browser_proxy_config(self) -> dict[str, str] | None:
        self.settings = load_settings()
        self.proxy_platform = str(getattr(self, "proxy_platform", "dola") or "dola")
        self.active_proxy_source = ""
        self.active_proxy_server = ""
        self.proxy_node_id = ""
        self.proxy_exit_id = "direct"
        self.proxy_timezone_id = ""
        meta = get_meta(self.task_id) if self._task_exists() else {}
        avoid_node_id = str(meta.get("proxy_retry_avoid_node_id") or "").strip()
        excluded_node_ids = {
            str(item).strip()
            for item in meta.get("ambiguous_proxy_avoid_node_ids") or []
            if str(item).strip()
        }
        if avoid_node_id:
            excluded_node_ids.add(avoid_node_id)
        if not self.settings.proxy_enabled:
            self._save_result(extra={"proxy_source": "direct", "proxy_platform": self.proxy_platform, "proxy_server": ""})
            return None
        platform_sources = getattr(self.settings, "platform_proxy_sources", {})
        platform_random = getattr(self.settings, "platform_proxy_random", {})
        selected_source = str(platform_sources.get(self.proxy_platform) or self.settings.proxy_source).strip().lower()
        random_select = bool(platform_random.get(self.proxy_platform))
        account = getattr(self, "account", {}) or {}
        if self.proxy_platform == "doubao" and selected_source == "subscription":
            self.proxy_timezone_id = doubao_proxy_timezone_id(self.settings.proxy_auto_countries)
        pinned_proxy_node_id = (
            str(account.get("pinned_proxy_node_id") or "").strip()
            if self.proxy_platform == "doubao"
            else ""
        )
        if pinned_proxy_node_id and pinned_proxy_node_id in excluded_node_ids:
            from .accounts import clear_account_pinned_proxy_node

            clear_account_pinned_proxy_node(str(account.get("id") or ""), pinned_proxy_node_id)
            if isinstance(account, dict):
                account["pinned_proxy_node_id"] = ""
            pinned_proxy_node_id = ""
        if pinned_proxy_node_id:
            excluded_node_ids.clear()
            avoid_node_id = ""
            random_select = False
        if selected_source == "direct":
            self._save_result(extra={"proxy_source": "direct", "proxy_platform": self.proxy_platform, "proxy_server": ""})
            return None

        configured = {
            "subscription": bool(self.settings.proxy_subscription_url),
            "account": account_proxy_configured(self.settings),
            "api": bool(self.settings.proxy_api_url),
        }
        candidates = [selected_source] if configured.get(selected_source) else []
        errors: list[str] = []
        attempted_sources = 0
        cooling_delays: list[int] = []
        for source in candidates:
            if not proxy_source_available(source):
                errors.append(f"{source}: cooling down")
                cooling_delays.append(proxy_source_retry_after(source))
                continue
            attempted_sources += 1
            try:
                if source == "subscription":
                    proxy = await acquire_dola_subscription_proxy(
                        self.settings.proxy_subscription_url,
                        timeout_seconds=self.settings.proxy_api_timeout_seconds,
                        scheme=self.settings.proxy_subscription_scheme,
                        refresh_seconds=self.settings.proxy_subscription_refresh_seconds,
                        auto_select=False if pinned_proxy_node_id else self.settings.proxy_auto_select,
                        selected_node=pinned_proxy_node_id or self.settings.proxy_selected_node,
                        selected_countries=() if pinned_proxy_node_id else self.settings.proxy_auto_countries,
                        latency_threshold_ms=self.settings.proxy_latency_threshold_ms,
                        excluded_node_ids=excluded_node_ids,
                        random_select=random_select,
                        prefer_country_order=self.proxy_platform == "doubao",
                    )
                    self.subscription_proxy = proxy
                    self.proxy_node_id = str(proxy.get("node_id") or "")
                    self.active_proxy_source = source
                    self.active_proxy_server = str(proxy.get("server") or "")
                    self.proxy_exit_id = str(proxy.get("exit_id") or f"node:{self.proxy_node_id}")
                    if self.proxy_platform == "doubao" and self.proxy_node_id and str(account.get("id") or "").strip():
                        from .accounts import set_account_pinned_proxy_node

                        set_account_pinned_proxy_node(str(account.get("id") or ""), self.proxy_node_id)
                        account["pinned_proxy_node_id"] = self.proxy_node_id
                    mark_proxy_source_available(source)
                    if avoid_node_id and self.proxy_node_id != avoid_node_id:
                        update_meta(self.task_id, proxy_retry_avoid_node_id="")
                    self._save_result(
                        extra={
                            "proxy_source": source,
                            "proxy_platform": self.proxy_platform,
                            "proxy_server": proxy["server"],
                            "proxy_node_count": int(proxy["node_count"]) if proxy["node_count"].isdigit() else proxy["node_count"],
                            "proxy_node_id": self.proxy_node_id,
                            "proxy_node_name": str(proxy.get("node_name") or ""),
                            "proxy_exit_id": self.proxy_exit_id,
                        },
                    )
                    return browser_proxy_config_for(proxy["server"], default_scheme=self.settings.proxy_subscription_scheme)
                if source == "account":
                    account_errors = 0
                    cooling_delays_for_accounts: list[int] = []
                    account_candidates = account_proxy_candidates(self.settings)
                    if random_select:
                        secrets.SystemRandom().shuffle(account_candidates)
                    for entry in account_candidates:
                        entry_id = str(entry.get("id") or "")
                        if entry_id in excluded_node_ids:
                            continue
                        if retry_after := node_retry_after(entry_id):
                            cooling_delays_for_accounts.append(retry_after)
                            continue
                        probe_url = account_proxy_url(entry)
                        if not probe_url or not await dola_proxy_available(probe_url, min(12.0, float(self.settings.proxy_api_timeout_seconds))):
                            account_errors += 1
                            continue
                        entry_scheme = str(entry.get("scheme") or "socks5").strip().lower()
                        if entry_scheme in {"socks5", "socks5h"}:
                            bridge = await acquire_authenticated_socks_proxy(
                                probe_url,
                                entry_id,
                                f"{entry.get('host')}:{entry.get('port')}",
                            )
                            self.account_proxy_bridge = bridge
                            config = browser_proxy_config_for(str(bridge.get("server") or ""))
                        else:
                            config = account_browser_config(entry)
                        self.proxy_node_id = str(entry.get("id") or "")
                        self.active_proxy_source = source
                        self.active_proxy_server = str(
                            (getattr(self, "account_proxy_bridge", None) or {}).get("server") or probe_url
                        )
                        self.proxy_exit_id = str((getattr(self, "account_proxy_bridge", None) or {}).get("exit_id") or "") or await proxy_exit_identity(probe_url, self.proxy_node_id)
                        mark_proxy_source_available(source)
                        if avoid_node_id and self.proxy_node_id != avoid_node_id:
                            update_meta(self.task_id, proxy_retry_avoid_node_id="")
                        self._save_result(extra={
                            "proxy_source": source,
                            "proxy_platform": self.proxy_platform,
                            "proxy_server": config["server"] if config else "",
                            "proxy_node_id": self.proxy_node_id,
                            "proxy_node_name": f"{entry.get('host')}:{entry.get('port')}",
                            "proxy_exit_id": self.proxy_exit_id,
                        })
                        return config
                    if cooling_delays_for_accounts:
                        raise ProxyCoolingDownError(min(cooling_delays_for_accounts))
                    raise RuntimeError(f"all selected authenticated proxies are unavailable for Dola ({account_errors} checked)")
                return await self._api_browser_proxy_config(excluded_node_ids)
            except ProxyCoolingDownError:
                raise
            except Exception as exc:
                if source == "subscription" and pinned_proxy_node_id:
                    self._clear_doubao_pinned_proxy_node()
                else:
                    mark_proxy_source_unavailable(source)
                errors.append(f"{source}: {str(exc)[:160]}")
                if source == "subscription":
                    await release_dola_subscription_proxy(self.subscription_proxy)
                    self.subscription_proxy = None
                    self.proxy_node_id = ""
        if not candidates:
            raise RuntimeError("selected proxy mode is not configured")
        if attempted_sources == 0 and cooling_delays:
            raise ProxyCoolingDownError(min(cooling_delays))
        raise RuntimeError(f"all configured proxy modes are unavailable ({'; '.join(errors)})")

    async def acquire_browser_proxy(self) -> dict[str, str] | None:
        """Acquire the configured shared proxy lease for a browser task."""
        return await self._browser_proxy_config()

    async def release_browser_proxy(self) -> None:
        """Release any proxy resources acquired by this automation instance."""
        await _bounded_cleanup(release_dola_subscription_proxy(self.subscription_proxy))
        self.subscription_proxy = None
        await _bounded_cleanup(release_task_mihomo_proxy(self.account_proxy_bridge))
        self.account_proxy_bridge = None
        if getattr(self, "api_proxy_lease", None) is not None:
            await _bounded_cleanup(self.api_proxy_lease.release())
            self.api_proxy_lease = None
    def mark_browser_proxy_unavailable(self, *, reason: str = "runtime_failure") -> None:
        """Retire the active proxy after a browser-level network or region failure."""
        self._mark_active_proxy_unavailable(reason=reason)

    async def _inject_account(self, context: BrowserContext) -> None:
        cookies = self.account.get("cookies")
        if not isinstance(cookies, list) or not cookies:
            return
        await context.add_cookies([dict(item) for item in cookies if isinstance(item, dict) and item.get("name")])
        self._save_result(
            extra={
                "account_id": str(self.account.get("id") or ""),
                "account_name": str(self.account.get("name") or ""),
            },
        )

    async def _prepare_page(self, page: Page) -> None:
        await page.route("**/*", self._route_handler)

    async def _route_handler(self, route, request) -> None:
        url = request.url.lower()
        if ".jpeg" in url or ".jpg" in url:
            if self._is_blocked_jpeg(url):
                await route.abort()
                return
        await route.continue_()

    @staticmethod
    def _is_blocked_jpeg(url: str) -> bool:
        return ".jpeg~" in url or ".jpeg?" in url or url.endswith(".jpeg") or ".jpg~" in url or ".jpg?" in url or url.endswith(".jpg")

    async def _prepare_image_upload(self, page: Page) -> dict[str, Any]:
        last_transport_error = ""
        attempts = max(1, int(PREPARE_UPLOAD_TRANSPORT_ATTEMPTS))
        for attempt in range(1, attempts + 1):
            try:
                if self.prepare_upload_slot is not None:
                    async with self.prepare_upload_slot():
                        result = await asyncio.wait_for(
                            page.evaluate(PREPARE_UPLOAD_SCRIPT, {"body": PREPARE_UPLOAD_BODY}),
                            timeout=PREPARE_UPLOAD_TIMEOUT_SECONDS,
                        )
                else:
                    result = await asyncio.wait_for(
                        page.evaluate(PREPARE_UPLOAD_SCRIPT, {"body": PREPARE_UPLOAD_BODY}),
                        timeout=PREPARE_UPLOAD_TIMEOUT_SECONDS,
                    )
            except asyncio.TimeoutError:
                last_transport_error = "prepare_upload timed out"
            except Exception as exc:
                detail = exception_reason(exc)
                if not is_proxy_transport_failure(detail):
                    raise
                last_transport_error = detail
            else:
                if not isinstance(result, dict):
                    raise RuntimeError("prepare_upload returned invalid response")
                if result.get("networkError"):
                    error_name = str(result.get("errorName") or "Error")
                    error_message = str(result.get("errorMessage") or "Failed to fetch")
                    last_transport_error = f"{error_name}: {error_message}"[:500]
                elif not result.get("ok"):
                    raise RuntimeError(
                        f"prepare_upload failed with HTTP {result.get('status')}: {str(result.get('text') or '')[:500]}"
                    )
                else:
                    data = result.get("json")
                    if not isinstance(data, dict) or data.get("code") != 0:
                        raise RuntimeError(f"prepare_upload returned unexpected body: {str(result.get('text') or data)[:500]}")
                    upload_config = data.get("data")
                    if not isinstance(upload_config, dict):
                        raise RuntimeError("prepare_upload did not return upload config")
                    self._save_result(extra={
                        "reference_upload_prepare_attempts": attempt,
                        "reference_upload_prepare_recovered": attempt > 1,
                        "reference_upload_prepare_error": "",
                    })
                    return upload_config

            self._save_result(extra={
                "reference_upload_prepare_attempts": attempt,
                "reference_upload_prepare_recovered": False,
                "reference_upload_prepare_error": last_transport_error,
            })
            if attempt < attempts:
                await asyncio.sleep(1.5 * attempt)

        if self.active_proxy_server:
            self._cooldown_active_proxy_transport()
        raise RuntimeError(
            f"prepare_upload transport failure after {attempts} attempts: {last_transport_error or 'transport error'}"
        )

    async def _upload_one_image_by_fetch(self, page: Page, image_path: Path) -> dict[str, Any]:
        buffer = image_path.read_bytes()
        file_name = image_path.name
        ext = _file_extension_for_upload(image_path)
        mime = _mime_from_path(image_path)
        self.reference_upload_stage = "prepare_upload"
        self._save_result(extra={
            "reference_upload_stage": self.reference_upload_stage,
            "reference_upload_proxy_node_id": self.proxy_node_id,
            "reference_upload_stage_started_at": datetime.now(timezone.utc).isoformat(),
        })
        upload_config = await self._prepare_image_upload(page)
        credentials = _normalize_upload_credentials(upload_config.get("upload_auth_token") or {})
        service_id = str(upload_config.get("service_id") or "")
        imagex_host = str(upload_config.get("upload_host") or "imagex-ap-southeast-1.bytevcloudapi.com")
        if not service_id:
            raise RuntimeError("prepare_upload did not return service_id")

        if not self.reference_upload_route_selected:
            async def probe(proxy_server: str) -> float | None:
                options: dict[str, Any] = {
                    "timeout": httpx.Timeout(6.0, connect=4.0),
                    "follow_redirects": False,
                    "trust_env": False,
                }
                if proxy_server:
                    options["proxy"] = proxy_server
                started_at = time.monotonic()
                try:
                    async with httpx.AsyncClient(**options) as probe_client:
                        await probe_client.head(f"https://{imagex_host}/")
                    return max(0.0, time.monotonic() - started_at)
                except Exception:
                    return None

            direct_latency, proxy_latency = await asyncio.gather(
                probe(""),
                probe(self.active_proxy_server) if self.active_proxy_server else asyncio.sleep(0, result=None),
            )
            use_proxy = bool(
                self.active_proxy_server
                and proxy_latency is not None
                and (direct_latency is None or proxy_latency < direct_latency)
            )
            self.reference_upload_route_proxy = self.active_proxy_server if use_proxy else ""
            self.reference_upload_route_selected = True
            self._save_result(extra={
                "reference_upload_route": "proxy" if use_proxy else "direct",
                "reference_upload_direct_probe_ms": round(direct_latency * 1000, 1) if direct_latency is not None else None,
                "reference_upload_proxy_probe_ms": round(proxy_latency * 1000, 1) if proxy_latency is not None else None,
            })

        timeout = httpx.Timeout(90.0, connect=30.0)
        client_options: dict[str, Any] = {
            "timeout": timeout,
            "follow_redirects": False,
            "trust_env": False,
        }
        if self.reference_upload_route_proxy:
            client_options["proxy"] = self.reference_upload_route_proxy
        self.reference_upload_last_route = "proxy" if self.reference_upload_route_proxy else "direct"
        async with httpx.AsyncClient(**client_options) as client:
            self.reference_upload_stage = "apply_upload"
            self._save_result(extra={
                "reference_upload_stage": self.reference_upload_stage,
                "reference_upload_proxy_node_id": self.proxy_node_id,
                "reference_upload_stage_started_at": datetime.now(timezone.utc).isoformat(),
            })
            apply_params = {
                "Action": "ApplyImageUpload",
                "Version": IMAGEX_API_VERSION,
                "ServiceId": service_id,
                "FileSize": str(len(buffer)),
                "FileExtension": ext,
                "s": _random_base36(),
            }
            apply_url = f"https://{imagex_host}/?{urlencode(apply_params)}"
            apply_headers = {
                "Accept": "*/*",
                **_sign_imagex_request(method="GET", raw_url=apply_url, credentials=credentials),
            }
            apply_data, _ = await _fetch_json(
                client,
                apply_url,
                label="ApplyImageUpload",
                method="GET",
                headers=apply_headers,
            )

            upload_address = (((apply_data or {}).get("Result") or {}).get("UploadAddress") or {})
            store_infos = upload_address.get("StoreInfos") or []
            upload_hosts = upload_address.get("UploadHosts") or []
            store_info = store_infos[0] if store_infos and isinstance(store_infos[0], dict) else {}
            upload_host = str(upload_hosts[0]) if upload_hosts else ""
            session_key = str(upload_address.get("SessionKey") or "")
            store_uri = str(store_info.get("StoreUri") or "")
            store_auth = str(store_info.get("Auth") or "")
            if not store_uri or not store_auth or not upload_host or not session_key:
                raise RuntimeError("ApplyImageUpload did not return a complete upload address")

            upload_headers = {
                "Authorization": store_auth,
                "Content-CRC32": f"{binascii.crc32(buffer) & 0xffffffff:08x}",
                "Content-Disposition": f'attachment; filename="{file_name.replace(chr(34), "")}"',
                "Content-Type": "application/octet-stream",
            }
            if isinstance(upload_address.get("UploadHeader"), dict):
                upload_headers.update({str(key): str(value) for key, value in upload_address["UploadHeader"].items()})
            upload_url = f"https://{upload_host}/upload/v1/{store_uri}"
            self.reference_upload_stage = "upload_binary"
            self._save_result(extra={
                "reference_upload_stage": self.reference_upload_stage,
                "reference_upload_input_bytes": len(buffer),
                "reference_upload_stage_started_at": datetime.now(timezone.utc).isoformat(),
            })
            upload_data, upload_response = await _fetch_json(
                client,
                upload_url,
                label="direct image upload",
                method="POST",
                headers=upload_headers,
                content=buffer,
            )
            if upload_data.get("code") != 2000:
                raise RuntimeError(f"direct image upload returned unexpected body: {json.dumps(upload_data, ensure_ascii=False)[:500]}")

            commit_url = f"https://{imagex_host}/?{urlencode({'Action': 'CommitImageUpload', 'Version': IMAGEX_API_VERSION, 'ServiceId': service_id})}"
            commit_body = _json_compact({"SessionKey": session_key})
            commit_headers = {
                "Accept": "*/*",
                "Content-Type": "application/json",
                **_sign_imagex_request(
                    method="POST",
                    raw_url=commit_url,
                    credentials=credentials,
                    body=commit_body,
                    include_payload_hash=True,
                ),
            }
            self.reference_upload_stage = "commit_upload"
            self._save_result(extra={
                "reference_upload_stage": self.reference_upload_stage,
                "reference_upload_stage_started_at": datetime.now(timezone.utc).isoformat(),
            })
            commit_data, _ = await _fetch_json(
                client,
                commit_url,
                label="CommitImageUpload",
                method="POST",
                headers=commit_headers,
                content=commit_body,
            )

        result = (commit_data or {}).get("Result") or {}
        results = result.get("Results") if isinstance(result, dict) else []
        plugins = result.get("PluginResult") if isinstance(result, dict) else []
        first_result = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else {}
        plugin = plugins[0] if isinstance(plugins, list) and plugins and isinstance(plugins[0], dict) else {}
        uri = str(first_result.get("Uri") or "")
        if not uri:
            raise RuntimeError(f"CommitImageUpload did not return image uri: {json.dumps(commit_data, ensure_ascii=False)[:500]}")
        self.reference_upload_stage = "completed"
        self._save_result(extra={
            "reference_upload_stage": self.reference_upload_stage,
            "reference_upload_completed_at": datetime.now(timezone.utc).isoformat(),
        })
        return {
            "uri": uri,
            "name": plugin.get("FileName") or Path(uri).name or file_name,
            "width": plugin.get("ImageWidth") or 0,
            "height": plugin.get("ImageHeight") or 0,
            "size": plugin.get("ImageSize") or len(buffer),
            "mime": mime,
            "uploadStatus": upload_response.status_code,
        }

    async def _upload_one_image_with_timeout(self, page: Page, image_path: Path) -> dict[str, Any]:
        last_transport_error: BaseException | None = None
        attempts = max(1, int(REFERENCE_IMAGE_UPLOAD_TRANSPORT_ATTEMPTS))
        for attempt in range(1, attempts + 1):
            try:
                uploaded = await asyncio.wait_for(
                    self._upload_one_image_by_fetch(page, image_path),
                    timeout=REFERENCE_IMAGE_UPLOAD_TIMEOUT_SECONDS,
                )
                if attempt > 1:
                    self._save_result(extra={
                        "reference_upload_transport_recovered": True,
                        "reference_upload_transport_attempts": attempt,
                        "reference_upload_transport_error": "",
                    })
                return uploaded
            except asyncio.TimeoutError as exc:
                last_transport_error = exc
                self._save_result(extra={
                    "reference_upload_transport_attempts": attempt,
                    "reference_upload_transport_error": "TimeoutError",
                    "reference_upload_timeout_stage": self.reference_upload_stage or "unknown",
                    "reference_upload_input_bytes": image_path.stat().st_size if image_path.is_file() else 0,
                })
                if attempt >= attempts:
                    break
            except httpx.TransportError as exc:
                last_transport_error = exc
                error_name = type(exc).__name__
                self._save_result(extra={
                    "reference_upload_transport_attempts": attempt,
                    "reference_upload_transport_error": error_name,
                    "reference_upload_input_bytes": image_path.stat().st_size if image_path.is_file() else 0,
                })
                if attempt >= attempts:
                    break
            if self.active_proxy_server:
                failed_route = self.reference_upload_last_route
                self.reference_upload_route_proxy = "" if failed_route == "proxy" else self.active_proxy_server
                self.reference_upload_route_selected = True
                self._save_result(extra={
                    "reference_upload_route_fallback": f"{failed_route}_to_{'proxy' if self.reference_upload_route_proxy else 'direct'}",
                })
            self._set_phase(
                "uploading_reference_retry",
                f"参考图上传连接恢复中（{attempt}/{attempts}）",
            )
            await asyncio.sleep(min(5.0, 1.5 * attempt))
        if isinstance(last_transport_error, asyncio.TimeoutError):
            if self.active_proxy_server and self.reference_upload_last_route == "proxy":
                self._cooldown_active_proxy_transport()
            raise RuntimeError(
                f"reference image upload timed out after {attempts} attempts during "
                f"{self.reference_upload_stage or 'unknown'}"
            ) from last_transport_error
        error_name = type(last_transport_error).__name__ if last_transport_error is not None else "TransportError"
        if self.active_proxy_server and self.reference_upload_last_route == "proxy":
            self._cooldown_active_proxy_transport()
        raise RuntimeError(
            f"direct image upload transport failure after {attempts} attempts: {error_name}"
        ) from last_transport_error

    async def _upload_images_if_needed(self, page: Page) -> list[dict[str, Any]]:
        if not self._task_exists():
            return []
        paths = await asyncio.to_thread(prepare_task_reference_images, self.task_id)
        if not paths:
            return []
        try:
            bypass_cache = bool(get_meta(self.task_id).get("reference_upload_cache_bypass"))
        except FileNotFoundError:
            return []
        account_id = str(self.account.get("id") or "").strip()
        account_scope = reference_attachment_account_scope(self.account)
        unique_paths: list[tuple[Path, str]] = []
        seen_content_keys: set[str] = set()
        for path in paths:
            content_key = reference_image_cache_key(path)
            if content_key in seen_content_keys:
                continue
            seen_content_keys.add(content_key)
            unique_paths.append((path, reference_attachment_cache_key(account_scope, content_key)))
        keys = [cache_key for _, cache_key in unique_paths if cache_key]
        if bypass_cache:
            invalidate_reference_attachment_keys(keys)
        images: list[dict[str, Any]] = []
        cache_hits = 0
        cache_misses = 0
        cache_waits = 0
        for index, (path, cache_key) in enumerate(unique_paths, start=1):
            if not self._task_exists():
                return []
            cached = None if bypass_cache or not cache_key else cached_reference_attachment(cache_key)
            if cached:
                images.append(cached)
                cache_hits += 1
                continue
            uploaded: dict[str, Any] | None = None
            performed_upload = False
            while uploaded is None:
                upload_future: asyncio.Future[dict[str, Any] | None] | None = None
                owns_upload = True
                if cache_key and not bypass_cache:
                    with _REFERENCE_CACHE_LOCK:
                        cached = cached_reference_attachment(cache_key)
                        if cached:
                            uploaded = cached
                            cache_hits += 1
                            break
                        upload_future = _REFERENCE_UPLOADS_IN_FLIGHT.get(cache_key)
                        if upload_future is None:
                            upload_future = asyncio.get_running_loop().create_future()
                            _REFERENCE_UPLOADS_IN_FLIGHT[cache_key] = upload_future
                        else:
                            owns_upload = False
                if not owns_upload and upload_future is not None:
                    self._set_phase(f"waiting_reference_{index}", f"正在复用参考图（{index}/{len(unique_paths)}）")
                    cache_waits += 1
                    try:
                        uploaded = await asyncio.wait_for(
                            asyncio.shield(upload_future),
                            timeout=REFERENCE_CACHE_WAIT_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        with _REFERENCE_CACHE_LOCK:
                            current = _REFERENCE_UPLOADS_IN_FLIGHT.get(cache_key)
                            if current is upload_future and not current.done():
                                _REFERENCE_UPLOADS_IN_FLIGHT.pop(cache_key, None)
                                current.set_result(None)
                        uploaded = None
                    if uploaded:
                        cache_hits += 1
                    continue
                try:
                    performed_upload = True
                    if self.image_upload_slot is not None:
                        self._set_phase("waiting_image_upload_slot", "等待参考图上传时段")
                        async with self.image_upload_slot():
                            self._set_phase(f"uploading_reference_{index}", f"正在上传参考图（{index}/{len(unique_paths)}）")
                            uploaded = await self._upload_one_image_with_timeout(page, path)
                    else:
                        self._set_phase(f"uploading_reference_{index}", f"正在上传参考图（{index}/{len(unique_paths)}）")
                        uploaded = await self._upload_one_image_with_timeout(page, path)
                    if cache_key:
                        cache_reference_attachment(cache_key, uploaded)
                finally:
                    if cache_key and upload_future is not None:
                        with _REFERENCE_CACHE_LOCK:
                            current = _REFERENCE_UPLOADS_IN_FLIGHT.pop(cache_key, None)
                            if current is upload_future and not current.done():
                                current.set_result(dict(uploaded) if uploaded else None)
            images.append(uploaded)
            if performed_upload:
                cache_misses += 1
        self.uploaded_images = self._unique_images(images)
        if len(self.uploaded_images) < len(unique_paths):
            raise RuntimeError("image upload did not return uri")
        update_meta(self.task_id, reference_upload_cache_bypass=False)
        self._save_result(extra={
            "reference_image_cache_keys": keys,
            "reference_image_cache_hits": cache_hits,
            "reference_image_cache_misses": cache_misses,
            "reference_image_cache_waits": cache_waits,
            "reference_image_cache_account_id": account_id,
        })
        return self.uploaded_images[: len(unique_paths)]

    @staticmethod
    def _unique_images(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in items:
            uri = str(item.get("uri") or "")
            if not uri or uri in seen:
                continue
            seen.add(uri)
            out.append(item)
        return out

    @staticmethod
    def _is_region_restricted(url: str) -> bool:
        return url.startswith(REGION_RESTRICTED_URL)

    def _browser_executable_path(self) -> str | None:
        return resolve_browser_executable(self.settings.browser_executable_path)
