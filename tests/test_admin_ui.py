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

    def test_user_balance_modal_supports_points_and_video_quota(self) -> None:
        self.assertIn('id="userBalanceModal"', self.html)
        self.assertIn('data-user-balance-type="points"', self.html)
        self.assertIn('data-user-balance-type="video_quota"', self.html)
        self.assertIn("function openSettingsModal(modal, focusTarget)", self.javascript)
        self.assertNotIn("const openSettingsModal =", self.javascript)
        self.assertIn('balance_type: userBalanceState.balanceType', self.javascript)
        self.assertIn('id="userBalanceVisibleToClient"', self.html)
        self.assertIn('els.userBalanceVisibilityRow?.classList.toggle("hidden", credit)', self.javascript)
        self.assertIn('visible_to_client: userBalanceState.action === "credit" || Boolean(els.userBalanceVisibleToClient?.checked)', self.javascript)
        self.assertIn('data-user-quota-balance=', self.javascript)
        self.assertIn('admin_video_quota_credit: "管理员充值视频额度"', self.javascript)
        self.assertNotIn('window.prompt("请输入充值积分数量"', self.javascript)
        self.assertNotIn('window.prompt(`请输入扣除积分数量', self.javascript)

    def test_settings_use_modals_and_read_only_account(self) -> None:
        self.assertIn('id="passwordModal"', self.html)
        self.assertIn('id="proxyModal"', self.html)
        self.assertIn('id="modelModal"', self.html)
        self.assertIn('id="changeAdminUsername" readonly', self.html)
        self.assertIn("各时长积分", self.html)
        self.assertIn("data-model-duration-cost", self.javascript)
        self.assertIn("各时长积分", self.javascript)

    def test_client_api_documentation_is_isolated_from_admin_endpoints(self) -> None:
        styles = (Path(__file__).resolve().parents[1] / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('id="apiDocsNavItem"', self.html)
        self.assertIn('id="api-docsView"', self.html)
        self.assertIn('body[data-portal="admin"] #apiDocsNavItem', styles)
        self.assertIn('body[data-portal="admin"] #api-docsView', styles)
        section = self.html.split('<section class="view" id="api-docsView"', 1)[1].split('<section class="view" id="docsView"', 1)[0]
        for endpoint in (
            "/v1/models",
            "/v1/videos",
            "/v1/api/v3/contents/generations/tasks",
            "/v1/chat/completions",
            "/tasks/{TASK_ID}",
        ):
            self.assertIn(endpoint, section)
        self.assertIn("reference_is_real_person", section)
        for admin_endpoint in ("/config/", "/users", "/accounts", "/admin/", "repository-update", "proxy-api"):
            self.assertNotIn(admin_endpoint, section)

    def test_admin_only_settings_use_one_stable_scrollable_grid(self) -> None:
        styles = (Path(__file__).resolve().parents[1] / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
        self.assertEqual(self.html.count('id="invitationConfigPanel"'), 1)
        self.assertEqual(self.html.count('id="adminAuditPanel"'), 1)
        self.assertEqual(self.html.count('class="admin-settings-grid"'), 1)
        self.assertNotIn('class="admin-settings-columns"', self.html)
        self.assertNotIn('class="admin-settings-compact-grid"', self.html)
        self.assertIn('body[data-portal="client"] .admin-settings-grid', styles)
        self.assertIn('grid-template-columns: repeat(2, minmax(0, 1fr));', styles)
        self.assertIn('#settingsView.active .settings-layout', styles)
        self.assertIn('scrollbar-gutter: stable;', styles)
        self.assertIn('id="batchHistoryRetentionDays"', self.html)
        self.assertIn('id="taskRetryLimit"', self.html)
        self.assertIn('id="doubaoSubmitRetryLimit"', self.html)
        self.assertIn('task_retry_limit: retryLimit', self.javascript)
        self.assertIn('doubao_submit_retry_limit: doubaoRetryLimit', self.javascript)
        self.assertIn('id="resourceMonitorPanel"', self.html)
        self.assertIn('id="resourceAlertBanner"', self.html)
        self.assertIn('renderResourceMonitoring(data.components?.monitoring || {})', self.javascript)
        self.assertIn('body[data-portal="client"] .settings-stack {\n  align-content: start;', styles)
        self.assertIn('body[data-portal="client"] .client-billing-panel {\n  grid-column: 1 / -1;', styles)
        self.assertIn('body[data-portal="client"] .settings-card-value {\n    display: grid;', styles)

    def test_account_header_uses_wrapping_grid_without_overflow(self) -> None:
        styles = (Path(__file__).resolve().parents[1] / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('.accounts-panel > .panel-header {\n  display: grid;', styles)
        self.assertIn('grid-template-columns: minmax(420px, 1fr) auto minmax(180px, 240px) auto auto;', styles)
        self.assertIn('grid-template-columns: auto minmax(96px, 1fr) auto minmax(112px, 1fr) auto;', styles)

    def test_repository_update_control_is_present(self) -> None:
        for element_id in ("repositoryUpdatePanel", "repositoryUpdateState", "repositoryUpdateError", "repositoryRevision", "updateRepository"):
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
        self.assertIn("showRepositoryUpdateError(data.error)", self.javascript)

    def test_backup_restore_and_video_download_settings_are_bound(self) -> None:
        for element_id in ("dataBackupPanel", "downloadDataBackup", "dataRestoreFile", "openVideoDownloadSettings", "videoDownloadSettingsModal", "selectVideoDownloadFolder"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('fetch("/admin/data-backup"', self.javascript)
        self.assertIn('apiFetch("/admin/data-restore"', self.javascript)
        self.assertIn('els.openVideoDownloadSettings?.addEventListener("click"', self.javascript)
        self.assertIn('els.selectVideoDownloadFolder?.addEventListener("click", selectDownloadDirectory)', self.javascript)

    def test_disk_cleanup_control_is_bound(self) -> None:
        for element_id in ("runDiskCleanup", "diskCleanupState"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('apiFetch("/admin/disk-cleanup"', self.javascript)
        self.assertIn('els.runDiskCleanup?.addEventListener("click", runDiskCleanup)', self.javascript)

    def test_update_is_admin_only_and_proxy_has_single_entry(self) -> None:
        styles = (Path(__file__).resolve().parents[1] / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('body[data-portal="client"] #repositoryUpdatePanel', styles)
        self.assertNotIn('id="proxyConfigPanel"', self.html)
        self.assertEqual(self.html.count('id="openProxyModalFromNodes"'), 1)

    def test_proxy_nodes_can_be_filtered_by_country(self) -> None:
        self.assertIn('id="proxyCountryFilter"', self.html)
        self.assertIn('id="proxyLatencyThreshold"', self.html)
        self.assertIn('id="proxyNodeCount"', self.html)
        self.assertIn('state.proxyCountries.includes(node.country)', self.javascript)
        self.assertIn('proxy_auto_countries: state.proxyCountries', self.javascript)
        for status in ('"不可用"', '"已过期"', '"未检测"'):
            self.assertIn(status, self.javascript)
        self.assertIn('timeout: refreshSubscription ? 60000 : 20000', self.javascript)
        self.assertIn('timeout: 90000', self.javascript)
        self.assertIn('节点已更新，延迟未完成', self.javascript)

    def test_task_retry_and_batch_reference_images_are_available(self) -> None:
        for element_id in ("batchSharedImageInput", "batchMappedImageInput", "batchRowImageInput", "batchReferenceState", "referenceIsRealPerson", "batchReferenceIsRealPerson"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('data-action="retry"', self.javascript)
        self.assertIn('/retry`, { method: "POST"', self.javascript)
        self.assertIn('data-batch-image-index', self.javascript)
        self.assertIn('form.append("images", file, file.name)', self.javascript)
        for element_id in ("referenceModal", "referenceGallery", "closeReferenceModal", "confirmReferenceModal"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('data-action="open-references"', self.javascript)
        self.assertIn('/references/${index + 1}', self.javascript)
        self.assertIn('task.reference_image_names', self.javascript)
        self.assertIn('class="task-reference-names"', self.javascript)
        self.assertIn('form.append("reference_is_real_person", els.referenceIsRealPerson?.checked ? "true" : "false")', self.javascript)
        self.assertIn('reference_is_real_person: Boolean(state.batchReferenceIsRealPerson)', self.javascript)
        self.assertIn('referenceIsRealPerson: Boolean(state.batchReferenceIsRealPerson)', self.javascript)

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
        for element_id in ("proxySource", "proxySubscriptionUrl", "proxyAccountField", "proxyAccountScheme", "proxyAccountHost", "proxyAccountPort", "proxyAccountUsername", "proxyAccountPassword", "proxyApiUrl", "proxyApiScheme", "testProxyApi", "openAccountProxyImport", "accountProxyImportModal", "accountProxyImportText", "accountProxyBulkbar", "proxyHealthRefreshMinutes", "proxyRefreshCountdown", "dolaProxySource", "doubaoProxySource", "qianwenProxySource", "dolaProxyRandom", "doubaoProxyRandom", "qianwenProxyRandom", "savePlatformProxyRouting"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('<option value="account">账密连接</option>', self.html)
        self.assertIn('id="proxyAccountPassword" type="password"', self.html)
        self.assertIn('autocomplete="new-password"', self.html)
        self.assertIn('proxy_subscription_url', self.javascript)
        self.assertIn('proxy_account_password', self.javascript)
        self.assertIn('apiFetch("/config/proxy-api/test"', self.javascript)
        self.assertIn('toast(`连接成功：${data.proxy_host_port}${latency}`)', self.javascript)
        self.assertIn('proxy_api_scheme: els.proxyApiScheme?.value || "http"', self.javascript)
        self.assertIn('proxy_source: source', self.javascript)
        self.assertIn('proxy_subscription_refresh_seconds: 900', self.javascript)
        self.assertIn('toast("请输入节点订阅链接", "error")', self.javascript)
        self.assertIn('els.proxySource?.addEventListener("change", updateProxySourceFields)', self.javascript)
        self.assertIn('platform_proxy_sources: sources', self.javascript)
        self.assertIn('platform_proxy_random: random', self.javascript)
        self.assertIn('function proxySubscriptionError(error)', self.javascript)
        self.assertIn('await loadProxyNodes(source === "subscription")', self.javascript)
        self.assertIn('/config/account-proxies/import', self.javascript)
        self.assertIn('/config/account-proxies/action', self.javascript)
        self.assertIn('function renderAccountProxyNodes()', self.javascript)
        self.assertIn('data-account-proxy-select', self.javascript)
        self.assertIn('已过滤 ${state.proxyFilteredCount} 个高延迟或不可用节点', self.javascript)
        self.assertIn('function updateProxyRefreshCountdown()', self.javascript)
        self.assertNotIn('每 10 分钟自动测速；未勾选国家时使用全部节点', self.html)

    def test_proxy_nodes_are_split_into_platform_configuration_views(self) -> None:
        for platform, label in (
            ("dola", "Dola 代理配置"),
            ("doubao", "豆包代理配置"),
            ("qianwen", "千问代理配置"),
        ):
            self.assertIn(f'data-proxy-platform="{platform}"', self.html)
            self.assertIn(f'>{label}</span>', self.html)
            self.assertIn(f'data-proxy-platform-panel="{platform}"', self.html)
        self.assertIn('proxyPlatform: "dola"', self.javascript)
        self.assertIn('new URLSearchParams({ platform })', self.javascript)
        self.assertIn('/config/proxy-nodes?${query.toString()}', self.javascript)
        self.assertIn('body: { platform }', self.javascript)
        self.assertIn('platform: state.proxyPlatform, node_id: nodeId', self.javascript)
        self.assertIn('platform: state.proxyPlatform, node_ids: ids', self.javascript)
        self.assertIn('state.proxyConfigPlatform = state.proxyPlatform || "dola"', self.javascript)

    def test_admin_submission_can_select_an_available_account(self) -> None:
        self.assertIn('id="preferredAccountSelect"', self.html)
        self.assertIn('class="composer-select admin-credential-field"', self.html)
        self.assertIn('apiFetch(`/accounts/available?platform=${encodeURIComponent(platform)}&duration=${encodeURIComponent(duration)}`)', self.javascript)
        self.assertIn('const durationLabel = account.ten_second_only ? " / 仅5-10秒" : ""', self.javascript)
        self.assertIn('loadPreferredAccounts().catch((error) => toast(`可用账号读取失败：${error.message}`', self.javascript)
        self.assertIn('const platformLabel = PLATFORM_LABELS[platform] || platform', self.javascript)
        self.assertIn('<option value="doubao">豆包</option>', self.html)
        self.assertIn('<option value="qianwen">千问</option>', self.html)
        self.assertIn('form.append("preferred_account_id", els.preferredAccountSelect.value)', self.javascript)
        self.assertIn('preferredAccountId: portal === "admin"', self.javascript)

    def test_manual_account_import_can_mark_api_accounts(self) -> None:
        self.assertIn('id="accountIsApi" type="checkbox"', self.html)
        self.assertIn("标记为 API 账号", self.html)
        self.assertIn('accountIsApi: document.getElementById("accountIsApi")', self.javascript)
        self.assertIn('account_source: els.accountIsApi?.checked ? "api" : "admin"', self.javascript)

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

    def test_registration_security_toggle_and_admin_user_creation_are_present(self) -> None:
        for element_id in (
            "registrationSecurityPanel",
            "registrationSecurityEnabled",
            "openCreateUser",
            "createUserModal",
            "createUserForm",
            "createUserName",
            "createUserPassword",
            "createUserConfirmPassword",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('/config/registration-security', self.javascript)
        self.assertIn('await apiFetch("/users", { method: "POST"', self.javascript)
        self.assertIn('els.registrationSecurityEnabled?.addEventListener("change", saveRegistrationSecurityConfig)', self.javascript)

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
        self.assertIn('id="accountSliderVerificationCount"', self.html)
        self.assertIn('<option value="slider_verification">跳验证</option>', self.html)
        self.assertIn('<option value="ten_second">10秒</option>', self.html)
        self.assertIn('Boolean(item.ten_second_only)', self.javascript)
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
        self.assertIn('state.modelDiscounts?.[state.platform]?.[state.model]', self.javascript)
        self.assertIn('Math.max(0.1, Math.round((modelCost - membershipDiscount - modelDiscount) * 10) / 10)', self.javascript)
        self.assertIn('`专属减免后需 ${discountedCost} 积分`', self.javascript)
        self.assertIn('data.queued_for_concurrency', self.javascript)
        self.assertIn('空出并发后自动执行', self.javascript)

    def test_user_details_can_edit_per_model_discounts(self) -> None:
        self.assertIn('id="userModelDiscountList"', self.html)
        self.assertIn('id="saveUserModelDiscounts"', self.html)
        self.assertIn('data-user-model-discount', self.javascript)
        self.assertIn('/model-discounts`, { method: "PUT", body: { discounts } }', self.javascript)
        self.assertIn('renderUserModelDiscounts(data.model_discount_catalog)', self.javascript)

    def test_task_and_account_tables_use_server_pagination(self) -> None:
        self.assertIn('const data = await apiFetch(`/tasks?${params}`)', self.javascript)
        self.assertIn('const data = await apiFetch(`/accounts?${params}`)', self.javascript)
        self.assertIn('page_size: String(state.pageSize)', self.javascript)
        self.assertIn('page_size: String(state.accountPageSize)', self.javascript)
        self.assertIn('state.taskSearchTimer = window.setTimeout', self.javascript)
        self.assertIn('state.accountSearchTimer = window.setTimeout', self.javascript)
        self.assertIn('const requestId = ++state.accountRefreshRequestId', self.javascript)
        self.assertNotIn('const filteredAccounts = state.accounts.filter', self.javascript)

    def test_admin_task_status_filter_and_bulk_retry_controls_are_bound(self) -> None:
        self.assertIn('id="taskStatusFilter"', self.html)
        self.assertIn('<option value="generating">生成中</option>', self.html)
        self.assertIn('<option value="failed">失败</option>', self.html)
        self.assertIn('id="selectVisibleFailedTasks"', self.html)
        self.assertIn('id="retrySelectedTasks"', self.html)
        self.assertIn('id="retryAllFilteredTasks"', self.html)
        self.assertIn('params.set("status", state.taskStatusFilter)', self.javascript)
        self.assertIn('apiFetch("/tasks/bulk-retry"', self.javascript)
        self.assertIn('{ retry_all: true, q: els.taskSearch.value.trim(), platform: "" }', self.javascript)
        self.assertIn('els.retrySelectedTasks?.addEventListener("click"', self.javascript)
        self.assertIn('els.retryAllFilteredTasks?.addEventListener("click"', self.javascript)
        self.assertIn('input[data-task-select]', self.javascript)

    def test_api_proxy_pool_and_batch_failed_retry_controls_are_bound(self) -> None:
        for element_id in ("batchStatusFilter", "retrySelectedBatchTasks", "retryAllFailedBatchTasks"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('state.proxySource === "api"', self.javascript)
        self.assertIn('function renderApiProxyNodes()', self.javascript)
        self.assertIn('total_leases', self.javascript)
        self.assertIn('/batch-prompts/jobs/${encodeURIComponent(jobId)}/retry', self.javascript)
        self.assertIn('els.retrySelectedBatchTasks?.addEventListener', self.javascript)
        self.assertIn('els.retryAllFailedBatchTasks?.addEventListener', self.javascript)

    def test_expired_sessions_stop_polling_and_account_refreshes_are_coalesced(self) -> None:
        self.assertIn('function stopAutoRefresh()', self.javascript)
        self.assertIn('function showLogin(message = "等待输入") {\n  stopAutoRefresh();', self.javascript)
        self.assertIn('if (state.proxyCountdownTimer) window.clearInterval(state.proxyCountdownTimer);', self.javascript)
        self.assertIn('state.accountRefreshPending = true;', self.javascript)
        self.assertIn('state.accountRefreshPromise = request;', self.javascript)
        self.assertIn('if (!state.authenticated || els.appShell.classList.contains("hidden")) return;', self.javascript)
        self.assertIn('state.nextQuotaResetAt = "";\n    refreshAccounts({ quiet: true }).catch(() => {});', self.javascript)

    def test_task_status_contract_uses_authoritative_terminal_state(self) -> None:
        success_index = self.javascript.index('if (rawStatus === "success") return')
        running_index = self.javascript.index('if (rawStatus === "running" || rawStatus === "submitted")')
        query_success_index = self.javascript.index('if (resultCode === "2" || resultUrl) return')
        self.assertLess(query_success_index, success_index)
        self.assertLess(query_success_index, running_index)
        self.assertIn('if (rawStatus === "failed") return { state: "failed"', self.javascript)
        self.assertIn('if (rawStatus === "canceled") return { state: "failed"', self.javascript)
        self.assertIn('const terminal = ["failed", "canceled"].includes', self.javascript)
        self.assertIn('if (/生成超过\\d+分钟/.test(text)) return terminal ? "生成失败，请重试！" : "正在生成中，请稍等！";', self.javascript)
        self.assertIn('label: "重试已入队"', self.javascript)
        self.assertIn('"重试执行中"', self.javascript)
        self.assertIn('"重试等待结果"', self.javascript)
        self.assertIn('const statusReason = clientSafeText(task.status_reason || "", task)', self.javascript)
        self.assertIn('statusReason || resultText || task.error', self.javascript)
        self.assertIn('const retryProgress = statusReason || "正在重试中，请稍等！"', self.javascript)
        self.assertNotIn('上次原因：${adminReason}', self.javascript)
        self.assertIn('`第 ${Math.min(retryCount, 2)} / 2 次重试`', self.javascript)
        self.assertNotIn("activeIds.has(task.id)", self.javascript)
        self.assertIn('label: "待执行"', self.javascript)
        self.assertNotIn("重试 ${retryCount}", self.javascript)
        self.assertIn("生成异常请重试！", self.javascript)
        self.assertIn('return "生成接口繁忙请稍后重试！";', self.javascript)
        self.assertIn('return "服务繁忙正在重试！";', self.javascript)
        self.assertNotIn('history.some((item) => /service[ _-]*frequent', self.javascript)
        portal_guard = self.javascript.index('if (portal !== "client") return text;')
        service_mask = self.javascript.index('if (/service[ _-]*frequent|risk check:')
        self.assertLess(portal_guard, service_mask)
        self.assertIn('proxy_refresh: "代理重试"', self.javascript)
        self.assertIn('id="taskTodaySuccessCount"', self.html)
        self.assertIn('id="taskTodayFailedCount"', self.html)
        self.assertIn('id="taskFailureReasonBreakdown"', self.html)
        self.assertIn('["今日成功", summary.today_success ?? 0]', self.javascript)
        self.assertIn('["今日失败", summary.today_failed ?? 0]', self.javascript)
        self.assertNotIn('const rawStatus = String(task.status || "未知")', self.javascript)

    def test_generation_wait_copy_uses_current_range(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.assertIn("预计等待 3~8 分钟", (root / "app" / "query.py").read_text(encoding="utf-8"))
        self.assertIn("预计等待 3~8 分钟", (root / "app" / "worker.py").read_text(encoding="utf-8"))

    def test_task_batch_query_contract_batches_repaints_and_storage(self) -> None:
        self.assertIn("if (!options.deferRender) renderTaskTable()", self.javascript)
        self.assertIn("await queryTask(id, { quiet: true, deferRender: true })", self.javascript)
        self.assertIn('rawStatus === "submitted" || (rawStatus === "success" && !status.url)', self.javascript)
        self.assertIn("await runPool(ids, 3", self.javascript)
        self.assertIn("本页任务状态均已是最新", self.javascript)
        self.assertIn("renderTaskTable({ skipUnchanged: true })", self.javascript)
        self.assertIn('taskRenderSignature: ""', self.javascript)
        self.assertIn('setAttribute("aria-busy", "true")', self.javascript)

    def test_client_title_paging_and_submission_are_resilient(self) -> None:
        self.assertIn("<title>HS</title>", self.html)
        self.assertIn('document.title = document.body.dataset.portal === "client" ? "客户入口" : "管理面板"', self.html)
        task_page_size = re.search(r'<select id="taskPageSize"[^>]*>(.*?)</select>', self.html, flags=re.DOTALL)
        self.assertIsNotNone(task_page_size)
        self.assertEqual(re.findall(r'value="(\d+)"', task_page_size.group(1)), ["20", "50", "100"])
        self.assertIn('"Idempotency-Key": idempotencyKey', self.javascript)
        self.assertIn('timeout: 45000', self.javascript)
        self.assertIn('timeoutError.code = "REQUEST_TIMEOUT"', self.javascript)
        self.assertIn('els.submitState.textContent = "正在确认提交结果"', self.javascript)
        self.assertIn("服务暂时异常，请重试！", self.javascript)
        self.assertIn("正在重试中，请稍等！", self.javascript)
        self.assertIn("goto|click|evaluate|waitFor", self.javascript)
        self.assertIn('label: "排队中"', self.javascript)
        self.assertIn("task.queue_reason", self.javascript)

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
        self.assertIn('value="2" placeholder="Dola 默认 2"', self.html)
        self.assertIn('platform === "dola" ? 2', self.javascript)

    def test_account_quota_settings_and_api_account_filter_are_bound(self) -> None:
        for element_id in (
            "openAccountQuotaSettings",
            "accountQuotaSettingsModal",
            "accountQuotaConfigList",
            "saveAccountQuotaSettings",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('<option value="api">API账号</option>', self.html)
        self.assertIn('apiFetch("/config/account-quotas")', self.javascript)
        self.assertIn('apiFetch("/config/account-quotas", { method: "POST"', self.javascript)
        self.assertIn('data-account-quota-cost', self.javascript)
        self.assertIn('API账号</span>', self.javascript)

    def test_batch_row_images_use_natural_ordinal_mapping(self) -> None:
        self.assertIn('function naturallySortedBatchImageFiles(files)', self.javascript)
        self.assertIn('new Intl.Collator(undefined, { numeric: true, sensitivity: "base" })', self.javascript)
        self.assertIn('const targets = state.batchPrompts.filter((item) => item.prompt.trim() && !batchItemIsCreated(item));', self.javascript)
        self.assertIn('targets[index].images = createBatchImageEntries([sortedFiles[index]])', self.javascript)
        self.assertIn('releaseBatchImageEntries(item.images);', self.javascript)
        self.assertNotIn('index % state.batchPrompts.length', self.javascript)
        self.assertNotIn('Number(entry.row) === number', self.javascript)

    def test_account_access_key_management_is_admin_only_ui(self) -> None:
        for element_id in (
            "accountAccessPanel", "openAccountAccessModal", "accountAccessModal", "accountAccessEnabled",
            "rotateAccountAccessKey", "saveAccountAccessState", "revokeAccountAccessKey", "copyAccountAccessKey",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('apiFetch("/config/account-access")', self.javascript)
        self.assertIn('apiFetch("/config/account-access/rotate", { method: "POST" })', self.javascript)
        self.assertIn('loadAccountAccessConfig()', self.javascript)

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
        self.assertIn('<canvas class="client-ink-splatters"', self.html)
        self.assertIn('<span class="client-register-prompt">还没有账户？</span>', self.html)
        self.assertIn('/admin/assets/ink-bg.js?v=1.4.49', self.html)
        self.assertIn('data-client-stage="landing"', self.html)
        self.assertIn('id="loginButton" type="submit">登录</button>', self.html)
        self.assertIn('id="clientRegisterTab" type="button">注册</button>', self.html)
        self.assertIn('id="clientInvitationCode"', self.html)
        self.assertIn("function syncClientRegistrationControls()", self.javascript)
        self.assertIn("const invitationRequired = register && state.registrationInvitationRequired", self.javascript)
        self.assertIn("[els.clientInvitationCode, register, invitationRequired]", self.javascript)
        self.assertIn("control.disabled = !enabled", self.javascript)
        self.assertIn('id="registrationInvitationRequired" type="checkbox"', self.html)
        self.assertIn('id="invitationManagementModal"', self.html)
        self.assertIn('id="invitationCodeLength" type="number" min="4" max="32"', self.html)
        self.assertIn('id="invitationCodeNote"', self.html)
        self.assertIn('id="generateInvitationCodes"', self.html)
        self.assertIn('id="invitationCodeList"', self.html)
        self.assertIn('id="invitationSearch"', self.html)
        self.assertIn('id="invitationUsageFilter"', self.html)
        self.assertIn('id="prevInvitationPage"', self.html)
        self.assertIn('id="adminAuditPanel"', self.html)
        self.assertIn('id="adminAuditModal"', self.html)
        self.assertIn('id="adminAuditDetailsModal"', self.html)
        self.assertIn('id="adminAuditList"', self.html)
        self.assertIn('data-delete-invitation-code', self.javascript)
        self.assertIn('data-save-invitation-note', self.javascript)
        self.assertIn('data-admin-audit-details', self.javascript)
        self.assertIn('method: "DELETE"', self.javascript)
        self.assertIn('邀请码可长期重复使用，手动删除后失效。', self.html)
        self.assertIn('/admin/audit-logs?', self.javascript)
        self.assertIn('loadAdminAuditLogs()', self.javascript)
        self.assertIn('id="accountStatusFilter"', self.html)
        self.assertIn('id="openAccountDeletionHistory"', self.html)
        self.assertIn('id="accountDeletionHistoryModal"', self.html)
        self.assertIn('/accounts/deletion-history?limit=90', self.javascript)
        self.assertIn('els.openAccountDeletionHistory?.addEventListener("click", openAccountDeletionHistory)', self.javascript)
        self.assertIn('invitation_code: state.clientRegisterMode', self.javascript)
        self.assertIn('params.set("status", state.accountStatusFilter)', self.javascript)
        self.assertIn('sliderVerification ? "跳验证"', self.javascript)
        self.assertIn('710022004|rate limited|slider verification|跳验证|滑块风控', self.javascript)
        self.assertIn('["注册邀请码", user.invitation_code || "-"]', self.javascript)
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
        self.assertIn('class HSRainScene', ink_script)
        self.assertIn('this.frameInterval = 1000 / 60', ink_script)
        self.assertIn('const count = compact ? 88', ink_script)
        self.assertIn('landingSphereGeometry()', ink_script)
        self.assertNotIn('this.drawLandingSphereOrbit(context, now)', ink_script)
        self.assertIn('this.targetStrength = 0', ink_script)
        self.assertIn('uniform float uPuddle', ink_script)
        self.assertIn('gl.uniform1f(this.locations.puddle', ink_script)
        self.assertIn('for (const drop of this.orbitDrops)', ink_script)
        self.assertIn('drawConvergence(context, now)', ink_script)
        self.assertIn('drawWaterOrbit(context, now, 1)', ink_script)
        self.assertIn('gl.uniform1f(this.locations.vortex', ink_script)
        self.assertIn('const delay = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 1750', self.javascript)
        self.assertIn('window.HSRainScene = HSRainScene', ink_script)
        self.assertIn('burstAt(clientX, clientY)', ink_script)
        self.assertIn('drawGlassSurface(context, deltaSeconds, now)', ink_script)
        self.assertIn('drawSprays(context, now)', ink_script)
        self.assertIn('float bodyRotation = uVortex * time * 4.8', ink_script)
        self.assertIn('const sheet = context.createRadialGradient', ink_script)
        self.assertIn('context.quadraticCurveTo(drop.x + slant * 0.42 + sway', ink_script)
        self.assertIn('this.glassDrops = Array.from', ink_script)
        self.assertIn('this.sprays.push(', ink_script)
        self.assertNotIn('angularCrack', ink_script)
        self.assertNotIn('ringCrack', ink_script)
        self.assertNotIn('uShatter', ink_script)
        self.assertNotIn('sphere-bursting', self.javascript)
        self.assertIn('clientRainScene?.burstAt?.(burst.x, burst.y, burst.radius)', self.javascript)
        self.assertIn('clientRainScene?.setActive(false)', self.javascript)
        self.assertIn('#loginView[data-client-stage="landing"]', styles)
        self.assertIn('powerPreference: "high-performance"', ink_script)
        self.assertIn('requestAnimationFrame(this.render)', ink_script)
        self.assertIn('gl.enable(gl.SCISSOR_TEST)', ink_script)
        self.assertIn('float cloud = fbm(inkPoint * detailScale', ink_script)
        self.assertIn('float sphereDepth = sqrt(max(0.0, 1.0 - dot(spherePoint, spherePoint)))', ink_script)
        self.assertIn('float depth = mix(sphereDepth, puddleDepth, uPuddle)', ink_script)
        self.assertIn('screenPoint.x / max(uSize * 1.42', ink_script)
        self.assertIn('this.transitionDuration = 1650', ink_script)
        self.assertIn('function traceInkPool(', self.javascript)
        self.assertIn('function drawInkSplash(', self.javascript)
        self.assertIn('const burstCount = compact ? 4 : 6', self.javascript)
        self.assertIn('.client-ink-splatters.is-entering', styles)
        self.assertIn('.login-panel.register-mode .client-login-only', styles)
        self.assertNotIn('保存后系统将签发新 Token，并迁移当前账号的历史任务归属。', self.html)
        self.assertIn('.ratio-options button.active', styles)
        self.assertIn('.billing-priority-control input { accent-color: #171a19; }', styles)
        self.assertNotIn('class="modal-eyebrow"', self.html)
        self.assertIn('#loginView[data-client-stage="landing"] .client-ink-canvas {\n  filter: none;', styles)
        self.assertNotIn('验证码只发送到已绑定并验证的邮箱。', self.html)
        self.assertIn('sidebar-content-hidden', self.javascript)
        self.assertIn('.app-shell.sidebar-content-hidden .sidebar-client-identity', styles)

    def test_user_action_buttons_use_aligned_grid(self) -> None:
        styles = (Path(__file__).resolve().parents[1] / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".users-table .user-point-actions", styles)
        self.assertIn("grid-template-columns: repeat(2, minmax(64px, 1fr))", styles)

    def test_web_cookie_auth_migrates_and_removes_legacy_local_token(self) -> None:
        self.assertIn(
            'const legacyToken = portal === "client" ? tokenFromUrl || localStorage.getItem(portalStorageKey(TOKEN_KEY)) || "" : "";',
            self.javascript,
        )
        self.assertIn('await requestJson("/auth/session", legacyToken, { method: "POST" })', self.javascript)
        self.assertIn('return requestJson(path, "", options)', self.javascript)
        self.assertNotIn('localStorage.setItem(portalStorageKey(TOKEN_KEY)', self.javascript)
        self.assertIn('localStorage.removeItem("dola_fetch_api_token");', self.javascript)

    def test_dola_submit_interval_is_conservative(self) -> None:
        worker = (Path(__file__).resolve().parents[1] / "app" / "worker.py").read_text(encoding="utf-8")
        self.assertIn("submit_interval = load_settings().dola_global_submit_interval_seconds", worker)
        self.assertIn("delay = submit_interval -", worker)
        self.assertIn("self._dola_submit_lock", worker)
        self.assertIn('id="dolaSubmitInterval" type="number" min="3" max="30" step="0.5"', self.html)
        self.assertIn("全局提交间隔", self.html)
        self.assertIn('apiFetch("/config/runtime"', self.javascript)
        self.assertIn("RESULT_WATCH_DEADLINE_MINUTES = 20", worker)
        self.assertIn("生成超过20分钟，仍未返回结果", worker)
        self.assertNotIn("retry_timed_out_submitted_task", worker)
        self.assertIn(r"/生成超过\d+分钟", self.javascript)

    def test_admin_task_pause_control_is_wired(self) -> None:
        self.assertIn('id="toggleTaskPause"', self.html)
        self.assertIn('apiFetch("/admin/task-pause")', self.javascript)
        self.assertIn('body: { paused }', self.javascript)
        self.assertIn('全部排队任务会立即取消', self.javascript)
        self.assertIn('els.toggleTaskPause?.addEventListener("click", toggleTaskPause)', self.javascript)

    def test_requested_security_and_node_copy_is_simplified(self) -> None:
        self.assertNotIn('<span class="modal-eyebrow">账号安全</span><h2 id="clientPasswordModalTitle">', self.html)
        self.assertNotIn('<span class="modal-eyebrow">账号安全</span><h2 id="clientEmailModalTitle">', self.html)
        self.assertIn("修改密码后API Token自动重置。", self.html)
        self.assertNotIn("Dola 网络出口", self.html)

    def test_message_center_replaces_settings_feedback_entry(self) -> None:
        styles = (Path(__file__).resolve().parents[1] / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
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
        self.assertNotIn("announcement-modal-accent", self.html)
        self.assertNotIn(".announcement-modal-accent", styles)

    def test_points_messages_memberships_and_cards_are_wired(self) -> None:
        for element_id in ("pointCardsNavItem", "pointCardForm", "pointCardSearch", "openPointCardModal", "redeemForm", "transactionsView", "membershipList", "membershipModal", "membershipConcurrency", "membershipTaskDiscount", "membershipDetailsButton", "membershipDetailsModal", "membershipBonus", "packagePaymentUrl", "userSearch", "announcementLevel", "announcementExemptField", "announcementExemptSearch", "announcementExemptUsers", "announcementExemptModal", "saveAnnouncementExemptUsers", "emergencyAnnouncementOverlay", "smallAnnouncementToast", "repositoryLatestVersion", "sidebarMembershipName", "sidebarVersion", "dashboardPointsBalance", "openMyPrompts", "promptPickerModal", "promptPickerList", "promptPickerPrev", "promptPickerNext", "messagesRefreshState", "billingPriorityControl", "billingPriorityState"):
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
        self.assertIn("lock_exempt_user_ids", self.javascript)
        self.assertIn("data-edit-announcement-exempt", self.javascript)
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

    def test_spreadsheet_batch_submission_is_complete_and_editable(self) -> None:
        for element_id in (
            "batchSubmitNavItem",
            "batch-submitView",
            "resetBatchTaskPage",
            "batchSpreadsheetInput",
            "parseBatchSpreadsheet",
            "batchTaskRatio",
            "batchSelectionLimit",
            "batchAutoConcurrency",
            "batchAutoModal",
            "confirmBatchAutoSubmit",
            "batchAutoDownload",
            "batchDownloadFolderName",
            "selectBatchDownloadFolder",
            "clearBatchDownloadFolder",
            "batchPageSize",
            "batchPrevPage",
            "batchNextPage",
            "batchPageState",
            "selectAllBatchPrompts",
            "batchPromptList",
            "refreshBatchTasks",
            "autoSubmitBatchTasks",
            "submitBatchTasks",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('accept=".xlsx,.xls,.ods,.csv,.tsv,.txt"', self.html)
        self.assertIn('apiFetch("/batch-prompts/parse"', self.javascript)
        self.assertIn('apiFetch("/batch-prompts/jobs"', self.javascript)
        self.assertIn('apiFetch("/batch-prompts/job-assets"', self.javascript)
        self.assertIn('const BATCH_ASSET_CHUNK_BYTES = 48 * 1024 * 1024', self.javascript)
        self.assertIn('async function prepareBatchTaskImageAssets(sessionId, selected)', self.javascript)
        self.assertIn('timeout: batchAssetChunkTimeout(chunk)', self.javascript)
        self.assertIn('form.append("manifest", JSON.stringify({', self.javascript)
        self.assertNotIn('form.append("batch", "true")', self.javascript)
        self.assertNotIn('MAX_BATCH_SELECTION', self.javascript)
        self.assertIn('selected: true, status: ""', self.javascript)
        self.assertIn('function syncBatchPromptsFromTaskState()', self.javascript)
        self.assertIn('item.videoUrl = resultUrl', self.javascript)
        self.assertNotIn('id="batchSelectionLimit" type="number" min="1" max="30"', self.html)
        self.assertNotIn('const BATCH_VIDEO_DURATION = "15"', self.javascript)
        self.assertIn('id="batchDurationSelect"', self.html)
        self.assertIn('id="durationSelect"', self.html)
        self.assertIn('duration: state.batchDuration', self.javascript)
        self.assertIn('form.append("duration", String(state.duration))', self.javascript)
        self.assertIn('async function autoSubmitBatchTasks()', self.javascript)
        self.assertIn('async function monitorPersistentBatchJob(jobId)', self.javascript)
        self.assertIn('async function restorePersistentBatchJob()', self.javascript)
        self.assertIn('async function refreshBatchTaskStatuses(options = {})', self.javascript)
        self.assertIn('const batchStatusIsTerminal = ["completed", "failed", "canceled"].includes', self.javascript)
        self.assertIn('if (batchStatusIsTerminal) return false;', self.javascript)
        self.assertIn('els.refreshBatchTasks?.addEventListener("click", () => refreshBatchTaskStatuses())', self.javascript)
        self.assertIn('data-batch-video-view', self.javascript)
        self.assertIn('data-batch-video-download', self.javascript)
        self.assertIn('openVideoModal(item.videoUrl, item.taskId)', self.javascript)
        self.assertIn('downloadVideo(item.videoUrl, item.taskId)', self.javascript)
        self.assertNotIn('link.download = `${id || "video"}.mp4`', self.javascript)
        self.assertIn('item.videoUrl = String(result.url || "")', self.javascript)
        self.assertIn('return "服务暂时繁忙，请重新生成"', self.javascript)
        self.assertIn('/batch-prompts/${encodeURIComponent(sessionId)}/cancel', self.javascript)
        self.assertIn('apiFetch("/batch-prompts/references"', self.javascript)
        self.assertIn('reference_id: referenceBundle?.id || ""', self.javascript)
        self.assertIn('asset_upload_id: assetUploadId', self.javascript)
        self.assertIn('return "正在分片上传参考图"', self.javascript)
        self.assertIn('"停止生成"', self.javascript)
        self.assertNotIn('data-title="多任务提交"', self.html)
        self.assertIn('data-title="批量提交"', self.html)
        self.assertIn("在节点选择页分别指定三个平台使用哪个代理池", self.html)
        self.assertNotIn("首选模式不可用时自动切换备用模式", self.html)
        self.assertIn('els.submitBatchTasks.disabled = state.batchAutoStopRequested', self.javascript)
        self.assertIn('await requestBatchSubmissionStop()', self.javascript)
        self.assertIn('renderBatchPrompts();\n    if (els.batchTaskProgress) els.batchTaskProgress.textContent = "正在停止', self.javascript)
        self.assertNotIn('while ((active.size || cursor < selected.length) && !state.batchAutoStopRequested && !stopped)', self.javascript)
        self.assertIn('批次排队 ${queued} 条', self.javascript)
        self.assertIn('批次已进入后台公平队列', self.javascript)
        self.assertNotIn('blockedSlots', self.javascript)
        self.assertNotIn('await apiFetch(`/tasks/${encodeURIComponent(taskId)}`', self.javascript)
        self.assertIn('URL.createObjectURL(file)', self.javascript)
        self.assertIn('class="batch-reference-thumbs"', self.html)
        self.assertNotIn('id="batchPlatformSelect"', self.html)
        self.assertIn('id="batchModelSelect"', self.html)
        self.assertIn("platform: state.batchPlatform", self.javascript)
        self.assertIn("model: state.batchModel", self.javascript)
        self.assertNotIn('<span>生成平台</span>', self.html)
        self.assertIn('<span>生成模型</span><select id="batchModelSelect"', self.html)
        self.assertIn("function batchModelChoices()", self.javascript)
        self.assertIn("state.batchPlatform = selected.platform", self.javascript)
        self.assertNotIn('id="batchAutoConcurrencyState"', self.html)
        self.assertNotIn("当前最多可使用", self.javascript)
        self.assertNotIn('function dolaBatchGenerationSelection()', self.javascript)
        self.assertNotIn('for (let current = 0; current < selected.length; current += 1)', self.javascript)
        self.assertIn('await autoSubmitBatchTasks()', self.javascript)
        self.assertIn('return "批次排队中，尚未创建任务"', self.javascript)
        eager_submit_body = self.javascript.split("async function submitBatchTasks()", 1)[1].split("function waitForBatchPoll", 1)[0]
        self.assertNotIn("createBatchTask(", eager_submit_body)
        windowed_submit_body = self.javascript.split("async function autoSubmitBatchTasks()", 1)[1].split("async function copyText", 1)[0]
        self.assertNotIn("createBatchTask(", windowed_submit_body)
        self.assertIn('apiFetch("/batch-prompts/jobs"', windowed_submit_body)
        self.assertNotIn('Math.min(3, selected.length)', self.javascript)
        self.assertNotIn('id="batchTaskModal"', self.html)
        self.assertIn('data-batch-prompt-text', self.javascript)
        self.assertIn('data-delete-batch-prompt', self.javascript)
        self.assertIn('els.parseBatchSpreadsheet?.addEventListener("click", parseBatchSpreadsheet)', self.javascript)
        self.assertIn('els.submitBatchTasks?.addEventListener("click", submitBatchTasks)', self.javascript)
        self.assertIn('els.autoSubmitBatchTasks?.addEventListener("click", () => {', self.javascript)
        self.assertNotIn('syncBatchConcurrencyControls(true)', self.javascript)
        self.assertIn('useMaximum || !state.batchConcurrencyCustomized ? maximum', self.javascript)
        self.assertIn('state.batchConcurrencyCustomized = true', self.javascript)
        self.assertIn('state.concurrency !== previousConcurrency', self.javascript)
        self.assertIn('id="autoSubmitBatchTasks" type="button" disabled>生成设置</button>', self.html)
        self.assertIn('<span>批量提交</span>', self.html)
        self.assertIn('<h2 id="batchAutoModalTitle">生成设置</h2>', self.html)
        self.assertIn('<span>生成数量</span><input id="batchSelectionLimit"', self.html)
        self.assertIn('<span>生成并发</span><input id="batchAutoConcurrency"', self.html)
        self.assertIn('id="batchAutoDownload" type="checkbox"', self.html)
        self.assertIn('id="selectBatchDownloadFolder"', self.html)
        self.assertIn('id="confirmBatchAutoSubmit" type="button">保存</button>', self.html)
        self.assertNotIn('id="confirmBatchAutoSubmit" type="button">开始生成</button>', self.html)
        self.assertNotIn('id="applyBatchSelectionLimit"', self.html)
        self.assertIn('toast("生成设置已保存")', self.javascript)
        self.assertIn('id="downloadSelectedVideos"', self.html)
        self.assertIn('els.downloadSelectedVideos?.addEventListener("click", downloadSelectedVideos)', self.javascript)
        settings_save_body = self.javascript.split('els.confirmBatchAutoSubmit?.addEventListener("click", () => {', 1)[1].split('els.batchAutoConcurrency?', 1)[0]
        self.assertNotIn('autoSubmitBatchTasks()', settings_save_body)
        self.assertIn('item.taskId = ""', windowed_submit_body)
        self.assertIn('item.batchJobId = ""', windowed_submit_body)
        self.assertIn('String(entry.batchJobId || "") === jobId', self.javascript)
        self.assertNotIn('state.batchPrompts[index] || state.batchPrompts.find', self.javascript)
        self.assertIn('function taskResultDetail(task, status = getTaskStatus(task))', self.javascript)
        self.assertIn('if (portal === "client") return primary;', self.javascript)
        self.assertIn('后台尝试记录', self.javascript)
        self.assertIn('function persistentBatchStatusPath(jobId)', self.javascript)
        self.assertIn('?since_revision=${encodeURIComponent(revision)}', self.javascript)
        self.assertIn('if (rowsChanged || summaryChanged || !incremental) renderBatchPrompts();', self.javascript)
        self.assertIn('if (status === "queued" && taskId) status = "running"', self.javascript)
        self.assertIn('const BATCH_DRAFT_VERSION = 1', self.javascript)
        self.assertIn('localStorage.setItem(key, JSON.stringify({', self.javascript)
        self.assertIn('function loadBatchDraft()', self.javascript)
        self.assertIn('const BATCH_IMAGE_DB_NAME = "dfyue_batch_images"', self.javascript)
        self.assertIn('const BATCH_IMAGE_DB_VERSION = 2', self.javascript)
        self.assertIn('const DOWNLOAD_DIRECTORY_STORE = "download_directories"', self.javascript)
        self.assertIn('window.indexedDB.open(BATCH_IMAGE_DB_NAME', self.javascript)
        self.assertIn('window.showDirectoryPicker({ mode: "readwrite" })', self.javascript)
        self.assertIn('async function downloadTaskToDirectory(taskId, directory = state.downloadDirectoryHandle)', self.javascript)
        self.assertIn('response.body.pipeTo(writable)', self.javascript)
        self.assertIn('function queueAutomaticVideoDownload(taskId)', self.javascript)
        self.assertIn('async function persistBatchReferenceImages()', self.javascript)
        self.assertIn('async function readBatchImageDraft(owner)', self.javascript)
        self.assertIn('state.batchSharedImages = createBatchImageEntries(restoredBatchImageFiles(storedImages.shared))', self.javascript)
        self.assertIn('item.images = createBatchImageEntries(restoredBatchImageFiles(rowImages.get(Number(item.row || 0))))', self.javascript)
        self.assertIn('resizeBatchPromptTextareas', self.javascript)
        self.assertIn('batchPageSize: 10', self.javascript)
        self.assertIn('.slice(start, start + state.batchPageSize)', self.javascript)
        self.assertIn('[10, 30, 50].includes(requested)', self.javascript)
        self.assertIn('els.batchPrevPage?.addEventListener("click"', self.javascript)
        self.assertIn('els.batchNextPage?.addEventListener("click"', self.javascript)
        self.assertNotIn('<option value="5">5 秒</option>', self.html)
        self.assertNotIn('<h2 id="batchTaskPageTitle">批量生成</h2>', self.html)
        critical_styles = self.html.split('<style id="criticalPortalStyles">', 1)[1].split('</style>', 1)[0]
        admin_rule = next(line for line in critical_styles.splitlines() if 'body[data-portal="admin"]' in line)
        self.assertNotIn('#batchSubmitNavItem', admin_rule)
        self.assertNotIn('#batch-submitView', admin_rule)
        styles = (Path(__file__).resolve().parents[1] / "app" / "admin" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('.batch-task-page', styles)
        self.assertIn('body[data-view="batch-submit"] .main', styles)
        self.assertIn('scrollbar-gutter: stable', styles)
        self.assertIn('.batch-prompt-row textarea {', styles)
        self.assertIn('resize: none', styles)
        self.assertIn('body[data-portal] .proxy-node-card[aria-pressed="true"]', styles)

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
            "submissionConcurrencyInput",
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
        self.assertNotIn('id="remoteGenerationLimitInput"', self.html)
        self.assertIn('body: { browser_workers: submissionConcurrency, max_effective_workers: submissionConcurrency }', self.javascript)
        self.assertIn('每个用户的远端上限在用户管理中单独设置', self.html)
        self.assertIn('data-user-remote-limit=', self.javascript)
        self.assertIn('浏览器提交池固定为 8 × 4', self.html)
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
