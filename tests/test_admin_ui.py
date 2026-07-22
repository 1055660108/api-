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
        self.assertNotIn("function compareTasks", self.javascript)

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
        self.assertEqual(self.html.count('src="/admin/assets/hs-logo.png'), 2)
        self.assertNotIn('<div class="brand-mark">DF</div>', self.html)
        self.assertIn('value="1" placeholder="Dola 默认 1"', self.html)
        self.assertIn('platform === "dola" ? 1', self.javascript)

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
        self.assertIn("DOLA_SUBMIT_INTERVAL_SECONDS = 5.0", worker)
        self.assertIn("DOLA_SUBMIT_INTERVAL_SECONDS -", worker)
        self.assertIn("RESULT_WATCH_DEADLINE_MINUTES = 8", worker)
        self.assertIn("生成超过8分钟，正在重试", worker)
        self.assertIn(r"/生成超过\d+分钟", self.javascript)

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
        for text in ("用户首页", "我的视频", "提示词库", "会员订阅", "积分账号", "当前积分"):
            self.assertIn(text, self.html)
        for removed in ("一处掌控所有生成任务", "可用于第三方软件调用，消耗同一积分余额，请妥善保管。", "账户概览", "系统运行概况"):
            self.assertNotIn(removed, self.html)
        self.assertIn("function newPromptId()", self.javascript)
        self.assertIn('data-copy-point-card', self.javascript)
        self.assertNotIn('item.code || item.code_hint', self.javascript)
        self.assertIn('data-prompt-picker-id', self.javascript)
        self.assertIn('data-delete-feedback', self.javascript)
        self.assertIn('data-delete-notification', self.javascript)
        self.assertIn('data-delete-announcement', self.javascript)
        self.assertNotIn('data-user-concurrency=', self.javascript)
        self.assertIn("updateMembershipRemaining", self.javascript)
        self.assertIn('return `${state.membership.name} · 剩余 ${parts.join(" ")}`', self.javascript)
        self.assertIn("const text = membershipRemainingText();", self.javascript)
        self.assertNotIn('`${state.membership.name} 至 ${formatTime(state.membership.expires_at)}`', self.javascript)
        self.assertIn("document.hidden", self.javascript)
        self.assertIn("30000", self.javascript)
        self.assertIn('apiFetch("/auth/access-state")', self.javascript)
        self.assertIn("5000", self.javascript)
        self.assertIn("已扣除 ${data.billing.points_used} 积分", self.javascript)
        self.assertNotIn("创建新任务", self.html)
        self.assertNotIn("历史掩码", self.html + self.javascript)
        self.assertIn('addEventListener("click", refreshMessageCenter)', self.javascript)


if __name__ == "__main__":
    unittest.main()
