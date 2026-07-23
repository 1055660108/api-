from __future__ import annotations

import re
import unittest
from pathlib import Path


class AdminUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        admin_directory = Path(__file__).resolve().parents[1] / "app" / "admin"
        cls.html = (admin_directory / "index.html").read_text(encoding="utf-8")
        cls.javascript = (admin_directory / "app.js").read_text(encoding="utf-8")

    def test_settings_use_modals_and_read_only_account(self) -> None:
        self.assertIn('id="passwordModal"', self.html)
        self.assertIn('id="proxyModal"', self.html)
        self.assertIn('id="modelModal"', self.html)
        self.assertIn('id="changeAdminUsername" readonly', self.html)
        self.assertIn("单次消耗积分", self.html)
        self.assertIn("data-model-cost", self.javascript)
        self.assertIn("积分/次", self.javascript)

    def test_repository_update_control_is_present(self) -> None:
        for element_id in ("repositoryUpdatePanel", "repositoryUpdateState", "repositoryRevision", "updateRepository"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('/admin/repository-update', self.javascript)
        self.assertIn('els.updateRepository?.addEventListener("click", updateRepository)', self.javascript)
        self.assertIn('Promise.allSettled([loadRepositoryStatus(), loadProxyConfig()', self.javascript)
        self.assertIn('data.update_available ? "有可用更新" : "已是最新"', self.javascript)
        self.assertIn('els.repositoryRevision.textContent = data.version ? `v${data.version}` : "版本未知"', self.javascript)
        self.assertIn("await pollRepositoryUpdate()", self.javascript)
        self.assertIn("isTransientRepositoryUpdateError", self.javascript)
        self.assertIn("[502, 503, 504].includes(status)", self.javascript)
        self.assertIn("服务正在重启，将继续检查更新结果", self.javascript)
        self.assertIn("系统更新成功，前后端服务已恢复", self.javascript)

    def test_update_is_admin_only_and_proxy_has_single_entry(self) -> None:
        styles = (Path(__file__).resolve().parents[1] / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('body[data-portal="client"] #repositoryUpdatePanel', styles)
        self.assertNotIn('id="proxyConfigPanel"', self.html)
        self.assertEqual(self.html.count('id="openProxyModalFromNodes"'), 1)

    def test_proxy_nodes_can_be_filtered_by_country(self) -> None:
        self.assertIn('id="proxyCountryFilter"', self.html)
        self.assertIn('id="proxyNodeCount"', self.html)
        self.assertIn('node.country === state.proxyCountry', self.javascript)
        for status in ('"不可用"', '"已过期"', '"未检测"'):
            self.assertIn(status, self.javascript)
        self.assertIn('timeout: refresh ? 60000 : 20000', self.javascript)
        self.assertIn('timeout: 90000', self.javascript)
        self.assertIn('节点已更新，延迟未完成', self.javascript)

    def test_release_version_is_displayed_in_sidebar_and_admin_update_panel(self) -> None:
        self.assertIn('id="sidebarVersion"', self.html)
        self.assertIn('sidebarVersion: document.getElementById("sidebarVersion")', self.javascript)
        self.assertNotIn("data.revision", self.javascript)
        self.assertNotIn("data.commit_message", self.javascript)
        self.assertIn('els.repositoryRevision.textContent = data.version ? `v${data.version}` : "版本未知"', self.javascript)

    def test_dashboard_copy_and_desktop_scroll_regions_are_simplified(self) -> None:
        styles = (Path(__file__).resolve().parents[1] / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
        self.assertNotIn("实时运行概览", self.html)
        self.assertNotIn("监控服务、并发与任务队列", self.html)
        self.assertIn("height: 100vh", styles)
        self.assertIn("overflow-y: auto", styles)

    def test_proxy_settings_support_node_subscriptions(self) -> None:
        for element_id in ("proxySource", "proxySubscriptionUrl", "proxyApiUrl"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('proxy_subscription_url', self.javascript)
        self.assertIn('proxy_subscription_refresh_seconds: 900', self.javascript)
        self.assertIn('toast("请输入节点订阅链接", "error")', self.javascript)
        self.assertIn('els.proxySource?.addEventListener("change", updateProxySourceFields)', self.javascript)
        self.assertIn('function proxySubscriptionError(error)', self.javascript)
        self.assertIn('await loadProxyNodes(source === "subscription")', self.javascript)

    def test_client_security_pagination_and_package_management_are_present(self) -> None:
        for element_id in ("clientPasswordModal", "clientEmailModal", "openClientEmailModal", "prevUserPage", "nextUserPage", "packageModal", "packageList"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('/auth/password', self.javascript)
        self.assertIn('/video-visibility', self.javascript)
        self.assertIn('/admin/points/packages', self.javascript)

    def test_registration_email_verification_controls_are_present(self) -> None:
        for element_id in ("clientEmailLocal", "clientEmailDomain", "clientEmailCode", "sendEmailCode", "emailConfigPanel", "emailModal"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('/auth/register/email-code', self.javascript)
        self.assertIn('/auth/register/email-domains', self.javascript)
        self.assertIn('/auth/email', self.javascript)
        self.assertIn('/config/registration-email', self.javascript)

    def test_email_domains_are_independently_editable_and_free_copy_is_renamed(self) -> None:
        self.assertIn('id="addRegistrationEmailDomain"', self.html)
        self.assertIn('data-email-domain', self.javascript)
        self.assertIn('data-remove-email-domain', self.javascript)
        self.assertNotIn("免费试用", self.html)
        self.assertNotIn("免费试用", self.javascript)
        self.assertIn("视频额度", self.html)
        self.assertIn("视频额度", self.javascript)

    def test_dynamic_purchase_copy_is_removed(self) -> None:
        for text in ("固定套餐", "支付渠道准备中", "当前仅展示固定套餐"):
            self.assertNotIn(text, self.html)
            self.assertNotIn(text, self.javascript)

    def test_account_platform_count_is_present_and_dynamic(self) -> None:
        self.assertIn('id="accountPlatformCount"', self.html)
        self.assertIn('id="accountNormalCount"', self.html)
        self.assertIn('id="accountAbnormalCount"', self.html)
        self.assertIn('全部平台 0 个', self.html)
        self.assertIn('`${platformLabel} ${Number(stats.total ?? state.accountTotal)} 个`', self.javascript)

    def test_purchase_bonus_emphasizes_the_free_use_count(self) -> None:
        styles = (Path(__file__).resolve().parents[1] / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('class="purchase-package"', self.javascript)
        self.assertIn("purchase-package-meta", self.javascript)
        self.assertIn(".purchase-options button.purchase-package", styles)

    def test_task_refresh_uses_stable_order_and_latest_response(self) -> None:
        self.assertIn("const requestId = ++state.taskRefreshRequestId", self.javascript)
        self.assertIn("requestId !== state.taskRefreshRequestId", self.javascript)
        self.assertIn("state.tasks = tasks", self.javascript)
        self.assertIn('if (!options.quiet) setBusy(els.refreshTasks, false);', self.javascript)
        self.assertNotIn("function compareTasks", self.javascript)

    def test_membership_discount_allows_zero(self) -> None:
        self.assertIn('id="membershipTaskDiscount" type="number" min="0" step="0.1" value="0"', self.html)
        self.assertIn('data-membership-discount type="number" min="0" step="0.1"', self.javascript)

    def test_submit_cost_shows_active_membership_discount_and_queue_state(self) -> None:
        self.assertIn('`${state.membership.name} · 减免后需 ${discountedCost} 积分`', self.javascript)
        self.assertIn('Math.max(0.1, Math.round((modelCost - membershipDiscount) * 10) / 10)', self.javascript)
        self.assertIn('data.queued_for_concurrency', self.javascript)
        self.assertIn('空出并发后自动执行', self.javascript)

    def test_task_and_account_tables_use_server_pagination(self) -> None:
        self.assertIn('const data = await apiFetch(`/tasks?${params}`)', self.javascript)
        self.assertIn('const data = await apiFetch(`/accounts?${params}`)', self.javascript)
        self.assertIn('page_size: String(state.pageSize)', self.javascript)
        self.assertIn('page_size: String(state.accountPageSize)', self.javascript)
        self.assertIn('state.taskSearchTimer = window.setTimeout', self.javascript)
        self.assertIn('state.accountSearchTimer = window.setTimeout', self.javascript)
        self.assertIn('const requestId = ++state.accountRefreshRequestId', self.javascript)
        self.assertNotIn('const filteredAccounts = state.accounts.filter', self.javascript)

    def test_task_status_contract_uses_authoritative_terminal_state(self) -> None:
        success_index = self.javascript.index('if (rawStatus === "success") return')
        running_index = self.javascript.index('if (rawStatus === "running" || rawStatus === "submitted")')
        query_success_index = self.javascript.index('if (resultCode === "2" || resultUrl) return')
        self.assertLess(query_success_index, success_index)
        self.assertLess(query_success_index, running_index)
        self.assertIn('if (rawStatus === "failed") return { state: "failed"', self.javascript)
        self.assertIn('if (rawStatus === "canceled") return { state: "failed"', self.javascript)
        self.assertIn('label: "重试中"', self.javascript)
        self.assertIn('`第 ${Math.min(retryCount, 2)} / 2 次`', self.javascript)
        self.assertNotIn("activeIds.has(task.id)", self.javascript)
        self.assertIn('label: "待执行"', self.javascript)
        self.assertNotIn("重试 ${retryCount}", self.javascript)
        self.assertIn("生成异常请重试！", self.javascript)
        self.assertNotIn('const rawStatus = String(task.status || "未知")', self.javascript)

    def test_generation_wait_copy_uses_current_range(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.assertIn("预计等待 3~8 分钟", (root / "app" / "query.py").read_text(encoding="utf-8"))
        self.assertIn("预计等待 3~8 分钟", (root / "app" / "worker.py").read_text(encoding="utf-8"))

    def test_task_batch_query_contract_batches_repaints_and_storage(self) -> None:
        self.assertIn("if (!options.deferRender) renderTaskTable()", self.javascript)
        self.assertIn("await queryTask(id, { quiet: true, deferRender: true })", self.javascript)
        self.assertIn("renderTaskTable({ skipUnchanged: true })", self.javascript)
        self.assertIn('taskRenderSignature: ""', self.javascript)
        self.assertIn('setAttribute("aria-busy", "true")', self.javascript)

    def test_task_table_uses_stable_operational_layout(self) -> None:
        styles = (Path(__file__).resolve().parents[1] / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("scrollbar-gutter: stable both-edges", styles)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", styles)
        self.assertIn("#tasksView .task-table td", styles)

    def test_every_static_non_navigation_button_is_bound(self) -> None:
        button_tags = re.findall(r"<button\b([^>]*)>", self.html, flags=re.IGNORECASE)
        button_ids = []
        for attributes in button_tags:
            match = re.search(r'\bid="([^"]+)"', attributes, flags=re.IGNORECASE)
            if match and "nav-item" not in attributes:
                button_ids.append(match.group(1))
        self.assertGreaterEqual(len(set(button_ids)), 100)
        for button_id in sorted(set(button_ids)):
            self.assertIn(f'getElementById("{button_id}")', self.javascript, button_id)
            self.assertIn(f"els.{button_id}", self.javascript, button_id)

    def test_mobile_navigation_uses_compact_horizontal_workspace_tabs(self) -> None:
        styles = (Path(__file__).resolve().parents[1] / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("scroll-snap-type: x proximity", styles)
        self.assertIn("grid-template-columns: auto minmax(0, 1fr)", styles)
        self.assertIn("overflow-x: auto", styles)

    def test_hs_brand_and_dola_default_quota_are_present(self) -> None:
        self.assertEqual(self.html.count('class="brand-mark"'), 2)
        self.assertEqual(self.html.count('aria-label="HS"'), 2)
        self.assertIn('href="/admin/assets/hs-logo.png', self.html)
        self.assertEqual(self.html.count('src="/admin/assets/hs-logo.png'), 3)
        self.assertNotIn('<div class="brand-mark">DF</div>', self.html)
        self.assertIn('value="1" placeholder="Dola 默认 1"', self.html)
        self.assertIn('platform === "dola" ? 1', self.javascript)

    def test_client_ink_entry_flow_preserves_existing_auth_and_workspace(self) -> None:
        root = Path(__file__).resolve().parents[1]
        styles = (root / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
        ink_script = (root / "app" / "admin" / "ink-bg.js").read_text(encoding="utf-8")
        self.assertIn('id="clientInkCanvas"', self.html)
        self.assertIn('id="clientWorkspaceInk"', self.html)
        self.assertIn('id="openClientLogin"', self.html)
        self.assertIn('id="returnClientLanding"', self.html)
        self.assertNotIn('灵感正在聚合，', self.html)
        self.assertNotIn('思维正在展开', self.html)
        self.assertIn('id="clientInkSplatters"', self.html)
        self.assertIn('<span class="client-register-prompt">还没有账户？</span>', self.html)
        self.assertIn('/admin/assets/ink-bg.js?v=1.4.3', self.html)
        self.assertIn('data-client-stage="landing"', self.html)
        self.assertIn('id="loginButton" type="submit">登录</button>', self.html)
        self.assertIn('id="clientRegisterTab" type="button">注册</button>', self.html)
        self.assertIn('id="loginHeadingTitle">登录</h1>', self.html)
        self.assertIn('els.loginHeadingTitle.textContent = register ? "注册" : "登录"', self.javascript)
        self.assertIn('startClientLoginTransition()', self.javascript)
        self.assertIn('els.returnClientLanding?.addEventListener("click"', self.javascript)
        self.assertIn('clientEntryInk?.randomize?.()', self.javascript)
        self.assertIn('id="logoutConfirmModal"', self.html)
        self.assertIn('openLogoutConfirmation()', self.javascript)
        self.assertIn('jobs.push(loadClientNotifications(), loadMemberships(), loadClientProfile())', self.javascript)
        self.assertIn('clientEntryInk?.setMode(inkMode', self.javascript)
        self.assertIn('clientWorkspaceInk?.setMode("workspace", true)', self.javascript)
        self.assertIn('document.addEventListener("pointerdown", createClientInkSplash)', self.javascript)
        self.assertIn('body[data-portal="client"] .client-entry-header', styles)
        self.assertIn('body[data-portal="admin"] .client-entry-header', styles)
        self.assertIn('body[data-portal] .client-workspace-ink', styles)
        self.assertIn('body[data-portal="client"] .app-shell > .sidebar', styles)
        self.assertIn('class HSInkBackground', ink_script)
        self.assertIn('powerPreference: "high-performance"', ink_script)
        self.assertIn('requestAnimationFrame(this.render)', ink_script)
        self.assertIn('gl.enable(gl.SCISSOR_TEST)', ink_script)
        self.assertIn('vec3 wash = vec3(0.74, 0.75, 0.745)', ink_script)
        self.assertIn('const count = compact ? 28 : 46', self.javascript)
        self.assertIn('.login-panel.register-mode .client-login-only', styles)
        self.assertNotIn('保存后系统将签发新 Token，并迁移当前账号的历史任务归属。', self.html)
        self.assertIn('.ratio-options button.active', styles)
        self.assertIn('.billing-priority-control input { accent-color: #171a19; }', styles)
        self.assertIn('sidebar-content-hidden', self.javascript)
        self.assertIn('.app-shell.sidebar-content-hidden .sidebar-client-identity', styles)

    def test_user_action_buttons_use_aligned_grid(self) -> None:
        styles = (Path(__file__).resolve().parents[1] / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".users-table .user-point-actions", styles)
        self.assertIn("grid-template-columns: repeat(2, minmax(64px, 1fr))", styles)

    def test_admin_cookie_auth_is_not_overridden_by_legacy_token(self) -> None:
        self.assertIn(
            'const savedToken = portal === "client" ? tokenFromUrl || localStorage.getItem(portalStorageKey(TOKEN_KEY)) || "" : "";',
            self.javascript,
        )
        self.assertIn('localStorage.removeItem("dola_fetch_api_token");', self.javascript)

    def test_dola_submit_interval_is_conservative(self) -> None:
        worker = (Path(__file__).resolve().parents[1] / "app" / "worker.py").read_text(encoding="utf-8")
        self.assertIn("submit_interval = load_settings().dola_submit_interval_seconds", worker)
        self.assertIn("delay = submit_interval -", worker)
        self.assertIn('id="dolaSubmitInterval" type="number" min="1" max="5" step="0.1"', self.html)
        self.assertIn('apiFetch("/config/runtime"', self.javascript)
        self.assertIn("RESULT_WATCH_DEADLINE_MINUTES = 8", worker)
        self.assertIn("生成超过8分钟，正在重试", worker)
        self.assertIn(r"/生成超过\d+分钟", self.javascript)

    def test_requested_security_and_node_copy_is_simplified(self) -> None:
        self.assertNotIn('<span class="modal-eyebrow">账号安全</span><h2 id="clientPasswordModalTitle">', self.html)
        self.assertNotIn('<span class="modal-eyebrow">账号安全</span><h2 id="clientEmailModalTitle">', self.html)
        self.assertIn("修改密码后API Token自动重置。", self.html)
        self.assertNotIn("Dola 网络出口", self.html)

    def test_message_center_replaces_settings_feedback_entry(self) -> None:
        self.assertIn('id="messagesNavItem"', self.html)
        self.assertIn('id="messagesView"', self.html)
        self.assertIn('id="clientFeedbackList"', self.html)
        self.assertIn('id="clientNotificationList"', self.html)
        self.assertIn('id="adminNotificationForm"', self.html)
        self.assertNotIn("client-feedback-panel", self.html)
        self.assertIn('apiFetch("/feedback")', self.javascript)
        self.assertIn('apiFetch("/notifications")', self.javascript)
        self.assertIn('apiFetch("/admin/notification-recipients")', self.javascript)
        self.assertIn("saveFeedbackRecord", self.javascript)

    def test_points_messages_memberships_and_cards_are_wired(self) -> None:
        for element_id in ("pointCardsNavItem", "pointCardForm", "pointCardSearch", "openPointCardModal", "redeemForm", "transactionsView", "membershipList", "membershipModal", "membershipConcurrency", "membershipTaskDiscount", "membershipDetailsButton", "membershipDetailsModal", "membershipBonus", "packagePaymentUrl", "userSearch", "announcementLevel", "emergencyAnnouncementOverlay", "smallAnnouncementToast", "repositoryLatestVersion", "sidebarMembershipName", "sidebarVersion", "dashboardPointsBalance", "openMyPrompts", "promptPickerModal", "promptPickerList", "promptPickerPrev", "promptPickerNext", "messagesRefreshState", "billingPriorityControl", "billingPriorityState"):
            self.assertIn(f'id="{element_id}"', self.html)
        for endpoint in ("/admin/point-cards", "/points/redeem", "/points/transactions", "/admin/memberships", "/memberships/", "/admin/announcements", "/admin/notifications/", "/admin/feedback/", "/notifications/read-all"):
            self.assertIn(endpoint, self.javascript)
        self.assertIn("https://pay.ldxp.cn/shop/huisu/fhm9gj", self.javascript)
        self.assertIn("8000", self.javascript)
        self.assertIn('/auth/billing-priority', self.javascript)
        self.assertNotIn("Prompt Library", self.html)
        self.assertIn("视频额度变动", self.html)
        self.assertIn('video_quota_consume: "额度使用"', self.javascript)
        self.assertIn('item.video_quota_change', self.javascript)
        self.assertIn('data-announcement-level="small"', self.html)
        self.assertIn('data-announcement-level="large"', self.html)
        self.assertIn('data-announcement-level="emergency"', self.html)
        self.assertIn('setAnnouncementComposerLevel(button.dataset.announcementLevel)', self.javascript)
        self.assertIn('els.refreshTransactions?.addEventListener("click", refreshTransactions)', self.javascript)
        self.assertIn('els.refreshPointCards?.addEventListener("click", refreshPointCards)', self.javascript)
        self.assertIn('toast("消费明细已刷新")', self.javascript)
        self.assertIn('toast("卡密记录已刷新")', self.javascript)
        styles = (Path(__file__).resolve().parents[1] / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".ledger-table tbody tr", styles)
        self.assertIn("height: 108px", styles)
        self.assertIn("overflow-wrap: anywhere", styles)
        self.assertNotIn(".ledger-task-id { width: fit-content", styles)
        self.assertIn("#feedbackModal .feedback-modal-panel", styles)
        self.assertIn(".announcement-level-picker", styles)
        self.assertIn(".ledger-table .ledger-detail-col { width: 28%; }", styles)
        self.assertIn("积分减免", self.html + self.javascript)
        self.assertNotIn("单任务积分减免", self.html + self.javascript)
        self.assertNotIn("单任务减免", self.html + self.javascript)

    def test_client_home_prompt_cards_and_user_actions_match_122_contract(self) -> None:
        for text in ("用户首页", "我的视频", "提示词库", "会员订阅", "购买积分", "当前积分"):
            self.assertIn(text, self.html)
        self.assertIn('<div><h2>积分套餐</h2></div>', self.html)
        self.assertIn("panel-action-header", self.html)
        for removed in (
            "积分账号",
            "购买套餐或使用卡密为当前账号充值。",
            "购买后将在新页面完成支付。",
            "记录积分与视频额度的增加、使用和退款。",
            "提交使用问题，并在这里查看管理员的处理状态和回复。",
            "选择视频后可批量删除对应任务记录",
            "完善描述并选择生成参数",
            "保存创作灵感，拖动排序，点击即可带入任务。",
        ):
            self.assertNotIn(removed, self.html)
        for removed in ("一处掌控所有生成任务", "可用于第三方软件调用，消耗同一积分余额，请妥善保管。", "账户概览", "系统运行概况"):
            self.assertNotIn(removed, self.html)
        self.assertIn("function newPromptId()", self.javascript)
        self.assertIn('data-copy-point-card', self.javascript)
        self.assertNotIn('item.code || item.code_hint', self.javascript)
        self.assertIn('data-prompt-picker-id', self.javascript)
        self.assertIn('data-delete-feedback', self.javascript)
        self.assertIn('data-delete-notification', self.javascript)
        self.assertIn('data-delete-announcement', self.javascript)

    def test_client_copy_and_redeem_modal_match_133_contract(self) -> None:
        for element_id in ("openRedeemModal", "redeemModal", "redeemForm", "purchaseHistoryList", "refreshPurchaseHistory"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertNotIn("高级会员优先生效，暂停的低级会员不会消耗剩余天数。", self.html)
        self.assertNotIn('<label class="field search-field">\n                <span>搜索</span>', self.html)
        self.assertNotIn("管理员发送给你的服务通知和处理消息。", self.html)
        self.assertNotIn("查看管理员发布的平台公告。", self.html)
        self.assertIn("暂无反馈", self.javascript)
        self.assertIn('isClient ? "用户设置" : "设置"', self.javascript)
        self.assertIn("async function loadPurchaseHistory()", self.javascript)
        self.assertIn('["redeem", "admin_credit"]', self.javascript)
        styles = (Path(__file__).resolve().parents[1] / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
        self.assertNotIn('body[data-portal="client"][data-view="points"] .page-header {\n  display: none;', styles)
        self.assertIn(".redeem-modal-panel", styles)

    def test_sidebar_cards_and_message_history_match_134_contract(self) -> None:
        for element_id in (
            "toggleSidebar",
            "selectAllPointCards",
            "deletePointCards",
            "deleteUnusedPointCards",
            "deleteRedeemedPointCards",
            "openNotificationHistory",
            "notificationHistoryModal",
            "openAnnouncementHistory",
            "announcementHistoryModal",
            "effectiveWorkersInput",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertGreaterEqual(self.html.count('class="nav-icon" data-lucide='), 16)
        self.assertIn('/admin/assets/lucide.min.js?v=', self.html)
        self.assertIn('apiFetch("/admin/point-cards/delete"', self.javascript)
        self.assertIn('data-point-card-select=', self.javascript)
        self.assertIn('localStorage.setItem("dola_sidebar_collapsed"', self.javascript)
        self.assertNotIn("完整兑换码会保存到卡密列表，可随时复制或导出。", self.html)
        self.assertNotIn('<span class="modal-eyebrow">卡密积分</span>', self.html)
        styles = (Path(__file__).resolve().parents[1] / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('body[data-portal="client"][data-view="points"] .page-header { display: none; }', styles)
        self.assertIn(".app-shell.sidebar-collapsed", styles)
        self.assertIn(".announcement-lock-control", styles)
        self.assertIn('id="criticalPortalStyles"', self.html)
        self.assertIn('body[data-portal="client"] #pointCardsNavItem', self.html)
        self.assertIn('body[data-portal="client"] .admin-credential-field', self.html)
        self.assertIn('max_effective_workers: maxEffectiveWorkers', self.javascript)
        self.assertIn('Number(item.task_discount_points || 0) > 0', self.javascript)
        self.assertNotIn('id="pointCardDeleteMode"', self.html)
        self.assertIn('data-point-card-delete-mode="unused"', self.html)
        self.assertIn('data-point-card-delete-mode="redeemed"', self.html)
        self.assertIn('if (!window.confirm("确认删除视频？")) return;', self.javascript)
        self.assertNotIn("从本端隐藏", self.javascript)
        self.assertNotIn('<span class="eyebrow">视频任务</span>', self.html)
        self.assertIn('item.kind === "redeem" ? "积分充值"', self.javascript)
        self.assertIn("membershipTierNumber", self.javascript)
        self.assertIn(".video-empty-state", styles)
        self.assertIn("scrollbar-color: #c8cdd1 #f5f6f7", styles)
        self.assertNotIn('data-user-concurrency=', self.javascript)
        self.assertIn("updateMembershipRemaining", self.javascript)
        self.assertIn('return `${state.membership.name} · 剩余 ${parts.join(" ")}`', self.javascript)
        self.assertIn("const text = membershipRemainingText();", self.javascript)
        self.assertNotIn('`${state.membership.name} 至 ${formatTime(state.membership.expires_at)}`', self.javascript)
        self.assertIn("document.hidden", self.javascript)
        self.assertIn("30000", self.javascript)
        self.assertIn('apiFetch("/auth/access-state")', self.javascript)
        self.assertIn("15000", self.javascript)
        self.assertIn("state.accessRefreshing", self.javascript)
        self.assertIn("已扣除 ${data.billing.points_used} 积分", self.javascript)
        self.assertNotIn("创建新任务", self.html)
        self.assertNotIn("历史掩码", self.html + self.javascript)
        self.assertIn('addEventListener("click", refreshMessageCenter)', self.javascript)


if __name__ == "__main__":
    unittest.main()
