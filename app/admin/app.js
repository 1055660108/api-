const TOKEN_KEY = "dfyue_api_token";
const AUTH_KEY = "dfyue_auth_ok";
const RESULTS_KEY = "dfyue_task_results";
const pathName = window.location.pathname.replace(/\/+$/, "");
const portal = pathName === "/client" ? "client" : "admin";
const authPath = portal === "client" ? "/auth/client" : "/auth/admin";

const els = {
  loginView: document.getElementById("loginView"),
  loginForm: document.getElementById("loginForm"),
  loginToken: document.getElementById("loginToken"),
  loginButton: document.getElementById("loginButton"),
  loginState: document.getElementById("loginState"),
  clientUsername: document.getElementById("clientUsername"),
  clientPassword: document.getElementById("clientPassword"),
  clientConfirmPassword: document.getElementById("clientConfirmPassword"),
  clientEmailLocal: document.getElementById("clientEmailLocal"),
  clientEmailDomain: document.getElementById("clientEmailDomain"),
  clientEmailCode: document.getElementById("clientEmailCode"),
  sendEmailCode: document.getElementById("sendEmailCode"),
  clientLoginTab: document.getElementById("clientLoginTab"),
  clientRegisterTab: document.getElementById("clientRegisterTab"),
  forgotPasswordButton: document.getElementById("forgotPasswordButton"),
  forgotPasswordModal: document.getElementById("forgotPasswordModal"),
  closeForgotPasswordModal: document.getElementById("closeForgotPasswordModal"),
  cancelForgotPasswordModal: document.getElementById("cancelForgotPasswordModal"),
  forgotPasswordForm: document.getElementById("forgotPasswordForm"),
  forgotPasswordEmail: document.getElementById("forgotPasswordEmail"),
  forgotPasswordCode: document.getElementById("forgotPasswordCode"),
  sendForgotPasswordCode: document.getElementById("sendForgotPasswordCode"),
  forgotPasswordNew: document.getElementById("forgotPasswordNew"),
  forgotPasswordConfirm: document.getElementById("forgotPasswordConfirm"),
  resetPasswordButton: document.getElementById("resetPasswordButton"),
  adminUsername: document.getElementById("adminUsername"),
  adminPassword: document.getElementById("adminPassword"),
  clientTokenDisplay: document.getElementById("clientTokenDisplay"),
  clientAccountName: document.getElementById("clientAccountName"),
  dashboardPointsBalance: document.getElementById("dashboardPointsBalance"),
  sidebarMembershipName: document.getElementById("sidebarMembershipName"),
  sidebarVersion: document.getElementById("sidebarVersion"),
  copyClientToken: document.getElementById("copyClientToken"),
  refreshClientToken: document.getElementById("refreshClientToken"),
  pointsBalance: document.getElementById("pointsBalance"),
  purchaseOptions: document.getElementById("purchaseOptions"),
  redeemForm: document.getElementById("redeemForm"),
  redeemCode: document.getElementById("redeemCode"),
  redeemButton: document.getElementById("redeemButton"),
  membershipList: document.getElementById("membershipList"),
  membershipCurrentState: document.getElementById("membershipCurrentState"),
  refreshTransactions: document.getElementById("refreshTransactions"),
  transactionTableBody: document.getElementById("transactionTableBody"),
  pointCardForm: document.getElementById("pointCardForm"),
  pointCardModal: document.getElementById("pointCardModal"),
  openPointCardModal: document.getElementById("openPointCardModal"),
  closePointCardModal: document.getElementById("closePointCardModal"),
  cancelPointCardModal: document.getElementById("cancelPointCardModal"),
  pointCardPoints: document.getElementById("pointCardPoints"),
  pointCardCount: document.getElementById("pointCardCount"),
  pointCardNote: document.getElementById("pointCardNote"),
  generatePointCards: document.getElementById("generatePointCards"),
  generatedPointCards: document.getElementById("generatedPointCards"),
  generatedPointCardCodes: document.getElementById("generatedPointCardCodes"),
  copyGeneratedPointCards: document.getElementById("copyGeneratedPointCards"),
  refreshPointCards: document.getElementById("refreshPointCards"),
  exportPointCards: document.getElementById("exportPointCards"),
  pointCardSearch: document.getElementById("pointCardSearch"),
  pointCardTypeFilter: document.getElementById("pointCardTypeFilter"),
  pointCardStatusFilter: document.getElementById("pointCardStatusFilter"),
  pointCardTotalState: document.getElementById("pointCardTotalState"),
  pointCardTableBody: document.getElementById("pointCardTableBody"),
  submitFreeRemaining: document.getElementById("submitFreeRemaining"),
  submitPointsBalance: document.getElementById("submitPointsBalance"),
  submitCostText: document.getElementById("submitCostText"),
  usersNavItem: document.getElementById("usersNavItem"),
  messagesNavItem: document.getElementById("messagesNavItem"),
  messagesNavLabel: document.getElementById("messagesNavLabel"),
  messagesView: document.getElementById("messagesView"),
  messageUnreadCount: document.getElementById("messageUnreadCount"),
  feedbackTableBody: document.getElementById("feedbackTableBody"),
  refreshMessages: document.getElementById("refreshMessages"),
  messagesRefreshState: document.getElementById("messagesRefreshState"),
  clientFeedbackList: document.getElementById("clientFeedbackList"),
  clientNotificationList: document.getElementById("clientNotificationList"),
  adminNotificationForm: document.getElementById("adminNotificationForm"),
  notificationTitle: document.getElementById("notificationTitle"),
  notificationContent: document.getElementById("notificationContent"),
  notificationRecipients: document.getElementById("notificationRecipients"),
  selectAllNotificationUsers: document.getElementById("selectAllNotificationUsers"),
  notificationRecipientState: document.getElementById("notificationRecipientState"),
  sendNotificationButton: document.getElementById("sendNotificationButton"),
  adminNotificationList: document.getElementById("adminNotificationList"),
  clientAnnouncementList: document.getElementById("clientAnnouncementList"),
  adminAnnouncementForm: document.getElementById("adminAnnouncementForm"),
  announcementTitle: document.getElementById("announcementTitle"),
  announcementContent: document.getElementById("announcementContent"),
  announcementLevel: document.getElementById("announcementLevel"),
  announcementLockField: document.getElementById("announcementLockField"),
  announcementLockScreen: document.getElementById("announcementLockScreen"),
  publishAnnouncementButton: document.getElementById("publishAnnouncementButton"),
  adminAnnouncementList: document.getElementById("adminAnnouncementList"),
  announcementModal: document.getElementById("announcementModal"),
  announcementModalTitle: document.getElementById("announcementModalTitle"),
  announcementModalContent: document.getElementById("announcementModalContent"),
  closeAnnouncementModal: document.getElementById("closeAnnouncementModal"),
  confirmAnnouncementModal: document.getElementById("confirmAnnouncementModal"),
  smallAnnouncementToast: document.getElementById("smallAnnouncementToast"),
  smallAnnouncementTitle: document.getElementById("smallAnnouncementTitle"),
  smallAnnouncementContent: document.getElementById("smallAnnouncementContent"),
  emergencyAnnouncementOverlay: document.getElementById("emergencyAnnouncementOverlay"),
  emergencyAnnouncementTitle: document.getElementById("emergencyAnnouncementTitle"),
  emergencyAnnouncementContent: document.getElementById("emergencyAnnouncementContent"),
  closeEmergencyAnnouncement: document.getElementById("closeEmergencyAnnouncement"),
  emergencyLockNote: document.getElementById("emergencyLockNote"),
  openFeedbackModal: document.getElementById("openFeedbackModal"),
  feedbackModal: document.getElementById("feedbackModal"),
  closeFeedbackModal: document.getElementById("closeFeedbackModal"),
  cancelFeedbackModal: document.getElementById("cancelFeedbackModal"),
  feedbackForm: document.getElementById("feedbackForm"),
  feedbackCategory: document.getElementById("feedbackCategory"),
  feedbackContent: document.getElementById("feedbackContent"),
  feedbackContact: document.getElementById("feedbackContact"),
  submitFeedbackButton: document.getElementById("submitFeedbackButton"),
  settingsNavItem: document.getElementById("settingsNavItem"),
  settingsView: document.getElementById("settingsView"),
  settingsContent: document.getElementById("settingsContent"),
  userTableBody: document.getElementById("userTableBody"),
  userSearch: document.getElementById("userSearch"),
  searchUsers: document.getElementById("searchUsers"),
  clearUserSearch: document.getElementById("clearUserSearch"),
  onlineUserCount: document.getElementById("onlineUserCount"),
  prevUserPage: document.getElementById("prevUserPage"),
  nextUserPage: document.getElementById("nextUserPage"),
  userPageState: document.getElementById("userPageState"),
  userPageSize: document.getElementById("userPageSize"),
  userTotalState: document.getElementById("userTotalState"),
  tokenCommand: document.getElementById("tokenCommand"),
  copyTokenCommand: document.getElementById("copyTokenCommand"),
  appShell: document.getElementById("appShell"),
  logoutButton: document.getElementById("logoutButton"),
  dashboardLogoutButton: document.getElementById("dashboardLogoutButton"),
  settingsLogoutButton: document.getElementById("settingsLogoutButton"),
  adminAccountDisplay: document.getElementById("adminAccountDisplay"),
  openPasswordModal: document.getElementById("openPasswordModal"),
  passwordModal: document.getElementById("passwordModal"),
  closePasswordModal: document.getElementById("closePasswordModal"),
  cancelPasswordModal: document.getElementById("cancelPasswordModal"),
  adminPasswordForm: document.getElementById("adminPasswordForm"),
  changeAdminUsername: document.getElementById("changeAdminUsername"),
  currentAdminPassword: document.getElementById("currentAdminPassword"),
  newAdminPassword: document.getElementById("newAdminPassword"),
  confirmAdminPassword: document.getElementById("confirmAdminPassword"),
  changeAdminPasswordButton: document.getElementById("changeAdminPasswordButton"),
  adminPasswordState: document.getElementById("adminPasswordState"),
  clientPasswordState: document.getElementById("clientPasswordState"),
  clientSettingsAccount: document.getElementById("clientSettingsAccount"),
  openClientPasswordModal: document.getElementById("openClientPasswordModal"),
  clientPasswordModal: document.getElementById("clientPasswordModal"),
  closeClientPasswordModal: document.getElementById("closeClientPasswordModal"),
  cancelClientPasswordModal: document.getElementById("cancelClientPasswordModal"),
  clientPasswordForm: document.getElementById("clientPasswordForm"),
  currentClientPassword: document.getElementById("currentClientPassword"),
  newClientPassword: document.getElementById("newClientPassword"),
  confirmClientPassword: document.getElementById("confirmClientPassword"),
  changeClientPasswordButton: document.getElementById("changeClientPasswordButton"),
  clientEmailState: document.getElementById("clientEmailState"),
  clientEmailDisplay: document.getElementById("clientEmailDisplay"),
  openClientEmailModal: document.getElementById("openClientEmailModal"),
  clientEmailModal: document.getElementById("clientEmailModal"),
  closeClientEmailModal: document.getElementById("closeClientEmailModal"),
  cancelClientEmailModal: document.getElementById("cancelClientEmailModal"),
  clientEmailForm: document.getElementById("clientEmailForm"),
  changeEmailLocal: document.getElementById("changeEmailLocal"),
  changeEmailDomain: document.getElementById("changeEmailDomain"),
  changeEmailCode: document.getElementById("changeEmailCode"),
  sendChangeEmailCode: document.getElementById("sendChangeEmailCode"),
  changeClientEmailButton: document.getElementById("changeClientEmailButton"),
  viewTitle: document.getElementById("viewTitle"),
  sidebarStatusDot: document.getElementById("sidebarStatusDot"),
  sidebarStatusText: document.getElementById("sidebarStatusText"),
  metricService: document.getElementById("metricService"),
  metricServiceNote: document.getElementById("metricServiceNote"),
  metricWorkers: document.getElementById("metricWorkers"),
  editWorkers: document.getElementById("editWorkers"),
  metricTaskTotal: document.getElementById("metricTaskTotal"),
  metricPending: document.getElementById("metricPending"),
  metricRunning: document.getElementById("metricRunning"),
  metricFinished: document.getElementById("metricFinished"),
  taskRunningCount: document.getElementById("taskRunningCount"),
  taskTodayDoneCount: document.getElementById("taskTodayDoneCount"),
  taskForm: document.getElementById("taskForm"),
  promptInput: document.getElementById("promptInput"),
  saveCurrentPrompt: document.getElementById("saveCurrentPrompt"),
  openMyPrompts: document.getElementById("openMyPrompts"),
  promptPickerModal: document.getElementById("promptPickerModal"),
  closePromptPickerModal: document.getElementById("closePromptPickerModal"),
  cancelPromptPickerModal: document.getElementById("cancelPromptPickerModal"),
  promptPickerList: document.getElementById("promptPickerList"),
  promptPickerPrev: document.getElementById("promptPickerPrev"),
  promptPickerNext: document.getElementById("promptPickerNext"),
  promptPickerPageState: document.getElementById("promptPickerPageState"),
  promptGrid: document.getElementById("promptGrid"),
  addPromptButton: document.getElementById("addPromptButton"),
  promptEditor: document.getElementById("promptEditor"),
  promptTitleInput: document.getElementById("promptTitleInput"),
  promptContentInput: document.getElementById("promptContentInput"),
  cancelPromptEdit: document.getElementById("cancelPromptEdit"),
  promptPrevPage: document.getElementById("promptPrevPage"),
  promptNextPage: document.getElementById("promptNextPage"),
  promptPageState: document.getElementById("promptPageState"),
  ratioGroup: document.getElementById("ratioGroup"),
  ratioTrigger: document.querySelector("#ratioGroup .ratio-trigger"),
  platformSelect: document.getElementById("platformSelect"),
  modelSelect: document.getElementById("modelSelect"),
  resetSubmit: document.getElementById("resetSubmit"),
  submitTask: document.getElementById("submitTask"),
  submitState: document.getElementById("submitState"),
  imageInput: document.getElementById("imageInput"),
  clearImages: document.getElementById("clearImages"),
  imageList: document.getElementById("imageList"),
  ratioValue: document.getElementById("ratioValue"),
  refreshTasks: document.getElementById("refreshTasks"),
  queryVisibleTasks: document.getElementById("queryVisibleTasks"),
  clearTasks: document.getElementById("clearTasks"),
  deleteFailedTasks: document.getElementById("deleteFailedTasks"),
  taskSearch: document.getElementById("taskSearch"),
  prevPage: document.getElementById("prevPage"),
  nextPage: document.getElementById("nextPage"),
  pageState: document.getElementById("pageState"),
  currentPageTaskCount: document.getElementById("currentPageTaskCount"),
  taskPageSize: document.getElementById("taskPageSize"),
  taskTableBody: document.getElementById("taskTableBody"),
  proxyConfigPanel: document.getElementById("proxyConfigPanel"),
  proxyNodesNavItem: document.getElementById("proxyNodesNavItem"),
  proxyNodesView: document.getElementById("proxy-nodesView"),
  proxyNodeGrid: document.getElementById("proxyNodeGrid"),
  proxyEnabledSelect: document.getElementById("proxyEnabledSelect"),
  proxyAutoSelect: document.getElementById("proxyAutoSelect"),
  proxyCountryFilter: document.getElementById("proxyCountryFilter"),
  proxyNodeCount: document.getElementById("proxyNodeCount"),
  proxyNodesState: document.getElementById("proxyNodesState"),
  refreshProxyNodes: document.getElementById("refreshProxyNodes"),
  openProxyModalFromNodes: document.getElementById("openProxyModalFromNodes"),
  proxyApiDisplay: document.getElementById("proxyApiDisplay"),
  openProxyModal: document.getElementById("openProxyModal"),
  proxyModal: document.getElementById("proxyModal"),
  closeProxyModal: document.getElementById("closeProxyModal"),
  cancelProxyModal: document.getElementById("cancelProxyModal"),
  loadProxyConfig: document.getElementById("loadProxyConfig"),
  proxySource: document.getElementById("proxySource"),
  proxySubscriptionField: document.getElementById("proxySubscriptionField"),
  proxySubscriptionUrl: document.getElementById("proxySubscriptionUrl"),
  proxySubscriptionHint: document.getElementById("proxySubscriptionHint"),
  proxyApiField: document.getElementById("proxyApiField"),
  proxyApiUrl: document.getElementById("proxyApiUrl"),
  saveProxyConfig: document.getElementById("saveProxyConfig"),
  configState: document.getElementById("configState"),
  repositoryUpdatePanel: document.getElementById("repositoryUpdatePanel"),
  repositoryUpdateState: document.getElementById("repositoryUpdateState"),
  repositoryRevision: document.getElementById("repositoryRevision"),
  repositoryLatestVersion: document.getElementById("repositoryLatestVersion"),
  updateRepository: document.getElementById("updateRepository"),
  emailConfigPanel: document.getElementById("emailConfigPanel"),
  emailConfigState: document.getElementById("emailConfigState"),
  emailConfigDisplay: document.getElementById("emailConfigDisplay"),
  openEmailModal: document.getElementById("openEmailModal"),
  emailModal: document.getElementById("emailModal"),
  closeEmailModal: document.getElementById("closeEmailModal"),
  cancelEmailModal: document.getElementById("cancelEmailModal"),
  registrationEmailEnabled: document.getElementById("registrationEmailEnabled"),
  registrationEmailDomains: document.getElementById("registrationEmailDomains"),
  addRegistrationEmailDomain: document.getElementById("addRegistrationEmailDomain"),
  registrationSmtpUsername: document.getElementById("registrationSmtpUsername"),
  registrationSmtpAuthorizationCode: document.getElementById("registrationSmtpAuthorizationCode"),
  registrationEmailSenderName: document.getElementById("registrationEmailSenderName"),
  registrationEmailCodeTtl: document.getElementById("registrationEmailCodeTtl"),
  saveEmailConfig: document.getElementById("saveEmailConfig"),
  modelConfigPanel: document.getElementById("modelConfigPanel"),
  modelConfigDisplay: document.getElementById("modelConfigDisplay"),
  openModelModal: document.getElementById("openModelModal"),
  modelModal: document.getElementById("modelModal"),
  closeModelModal: document.getElementById("closeModelModal"),
  cancelModelModal: document.getElementById("cancelModelModal"),
  modelConfigList: document.getElementById("modelConfigList"),
  modelConfigState: document.getElementById("modelConfigState"),
  saveModelConfig: document.getElementById("saveModelConfig"),
  syncQianwenModels: document.getElementById("syncQianwenModels"),
  packageConfigPanel: document.getElementById("packageConfigPanel"),
  packageConfigState: document.getElementById("packageConfigState"),
  packageConfigDisplay: document.getElementById("packageConfigDisplay"),
  openPackageModal: document.getElementById("openPackageModal"),
  packageModal: document.getElementById("packageModal"),
  closePackageModal: document.getElementById("closePackageModal"),
  cancelPackageModal: document.getElementById("cancelPackageModal"),
  packageForm: document.getElementById("packageForm"),
  packageName: document.getElementById("packageName"),
  packagePoints: document.getElementById("packagePoints"),
  packageBonus: document.getElementById("packageBonus"),
  packageSortOrder: document.getElementById("packageSortOrder"),
  packagePaymentUrl: document.getElementById("packagePaymentUrl"),
  createPackageButton: document.getElementById("createPackageButton"),
  packageList: document.getElementById("packageList"),
  membershipConfigState: document.getElementById("membershipConfigState"),
  membershipConfigDisplay: document.getElementById("membershipConfigDisplay"),
  openMembershipModal: document.getElementById("openMembershipModal"),
  membershipModal: document.getElementById("membershipModal"),
  closeMembershipModal: document.getElementById("closeMembershipModal"),
  cancelMembershipModal: document.getElementById("cancelMembershipModal"),
  membershipForm: document.getElementById("membershipForm"),
  membershipName: document.getElementById("membershipName"),
  membershipPrice: document.getElementById("membershipPrice"),
  membershipDuration: document.getElementById("membershipDuration"),
  membershipConcurrency: document.getElementById("membershipConcurrency"),
  membershipBonus: document.getElementById("membershipBonus"),
  membershipSortOrder: document.getElementById("membershipSortOrder"),
  membershipDescription: document.getElementById("membershipDescription"),
  createMembershipButton: document.getElementById("createMembershipButton"),
  membershipAdminList: document.getElementById("membershipAdminList"),
  quotaNavItem: document.getElementById("quotaNavItem"),
  quotaView: document.getElementById("quotaView"),
  accountsNavItem: document.getElementById("accountsNavItem"),
  accountsView: document.getElementById("accountsView"),
  refreshAccounts: document.getElementById("refreshAccounts"),
  accountResetCountdown: document.getElementById("accountResetCountdown"),
  accountQuotaSummary: document.getElementById("accountQuotaSummary"),
  accountForm: document.getElementById("accountForm"),
  accountName: document.getElementById("accountName"),
  accountQuotaLimit: document.getElementById("accountQuotaLimit"),
  accountPlatform: document.getElementById("accountPlatform"),
  accountCookieData: document.getElementById("accountCookieData"),
  accountDetectedCount: document.getElementById("accountDetectedCount"),
  accountImportState: document.getElementById("accountImportState"),
  importAccount: document.getElementById("importAccount"),
  accountTaskSearch: document.getElementById("accountTaskSearch"),
  accountTableBody: document.getElementById("accountTableBody"),
  prevAccountPage: document.getElementById("prevAccountPage"),
  nextAccountPage: document.getElementById("nextAccountPage"),
  accountPageSize: document.getElementById("accountPageSize"),
  accountPageTotal: document.getElementById("accountPageTotal"),
  accountPlatformCount: document.getElementById("accountPlatformCount"),
  accountNormalCount: document.getElementById("accountNormalCount"),
  accountAbnormalCount: document.getElementById("accountAbnormalCount"),
  videoLibrary: document.getElementById("videoLibrary"),
  selectAllVideos: document.getElementById("selectAllVideos"),
  deleteSelectedVideos: document.getElementById("deleteSelectedVideos"),
  accountPlatformFilter: document.getElementById("accountPlatformFilter"),
  clientEntryUrl: document.getElementById("clientEntryUrl"),
  copyClientEntryUrl: document.getElementById("copyClientEntryUrl"),
  refreshTempTokens: document.getElementById("refreshTempTokens"),
  openCreateTokenModal: document.getElementById("openCreateTokenModal"),
  tempTokenTableBody: document.getElementById("tempTokenTableBody"),
  prevQuotaPage: document.getElementById("prevQuotaPage"),
  nextQuotaPage: document.getElementById("nextQuotaPage"),
  quotaPageState: document.getElementById("quotaPageState"),
  quotaPageSize: document.getElementById("quotaPageSize"),
  workersModal: document.getElementById("workersModal"),
  workersInput: document.getElementById("workersInput"),
  workersModalState: document.getElementById("workersModalState"),
  closeWorkersModal: document.getElementById("closeWorkersModal"),
  cancelWorkersModal: document.getElementById("cancelWorkersModal"),
  saveWorkers: document.getElementById("saveWorkers"),
  createTokenModal: document.getElementById("createTokenModal"),
  closeCreateTokenModal: document.getElementById("closeCreateTokenModal"),
  cancelCreateTokenModal: document.getElementById("cancelCreateTokenModal"),
  createTokenCount: document.getElementById("createTokenCount"),
  createTokenLimit: document.getElementById("createTokenLimit"),
  createTokenConcurrency: document.getElementById("createTokenConcurrency"),
  createTokenRetentionDays: document.getElementById("createTokenRetentionDays"),
  createTokenRemark: document.getElementById("createTokenRemark"),
  createTokenState: document.getElementById("createTokenState"),
  confirmCreateTokens: document.getElementById("confirmCreateTokens"),
  textModal: document.getElementById("textModal"),
  textModalContent: document.getElementById("textModalContent"),
  closeTextModal: document.getElementById("closeTextModal"),
  confirmTextModal: document.getElementById("confirmTextModal"),
  copyTextModal: document.getElementById("copyTextModal"),
  videoModal: document.getElementById("videoModal"),
  videoLoading: document.getElementById("videoLoading"),
  videoPlayer: document.getElementById("videoPlayer"),
  closeVideoModal: document.getElementById("closeVideoModal"),
  confirmVideoModal: document.getElementById("confirmVideoModal"),
  copyVideoUrl: document.getElementById("copyVideoUrl"),
  toastStack: document.getElementById("toastStack"),
};

const state = {
  apiToken: "",
  tasks: [],
  taskTotal: 0,
  taskTotalPages: 1,
  taskStats: null,
  results: loadSessionResults(),
  activeIds: [],
  page: 1,
  pageSize: 50,
  quotaPage: 1,
  quotaPageSize: 50,
  userPage: 1,
  userPageSize: 20,
  userTotalPages: 1,
  ratio: "9:16",
  platform: "dola",
  model: "Seedance 2.0",
  platforms: [],
  images: [],
  modalText: "",
  modalVideoUrl: "",
  submitting: false,
  isTempToken: false,
  tempTokens: [],
  accounts: [],
  accountTotal: 0,
  accountTotalPages: 1,
  accountStats: null,
  accountPlatformFilter: "all",
  accountPage: 1,
  accountPageSize: 20,
  accountQuotaSummary: null,
  savingTokenIds: new Set(),
  autoRefreshing: false,
  refreshTimer: 0,
  accessRefreshTimer: 0,
  countdownTimer: 0,
  nextQuotaResetAt: "",
  proxyNodes: [],
  proxyEnabled: true,
  proxyAutoSelect: true,
  proxySelectedNode: "",
  proxyCountry: "all",
  taskRetentionDays: 7,
  userName: "",
  prompts: [],
  promptPage: 1,
  promptPageSize: 8,
  promptPickerPage: 1,
  promptPickerPageSize: 6,
  editingPromptId: "",
  clientRegisterMode: false,
  registrationEmailVerificationEnabled: true,
  freeRemaining: 0,
  points: 0,
  concurrency: 1,
  version: "",
  selectedVideoIds: new Set(),
  adminUsername: "",
  queryingTaskIds: new Set(),
  deletingTaskIds: new Set(),
  pointPackages: [],
  memberships: [],
  membership: null,
  activeAnnouncement: null,
  pointCards: [],
  userSearch: "",
  announcementTimer: 0,
  taskRefreshRequestId: 0,
  accountRefreshRequestId: 0,
  taskSearchTimer: 0,
  accountSearchTimer: 0,
  taskRenderSignature: "",
  messageTab: "feedback",
  notificationUsers: [],
  selectedNotificationUserIds: new Set(),
};

const MAX_IMAGE_COUNT = 9;
const PLATFORM_LABELS = { dola: "Dola", doubao: "豆包", qianwen: "千问" };

function portalStorageKey(base) {
  return `${base}_${portal}`;
}

function promptStorageKey() {
  return `dfyue_prompts_${portal}_${state.apiToken}`;
}

function loadPrompts() {
  try {
    state.prompts = JSON.parse(localStorage.getItem(promptStorageKey()) || "[]");
  } catch {
    state.prompts = [];
  }
}

function savePrompts() {
  localStorage.setItem(promptStorageKey(), JSON.stringify(state.prompts));
}

function newPromptId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `prompt-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function renderPromptPicker() {
  if (!els.promptPickerList) return;
  const pages = Math.max(1, Math.ceil(state.prompts.length / state.promptPickerPageSize));
  state.promptPickerPage = Math.min(Math.max(1, state.promptPickerPage), pages);
  const start = (state.promptPickerPage - 1) * state.promptPickerPageSize;
  const items = state.prompts.slice(start, start + state.promptPickerPageSize);
  els.promptPickerList.innerHTML = items.length ? items.map((item) => `
    <button class="prompt-picker-item" type="button" data-prompt-picker-id="${escapeHtml(item.id)}">
      <strong>${escapeHtml(item.title || "常用提示词")}</strong>
      <span>${escapeHtml(item.content)}</span>
    </button>`).join("") : '<div class="empty-state">提示词库中暂无内容，请先保存提示词。</div>';
  if (els.promptPickerPageState) els.promptPickerPageState.textContent = `第 ${state.promptPickerPage} / ${pages} 页 · 共 ${state.prompts.length} 条`;
  if (els.promptPickerPrev) els.promptPickerPrev.disabled = state.promptPickerPage <= 1;
  if (els.promptPickerNext) els.promptPickerNext.disabled = state.promptPickerPage >= pages;
}

function renderPrompts() {
  if (!els.promptGrid) return;
  const pages = Math.max(1, Math.ceil(state.prompts.length / state.promptPageSize));
  state.promptPage = Math.min(Math.max(1, state.promptPage), pages);
  const start = (state.promptPage - 1) * state.promptPageSize;
  const items = state.prompts.slice(start, start + state.promptPageSize);
  els.promptGrid.innerHTML = items.length ? items.map((item, index) => `
    <article class="prompt-card" draggable="true" data-prompt-id="${escapeHtml(item.id)}">
      <div class="prompt-card-index">${String(start + index + 1).padStart(2, "0")}</div>
      <div class="prompt-card-body"><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.content)}</p><time>${escapeHtml(formatTime(item.updated_at))}</time></div>
      <div class="prompt-card-actions"><button type="button" data-prompt-action="use">使用</button><button type="button" data-prompt-action="edit">编辑</button><button class="danger-text" type="button" data-prompt-action="delete">删除</button><span class="drag-handle" title="拖动排序">⋮⋮</span></div>
    </article>`).join("") : `<div class="empty-state prompt-empty">还没有常用提示词，添加第一条创作灵感吧。</div>`;
  els.promptPageState.textContent = `第 ${state.promptPage} / ${pages} 页 · 共 ${state.prompts.length} 条`;
  els.promptPrevPage.disabled = state.promptPage <= 1;
  els.promptNextPage.disabled = state.promptPage >= pages;
}

function openPromptEditor(item = null) {
  state.editingPromptId = item?.id || "";
  els.promptTitleInput.value = item?.title || "";
  els.promptContentInput.value = item?.content || "";
  els.promptEditor.classList.remove("hidden");
  els.promptTitleInput.focus();
}

function addPrompt(content, title = "常用提示词") {
  const value = String(content || "").trim();
  if (!value) return toast("请先输入提示词", "error");
  state.prompts.unshift({ id: newPromptId(), title: String(title || "常用提示词").trim(), content: value, updated_at: new Date().toISOString() });
  state.promptPage = 1;
  savePrompts();
  renderPrompts();
  renderPromptPicker();
  toast("提示词已保存", "success");
}

function getClientEntryUrl() {
  const configured = window.DFYUE_RUNTIME_CONFIG?.clientEntryUrl || window.DFYUE_RUNTIME_CONFIG?.client_entry_url || "";
  if (configured) return String(configured);
  const url = new URL(window.location.href);
  url.pathname = "/client";
  url.search = "";
  url.hash = "";
  return url.toString();
}

function loadSessionResults() {
  try {
    const token = localStorage.getItem(portalStorageKey(TOKEN_KEY)) || "";
    return JSON.parse(sessionStorage.getItem(`${RESULTS_KEY}_${portal}_${token}`) || "{}");
  } catch (_) {
    return {};
  }
}

function saveSessionResults() {
  sessionStorage.setItem(`${RESULTS_KEY}_${portal}_${state.apiToken}`, JSON.stringify(state.results));
}

async function requestJson(path, token, options = {}) {
  const headers = new Headers(options.headers || {});
  if (token) headers.set("X-API-Token", token);

  let body = options.body;
  if (body && !(body instanceof FormData) && typeof body !== "string") {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }

  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), Number(options.timeout || 15000));
  let response;
  try {
    response = await fetch(path, {
      method: options.method || "GET",
      headers,
      body,
      signal: controller.signal,
      credentials: "same-origin",
    });
  } catch (error) {
    if (error.name === "AbortError") throw new Error("请求超时，请稍后重试");
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }

  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (_) {
      data = { text };
    }
  }

  if (!response.ok) {
    const detail = data?.detail || data?.message || text || `HTTP ${response.status}`;
    const error = new Error(clientSafeText(detail, { model: state.model }));
    error.status = response.status;
    throw error;
  }
  return data || {};
}

async function apiFetch(path, options = {}) {
  return requestJson(path, state.apiToken, options);
}

async function loadRepositoryStatus() {
  if (portal === "client" || !els.repositoryRevision) return;
  try {
    const data = await apiFetch("/admin/repository-update");
    els.repositoryRevision.textContent = data.version ? `v${data.version}` : "版本未知";
    if (els.repositoryLatestVersion) els.repositoryLatestVersion.textContent = data.latest_version ? `v${data.latest_version}` : "版本未知";
    els.repositoryUpdateState.textContent = data.updating ? data.phase || "正在更新" : data.error ? `更新失败：${data.error}` : data.update_available ? "有可用更新" : "已是最新";
    if (els.updateRepository) setBusy(els.updateRepository, Boolean(data.updating), data.phase || "正在更新");
    return data;
  } catch (error) {
    els.repositoryUpdateState.textContent = `更新不可用：${error.message}`;
    if (els.updateRepository) els.updateRepository.disabled = els.updateRepository.dataset.updatePolling === "true";
    return null;
  }
}

function isTransientRepositoryUpdateError(error) {
  const status = Number(error?.status || 0);
  const message = String(error?.message || "");
  return [502, 503, 504].includes(status) || /bad gateway|failed to fetch|networkerror|abort/i.test(message);
}

async function updateRepository() {
  if (!window.confirm("确定部署 GitHub main 最新版本吗？系统将自动构建镜像、重启 API 与 Worker，并执行健康检查。")) return;
  setBusy(els.updateRepository, true, "正在更新");
  els.repositoryUpdateState.textContent = "正在拉取";
  try {
    const data = await apiFetch("/admin/repository-update", { method: "POST" });
    els.repositoryUpdateState.textContent = data.updating ? "已开始部署" : data.updated ? "更新完成" : data.update_available ? "有可用更新" : "已是最新";
    toast(data.updating ? "更新已开始，服务会短暂重启，请稍后刷新页面查看结果" : data.updated ? "更新部署完成" : "当前已是最新版本");
    if (data.updating) await pollRepositoryUpdate();
    else await loadRepositoryStatus();
  } catch (error) {
    if (isTransientRepositoryUpdateError(error)) {
      els.repositoryUpdateState.textContent = "服务正在重启，等待恢复";
      toast("服务正在重启，将继续检查更新结果");
      await pollRepositoryUpdate();
    } else {
      els.repositoryUpdateState.textContent = "更新失败";
      toast(`更新失败：${error.message}`, "error");
    }
  } finally {
    if (!els.updateRepository?.dataset.updatePolling) setBusy(els.updateRepository, false);
  }
}

async function pollRepositoryUpdate() {
  const deadline = Date.now() + 15 * 60 * 1000;
  els.updateRepository.dataset.updatePolling = "true";
  while (Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, 3000));
    const status = await loadRepositoryStatus();
    if (status?.updating || !status) continue;
    delete els.updateRepository.dataset.updatePolling;
    setBusy(els.updateRepository, false);
    if (status.error) {
      toast(`更新失败：${status.error}`, "error");
      return;
    }
    els.repositoryUpdateState.textContent = "更新成功，前后端服务已恢复";
    toast("系统更新成功，前后端服务已恢复");
    return;
  }
  delete els.updateRepository.dataset.updatePolling;
  setBusy(els.updateRepository, false);
  els.repositoryUpdateState.textContent = "更新状态轮询超时，请稍后重新检查";
  toast("更新仍未完成，请稍后重新检查", "error");
}

function toast(message, type = "info") {
  const node = document.createElement("div");
  node.className = `toast ${type === "error" ? "error" : ""}`;
  node.textContent = message;
  els.toastStack.appendChild(node);
  window.setTimeout(() => node.remove(), 3000);
}

function setBusy(button, busy, label) {
  if (!button) return;
  if (!button.dataset.idleText) {
    button.dataset.idleText = button.textContent;
  }
  button.disabled = busy;
  if (button.classList.contains("submit-arrow-button")) {
    return;
  }
  button.textContent = busy ? label : button.dataset.idleText;
}

function setSubmitControlsDisabled(disabled) {
  state.submitting = disabled;
  els.taskForm.classList.toggle("is-submitting", disabled);
  els.promptInput.disabled = disabled;
  els.imageInput.disabled = disabled;
  els.submitTask.disabled = disabled || (portal === "client" && state.freeRemaining + state.points <= 0);
  if (els.resetSubmit) els.resetSubmit.disabled = disabled;
  els.ratioTrigger.disabled = disabled;
  els.ratioGroup.classList.remove("open");
  els.ratioGroup.querySelectorAll("button[data-ratio]").forEach((button) => {
    button.disabled = disabled;
  });
  els.imageList.querySelectorAll("button").forEach((button) => {
    button.disabled = disabled;
  });
}

function clearTokenFromUrl() {
  const url = new URL(window.location.href);
  if (!url.searchParams.has("token")) return;
  url.searchParams.delete("token");
  const query = url.searchParams.toString();
  window.history.replaceState({}, document.title, `${url.pathname}${query ? `?${query}` : ""}${url.hash}`);
}

function shortId(id) {
  return id ? `${id.slice(0, 8)}...${id.slice(-6)}` : "-";
}

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function formatBytes(size) {
  if (!Number.isFinite(size)) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(2)} MB`;
}

function md5ArrayBuffer(buffer) {
  const bytes = new Uint8Array(buffer);
  const shifts = [
    7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
    5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
    4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
    6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
  ];
  const constants = Array.from({ length: 64 }, (_, index) =>
    Math.floor(Math.abs(Math.sin(index + 1)) * 0x100000000) >>> 0
  );
  const rotateLeft = (value, bits) => ((value << bits) | (value >>> (32 - bits))) >>> 0;

  const paddedLength = (((bytes.length + 8) >> 6) + 1) * 64;
  const padded = new Uint8Array(paddedLength);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const bitLength = bytes.length * 8;
  for (let i = 0; i < 8; i += 1) {
    padded[paddedLength - 8 + i] = Math.floor(bitLength / (2 ** (8 * i))) & 0xff;
  }

  let a0 = 0x67452301;
  let b0 = 0xefcdab89;
  let c0 = 0x98badcfe;
  let d0 = 0x10325476;

  for (let offset = 0; offset < padded.length; offset += 64) {
    const words = [];
    for (let index = 0; index < 16; index += 1) {
      const base = offset + index * 4;
      words[index] = (
        padded[base] |
        (padded[base + 1] << 8) |
        (padded[base + 2] << 16) |
        (padded[base + 3] << 24)
      ) >>> 0;
    }

    let a = a0;
    let b = b0;
    let c = c0;
    let d = d0;

    for (let index = 0; index < 64; index += 1) {
      let f;
      let g;
      if (index < 16) {
        f = (b & c) | (~b & d);
        g = index;
      } else if (index < 32) {
        f = (d & b) | (~d & c);
        g = (5 * index + 1) % 16;
      } else if (index < 48) {
        f = b ^ c ^ d;
        g = (3 * index + 5) % 16;
      } else {
        f = c ^ (b | ~d);
        g = (7 * index) % 16;
      }
      const next = d;
      d = c;
      c = b;
      b = (b + rotateLeft((a + f + constants[index] + words[g]) >>> 0, shifts[index])) >>> 0;
      a = next;
    }

    a0 = (a0 + a) >>> 0;
    b0 = (b0 + b) >>> 0;
    c0 = (c0 + c) >>> 0;
    d0 = (d0 + d) >>> 0;
  }

  const hexWord = (value) => {
    let out = "";
    for (let i = 0; i < 4; i += 1) {
      out += ((value >> (i * 8)) & 0xff).toString(16).padStart(2, "0");
    }
    return out;
  };
  return [a0, b0, c0, d0].map(hexWord).join("");
}

async function fileMd5(file) {
  if (file.__dfyueMd5) return file.__dfyueMd5;
  const hash = md5ArrayBuffer(await file.arrayBuffer());
  Object.defineProperty(file, "__dfyueMd5", { value: hash, enumerable: false });
  return hash;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function clientSafeText(value, task = {}) {
  let text = String(value || "");
  if (portal !== "client") return text;
  const model = String(task.model || "当前模型");
  text = text.replace(/Dola|豆包|千问|qianwen|doubao|平台/gi, model);
  text = text.replace(/账号|账户|号池|换号|服务凭证/gi, "服务");
  if (/额度不足|额度已用完|次数不足|次数已用完|余额不足|正在切换服务重试|正在切换账号重试|多个服务额度均不足/.test(text)) return "生成异常请重试！";
  return text;
}

function setServiceState(ok, note) {
  els.sidebarStatusDot.classList.toggle("ok", ok);
  els.sidebarStatusDot.classList.toggle("bad", !ok);
  els.metricService.classList.toggle("ok", ok);
  els.metricService.classList.toggle("bad", !ok);
  els.sidebarStatusText.textContent = ok ? "已连接" : "连接失败";
  els.metricService.textContent = ok ? "正常" : "异常";
  els.metricServiceNote.textContent = note || (ok ? "API 可访问" : "检查 Token 或服务");
}

function showLogin(message = "等待输入") {
  document.body.dataset.portal = portal;
  els.appShell.classList.add("hidden");
  els.loginView.classList.remove("hidden");
  els.loginState.textContent = message;
  sessionStorage.removeItem(portalStorageKey(AUTH_KEY));
}

function expireSession(message = "登录已失效，请重新登录") {
  state.apiToken = "";
  localStorage.removeItem(portalStorageKey(TOKEN_KEY));
  sessionStorage.removeItem(portalStorageKey(AUTH_KEY));
  showLogin(message);
}

function showApp() {
  document.body.dataset.portal = portal;
  els.loginView.classList.add("hidden");
  els.appShell.classList.remove("hidden");
  switchView("dashboard");
  startAutoRefresh();
  if (portal === "client") window.setTimeout(() => showNextUnseenAnnouncement().catch(() => {}), 250);
}

function startAutoRefresh() {
  if (portal === "client" && !state.accessRefreshTimer) {
    state.accessRefreshTimer = window.setInterval(async () => {
      if (!state.apiToken || document.hidden || els.appShell.classList.contains("hidden") || state.submitting) return;
      try {
        applyAccessScope(await apiFetch("/auth/access-state"));
      } catch (error) {
        if (error.status === 401 || error.status === 403) expireSession();
      }
    }, 5000);
  }
  if (!state.refreshTimer) {
    state.refreshTimer = window.setInterval(async () => {
      if ((portal === "client" && !state.apiToken) || document.hidden || els.appShell.classList.contains("hidden")) return;
      if (state.autoRefreshing) return;
      state.autoRefreshing = true;
      try {
        await refreshHealth();
        const activeView = document.querySelector(".view.active")?.id || "";
        const jobs = [];
        if (activeView === "dashboardView" || activeView === "tasksView") jobs.push(refreshTasks({ quiet: true, keepPage: true }));
        if (portal === "admin" && activeView === "accountsView") jobs.push(refreshAccounts({ quiet: true }));
        if (portal === "admin" && activeView === "usersView") jobs.push(loadUsers());
        const editingMessage = activeView === "messagesView" && Boolean(document.activeElement?.closest("#messagesView input, #messagesView textarea, #messagesView select"));
        if (activeView === "messagesView" && !editingMessage) jobs.push(loadMessageCenter({ quiet: true }));
        if (portal === "client" && activeView !== "messagesView") jobs.push(loadClientNotifications());
        if (portal === "client") jobs.push(showNextUnseenAnnouncement());
        await Promise.allSettled(jobs);
        updateDashboardMetrics();
      } catch (error) {
        if (error.status === 401 || error.status === 403) {
          expireSession();
          toast("登录已失效，请重新登录", "error");
        }
      } finally {
        state.autoRefreshing = false;
      }
    }, 30000);
  }
  if (!state.countdownTimer) {
    state.countdownTimer = window.setInterval(() => {
      updateAccountResetCountdown();
      updateMembershipRemaining();
    }, 1000);
  }
}

function applyPortalText() {
  document.body.dataset.portal = portal;
  document.title = portal === "client" ? "客户入口" : "管理面板";
  const dashboardLabel = portal === "client" ? "用户首页" : "总览";
  const dashboardNavLabel = document.querySelector('.nav-item[data-view="dashboard"] span');
  const dashboardView = document.getElementById("dashboardView");
  if (dashboardNavLabel) dashboardNavLabel.textContent = dashboardLabel;
  if (dashboardView) dashboardView.dataset.title = dashboardLabel;
  if (els.viewTitle) els.viewTitle.textContent = dashboardLabel;
  if (els.clientEntryUrl) els.clientEntryUrl.value = getClientEntryUrl();
  document.querySelectorAll(".login-heading .eyebrow").forEach((node) => {
    node.textContent = portal === "client" ? "" : "管理面板";
  });
  const accountLabel = document.querySelector(".client-credential-field span");
  if (accountLabel) accountLabel.textContent = portal === "client" ? "账号或邮箱" : "账号";
  els.forgotPasswordButton?.classList.toggle("hidden", portal !== "client");
  if (els.adminUsername && !els.adminUsername.value) els.adminUsername.value = "1055660108";
  if (els.changeAdminUsername && !els.changeAdminUsername.value) els.changeAdminUsername.value = els.adminUsername?.value || "1055660108";
}

function applyAccessScope(data = {}) {
  state.isTempToken = portal === "client";
  const isClient = portal === "client";
  if (data.task_retention_days != null) state.taskRetentionDays = Number(data.task_retention_days || 7);
  if (data.user_name) state.userName = String(data.user_name);
  if (!state.userName) state.userName = "当前用户";
  if (data.token_concurrency != null || (isClient && data.browser_workers != null)) {
    state.concurrency = Math.max(1, Number(data.token_concurrency ?? data.browser_workers ?? 1));
    if (els.metricWorkers) els.metricWorkers.textContent = String(state.concurrency);
  }
  if (data.version) {
    state.version = String(data.version);
    if (els.sidebarVersion) els.sidebarVersion.textContent = `v${state.version}`;
  }
  if (data.admin_username) state.adminUsername = String(data.admin_username);
  if (els.adminAccountDisplay) els.adminAccountDisplay.textContent = state.adminUsername || els.adminUsername?.value || "-";
  if (els.changeAdminUsername) els.changeAdminUsername.value = state.adminUsername || els.adminUsername?.value || "";
  if (els.clientAccountName) els.clientAccountName.textContent = state.userName;
  if (els.clientSettingsAccount) els.clientSettingsAccount.textContent = state.userName;
  if (data.quota) {
    const used = Math.max(0, Number(data.quota.used || 0));
    const limit = Math.max(0, Number(data.quota.limit || 0));
    state.freeRemaining = Math.max(0, Number(data.quota.free_remaining ?? Math.min(3, limit) - used));
    state.points = Math.max(0, Number(data.quota.points ?? limit - Math.max(used, 3)));
  }
  if (els.pointsBalance) els.pointsBalance.textContent = String(state.points);
  if (els.dashboardPointsBalance) els.dashboardPointsBalance.textContent = String(state.points);
  if (els.submitFreeRemaining) els.submitFreeRemaining.textContent = String(state.freeRemaining);
  if (els.submitPointsBalance) els.submitPointsBalance.textContent = String(state.points);
  const selectedPlatform = state.platforms.find((item) => item.id === state.platform);
  const modelCost = Number(selectedPlatform?.model_costs?.[state.model] ?? 1);
  if (els.submitCostText) els.submitCostText.textContent = state.freeRemaining > 0 ? "本次使用 1 次视频额度" : `本次消耗 ${modelCost} 积分`;
  if (portal === "client" && els.submitTask && !state.submitting) els.submitTask.disabled = state.freeRemaining <= 0 && state.points < modelCost;
  if (els.clientTokenDisplay && state.apiToken) els.clientTokenDisplay.value = state.apiToken;
  document.getElementById("mainNav")?.classList.toggle("client-nav-empty", isClient);
  els.dashboardLogoutButton?.classList.toggle("hidden", !isClient);
  if (els.editWorkers) els.editWorkers.classList.toggle("hidden", isClient);
  if (els.proxyConfigPanel) els.proxyConfigPanel.classList.toggle("hidden", isClient);
  if (els.proxyNodesNavItem) els.proxyNodesNavItem.classList.toggle("hidden", isClient);
  if (els.proxyNodesView) els.proxyNodesView.classList.toggle("hidden", isClient);
  if (els.messagesNavLabel) els.messagesNavLabel.textContent = isClient ? "消息中心" : "消息处理";
  if (els.messagesView) els.messagesView.dataset.title = isClient ? "消息中心" : "消息处理";
  if (els.quotaNavItem) {
    els.quotaNavItem.classList.toggle("hidden", isClient);
    els.quotaNavItem.setAttribute("aria-hidden", isClient ? "true" : "false");
    els.quotaNavItem.tabIndex = isClient ? -1 : 0;
  }
  if (els.quotaView) {
    els.quotaView.classList.toggle("hidden", isClient);
    els.quotaView.setAttribute("aria-hidden", isClient ? "true" : "false");
  }
  if (els.accountsNavItem) {
    els.accountsNavItem.classList.toggle("hidden", isClient);
    els.accountsNavItem.setAttribute("aria-hidden", isClient ? "true" : "false");
    els.accountsNavItem.tabIndex = isClient ? -1 : 0;
  }
  if (els.accountsView) {
    els.accountsView.classList.toggle("hidden", isClient);
    els.accountsView.setAttribute("aria-hidden", isClient ? "true" : "false");
  }
  const docsNav = document.querySelector('.nav-item[data-view="docs"]');
  const docsView = document.getElementById("docsView");
  if (docsNav) {
    docsNav.classList.toggle("hidden", isClient);
    docsNav.setAttribute("aria-hidden", isClient ? "true" : "false");
    docsNav.tabIndex = isClient ? -1 : 0;
  }
  if (docsView) {
    docsView.classList.toggle("hidden", isClient);
    docsView.setAttribute("aria-hidden", isClient ? "true" : "false");
  }
  if (isClient && ["quotaView", "accountsView", "docsView", "proxy-nodesView"].includes(document.querySelector(".view.active")?.id || "")) {
    switchView("dashboard");
  }
}

async function login(event) {
  event.preventDefault();
  let token = els.loginToken.value.trim();
  setBusy(els.loginButton, true, "校验中");
  els.loginState.textContent = "校验中";
  try {
    if (portal === "client") {
      if (!els.clientUsername.value.trim() || !els.clientPassword.value) throw new Error("请填写用户名和密码");
      if (state.clientRegisterMode && els.clientPassword.value !== els.clientConfirmPassword.value) throw new Error("两次输入的密码不一致");
      const emailVerificationRequired = state.clientRegisterMode && state.registrationEmailVerificationEnabled;
      const email = emailVerificationRequired ? `${els.clientEmailLocal.value.trim()}${els.clientEmailDomain.value}` : "";
      if (emailVerificationRequired && (!els.clientEmailLocal.value.trim() || !els.clientEmailCode.value.trim())) throw new Error("请填写邮箱和验证码");
      const data = await requestJson(state.clientRegisterMode ? "/auth/register" : "/auth/login", "", {
        method: "POST", body: { identifier: els.clientUsername.value.trim(), username: els.clientUsername.value.trim(), password: els.clientPassword.value, confirm_password: els.clientConfirmPassword.value, email, email_code: emailVerificationRequired ? els.clientEmailCode.value.trim() : "" },
      });
      token = data.token;
    } else {
      if (!els.adminUsername.value.trim() || !els.adminPassword.value) throw new Error("请填写管理员账号和密码");
      await requestJson("/auth/admin/login", "", {
        method: "POST", body: { username: els.adminUsername.value.trim(), password: els.adminPassword.value },
      });
      token = "";
    }
    const health = await requestJson(authPath, token);
    if (state.apiToken !== token) state.results = {};
    state.apiToken = token;
    loadPrompts();
    applyAccessScope(health);
    if (token) localStorage.setItem(portalStorageKey(TOKEN_KEY), token);
    else {
      localStorage.removeItem(portalStorageKey(TOKEN_KEY));
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem("dola_fetch_api_token");
    }
    sessionStorage.setItem(portalStorageKey(AUTH_KEY), "1");
    clearTokenFromUrl();
    showApp();
    if (els.clientTokenDisplay) els.clientTokenDisplay.value = token;
    await refreshDashboard();
  } catch (error) {
    els.loginState.textContent = "校验失败";
    toast(`登录失败：${error.message}`, "error");
  } finally {
    setBusy(els.loginButton, false);
  }
}

async function changeAdminPassword(event) {
  event.preventDefault();
  const username = els.changeAdminUsername.value.trim();
  const currentPassword = els.currentAdminPassword.value;
  const newPassword = els.newAdminPassword.value;
  const confirmPassword = els.confirmAdminPassword.value;
  if (!username || !currentPassword || !newPassword || !confirmPassword) return toast("请完整填写改密信息", "error");
  if (newPassword !== confirmPassword) return toast("两次输入的新密码不一致", "error");
  setBusy(els.changeAdminPasswordButton, true, "修改中");
  if (els.adminPasswordState) els.adminPasswordState.textContent = "正在修改";
  try {
    await apiFetch("/auth/admin/password", { method: "POST", body: { username, current_password: currentPassword, new_password: newPassword, confirm_password: confirmPassword } });
    els.adminPasswordForm.reset();
    els.changeAdminUsername.value = username;
    els.adminUsername.value = username;
    els.adminPassword.value = "";
    if (els.adminPasswordState) els.adminPasswordState.textContent = "修改成功";
    toast("管理员密码已修改，请使用新密码登录");
    window.setTimeout(() => logout(), 500);
  } catch (error) {
    if (els.adminPasswordState) els.adminPasswordState.textContent = "修改失败";
    toast(`修改失败：${error.message}`, "error");
  } finally {
    setBusy(els.changeAdminPasswordButton, false);
  }
}

async function logout() {
  if (portal === "admin") {
    try {
      await requestJson("/auth/admin/logout", "", { method: "POST" });
    } catch (_) {
    }
  }
  state.tasks = [];
  state.results = {};
  state.apiToken = "";
  localStorage.removeItem(portalStorageKey(TOKEN_KEY));
  sessionStorage.removeItem(portalStorageKey(AUTH_KEY));
  if (state.refreshTimer) window.clearInterval(state.refreshTimer);
  if (state.accessRefreshTimer) window.clearInterval(state.accessRefreshTimer);
  if (state.countdownTimer) window.clearInterval(state.countdownTimer);
  state.refreshTimer = 0;
  state.accessRefreshTimer = 0;
  state.countdownTimer = 0;
  showLogin("已退出");
}

function switchView(name) {
  if (portal === "client" && ["quota", "accounts", "docs", "proxy-nodes"].includes(name)) {
    name = "dashboard";
  }
  const targetView = document.getElementById(`${name}View`);
  if (!targetView || targetView.classList.contains("hidden")) {
    name = "dashboard";
  }
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === name);
  });
  document.querySelectorAll(".view").forEach((view) => {
    const active = view.id === `${name}View`;
    view.classList.toggle("active", active);
    if (active && els.viewTitle) els.viewTitle.textContent = view.dataset.title || "";
  });
  if (name === "tasks" && !state.tasks.length) refreshTasks();
  if (name === "quota" && !state.tempTokens.length) refreshTempTokens();
  if (name === "accounts" && !state.accounts.length) refreshAccounts();
  if (name === "videos") renderVideoLibrary();
  if (name === "prompts") renderPrompts();
  if (name === "users") loadUsers();
  if (name === "messages") loadMessageCenter();
  if (name === "points") loadPointPackages();
  if (name === "membership") Promise.allSettled([loadMemberships(), loadClientProfile()]);
  if (name === "transactions") loadTransactions();
  if (name === "point-cards") loadPointCards();
  if (name === "settings" && portal === "admin") Promise.allSettled([loadRepositoryStatus(), loadProxyConfig(), loadEmailConfig(), loadPlatforms(), loadAdminPointPackages(), loadAdminMemberships()]);
  if (name === "proxy-nodes" && portal === "admin") loadProxyNodes();
  if (name === "settings" && portal === "client") loadClientProfile().catch((error) => toast(`邮箱读取失败：${error.message}`, "error"));
}

const FEEDBACK_STATUS_LABELS = { pending: "待处理", reviewing: "处理中", resolved: "已解决", closed: "已关闭" };

function setMessageTab(tab, options = {}) {
  state.messageTab = ["feedback", "notifications", "announcements"].includes(tab) ? tab : "feedback";
  document.querySelectorAll("[data-message-tab]").forEach((button) => button.classList.toggle("active", button.dataset.messageTab === state.messageTab));
  document.querySelectorAll("[data-message-panel]").forEach((panel) => panel.classList.toggle("hidden", panel.dataset.messagePanel !== state.messageTab));
  if (portal === "client" && state.messageTab === "notifications") {
    apiFetch("/notifications/read-all", { method: "POST" }).then(() => loadMessageCenter({ quiet: true })).catch(() => {});
  } else if (options.refresh !== false) {
    loadMessageCenter({ quiet: true });
  }
}

async function loadClientFeedback() {
  if (portal !== "client" || !els.clientFeedbackList) return;
  const data = await apiFetch("/feedback");
  const rows = Array.isArray(data.feedback) ? data.feedback : [];
  els.clientFeedbackList.innerHTML = rows.length ? rows.map((item) => `
    <article class="message-card">
      <div class="message-card-head"><div><strong>${escapeHtml(item.category || "其他")}</strong><span>${escapeHtml(formatTime(item.created_at))}</span></div><span class="message-status ${escapeHtml(item.status || "pending")}">${escapeHtml(FEEDBACK_STATUS_LABELS[item.status] || item.status || "待处理")}</span></div>
      <p>${escapeHtml(item.content || "")}</p>
      <div class="admin-reply ${item.admin_note ? "" : "empty"}"><span>管理员回复</span><p>${escapeHtml(item.admin_note || "暂未回复")}</p></div>
    </article>`).join("") : '<div class="empty-state">还没有提交过反馈</div>';
}

async function loadClientNotifications() {
  if (portal !== "client" || !els.clientNotificationList) return;
  const data = await apiFetch("/notifications");
  const rows = Array.isArray(data.notifications) ? data.notifications : [];
  const unread = Number(data.unread || 0);
  if (els.messageUnreadCount) {
    els.messageUnreadCount.textContent = unread > 0 ? "1" : "0";
    els.messageUnreadCount.classList.toggle("hidden", unread <= 0);
  }
  els.clientNotificationList.innerHTML = rows.length ? rows.map((item) => `
    <article class="message-card notification-card ${item.read_at ? "read" : "unread"}">
      <div class="message-card-head"><div><strong>${escapeHtml(item.title || "通知")}</strong><span>${escapeHtml(formatTime(item.created_at))}</span></div>${item.read_at ? '<span class="message-status resolved">已读</span>' : `<button class="text-button" type="button" data-read-notification="${escapeHtml(item.id)}">标为已读</button>`}</div>
      <p>${escapeHtml(item.content || "")}</p>
    </article>`).join("") : '<div class="empty-state">暂无通知</div>';
}

async function loadFeedback() {
  if (portal !== "admin" || !els.feedbackTableBody) return;
  const data = await apiFetch("/admin/feedback?page=1&page_size=100");
  const rows = Array.isArray(data.feedback) ? data.feedback : [];
  els.feedbackTableBody.innerHTML = rows.length ? rows.map((item) => `<tr>
    <td><strong>${escapeHtml(item.username || "未知用户")}</strong><br><small>${escapeHtml(formatTime(item.created_at))}</small></td>
    <td>${escapeHtml(item.category || "其他")}</td>
    <td><div class="feedback-content">${escapeHtml(item.content || "")}</div></td>
    <td><textarea class="feedback-reply-input" data-feedback-note="${escapeHtml(item.id)}" maxlength="5000" placeholder="输入回复内容">${escapeHtml(item.admin_note || "")}</textarea></td>
    <td><div class="feedback-status-actions"><select data-feedback-status="${escapeHtml(item.id)}"><option value="pending" ${item.status === "pending" ? "selected" : ""}>待处理</option><option value="reviewing" ${item.status === "reviewing" ? "selected" : ""}>处理中</option><option value="resolved" ${item.status === "resolved" ? "selected" : ""}>已解决</option><option value="closed" ${item.status === "closed" ? "selected" : ""}>已关闭</option></select><button class="primary-button compact-button" type="button" data-save-feedback="${escapeHtml(item.id)}">保存</button></div></td>
    <td><button class="danger-button compact-button" type="button" data-delete-feedback="${escapeHtml(item.id)}">删除</button></td>
  </tr>`).join("") : '<tr><td colspan="6"><div class="empty-state">暂无用户反馈</div></td></tr>';
}

async function saveFeedbackRecord(feedbackId) {
  const status = els.feedbackTableBody?.querySelector(`[data-feedback-status="${CSS.escape(feedbackId)}"]`)?.value || "pending";
  const adminNote = els.feedbackTableBody?.querySelector(`[data-feedback-note="${CSS.escape(feedbackId)}"]`)?.value.trim() || "";
  await apiFetch(`/admin/feedback/${encodeURIComponent(feedbackId)}`, { method: "PATCH", body: { status, admin_note: adminNote } });
}

function updateNotificationRecipientState() {
  const count = state.selectedNotificationUserIds.size;
  if (els.notificationRecipientState) els.notificationRecipientState.textContent = count ? `已选择 ${count} 位用户` : "请选择用户";
  if (els.selectAllNotificationUsers) els.selectAllNotificationUsers.checked = Boolean(state.notificationUsers.length) && count === state.notificationUsers.length;
}

async function loadNotificationRecipients() {
  if (portal !== "admin" || !els.notificationRecipients) return;
  const data = await apiFetch("/admin/notification-recipients");
  state.notificationUsers = Array.isArray(data.users) ? data.users : [];
  state.selectedNotificationUserIds = new Set(Array.from(state.selectedNotificationUserIds).filter((id) => state.notificationUsers.some((item) => item.id === id)));
  els.notificationRecipients.innerHTML = state.notificationUsers.length ? state.notificationUsers.map((item) => `<label class="recipient-option"><input type="checkbox" value="${escapeHtml(item.id)}" data-notification-user ${state.selectedNotificationUserIds.has(item.id) ? "checked" : ""} /><span><strong>${escapeHtml(item.username)}</strong><small>${escapeHtml(item.email || (item.enabled ? "未绑定邮箱" : "账号已停用"))}</small></span></label>`).join("") : '<div class="empty-state">暂无注册用户</div>';
  updateNotificationRecipientState();
}

async function loadAdminNotifications() {
  if (portal !== "admin" || !els.adminNotificationList) return;
  const data = await apiFetch("/admin/notifications?limit=200");
  const rows = Array.isArray(data.notifications) ? data.notifications : [];
  els.adminNotificationList.innerHTML = rows.length ? rows.map((item) => `<article class="admin-message-row" data-notification-id="${escapeHtml(item.id)}"><div class="message-card-head"><div><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(formatTime(item.created_at))}</span></div><div class="message-row-actions"><span class="message-status ${item.read_at ? "resolved" : "pending"}">${item.read_at ? "已读" : "未读"}</span><button class="danger-button compact-button" type="button" data-delete-notification>删除</button></div></div><p>${escapeHtml(item.content)}</p><small>接收用户：${escapeHtml(item.username || item.user_id)}</small></article>`).join("") : '<div class="empty-state">暂无发送记录</div>';
}

async function loadClientAnnouncements() {
  if (portal !== "client" || !els.clientAnnouncementList) return [];
  const data = await apiFetch("/announcements");
  const rows = Array.isArray(data.announcements) ? data.announcements : [];
  const levelLabels = { small: "小公告", large: "大公告", emergency: "紧急公告" };
  els.clientAnnouncementList.innerHTML = rows.length ? rows.map((item) => `<article class="message-card ${item.seen ? "read" : "unread"}"><div class="message-card-head"><div><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(levelLabels[item.level] || "大公告")} · ${escapeHtml(formatTime(item.created_at))}</span></div><span class="message-status ${item.seen ? "resolved" : "pending"}">${item.seen ? "已查看" : "未查看"}</span></div><p>${escapeHtml(item.content)}</p></article>`).join("") : '<div class="empty-state">暂无公告</div>';
  return rows;
}

async function loadAdminAnnouncements() {
  if (portal !== "admin" || !els.adminAnnouncementList) return;
  const data = await apiFetch("/admin/announcements");
  const rows = Array.isArray(data.announcements) ? data.announcements : [];
  const levelLabels = { small: "小公告", large: "大公告", emergency: "紧急公告" };
  els.adminAnnouncementList.innerHTML = rows.length ? rows.map((item) => `<article class="admin-message-row" data-announcement-id="${escapeHtml(item.id)}"><div class="message-card-head"><div><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(levelLabels[item.level] || "大公告")} · ${escapeHtml(formatTime(item.created_at))}</span></div><div class="announcement-actions">${item.level === "emergency" && item.enabled ? `<button class="secondary-button compact-button" type="button" data-toggle-announcement-lock data-locked="${Boolean(item.lock_screen)}">${item.lock_screen ? "解除锁屏" : "启用锁屏"}</button>` : ""}<button class="secondary-button compact-button" type="button" data-toggle-announcement data-enabled="${item.enabled}">${item.enabled ? "停用" : "启用"}</button><button class="danger-button compact-button" type="button" data-delete-announcement>删除</button></div></div><p>${escapeHtml(item.content)}</p></article>`).join("") : '<div class="empty-state">暂无公告</div>';
}

async function showNextUnseenAnnouncement() {
  if (portal !== "client" || !state.apiToken) return;
  const rows = await loadClientAnnouncements();
  if (state.activeAnnouncement) {
    const current = rows.find((row) => row.id === state.activeAnnouncement.id);
    if (current && (state.activeAnnouncement.level !== "emergency" || current.lock_screen === state.activeAnnouncement.lock_screen)) return;
    hideAnnouncementSurfaces();
    state.activeAnnouncement = null;
  }
  const unseen = rows.filter((row) => !row.seen);
  const item = unseen.find((row) => row.level === "emergency" && row.lock_screen) || unseen.find((row) => row.level === "emergency") || unseen[0];
  if (!item) return;
  state.activeAnnouncement = item;
  if (item.level === "small") {
    els.smallAnnouncementTitle.textContent = item.title || "平台公告";
    els.smallAnnouncementContent.textContent = item.content || "";
    els.smallAnnouncementToast.classList.remove("hidden");
    window.clearTimeout(state.announcementTimer);
    state.announcementTimer = window.setTimeout(() => closeActiveAnnouncement().catch(() => {}), 8000);
  } else if (item.level === "emergency") {
    els.emergencyAnnouncementTitle.textContent = item.title || "紧急公告";
    els.emergencyAnnouncementContent.textContent = item.content || "";
    els.closeEmergencyAnnouncement.classList.toggle("hidden", Boolean(item.lock_screen));
    els.emergencyLockNote.classList.toggle("hidden", !item.lock_screen);
    els.emergencyAnnouncementOverlay.classList.remove("hidden");
    document.body.classList.toggle("announcement-locked", Boolean(item.lock_screen));
  } else {
    els.announcementModalTitle.textContent = item.title || "平台公告";
    els.announcementModalContent.textContent = item.content || "";
    els.announcementModal.classList.remove("hidden");
    els.announcementModal.setAttribute("aria-hidden", "false");
  }
}

function hideAnnouncementSurfaces() {
  window.clearTimeout(state.announcementTimer);
  state.announcementTimer = 0;
  els.announcementModal?.classList.add("hidden");
  els.announcementModal?.setAttribute("aria-hidden", "true");
  els.smallAnnouncementToast?.classList.add("hidden");
  els.emergencyAnnouncementOverlay?.classList.add("hidden");
  document.body.classList.remove("announcement-locked");
}

async function closeActiveAnnouncement() {
  const item = state.activeAnnouncement;
  if (item?.level === "emergency" && item.lock_screen) return;
  hideAnnouncementSurfaces();
  state.activeAnnouncement = null;
  if (item?.id) {
    await apiFetch(`/announcements/${encodeURIComponent(item.id)}/seen`, { method: "PATCH" });
    await loadClientAnnouncements();
  }
}

async function submitAdminAnnouncement(event) {
  event.preventDefault();
  setBusy(els.publishAnnouncementButton, true, "发布中");
  try {
    await apiFetch("/admin/announcements", { method: "POST", body: { title: els.announcementTitle.value.trim(), content: els.announcementContent.value.trim(), level: els.announcementLevel.value, lock_screen: els.announcementLockScreen.checked } });
    els.adminAnnouncementForm.reset();
    els.announcementLockField.classList.add("hidden");
    await loadAdminAnnouncements();
    toast("公告已发布");
  } catch (error) {
    toast(`公告发布失败：${error.message}`, "error");
  } finally {
    setBusy(els.publishAnnouncementButton, false);
  }
}

async function loadMessageCenter(options = {}) {
  try {
    if (portal === "client") {
      if (state.messageTab === "feedback") await loadClientFeedback();
      else if (state.messageTab === "notifications") await loadClientNotifications();
      else await loadClientAnnouncements();
    } else if (state.messageTab === "feedback") {
      await loadFeedback();
    } else if (state.messageTab === "notifications") {
      await Promise.all([loadNotificationRecipients(), loadAdminNotifications()]);
    } else {
      await loadAdminAnnouncements();
    }
    return true;
  } catch (error) {
    if (!options.quiet) toast(`消息读取失败：${error.message}`, "error");
    return false;
  }
}

async function refreshMessageCenter() {
  setBusy(els.refreshMessages, true, "刷新中");
  if (els.messagesRefreshState) els.messagesRefreshState.textContent = "正在刷新...";
  try {
    const refreshed = await loadMessageCenter();
    if (!refreshed) {
      if (els.messagesRefreshState) els.messagesRefreshState.textContent = "刷新失败";
      return;
    }
    const refreshedAt = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    if (els.messagesRefreshState) els.messagesRefreshState.textContent = `最近刷新 ${refreshedAt}`;
    toast("消息已刷新");
  } finally {
    setBusy(els.refreshMessages, false);
  }
}

async function submitFeedback(event) {
  event.preventDefault();
  setBusy(els.submitFeedbackButton, true, "提交中");
  try {
    await apiFetch("/feedback", { method: "POST", body: { category: els.feedbackCategory.value, content: els.feedbackContent.value.trim(), contact: els.feedbackContact.value.trim(), source_page: location.pathname } });
    els.feedbackForm.reset();
    closeSettingsModal(els.feedbackModal);
    toast("反馈已提交，感谢你的建议");
    setMessageTab("feedback", { refresh: false });
    await loadClientFeedback();
  } catch (error) {
    toast(`提交失败：${error.message}`, "error");
  } finally {
    setBusy(els.submitFeedbackButton, false);
  }
}

async function submitAdminNotification(event) {
  event.preventDefault();
  const userIds = Array.from(state.selectedNotificationUserIds);
  if (!userIds.length) return toast("请至少选择一位用户", "error");
  setBusy(els.sendNotificationButton, true, "发送中");
  try {
    const data = await apiFetch("/admin/notifications", { method: "POST", body: { user_ids: userIds, title: els.notificationTitle.value.trim(), content: els.notificationContent.value.trim() } });
    toast(`通知已发送给 ${data.recipient_count || userIds.length} 位用户`);
    els.adminNotificationForm.reset();
    state.selectedNotificationUserIds.clear();
    await Promise.all([loadNotificationRecipients(), loadAdminNotifications()]);
  } catch (error) {
    toast(`通知发送失败：${error.message}`, "error");
  } finally {
    setBusy(els.sendNotificationButton, false);
  }
}

async function loadPointPackages() {
  if (!els.purchaseOptions || portal !== "client") return;
  try {
    const data = await apiFetch("/points/packages");
    els.purchaseOptions.innerHTML = (data.packages || []).map((item) => `<button class="purchase-package" type="button" data-purchase-url="${escapeHtml(item.payment_url || data.payment_url || "https://pay.ldxp.cn/shop/huisu/fhm9gj")}"><span class="purchase-package-name">${escapeHtml(item.name || "积分套餐")}</span><strong>${escapeHtml(item.points)}<small>积分</small></strong><span class="purchase-package-meta">${item.bonus_free_uses ? `赠送 ${escapeHtml(item.bonus_free_uses)} 次视频额度` : "标准积分套餐"}</span><span class="purchase-package-action">前往购买</span></button>`).join("");
  } catch (error) {
    toast(`套餐读取失败：${error.message}`, "error");
  }
}

async function redeemPoints(event) {
  event.preventDefault();
  setBusy(els.redeemButton, true, "兑换中");
  try {
    const data = await apiFetch("/points/redeem", { method: "POST", body: { code: els.redeemCode.value.trim() } });
    els.redeemForm.reset();
    await refreshHealth();
    toast(`已兑换 ${data.points} 积分`);
  } catch (error) {
    toast(`兑换失败：${error.message}`, "error");
  } finally {
    setBusy(els.redeemButton, false);
  }
}

async function loadMemberships() {
  if (portal !== "client" || !els.membershipList) return;
  const data = await apiFetch("/memberships");
  els.membershipList.innerHTML = (data.packages || []).length ? data.packages.map((item) => `<article class="membership-item"><div><span>${escapeHtml(item.duration_days)} 天</span><h3>${escapeHtml(item.name)}</h3><p>${escapeHtml(item.description || "")}</p><div class="membership-benefits"><span>并发 +${escapeHtml(item.concurrency)}</span><span>赠送 ${escapeHtml(item.bonus_free_uses)} 次视频额度</span></div></div><div class="membership-price"><strong>${escapeHtml(item.points_cost)} 积分</strong><button class="primary-button" type="button" data-membership-id="${escapeHtml(item.id)}" data-membership-name="${escapeHtml(item.name)}" data-membership-cost="${escapeHtml(item.points_cost)}">积分购买</button></div></article>`).join("") : '<div class="empty-state">暂无可用会员套餐</div>';
  updateMembershipPurchaseButtons();
}

function purchasedMembershipIds() {
  if (!state.membership) return new Set();
  const ids = Array.isArray(state.membership.purchased_package_ids) ? [...state.membership.purchased_package_ids] : [];
  if (state.membership.package_id) ids.push(state.membership.package_id);
  return new Set(ids.map(String));
}

function updateMembershipPurchaseButtons() {
  const purchasedIds = purchasedMembershipIds();
  els.membershipList?.querySelectorAll("[data-membership-id]").forEach((button) => {
    const purchased = purchasedIds.has(String(button.dataset.membershipId || ""));
    button.disabled = purchased;
    button.textContent = purchased ? "有效期内已购买" : "积分购买";
  });
}

function membershipRemainingText() {
  if (!state.membership?.expires_at) return "普通用户";
  const remaining = new Date(state.membership.expires_at).getTime() - Date.now();
  if (!Number.isFinite(remaining) || remaining <= 0) return "普通用户";
  const totalMinutes = Math.max(1, Math.ceil(remaining / 60000));
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  const parts = [];
  if (days) parts.push(`${days} 天`);
  if (hours || days) parts.push(`${hours} 小时`);
  if (!days) parts.push(`${minutes} 分钟`);
  return `${state.membership.name} · 剩余 ${parts.join(" ")}`;
}

function updateMembershipRemaining() {
  const text = membershipRemainingText();
  if (els.sidebarMembershipName && els.sidebarMembershipName.textContent !== text) els.sidebarMembershipName.textContent = text;
}

async function purchaseMembership(button) {
  const name = button.dataset.membershipName || "会员套餐";
  const cost = Number(button.dataset.membershipCost || 0);
  if (!window.confirm(`确认使用 ${cost} 积分购买“${name}”？`)) return;
  setBusy(button, true, "购买中");
  try {
    const data = await apiFetch(`/memberships/${encodeURIComponent(button.dataset.membershipId)}/purchase`, { method: "POST" });
    applyAccessScope({ quota: { ...data.balance, points: Number(data.balance.credit_units || 0) / 10, free_remaining: data.balance.free_remaining }, token_concurrency: data.balance.concurrency, user_name: state.userName });
    await Promise.all([refreshHealth(), loadClientProfile(), loadTransactions(), loadMemberships()]);
    toast(`已购买 ${name}，当前并发 ${state.concurrency}`);
  } catch (error) {
    toast(`购买失败：${error.message}`, "error");
  } finally {
    setBusy(button, false);
  }
}

async function loadTransactions() {
  if (portal !== "client" || !els.transactionTableBody) return;
  const data = await apiFetch("/points/transactions?page=1&page_size=100");
  const labels = { consume: "任务消费", refund: "任务退款", redeem: "卡密兑换", membership_purchase: "会员购买", admin_credit: "管理员充值", admin_deduct: "管理员扣除" };
  els.transactionTableBody.innerHTML = (data.transactions || []).length ? data.transactions.map((item) => {
    const detailLines = String(item.detail || "").split("\n").filter((line) => line && !line.startsWith("任务 ID："));
    const taskReference = item.kind === "consume" && item.reference_id ? `<code class="ledger-task-id">任务 ID：${escapeHtml(item.reference_id)}</code>` : "";
    const detail = detailLines.length ? `<small>${escapeHtml(detailLines.join(" / "))}</small>` : "";
    return `<tr><td>${escapeHtml(formatTime(item.created_at))}</td><td>${escapeHtml(labels[item.kind] || item.title || "积分变动")}</td><td><div class="ledger-description"><strong>${escapeHtml(item.title || "积分变动")}</strong>${taskReference}${detail}</div></td><td><strong class="ledger-amount ${Number(item.amount) >= 0 ? "credit" : "debit"}">${Number(item.amount) >= 0 ? "+" : ""}${escapeHtml(item.amount)}</strong></td><td>${item.balance == null ? "-" : escapeHtml(item.balance)}</td></tr>`;
  }).join("") : '<tr><td colspan="5"><div class="empty-state">暂无积分明细</div></td></tr>';
}

async function loadPointCards() {
  if (portal !== "admin" || !els.pointCardTableBody) return;
  const params = new URLSearchParams({ limit: "2000" });
  const query = els.pointCardSearch?.value.trim() || "";
  const status = els.pointCardStatusFilter?.value || "all";
  if (query) params.set("q", query);
  if (status !== "all") params.set("status", status);
  const data = await apiFetch(`/admin/point-cards?${params}`);
  state.pointCards = Array.isArray(data.cards) ? data.cards : [];
  if (els.pointCardTotalState) els.pointCardTotalState.textContent = `共 ${state.pointCards.length} 条记录`;
  els.pointCardTableBody.innerHTML = state.pointCards.length ? state.pointCards.map((item) => {
    const code = item.code || "";
    const copyButton = `<button type="button" class="icon-button card-copy-button" data-copy-point-card="${escapeHtml(code)}" title="复制完整卡密">复制</button>`;
    return `<tr><td><div class="point-card-code"><code title="${escapeHtml(code)}">${escapeHtml(code)}</code>${copyButton}</div></td><td><span class="card-type-chip">积分卡密</span></td><td>${escapeHtml(item.points)} 积分</td><td><span class="chip ${item.status === "unused" ? "success" : "failed"}">${item.status === "unused" ? "未使用" : "已兑换"}</span></td><td>${escapeHtml(item.redeemed_username || "-")}</td><td>${item.redeemed_at ? escapeHtml(formatTime(item.redeemed_at)) : "-"}</td><td>永久有效</td><td>${escapeHtml(item.note || "-")}</td></tr>`;
  }).join("") : '<tr><td colspan="8"><div class="empty-state">暂无卡密记录</div></td></tr>';
}

function exportPointCardsCsv() {
  const rows = [["兑换码", "类型", "面值", "状态", "使用者", "使用时间", "过期时间", "备注"], ...state.pointCards.map((item) => [item.code, "积分卡密", item.points, item.status === "unused" ? "未使用" : "已兑换", item.redeemed_username || "", item.redeemed_at || "", "永久有效", item.note || ""])];
  const csv = `\ufeff${rows.map((row) => row.map((value) => `"${String(value ?? "").replaceAll('"', '""')}"`).join(",")).join("\r\n")}`;
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  link.download = `point-cards-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  URL.revokeObjectURL(link.href);
}

async function generatePointCards(event) {
  event.preventDefault();
  setBusy(els.generatePointCards, true, "生成中");
  try {
    const data = await apiFetch("/admin/point-cards", { method: "POST", body: { points: Number(els.pointCardPoints.value), count: Number(els.pointCardCount.value), note: els.pointCardNote.value.trim() } });
    const codes = (data.cards || []).map((item) => item.code).join("\n");
    els.generatedPointCardCodes.textContent = codes;
    els.generatedPointCards.classList.toggle("hidden", !codes);
    await loadPointCards();
    toast(`已生成 ${data.count || 0} 张卡密`);
  } catch (error) {
    toast(`生成失败：${error.message}`, "error");
  } finally {
    setBusy(els.generatePointCards, false);
  }
}

async function loadUsers() {
  if (!els.userTableBody || portal !== "admin") return;
  const data = await apiFetch(`/users?page=${state.userPage}&page_size=${state.userPageSize}&q=${encodeURIComponent(state.userSearch)}`);
  els.onlineUserCount.textContent = String(data.online || 0);
  state.userPage = Number(data.page || 1);
  state.userTotalPages = Number(data.total_pages || 1);
  if (els.userPageState) els.userPageState.textContent = `第 ${state.userPage} / ${state.userTotalPages} 页`;
  if (els.userTotalState) els.userTotalState.textContent = `共 ${Number(data.total || 0)} 位用户`;
  if (els.prevUserPage) els.prevUserPage.disabled = state.userPage <= 1;
  if (els.nextUserPage) els.nextUserPage.disabled = state.userPage >= state.userTotalPages;
  const users = Array.isArray(data.users) ? data.users : [];
  els.userTableBody.innerHTML = users.length ? users.map((item) => `<tr><td><strong>${escapeHtml(item.username)}</strong>${item.membership ? `<br><span class="chip success">${escapeHtml(item.membership.name)} 至 ${escapeHtml(formatTime(item.membership.expires_at))}</span>` : ""}<br><small>${escapeHtml(item.email || formatTime(item.last_login_at))}</small></td><td><div class="user-token-cell"><code title="${escapeHtml(item.token)}">${escapeHtml(item.token)}</code><button type="button" class="icon-button" data-copy-user-token="${escapeHtml(item.token)}">复制</button></div></td><td>${escapeHtml(formatTime(item.created_at))}</td><td>${escapeHtml(formatTime(item.last_seen_at))}</td><td>${item.enabled ? (item.online ? '<span class="online-dot"></span>在线' : '离线') : '<span class="chip failed">已停用</span>'}</td><td>视频额度 ${item.free_remaining}<br>积分 ${item.points}<br>并发 ${item.concurrency}</td><td><div class="row-actions user-point-actions"><button class="secondary-button" data-user-points="${escapeHtml(item.id)}">充值</button><button class="secondary-button deduct-button" data-deduct-user-points="${escapeHtml(item.id)}" data-user-points-balance="${Number(item.points || 0)}">扣除</button><button class="icon-button" data-toggle-user="${escapeHtml(item.id)}" data-enabled="${item.enabled}">${item.enabled ? "停用" : "启用"}</button><button class="danger-button" data-delete-user="${escapeHtml(item.id)}" data-user-name="${escapeHtml(item.username)}">删除</button></div></td></tr>`).join("") : '<tr><td colspan="7"><div class="empty-state">未找到匹配用户</div></td></tr>';
}

async function changeClientPassword(event) {
  event.preventDefault();
  const currentPassword = els.currentClientPassword.value;
  const newPassword = els.newClientPassword.value;
  const confirmPassword = els.confirmClientPassword.value;
  if (!currentPassword || !newPassword || !confirmPassword) return toast("请完整填写改密信息", "error");
  if (newPassword !== confirmPassword) return toast("两次输入的新密码不一致", "error");
  setBusy(els.changeClientPasswordButton, true, "修改中");
  if (els.clientPasswordState) els.clientPasswordState.textContent = "正在轮换";
  try {
    const data = await apiFetch("/auth/password", { method: "POST", body: { current_password: currentPassword, new_password: newPassword, confirm_password: confirmPassword } });
    if (!data.token || data.token === state.apiToken) throw new Error("后端未签发新的 Token");
    state.apiToken = data.token;
    localStorage.setItem(portalStorageKey(TOKEN_KEY), data.token);
    if (els.clientTokenDisplay) els.clientTokenDisplay.value = data.token;
    els.clientPasswordForm.reset();
    applyAccessScope(data);
    closeSettingsModal(els.clientPasswordModal);
    await refreshTasks({ quiet: true, keepPage: true });
    if (els.clientPasswordState) els.clientPasswordState.textContent = "已更新";
    toast("密码已修改，Token 已安全轮换");
  } catch (error) {
    if (els.clientPasswordState) els.clientPasswordState.textContent = "修改失败";
    toast(`修改失败：${error.message}`, "error");
  } finally {
    setBusy(els.changeClientPasswordButton, false);
  }
}

async function loadEmailDomains() {
  const data = await requestJson("/auth/register/email-domains", "");
  state.registrationEmailVerificationEnabled = data.enabled !== false;
  const domains = Array.isArray(data.domains) ? data.domains : [];
  const options = domains.map((domain) => `<option value="${escapeHtml(domain)}">${escapeHtml(domain)}</option>`).join("");
  if (els.clientEmailDomain) els.clientEmailDomain.innerHTML = options;
  if (els.changeEmailDomain) els.changeEmailDomain.innerHTML = options;
}

async function loadClientProfile() {
  if (portal !== "client" || !state.apiToken) return;
  const data = await apiFetch("/auth/profile");
  if (els.clientEmailDisplay) els.clientEmailDisplay.textContent = data.email || "未绑定";
  if (els.clientEmailState) els.clientEmailState.textContent = data.email_verified_at ? "已验证" : "未验证";
  state.membership = data.membership || null;
  if (els.membershipCurrentState) els.membershipCurrentState.textContent = data.membership ? `${membershipRemainingText()} · ${formatTime(data.membership.expires_at)} 到期` : "当前无会员";
  updateMembershipRemaining();
  updateMembershipPurchaseButtons();
}

function selectedEmail(localInput, domainSelect) {
  return `${localInput?.value.trim() || ""}${domainSelect?.value || ""}`;
}

async function changeClientEmail(event) {
  event.preventDefault();
  const email = selectedEmail(els.changeEmailLocal, els.changeEmailDomain);
  if (!els.changeEmailLocal.value.trim() || !els.changeEmailCode.value.trim()) return toast("请填写新邮箱和验证码", "error");
  setBusy(els.changeClientEmailButton, true, "修改中");
  try {
    await apiFetch("/auth/email", { method: "PATCH", body: { email, email_code: els.changeEmailCode.value.trim() } });
    els.clientEmailForm.reset();
    await loadClientProfile();
    closeSettingsModal(els.clientEmailModal);
    toast("绑定邮箱已更新");
  } catch (error) {
    toast(`修改失败：${error.message}`, "error");
  } finally {
    setBusy(els.changeClientEmailButton, false);
  }
}

function renderAdminPointPackages() {
  if (!els.packageList || portal !== "admin") return;
  const enabledCount = state.pointPackages.filter((item) => item.enabled).length;
  if (els.packageConfigDisplay) els.packageConfigDisplay.textContent = `${enabledCount} 个已上架 / ${state.pointPackages.length} 个套餐`;
  if (els.packageConfigState) els.packageConfigState.textContent = "已读取";
  els.packageList.innerHTML = state.pointPackages.length ? state.pointPackages.map((item) => `<article class="package-item" data-package-id="${escapeHtml(item.id)}"><div class="package-item-heading"><div><strong>${escapeHtml(item.name)}</strong><span class="chip ${item.enabled ? "success" : "failed"}">${item.enabled ? "已上架" : "已停用"}</span></div><span>${escapeHtml(item.points)} 积分</span></div><div class="package-item-fields"><label class="field"><span>名称</span><input data-package-name value="${escapeHtml(item.name)}" maxlength="80"></label><label class="field"><span>积分</span><input data-package-points type="number" min="0.1" step="0.1" value="${escapeHtml(item.points)}"></label><label class="field"><span>赠送次数</span><input data-package-bonus type="number" min="0" step="1" value="${escapeHtml(item.bonus_free_uses)}"></label><label class="field"><span>排序</span><input data-package-sort type="number" step="1" value="${escapeHtml(item.sort_order)}"></label><label class="field field-wide"><span>购买跳转链接</span><input data-package-url type="url" value="${escapeHtml(item.payment_url || "https://pay.ldxp.cn/shop/huisu/fhm9gj")}"></label></div><div class="package-item-actions"><button class="secondary-button" type="button" data-action="save-package">保存调整</button><button class="${item.enabled ? "danger-button" : "primary-button"}" type="button" data-action="toggle-package">${item.enabled ? "停用" : "重新上架"}</button></div></article>`).join("") : '<div class="empty-state">暂无积分套餐</div>';
}

function addEmailDomainRow(value = "") {
  if (!els.registrationEmailDomains) return;
  const row = document.createElement("div");
  row.className = "email-domain-item";
  const domain = String(value || "").trim().toLowerCase().replace(/^@+/, "");
  row.innerHTML = `<input data-email-domain placeholder="qq.com" value="${escapeHtml(domain)}"><button class="danger-button" data-remove-email-domain type="button">删除</button>`;
  els.registrationEmailDomains.appendChild(row);
}

function configuredEmailDomains() {
  return [...new Set(Array.from(els.registrationEmailDomains?.querySelectorAll("[data-email-domain]") || []).map((input) => input.value.trim().toLowerCase().replace(/^@+/, "")).filter(Boolean))];
}

async function loadAdminPointPackages() {
  if (portal !== "admin") return;
  const data = await apiFetch("/admin/points/packages");
  state.pointPackages = Array.isArray(data.packages) ? data.packages : [];
  renderAdminPointPackages();
}

async function createPointPackage(event) {
  event.preventDefault();
  setBusy(els.createPackageButton, true, "新增中");
  try {
    await apiFetch("/admin/points/packages", { method: "POST", body: { name: els.packageName.value.trim(), points: Number(els.packagePoints.value), bonus_free_uses: Number(els.packageBonus.value), sort_order: Number(els.packageSortOrder.value), payment_url: els.packagePaymentUrl.value.trim() } });
    els.packageForm.reset();
    els.packageBonus.value = "0";
    els.packageSortOrder.value = "0";
    els.packagePaymentUrl.value = "https://pay.ldxp.cn/shop/huisu/fhm9gj";
    await loadAdminPointPackages();
    toast("套餐已新增并上架");
  } catch (error) {
    toast(`新增失败：${error.message}`, "error");
  } finally {
    setBusy(els.createPackageButton, false);
  }
}

function renderAdminMemberships() {
  if (!els.membershipAdminList || portal !== "admin") return;
  const enabledCount = state.memberships.filter((item) => item.enabled).length;
  if (els.membershipConfigDisplay) els.membershipConfigDisplay.textContent = `${enabledCount} 个启用 / ${state.memberships.length} 个套餐`;
  if (els.membershipConfigState) els.membershipConfigState.textContent = "已读取";
  els.membershipAdminList.innerHTML = state.memberships.length ? state.memberships.map((item) => `<article class="package-item" data-membership-id="${escapeHtml(item.id)}"><div class="package-item-heading"><div><strong>${escapeHtml(item.name)}</strong><span class="chip ${item.enabled ? "success" : "failed"}">${item.enabled ? "已启用" : "已停用"}</span></div><span>${escapeHtml(item.points_cost)} 积分</span></div><div class="package-item-fields membership-item-fields"><label class="field"><span>名称</span><input data-membership-name value="${escapeHtml(item.name)}" maxlength="80"></label><label class="field"><span>所需积分</span><input data-membership-points type="number" min="0.1" step="0.1" value="${escapeHtml(item.points_cost)}"></label><label class="field"><span>天数</span><input data-membership-duration type="number" min="1" max="3650" value="${escapeHtml(item.duration_days)}"></label><label class="field"><span>并发增量</span><input data-membership-concurrency type="number" min="1" max="100" value="${escapeHtml(item.concurrency)}"></label><label class="field"><span>赠送额度</span><input data-membership-bonus type="number" min="0" value="${escapeHtml(item.bonus_free_uses)}"></label><label class="field"><span>排序</span><input data-membership-sort type="number" value="${escapeHtml(item.sort_order)}"></label><label class="field field-wide"><span>说明</span><input data-membership-description value="${escapeHtml(item.description || "")}" maxlength="500"></label></div><div class="package-item-actions"><button class="secondary-button" type="button" data-action="save-membership">保存</button><button class="${item.enabled ? "danger-button" : "primary-button"}" type="button" data-action="toggle-membership">${item.enabled ? "停用" : "启用"}</button></div></article>`).join("") : '<div class="empty-state">暂无会员套餐</div>';
}

async function loadAdminMemberships() {
  if (portal !== "admin") return;
  const data = await apiFetch("/admin/memberships");
  state.memberships = Array.isArray(data.packages) ? data.packages : [];
  renderAdminMemberships();
}

async function createMembership(event) {
  event.preventDefault();
  setBusy(els.createMembershipButton, true, "新增中");
  try {
    await apiFetch("/admin/memberships", { method: "POST", body: { name: els.membershipName.value.trim(), points_cost: Number(els.membershipPrice.value), duration_days: Number(els.membershipDuration.value), concurrency: Number(els.membershipConcurrency.value), bonus_free_uses: Number(els.membershipBonus.value), sort_order: Number(els.membershipSortOrder.value), description: els.membershipDescription.value.trim() } });
    els.membershipForm.reset();
    els.membershipDuration.value = "30";
    els.membershipConcurrency.value = "2";
    els.membershipBonus.value = "0";
    els.membershipSortOrder.value = "0";
    await loadAdminMemberships();
    toast("会员套餐已新增");
  } catch (error) {
    toast(`新增失败：${error.message}`, "error");
  } finally {
    setBusy(els.createMembershipButton, false);
  }
}

function updateDashboardMetrics() {
  let pending = 0;
  let running = 0;
  let finished = 0;
  let todayDone = 0;

  state.tasks.forEach((task) => {
    const status = getTaskStatus(task);
    if (status.state === "running") {
      running += 1;
      return;
    }
    if (status.state === "success") {
      finished += 1;
      if (task.completed_today === true) todayDone += 1;
      return;
    }
    if (status.state === "pending") {
      pending += 1;
      return;
    }
    if (status.state === "failed") {
      finished += 1;
    }
  });

  const stats = state.taskStats;
  els.metricTaskTotal.textContent = String(stats?.total ?? state.taskTotal ?? state.tasks.length);
  els.metricPending.textContent = String(stats?.pending ?? pending);
  els.metricRunning.textContent = String(stats?.running ?? running);
  els.metricFinished.textContent = String(stats ? Number(stats.success || 0) + Number(stats.failed || 0) : finished);
  if (els.taskRunningCount) els.taskRunningCount.textContent = String(stats?.running ?? running);
  if (els.taskTodayDoneCount) els.taskTodayDoneCount.textContent = String(stats?.completed_today ?? todayDone);
}

async function refreshHealth() {
  const data = await apiFetch("/health");
  state.activeIds = Array.isArray(data.active) ? data.active : [];
  els.metricWorkers.textContent = String(data.browser_workers ?? "-");
  applyAccessScope(data);
  setServiceState(true);
  updateDashboardMetrics();
  return data;
}

async function loadProxyConfig() {
  if (portal === "client" || !els.proxyApiUrl) return null;
  const data = await apiFetch("/config/proxy-api");
  els.proxyApiUrl.value = data.proxy_api_url || "";
  if (els.proxySubscriptionUrl) els.proxySubscriptionUrl.value = "";
  if (els.proxySource) els.proxySource.value = data.proxy_subscription_configured ? "subscription" : data.proxy_api_url ? "api" : "direct";
  if (els.proxySubscriptionHint) els.proxySubscriptionHint.textContent = data.proxy_subscription_configured ? "订阅已安全保存，留空保持不变；输入新链接可替换" : "支持 VLESS、VMess、Trojan、Hysteria2、SS、TUIC 及 Clash/Mihomo 订阅";
  updateProxySourceFields();
  if (els.proxyApiDisplay) els.proxyApiDisplay.textContent = data.proxy_subscription_configured ? "节点订阅" : data.proxy_api_url ? "代理提取 API" : "直连";
  state.proxyEnabled = data.proxy_enabled !== false;
  state.proxyAutoSelect = data.proxy_auto_select !== false;
  state.proxySelectedNode = data.proxy_selected_node || "";
  if (els.proxyEnabledSelect) els.proxyEnabledSelect.value = String(state.proxyEnabled);
  if (els.proxyAutoSelect) els.proxyAutoSelect.value = String(state.proxyAutoSelect);
  if (els.configState) els.configState.textContent = "已读取";
  return data;
}

function renderProxyNodes() {
  if (!els.proxyNodeGrid) return;
  const countries = [...new Set(state.proxyNodes.map((node) => node.country || "未知"))].sort((left, right) => left.localeCompare(right, "zh-CN"));
  if (els.proxyCountryFilter) {
    const current = countries.includes(state.proxyCountry) ? state.proxyCountry : "all";
    els.proxyCountryFilter.innerHTML = ['<option value="all">全部地区</option>', ...countries.map((country) => `<option value="${escapeHtml(country)}">${escapeHtml(country)}</option>`)].join("");
    els.proxyCountryFilter.value = current;
    state.proxyCountry = current;
  }
  const visibleNodes = state.proxyCountry === "all" ? state.proxyNodes : state.proxyNodes.filter((node) => node.country === state.proxyCountry);
  if (els.proxyNodeCount) els.proxyNodeCount.textContent = `${visibleNodes.length} / ${state.proxyNodes.length} 个节点`;
  if (!state.proxyNodes.length) {
    els.proxyNodeGrid.innerHTML = '<div class="empty-state">暂无可用节点，请配置订阅后刷新</div>';
    return;
  }
  if (!visibleNodes.length) {
    els.proxyNodeGrid.innerHTML = '<div class="empty-state">当前地区暂无节点</div>';
    return;
  }
  els.proxyNodeGrid.innerHTML = visibleNodes.map((node) => {
    const latency = node.latency_status === "available" ? `${node.latency_ms} ms` : node.latency_status === "unavailable" ? "不可用" : node.latency_status === "expired" ? "已过期" : "未检测";
    const selected = node.selected || node.id === state.proxySelectedNode;
    return `<button class="proxy-node-card${selected ? " selected" : ""}" type="button" data-proxy-node-id="${escapeHtml(node.id)}"><span class="proxy-node-name">${escapeHtml(node.name)}</span><span class="proxy-node-country">${escapeHtml(node.country)} · ${escapeHtml(node.protocol.toUpperCase())}</span><strong class="proxy-node-latency${node.latency_ms ? " good" : ""}">${escapeHtml(latency)}</strong></button>`;
  }).join("");
}

function proxySubscriptionError(error) {
  const message = String(error?.message || "未知错误");
  if (message.includes("redirect")) return `订阅重定向失败：${message}`;
  if (message.includes("timed out") || message.includes("network error")) return `订阅网络错误：${message}`;
  if (message.includes("HTTP")) return `订阅服务错误：${message}`;
  if (message.includes("too large")) return `订阅内容过大：${message}`;
  if (message.includes("empty") || message.includes("no usable nodes")) return `订阅格式错误：${message}`;
  return `节点读取失败：${message}`;
}

async function loadProxyNodes(refresh = false) {
  if (portal !== "admin") return;
  setBusy(els.refreshProxyNodes, true, "刷新中");
  try {
    let data = await apiFetch(`/config/proxy-nodes${refresh ? "?refresh=true" : ""}`);
    if (refresh && Array.isArray(data.nodes) && data.nodes.length) {
      data = { ...data, ...(await apiFetch("/config/proxy-nodes/latency", { method: "POST" })) };
    }
    state.proxyNodes = Array.isArray(data.nodes) ? data.nodes : [];
    state.proxyEnabled = Boolean(data.enabled);
    state.proxyAutoSelect = Boolean(data.auto_select);
    state.proxySelectedNode = data.selected_node || "";
    if (els.proxyEnabledSelect) els.proxyEnabledSelect.value = String(state.proxyEnabled);
    if (els.proxyAutoSelect) els.proxyAutoSelect.value = String(state.proxyAutoSelect);
    renderProxyNodes();
    if (els.proxyNodesState) els.proxyNodesState.textContent = refresh ? "延迟已更新" : "已读取";
  } catch (error) {
    if (els.proxyNodesState) els.proxyNodesState.textContent = "读取失败";
    if (refresh || error.message !== "proxy subscription is not configured") toast(proxySubscriptionError(error), "error");
    renderProxyNodes();
  } finally {
    setBusy(els.refreshProxyNodes, false);
  }
}

async function saveProxyMode() {
  try {
    const data = await apiFetch("/config/proxy-api", { method: "POST", body: { proxy_enabled: els.proxyEnabledSelect.value === "true", proxy_auto_select: els.proxyAutoSelect.value === "true" } });
    state.proxyEnabled = Boolean(data.proxy_enabled);
    state.proxyAutoSelect = Boolean(data.proxy_auto_select);
    toast(state.proxyEnabled ? (state.proxyAutoSelect ? "已开启自动节点代理" : "已开启手动节点代理") : "已关闭节点代理");
  } catch (error) {
    toast(`代理模式保存失败：${error.message}`, "error");
    await loadProxyNodes();
  }
}

async function selectProxyNode(nodeId) {
  try {
    const data = await apiFetch("/config/proxy-nodes/select", { method: "POST", body: { node_id: nodeId } });
    state.proxySelectedNode = data.selected_node || nodeId;
    state.proxyAutoSelect = false;
    if (els.proxyAutoSelect) els.proxyAutoSelect.value = "false";
    renderProxyNodes();
    toast(`已选择节点：${data.node?.name || "节点"}`);
  } catch (error) {
    toast(`节点选择失败：${error.message}`, "error");
  }
}

function updateProxySourceFields() {
  const source = els.proxySource?.value || "direct";
  els.proxySubscriptionField?.classList.toggle("hidden", source !== "subscription");
  els.proxyApiField?.classList.toggle("hidden", source !== "api");
}

async function saveProxyConfig() {
  if (portal === "client") return;
  const source = els.proxySource?.value || "direct";
  const apiUrl = els.proxyApiUrl?.value.trim() || "";
  const subscriptionUrl = els.proxySubscriptionUrl?.value.trim() || "";
  if (source === "api" && !apiUrl) {
    toast("请输入代理提取 API", "error");
    return;
  }
  if (source === "subscription" && !subscriptionUrl && els.proxySubscriptionHint?.textContent.startsWith("支持")) {
    toast("请输入节点订阅链接", "error");
    return;
  }
  setBusy(els.saveProxyConfig, true, "保存中");
  try {
    const body = source === "subscription"
      ? { proxy_api_url: "", proxy_subscription_scheme: "http", proxy_subscription_refresh_seconds: 900 }
      : source === "api"
        ? { proxy_api_url: apiUrl, proxy_subscription_url: "", proxy_api_scheme: "http" }
        : { proxy_api_url: "", proxy_subscription_url: "" };
    if (source === "subscription" && subscriptionUrl) body.proxy_subscription_url = subscriptionUrl;
    await apiFetch("/config/proxy-api", {
      method: "POST",
      body,
    });
    if (els.configState) els.configState.textContent = "已保存";
    if (els.proxyApiDisplay) els.proxyApiDisplay.textContent = source === "subscription" ? "节点订阅" : source === "api" ? "代理提取 API" : "直连";
    toast(source === "direct" ? "已切换为直连运行" : "代理配置已更新");
    closeSettingsModal(els.proxyModal);
    await loadProxyConfig();
    await loadProxyNodes(source === "subscription");
  } catch (error) {
    toast(source === "subscription" ? proxySubscriptionError(error) : `保存失败：${error.message}`, "error");
  } finally {
    setBusy(els.saveProxyConfig, false);
  }
}

async function loadEmailConfig() {
  if (portal === "client" || !els.registrationEmailDomains) return null;
  const data = await apiFetch("/config/registration-email");
  els.registrationEmailEnabled.value = data.enabled ? "true" : "false";
  els.registrationEmailDomains.innerHTML = "";
  (data.domains || []).forEach((domain) => addEmailDomainRow(domain));
  if (!els.registrationEmailDomains.children.length) addEmailDomainRow();
  els.registrationSmtpUsername.value = data.smtp_username || "";
  els.registrationSmtpAuthorizationCode.value = "";
  els.registrationSmtpAuthorizationCode.placeholder = data.authorization_code_configured ? "已安全保存，留空保持不变" : "请输入 QQ 邮箱 SMTP 授权码";
  els.registrationEmailSenderName.value = data.sender_name || "视频生成服务";
  els.registrationEmailCodeTtl.value = String(data.code_ttl_minutes || 10);
  els.emailConfigDisplay.textContent = (data.domains || []).join("、") || "未配置";
  els.emailConfigState.textContent = data.enabled ? (data.authorization_code_configured ? "已启用" : "缺少授权码") : "已停用";
  return data;
}

async function saveEmailConfig() {
  setBusy(els.saveEmailConfig, true, "保存中");
  try {
    const enabled = els.registrationEmailEnabled.value === "true";
    await apiFetch("/config/registration-email", { method: "POST", body: { enabled, domains: configuredEmailDomains(), smtp_host: "smtp.qq.com", smtp_port: 465, smtp_username: els.registrationSmtpUsername.value.trim(), authorization_code: els.registrationSmtpAuthorizationCode.value.trim(), sender_name: els.registrationEmailSenderName.value.trim(), code_ttl_minutes: Number(els.registrationEmailCodeTtl.value) } });
    await loadEmailConfig();
    closeSettingsModal(els.emailModal);
    toast("注册邮箱验证配置已保存");
  } catch (error) {
    toast(`保存失败：${error.message}`, "error");
  } finally {
    setBusy(els.saveEmailConfig, false);
  }
}

async function refreshAccounts(options = {}) {
  if (portal === "client") return;
  const requestId = ++state.accountRefreshRequestId;
  if (!options.quiet) setBusy(els.refreshAccounts, true, "刷新中");
  try {
    const params = new URLSearchParams({ page: String(state.accountPage), page_size: String(state.accountPageSize) });
    const keyword = (els.accountTaskSearch?.value || "").trim();
    if (keyword) params.set("q", keyword);
    if (state.accountPlatformFilter !== "all") params.set("platform", state.accountPlatformFilter);
    const data = await apiFetch(`/accounts?${params}`);
    if (requestId !== state.accountRefreshRequestId) return;
    state.accounts = Array.isArray(data.accounts) ? data.accounts : [];
    state.accountTotal = Number(data.total || 0);
    state.accountTotalPages = Math.max(1, Number(data.total_pages || 1));
    state.accountPage = Math.max(1, Number(data.page || state.accountPage));
    state.accountStats = data.stats || null;
    state.accountQuotaSummary = data.quota_summary || null;
    state.nextQuotaResetAt = String(data.next_quota_reset_at || "");
    renderAccountTable();
    renderAccountQuotaSummary();
    updateAccountResetCountdown();
    if (!options.quiet) toast(`已加载 ${state.accountTotal} 个账号`);
  } catch (error) {
    if (!options.quiet) toast(`账号列表读取失败：${error.message}`, "error");
    throw error;
  } finally {
    if (!options.quiet && requestId === state.accountRefreshRequestId) setBusy(els.refreshAccounts, false);
  }
}

function renderAccountQuotaSummary() {
  if (!els.accountQuotaSummary) return;
  const summary = state.accountQuotaSummary || {};
  const total = Number(summary.total_limit || 0);
  const used = Number(summary.total_used || 0);
  const remaining = Number(summary.total_remaining || 0);
  const unlimited = Number(summary.unlimited_count || 0);
  els.accountQuotaSummary.innerHTML = `
    <span>总额度 <strong>${escapeHtml(total || "不限")}</strong></span>
    <span>已用 <strong>${escapeHtml(used)}</strong></span>
    <span>剩余 <strong>${escapeHtml(total ? remaining : "不限")}</strong></span>
    ${unlimited ? `<span>不限账号 <strong>${escapeHtml(unlimited)}</strong></span>` : ""}
  `;
}

function formatDuration(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = String(Math.floor(total / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const seconds = String(total % 60).padStart(2, "0");
  return `${hours}:${minutes}:${seconds}`;
}

function updateAccountResetCountdown() {
  if (!els.accountResetCountdown) return;
  if (!state.nextQuotaResetAt) {
    els.accountResetCountdown.textContent = "清零倒计时 --:--:--";
    return;
  }
  const target = new Date(state.nextQuotaResetAt).getTime();
  const left = target - Date.now();
  els.accountResetCountdown.textContent = `清零倒计时 ${formatDuration(left)}`;
  if (left <= 0) refreshAccounts({ quiet: true });
}

function countCookieAccounts(raw) {
  const text = String(raw || "").trim();
  if (!text) return 0;
  try {
    const data = JSON.parse(text);
    if (Array.isArray(data)) return data.length;
    if (data && Array.isArray(data.cookies)) return 1;
    if (data && (data.cookie_data || data.cookie || data.name || data.value)) return 1;
  } catch (_) {
  }
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  return Math.max(1, lines.length);
}

function updateAccountDetectedCount() {
  if (!els.accountDetectedCount) return;
  const count = countCookieAccounts(els.accountCookieData?.value || "");
  els.accountDetectedCount.textContent = `已识别 ${count} 个账号`;
  els.accountDetectedCount.classList.toggle("active", count > 0);
}

async function importAccount(event) {
  event.preventDefault();
  if (portal === "client") return;
  const name = els.accountName.value.trim();
  const platform = els.accountPlatform?.value || "dola";
  const defaultQuota = platform === "dola" ? 1 : platform === "qianwen" ? 5 : 2;
  const quotaLimit = Number.parseInt(els.accountQuotaLimit?.value || String(defaultQuota), 10) || defaultQuota;
  const cookieData = els.accountCookieData.value.trim();
  if (!cookieData) {
    toast("Cookie 数据不能为空", "error");
    return;
  }
  setBusy(els.importAccount, true, "导入中");
  els.accountImportState.textContent = "导入中";
  try {
    const isBulk = cookieData.split(/\r?\n/).filter((line) => line.trim()).length > 1;
    const data = await apiFetch("/accounts", {
      method: "POST",
      body: { name, cookie_data: cookieData, quota_limit: quotaLimit, bulk: isBulk, platform },
    });
    els.accountName.value = "";
    if (els.accountQuotaLimit) els.accountQuotaLimit.value = platform === "dola" ? "1" : platform === "qianwen" ? "5" : "2";
    els.accountCookieData.value = "";
    updateAccountDetectedCount();
    const skipped = Number(data.skipped || 0);
    els.accountImportState.textContent = skipped ? `已导入，跳过重复 ${skipped} 个` : "已导入";
    toast(isBulk ? `新增 ${data.created || 0} 个账号${skipped ? `，跳过重复 ${skipped} 个` : ""}` : "账号已导入");
    await refreshAccounts({ quiet: true });
  } catch (error) {
    els.accountImportState.textContent = "导入失败";
    toast(`导入失败：${error.message}`, "error");
  } finally {
    setBusy(els.importAccount, false);
  }
}

function openWorkersModal() {
  const current = Number.parseInt(els.metricWorkers.textContent, 10);
  els.workersInput.value = Number.isFinite(current) ? String(current) : "1";
  els.workersModalState.textContent = "";
  els.workersModal.classList.remove("hidden");
  els.workersModal.setAttribute("aria-hidden", "false");
  els.workersInput.focus();
  els.workersInput.select();
}

function closeWorkersModal() {
  els.workersModal.classList.add("hidden");
  els.workersModal.setAttribute("aria-hidden", "true");
}

function closeSettingsModal(modal) {
  modal?.classList.add("hidden");
  modal?.setAttribute("aria-hidden", "true");
}

function openTextModal(text) {
  const value = String(text || "");
  if (!value || value === "-") return;
  state.modalText = value;
  els.textModalContent.textContent = value;
  els.textModal.classList.remove("hidden");
  els.textModal.setAttribute("aria-hidden", "false");
}

function closeTextModal() {
  state.modalText = "";
  els.textModal.classList.add("hidden");
  els.textModal.setAttribute("aria-hidden", "true");
}

function openVideoModal(url) {
  const value = String(url || "").trim();
  if (!value) return;
  state.modalVideoUrl = value;
  els.videoLoading.textContent = "正在加载视频链接...";
  els.videoLoading.classList.remove("hidden");
  els.videoPlayer.classList.add("hidden");
  els.videoPlayer.removeAttribute("src");
  els.videoPlayer.load();
  els.videoModal.classList.remove("hidden");
  els.videoModal.setAttribute("aria-hidden", "false");
  window.setTimeout(() => {
    if (state.modalVideoUrl !== value || els.videoModal.classList.contains("hidden")) return;
    els.videoPlayer.src = value;
    els.videoPlayer.classList.remove("hidden");
    els.videoLoading.classList.add("hidden");
    els.videoPlayer.load();
  }, 120);
}

function closeVideoModal() {
  state.modalVideoUrl = "";
  els.videoPlayer.pause();
  els.videoPlayer.removeAttribute("src");
  els.videoPlayer.load();
  els.videoModal.classList.add("hidden");
  els.videoModal.setAttribute("aria-hidden", "true");
}

async function saveWorkersConfig() {
  const workers = Number.parseInt(els.workersInput.value, 10);
  if (!Number.isInteger(workers) || workers < 1 || workers > 100) {
    els.workersModalState.textContent = "请输入 1 - 100";
    toast("并发数量范围是 1 - 100", "error");
    return;
  }
  setBusy(els.saveWorkers, true, "保存中");
  try {
    const data = await apiFetch("/config/workers", {
      method: "POST",
      body: { browser_workers: workers },
    });
    els.metricWorkers.textContent = String(data.browser_workers ?? workers);
    els.workersModalState.textContent = "已保存";
    toast("并发配置已更新");
    closeWorkersModal();
    await refreshHealth();
  } catch (error) {
    els.workersModalState.textContent = "保存失败";
    toast(`保存失败：${error.message}`, "error");
  } finally {
    setBusy(els.saveWorkers, false);
  }
}

async function loadPlatforms() {
  const data = await apiFetch("/config/platforms");
  state.platforms = Array.isArray(data.platforms) ? data.platforms : [];
  state.platform = String(data.default_platform || "dola");
  renderPlatformControls();
  renderModelConfig();
}

function renderModelConfig() {
  if (!els.modelConfigList || portal !== "admin") return;
  els.modelConfigList.innerHTML = state.platforms.map((platform) => {
    const models = Array.isArray(platform.all_models) ? platform.all_models : (platform.models || []).map((name) => ({ name, enabled: true, cost: platform.model_costs?.[name] ?? 1 }));
    const rows = models.map((item) => `<label class="model-config-row"><input type="checkbox" data-model-enabled ${item.enabled !== false ? "checked" : ""}><input type="text" data-model-name value="${escapeHtml(item.name || "")}"><span class="model-cost-field"><input type="number" data-model-cost min="0.1" step="0.1" value="${escapeHtml(item.cost ?? platform.model_costs?.[item.name] ?? 1)}"><em>积分/次</em></span><button class="secondary-button" type="button" data-remove-model>删除</button></label>`).join("");
    return `<section class="model-config-platform" data-model-platform="${escapeHtml(platform.id)}"><div class="model-config-heading"><strong>${escapeHtml(platform.label || platform.id)}</strong><span>${models.length} 个模型</span><button class="secondary-button" type="button" data-add-model>添加</button></div><div class="model-config-rows" data-model-rows>${rows}</div></section>`;
  }).join("");
  if (els.modelConfigDisplay) {
    const enabled = state.platforms.reduce((count, platform) => count + (platform.all_models || platform.models || []).filter((item) => typeof item === "string" || item.enabled !== false).length, 0);
    els.modelConfigDisplay.textContent = `${enabled} 个模型`;
  }
  if (els.modelConfigState) els.modelConfigState.textContent = "已读取";
}

async function saveModelConfig() {
  const platforms = Array.from(els.modelConfigList.querySelectorAll("[data-model-platform]")).map((section) => ({
    id: section.dataset.modelPlatform,
    models: Array.from(section.querySelectorAll(".model-config-row")).map((row) => ({
      name: row.querySelector("[data-model-name]").value.trim(),
      enabled: row.querySelector("[data-model-enabled]").checked,
      cost: Number(row.querySelector("[data-model-cost]").value),
    })).filter((item) => item.name),
  }));
  if (platforms.some((platform) => platform.models.some((model) => !Number.isFinite(model.cost) || model.cost <= 0 || !Number.isInteger(model.cost * 10)))) {
    toast("模型积分必须为正数且精确到 0.1", "error");
    return;
  }
  setBusy(els.saveModelConfig, true, "保存中");
  try {
    const data = await apiFetch("/config/platforms", { method: "POST", body: { default_platform: state.platform, platforms } });
    state.platforms = data.platforms || [];
    renderPlatformControls();
    renderModelConfig();
    toast("模型配置已更新");
    closeSettingsModal(els.modelModal);
  } catch (error) {
    toast(`保存失败：${error.message}`, "error");
  } finally {
    setBusy(els.saveModelConfig, false);
  }
}

async function syncQianwenModels() {
  setBusy(els.syncQianwenModels, true, "获取中");
  if (els.modelConfigState) els.modelConfigState.textContent = "正在读取三平台网页";
  try {
    const results = [];
    const failures = [];
    for (const platform of ["dola", "doubao", "qianwen"]) {
      try {
        results.push(await apiFetch(`/config/platforms/${platform}/sync`, { method: "POST", timeout: 120000 }));
      } catch (error) {
        failures.push(`${platform}: ${error.message}`);
      }
    }
    if (!results.length) throw new Error(failures.join("；"));
    const data = results[results.length - 1];
    state.platforms = data.platforms || [];
    renderPlatformControls();
    renderModelConfig();
    if (els.modelConfigState) els.modelConfigState.textContent = failures.length ? `已同步 ${results.length} 个平台，${failures.length} 个失败` : "三平台同步成功";
    toast(failures.length ? `模型同步完成：成功 ${results.length} 个，失败 ${failures.length} 个` : "三平台模型同步成功");
  } catch (error) {
    if (els.modelConfigState) els.modelConfigState.textContent = "获取失败";
    toast(`获取失败：${error.message}`, "error");
  } finally {
    setBusy(els.syncQianwenModels, false);
  }
}

function renderPlatformControls() {
  if (!els.platformSelect || !els.modelSelect) return;
  const platforms = state.platforms.length ? state.platforms : [{ id: "dola", label: "Dola", models: ["Seedance 2.0"], enabled: true }];
  els.platformSelect.innerHTML = platforms.map((item) => {
    const id = String(item.id || "dola");
    const label = String(item.label || PLATFORM_LABELS[id] || id);
    const disabled = item.enabled === false ? " disabled" : "";
    const selected = id === state.platform ? " selected" : "";
    return `<option value="${escapeHtml(id)}"${selected}${disabled}>${escapeHtml(disabled ? `${label}（待接入）` : label)}</option>`;
  }).join("");
  const choices = platforms.flatMap((item) => (item.enabled === false ? [] : (item.models || []).map((model) => ({ platform: String(item.id), model: String(model) }))));
  const selected = choices.find((item) => item.model === state.model && item.platform === state.platform) || choices[0] || { platform: "dola", model: "Seedance 2.0" };
  state.platform = selected.platform;
  state.model = selected.model;
  els.platformSelect.value = state.platform;
  els.modelSelect.innerHTML = choices.map((item) => `<option value="${escapeHtml(`${item.platform}::${item.model}`)}"${item.platform === state.platform && item.model === state.model ? " selected" : ""}>${escapeHtml(item.model)}</option>`).join("");
}

async function refreshDashboard() {
  try {
    await refreshHealth();
    const jobs = [refreshTasks({ quiet: true }), loadPlatforms()];
    if (portal === "admin") jobs.push(loadProxyConfig(), refreshAccounts({ quiet: true }));
    if (portal === "client") jobs.push(loadClientNotifications(), loadClientProfile());
    const results = await Promise.allSettled(jobs);
    const rejected = results.find((item) => item.status === "rejected");
    if (rejected) throw rejected.reason;
  } catch (error) {
    setServiceState(false, error.message);
    if (error.status === 401 || error.status === 403) {
      expireSession();
    }
    toast(`刷新失败：${error.message}`, "error");
  } finally {
    updateDashboardMetrics();
  }
}

async function refreshTasks(options = {}) {
  const requestId = ++state.taskRefreshRequestId;
  if (!options.quiet) setBusy(els.refreshTasks, true, "刷新中");
  try {
    const params = new URLSearchParams({ page: String(state.page), page_size: String(state.pageSize) });
    const keyword = els.taskSearch.value.trim();
    if (keyword) params.set("q", keyword);
    const data = await apiFetch(`/tasks?${params}`);
    if (requestId !== state.taskRefreshRequestId) return;
    const tasks = Array.isArray(data.tasks) ? data.tasks : [];
    state.tasks = tasks;
    state.taskTotal = Number(data.total || 0);
    state.taskTotalPages = Math.max(1, Number(data.total_pages || 1));
    state.page = Math.max(1, Number(data.page || state.page));
    state.taskStats = data.stats || null;
    renderTaskTable({ skipUnchanged: true });
    updateDashboardMetrics();
    if (!options.quiet) toast(`已载入 ${state.taskTotal} 条任务`);
  } catch (error) {
    if (!options.quiet) toast(`任务列表读取失败：${error.message}`, "error");
    throw error;
  } finally {
    if (!options.quiet && requestId === state.taskRefreshRequestId) setBusy(els.refreshTasks, false);
  }
}

function getTaskStatus(task) {
  const rawStatus = String(task.status || "").toLowerCase();
  const result = state.results[task.id];
  const resultUrl = String(result?.url ?? "");
  const resultCode = String(result?.code ?? "");
  const resultText = String(result?.text ?? "");
  const text = clientSafeText(resultText, task);
  if (resultCode === "2" || resultUrl) return { state: "success", label: "成功", className: "success", text, url: resultUrl };
  if (rawStatus === "success") return { state: "success", label: "成功", className: "success", text: text || clientSafeText(task.error || "", task), url: resultUrl };
  if (rawStatus === "failed") return { state: "failed", label: "失败", className: "failed", text: clientSafeText(task.error || resultText || "失败", task), url: "" };
  if (rawStatus === "canceled") return { state: "failed", label: "取消", className: "failed", text: clientSafeText(task.error || "用户取消生成", task), url: "" };
  if (rawStatus === "pending") return { state: "pending", label: "待执行", className: "unknown", text: clientSafeText(resultText || task.error || "", task), url: "" };
  if (rawStatus === "running" || rawStatus === "submitted") {
    return { state: "running", label: rawStatus === "submitted" ? "等待结果" : "生成中", className: "running", text: clientSafeText(resultText || task.error || "", task), url: "" };
  }
  if (!result) return { state: "unknown", label: "未查询", className: "unknown", text: "", url: "" };
  if (result.error) return { state: "unknown", label: "查询失败", className: "failed", text: clientSafeText(result.error, task), url: "" };
  if (text.includes("无法生成") || text.includes("违规") || text.includes("失败") || text.includes("游客模式") || text.includes("请登录后再试")) {
    return { state: "failed", label: "失败", className: "failed", text, url: "" };
  }
  if (resultCode === "1" || resultCode === "0") return { state: "running", label: "生成中", className: "running", text, url: "" };
  return { state: "unknown", label: resultCode ? `code ${resultCode}` : "未知", className: "unknown", text, url: "" };
}

function pageTasks() {
  return {
    tasks: state.tasks,
    total: state.taskTotal,
    totalPages: state.taskTotalPages,
  };
}

function renderTaskTable(options = {}) {
  const page = pageTasks();
  const signature = JSON.stringify({
    page: state.page,
    totalPages: page.totalPages,
    tasks: page.tasks.map((task) => [task, getTaskStatus(task), state.queryingTaskIds.has(task.id), state.deletingTaskIds.has(task.id)]),
  });
  if (options.skipUnchanged && signature === state.taskRenderSignature) return;
  state.taskRenderSignature = signature;
  els.currentPageTaskCount.textContent = String(page.tasks.length);
  els.pageState.textContent = `第 ${state.page} / ${page.totalPages} 页`;
  els.prevPage.disabled = state.page <= 1;
  els.nextPage.disabled = state.page >= page.totalPages;

  if (!page.tasks.length) {
    els.taskTableBody.innerHTML = `<tr><td colspan="6"><div class="empty-state">暂无任务</div></td></tr>`;
    return;
  }

  els.taskTableBody.innerHTML = page.tasks.map((task) => {
    const status = getTaskStatus(task);
    const resultText = status.text || task.error || "-";
    const canOpenResult = resultText && resultText !== "-";
    const rawStatus = String(task.status || "未知");
    const statusMeta = status.className === "success" ? "" : rawStatus;
    const fullPrompt = String(task.prompt || task.prompt_preview || "").trim();
    const promptPreview = String(task.prompt_preview || "").trim() || "-";
    const ownerName = String(task.owner_name || "未备注").trim() || "未备注";
    const modelLabel = String(task.model || "").trim();
    const platformLabel = portal === "client" ? modelLabel : (PLATFORM_LABELS[String(task.platform || "dola")] || String(task.platform || "Dola"));
    const video = status.url
      ? `<div class="video-actions">
           <button class="video-link-button" type="button" data-action="open-video" data-id="${escapeHtml(task.id)}">打开视频</button>
           <button class="video-link-button" type="button" data-action="copy-url" data-id="${escapeHtml(task.id)}">复制链接</button>
           <button class="video-link-button" type="button" data-action="download-video" data-id="${escapeHtml(task.id)}">一键下载</button>
         </div>`
      : "-";
    const isQuerying = state.queryingTaskIds.has(task.id);
    const isDeleting = state.deletingTaskIds.has(task.id);
    return `
      <tr>
        <td>
          <div class="task-id">
            <div class="task-prompt-row">
              <span class="task-prompt" title="${escapeHtml(fullPrompt || task.prompt_preview || "")}">${escapeHtml(promptPreview)}</span>
              <button class="task-copy-prompt" type="button" data-action="copy-prompt" data-id="${escapeHtml(task.id)}">复制</button>
            </div>
            <code title="${escapeHtml(task.id)}">${escapeHtml(shortId(task.id))}</code>
            <span class="task-time">${escapeHtml(portal === "client" ? (modelLabel || "当前模型") : (modelLabel ? `${platformLabel} · ${modelLabel}` : platformLabel))}</span>
            <span class="task-time">${escapeHtml(formatTime(task.created_at))}</span>
          </div>
        </td>
        <td><span class="task-owner-chip" title="${escapeHtml(ownerName)}">${escapeHtml(ownerName)}</span></td>
        <td>
          <div class="task-status-stack">
            <span class="chip ${status.className}">${escapeHtml(status.label)}</span>
            ${statusMeta ? `<span class="task-status-meta">${escapeHtml(statusMeta)}</span>` : ""}
          </div>
        </td>
        <td>
          <button class="result-text-button ${canOpenResult ? "has-detail" : ""}" type="button" data-action="show-result" data-id="${escapeHtml(task.id)}" title="${canOpenResult ? "查看完整文本" : ""}" ${canOpenResult ? "" : "disabled"}>
            <span class="result-text-preview">${escapeHtml(resultText)}</span>
          </button>
        </td>
        <td><div class="url-cell">${video}</div></td>
        <td>
          <div class="row-actions">
            <button class="icon-button" type="button" data-action="query" data-id="${escapeHtml(task.id)}" ${isQuerying ? "disabled" : ""}>${isQuerying ? "查询中" : "查询"}</button>
            <button class="icon-button" type="button" data-action="copy-id" data-id="${escapeHtml(task.id)}">复制ID</button>
            <button class="danger-button" type="button" data-action="delete" data-id="${escapeHtml(task.id)}" ${isDeleting ? "disabled" : ""}>${isDeleting ? "处理中" : (status.className === "running" ? "取消" : "删除")}</button>
          </div>
        </td>
      </tr>
    `;
  }).join("");
}

function renderVideoLibrary() {
  if (!els.videoLibrary) return;
  const cutoff = Date.now() - state.taskRetentionDays * 86400000;
  const videos = state.tasks.filter((task) => {
    const completedAt = Date.parse(task.finished_at || task.updated_at || "");
    const hidden = portal === "client" ? task.video_hidden_for_client : task.video_hidden_for_admin;
    return !hidden && getTaskStatus(task).url && (!Number.isFinite(completedAt) || completedAt >= cutoff);
  }).map((task) => ({ task, status: getTaskStatus(task) }));
  if (!videos.length) {
    els.videoLibrary.innerHTML = `<div class="empty-state">暂无已完成视频</div>`;
    if (els.selectAllVideos) els.selectAllVideos.checked = false;
    if (els.deleteSelectedVideos) els.deleteSelectedVideos.disabled = true;
    return;
  }
  els.videoLibrary.innerHTML = videos.map(({ task, status }) => `
    <article class="video-library-card ${state.selectedVideoIds.has(task.id) ? "selected" : ""}">
      <label class="video-card-select"><input type="checkbox" data-video-select="${escapeHtml(task.id)}" ${state.selectedVideoIds.has(task.id) ? "checked" : ""} /> 选择</label>
      <video src="${escapeHtml(status.url)}" controls preload="metadata"></video>
      <strong>${escapeHtml(task.prompt_preview || task.prompt || "视频任务")}</strong>
      <span>${escapeHtml(task.model || "")}</span>
      <span>用户：${escapeHtml(task.owner_name || state.userName || "当前用户")}</span>
      <time>生成完成：${escapeHtml(formatTime(task.finished_at || task.updated_at))}</time>
      <div class="row-actions"><button class="secondary-button" type="button" data-action="download-video" data-id="${escapeHtml(task.id)}">下载视频</button><button class="danger-button" type="button" data-action="delete-video" data-id="${escapeHtml(task.id)}">从本端隐藏</button></div>
    </article>`).join("");
  const ids = videos.map(({ task }) => task.id);
  if (els.selectAllVideos) els.selectAllVideos.checked = ids.length > 0 && ids.every((id) => state.selectedVideoIds.has(id));
  if (els.deleteSelectedVideos) els.deleteSelectedVideos.disabled = !ids.some((id) => state.selectedVideoIds.has(id));
}

async function deleteVideoTasks(ids) {
  const uniqueIds = Array.from(new Set(ids));
  if (!uniqueIds.length) return;
  if (!window.confirm(`确认从当前入口隐藏选中的 ${uniqueIds.length} 个视频？任务记录和另一端展示不会被删除。`)) return;
  let deleted = 0;
  for (const id of uniqueIds) {
    try {
      await apiFetch(`/tasks/${encodeURIComponent(id)}/video-visibility`, { method: "POST", body: { hidden: true } });
      const task = state.tasks.find((item) => item.id === id);
      if (task) task[portal === "client" ? "video_hidden_for_client" : "video_hidden_for_admin"] = true;
      state.selectedVideoIds.delete(id);
      deleted += 1;
    } catch (error) {
      toast(`${shortId(id)} 隐藏失败：${error.message}`, "error");
    }
  }
  saveSessionResults();
  renderTaskTable();
  renderVideoLibrary();
  updateDashboardMetrics();
  if (deleted) toast(`已从当前入口隐藏 ${deleted} 个视频`);
}

function pageTempTokens() {
  const tokens = state.tempTokens;
  const totalPages = Math.max(1, Math.ceil(tokens.length / state.quotaPageSize));
  state.quotaPage = Math.min(Math.max(1, state.quotaPage), totalPages);
  const start = (state.quotaPage - 1) * state.quotaPageSize;
  return {
    tokens: tokens.slice(start, start + state.quotaPageSize),
    total: tokens.length,
    totalPages,
  };
}

function renderTempTokenTable() {
  if (!els.tempTokenTableBody) return;
  const page = pageTempTokens();
  if (els.quotaPageState) els.quotaPageState.innerHTML = `<strong>${page.total}</strong><span>${state.quotaPage} / ${page.totalPages}</span>`;
  if (els.prevQuotaPage) els.prevQuotaPage.disabled = state.quotaPage <= 1;
  if (els.nextQuotaPage) els.nextQuotaPage.disabled = state.quotaPage >= page.totalPages;
  if (!page.tokens.length) {
    els.tempTokenTableBody.innerHTML = `<tr><td colspan="7"><div class="empty-state">暂无临时 Token</div></td></tr>`;
    return;
  }

  els.tempTokenTableBody.innerHTML = page.tokens.map((item) => {
    const token = String(item.token || "");
    const tokenId = String(item.id || "");
    const concurrency = Number(item.concurrency || 1);
    const taskRetentionDays = Number(item.task_retention_days || 7);
    const remark = String(item.remark || "");
    const remaining = Number(item.remaining || 0);
    return `
      <tr>
        <td>
          <input class="token-remark-input" type="text" maxlength="100" value="${escapeHtml(remark)}" placeholder="用户名字" data-token-remark="${escapeHtml(tokenId)}" />
        </td>
        <td>
          <div class="temp-token-cell">
            <code title="${escapeHtml(token)}">${escapeHtml(token)}</code>
            <button class="token-copy-button" type="button" data-action="copy-temp-token" data-id="${escapeHtml(tokenId)}">复制</button>
          </div>
        </td>
        <td>
          <input class="quota-limit-input" type="number" min="1" max="100" step="1" value="${escapeHtml(concurrency)}" data-token-concurrency="${escapeHtml(tokenId)}" />
        </td>
        <td>
          <input class="quota-limit-input" type="number" min="1" max="15" step="1" value="${escapeHtml(taskRetentionDays)}" data-token-retention="${escapeHtml(tokenId)}" />
        </td>
        <td>${escapeHtml(remaining)}</td>
        <td>${escapeHtml(formatTime(item.created_at))}</td>
        <td>
          <div class="row-actions quota-row-actions">
            <button class="icon-button" type="button" data-action="save-temp-token" data-id="${escapeHtml(tokenId)}">保存</button>
            <button class="danger-button" type="button" data-action="delete-temp-token" data-id="${escapeHtml(tokenId)}">删除</button>
          </div>
        </td>
      </tr>
    `;
  }).join("");
}

function renderAccountTable() {
  if (!els.accountTableBody) return;
  const platform = state.accountPlatformFilter || "all";
  const platformLabel = platform === "all" ? "全部平台" : (PLATFORM_LABELS[platform] || platform);
  const stats = state.accountStats || {};
  const accounts = state.accounts;
  if (els.accountPlatformCount) els.accountPlatformCount.textContent = `${platformLabel} ${Number(stats.total ?? state.accountTotal)} 个`;
  if (els.accountNormalCount) els.accountNormalCount.textContent = String(Number(stats.normal || 0));
  if (els.accountAbnormalCount) els.accountAbnormalCount.textContent = String(Number(stats.abnormal || 0));
  if (els.accountPageTotal) els.accountPageTotal.textContent = `本页 ${accounts.length} 条 · 共 ${state.accountTotal} 条 · ${state.accountPage} / ${state.accountTotalPages}`;
  if (els.prevAccountPage) els.prevAccountPage.disabled = state.accountPage <= 1;
  if (els.nextAccountPage) els.nextAccountPage.disabled = state.accountPage >= state.accountTotalPages;
  if (!accounts.length) {
    const emptyText = state.accounts.length ? "当前筛选条件下没有账号" : "暂无账号";
    els.accountTableBody.innerHTML = `<tr><td colspan="6"><div class="empty-state">${emptyText}</div></td></tr>`;
    return;
  }

  els.accountTableBody.innerHTML = accounts.map((item) => {
    const id = String(item.id || "");
    const name = String(item.name || id || "未命名账号");
    const platformLabel = PLATFORM_LABELS[String(item.platform || "dola")] || String(item.platform || "Dola");
    const enabled = item.enabled !== false;
    const abnormal = item.account_status === "abnormal";
    const accountStatus = abnormal ? "登录异常" : (enabled ? "正常" : "停用");
    const quotaLimit = Number(item.quota_limit || 0);
    const quotaUsed = Number(item.quota_used || 0);
    const quotaRemaining = item.quota_remaining === null || item.quota_remaining === undefined ? "不限" : String(item.quota_remaining);
    const quotaLabel = quotaLimit ? `${quotaUsed} / ${quotaLimit}` : `${quotaUsed} / 不限`;
    const currentTaskId = String(item.current_task_id || "");
    const currentWorkerId = String(item.current_worker_id || "");
    const activeTasks = Array.isArray(item.active_tasks) ? item.active_tasks : [];
    const activeCount = Number(item.active_task_count || activeTasks.length || 0);
    const activeTitle = activeTasks.map((task) => `${shortId(task.task_id)} ${task.worker_id || ""}`.trim()).join("\n");
    const activeText = activeCount > 1
      ? `${activeCount} 条执行中：${activeTasks.slice(0, 3).map((task) => shortId(task.task_id)).join("、")}${activeCount > 3 ? "..." : ""}`
      : (currentTaskId ? `${shortId(currentTaskId)}${currentWorkerId ? ` · ${currentWorkerId}` : ""}` : "无任务");
    return `
      <tr>
        <td>
          <div class="account-name-cell">
            <span class="account-card-label">账号</span>
            <strong>${escapeHtml(name)}</strong>
            <span class="chip unknown">${escapeHtml(platformLabel)}</span>
            <code>${escapeHtml(id)}</code>
          </div>
        </td>
        <td><span class="account-card-label">状态</span><span class="chip ${abnormal ? "failed" : (enabled ? "success" : "unknown")}" title="${escapeHtml(item.status_reason || "")}">${accountStatus}</span></td>
        <td>
          <div class="account-quota-cell">
            <span class="account-card-label">额度</span>
            <strong>${escapeHtml(quotaLabel)}</strong>
            <span>本地剩余 ${escapeHtml(quotaRemaining)}</span>
          </div>
        </td>
        <td>
          <span class="account-card-label">正在执行</span>
          <span class="account-current-task ${activeCount ? "active" : ""}" title="${escapeHtml(activeTitle || currentTaskId)}">${escapeHtml(activeText)}</span>
        </td>
        <td><span class="account-card-label">更新时间</span><span class="account-time-value">${escapeHtml(formatTime(item.updated_at || item.created_at))}</span></td>
        <td>
          <span class="account-card-label">操作</span>
          <div class="row-actions quota-row-actions">
            <button class="icon-button" type="button" data-action="toggle-account" data-id="${escapeHtml(id)}">${enabled ? "停用" : "启用"}</button>
            <button class="icon-button" type="button" data-action="edit-quota" data-id="${escapeHtml(id)}">额度</button>
            <button class="icon-button" type="button" data-action="reset-quota" data-id="${escapeHtml(id)}">清零</button>
            <button class="danger-button" type="button" data-action="delete-account" data-id="${escapeHtml(id)}">删除</button>
          </div>
        </td>
      </tr>
    `;
  }).join("");
}

async function refreshTempTokens(options = {}) {
  if (portal === "client") return;
  if (!options.quiet) setBusy(els.refreshTempTokens, true, "刷新中");
  try {
    const data = await apiFetch("/temp-tokens");
    state.tempTokens = Array.isArray(data.tokens) ? data.tokens : [];
    renderTempTokenTable();
    if (!options.quiet) toast(`已加载 ${state.tempTokens.length} 个临时 Token`);
  } catch (error) {
    if (!options.quiet) toast(`临时 Token 读取失败：${error.message}`, "error");
    throw error;
  } finally {
    if (!options.quiet) setBusy(els.refreshTempTokens, false);
  }
}

function openCreateTokenModal() {
  if (portal === "client") return;
  els.createTokenCount.value = "1";
  els.createTokenLimit.value = "100";
  if (els.createTokenConcurrency) els.createTokenConcurrency.value = "1";
  if (els.createTokenRetentionDays) els.createTokenRetentionDays.value = "7";
  if (els.createTokenRemark) els.createTokenRemark.value = "";
  els.createTokenState.textContent = "";
  els.createTokenModal.classList.remove("hidden");
  els.createTokenModal.setAttribute("aria-hidden", "false");
  els.createTokenCount.focus();
  els.createTokenCount.select();
}

function closeCreateTokenModal() {
  els.createTokenModal.classList.add("hidden");
  els.createTokenModal.setAttribute("aria-hidden", "true");
}

async function createTempTokens() {
  const count = Number.parseInt(els.createTokenCount.value, 10);
  const limit = Number.parseInt(els.createTokenLimit.value, 10);
  const concurrency = Number.parseInt(els.createTokenConcurrency?.value || "1", 10);
  const taskRetentionDays = Number.parseInt(els.createTokenRetentionDays?.value || "7", 10);
  const remark = String(els.createTokenRemark?.value || "").trim();
  if (!Number.isInteger(count) || count < 1 || count > 200) {
    toast("生成条数范围是 1 - 200", "error");
    return;
  }
  if (!Number.isInteger(limit) || limit < 1 || limit > 100000) {
    toast("额度范围是 1 - 100000", "error");
    return;
  }
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 100) {
    toast("单条并发范围是 1 - 100", "error");
    return;
  }
  if (!Number.isInteger(taskRetentionDays) || taskRetentionDays < 1 || taskRetentionDays > 15) {
    toast("清理时间范围是 1 - 15 天", "error");
    return;
  }

  setBusy(els.confirmCreateTokens, true, "生成中");
  try {
    const data = await apiFetch("/temp-tokens", {
      method: "POST",
      body: { count, limit, concurrency, remark, task_retention_days: taskRetentionDays },
    });
    const created = Array.isArray(data.tokens) ? data.tokens.length : 0;
    els.createTokenState.textContent = "已生成";
    toast(`已生成 ${created} 个临时 Token`);
    closeCreateTokenModal();
    await refreshTempTokens({ quiet: true });
  } catch (error) {
    els.createTokenState.textContent = "生成失败";
    toast(`生成失败：${error.message}`, "error");
  } finally {
    setBusy(els.confirmCreateTokens, false);
  }
}

async function saveTempTokenLimit(tokenId) {
  if (state.savingTokenIds.has(tokenId)) return;
  const concurrencyInput = Array.from(document.querySelectorAll("[data-token-concurrency]")).find((node) => node.dataset.tokenConcurrency === tokenId);
  const retentionInput = Array.from(document.querySelectorAll("[data-token-retention]")).find((node) => node.dataset.tokenRetention === tokenId);
  const remarkInput = Array.from(document.querySelectorAll("[data-token-remark]")).find((node) => node.dataset.tokenRemark === tokenId);
  const concurrency = Number.parseInt(concurrencyInput?.value || "", 10);
  const taskRetentionDays = Number.parseInt(retentionInput?.value || "", 10);
  const remark = String(remarkInput?.value || "").trim();
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 100) {
    toast("单条并发范围是 1 - 100", "error");
    return;
  }
  if (!Number.isInteger(taskRetentionDays) || taskRetentionDays < 1 || taskRetentionDays > 15) {
    toast("清理时间范围是 1 - 15 天", "error");
    return;
  }
  state.savingTokenIds.add(tokenId);
  try {
    await apiFetch(`/temp-tokens/${encodeURIComponent(tokenId)}`, {
      method: "PATCH",
      body: { concurrency, remark, task_retention_days: taskRetentionDays },
    });
    state.tempTokens = state.tempTokens.map((item) => item.id === tokenId ? { ...item, concurrency, remark, task_retention_days: taskRetentionDays } : item);
    state.tasks = state.tasks.map((task) => task.owner_token_hash === tokenId ? { ...task, owner_name: remark || "未备注" } : task);
    renderTaskTable();
    updateDashboardMetrics();
    toast("用户名称、并发和清理时间已保存");
    await refreshTempTokens({ quiet: true });
    await refreshTasks({ quiet: true });
  } finally {
    state.savingTokenIds.delete(tokenId);
  }
}

async function deleteTempTokenById(tokenId) {
  const item = state.tempTokens.find((token) => token.id === tokenId);
  const ok = window.confirm(`确认删除这个临时 Token？\n${item?.token || tokenId}`);
  if (!ok) return;
  await apiFetch(`/temp-tokens/${encodeURIComponent(tokenId)}/delete`, { method: "POST" });
  toast("临时 Token 已删除");
  await refreshTempTokens({ quiet: true });
}

async function toggleAccountById(accountId) {
  const item = state.accounts.find((account) => account.id === accountId);
  if (!item) return;
  await apiFetch(`/accounts/${encodeURIComponent(accountId)}`, {
    method: "PATCH",
    body: { enabled: item.enabled === false },
  });
  toast(item.enabled === false ? "账号已启用" : "账号已停用");
  await refreshAccounts({ quiet: true });
}

async function editAccountQuota(accountId) {
  const item = state.accounts.find((account) => account.id === accountId);
  if (!item) return;
  const value = window.prompt("请输入额度上限，0 表示不限", String(item.quota_limit || 0));
  if (value === null) return;
  const quotaLimit = Number.parseInt(value, 10);
  if (!Number.isInteger(quotaLimit) || quotaLimit < 0) {
    toast("额度必须是大于等于 0 的整数", "error");
    return;
  }
  await apiFetch(`/accounts/${encodeURIComponent(accountId)}`, {
    method: "PATCH",
    body: { quota_limit: quotaLimit },
  });
  toast("账号额度已更新");
  await refreshAccounts({ quiet: true });
}

async function resetAccountQuota(accountId) {
  const item = state.accounts.find((account) => account.id === accountId);
  const ok = window.confirm(`确认清零这个账号的已用额度？\n${item?.name || accountId}`);
  if (!ok) return;
  await apiFetch(`/accounts/${encodeURIComponent(accountId)}`, {
    method: "PATCH",
    body: { reset_quota: true },
  });
  toast("已用额度已清零");
  await refreshAccounts({ quiet: true });
}

async function deleteAccountById(accountId) {
  const item = state.accounts.find((account) => account.id === accountId);
  const ok = window.confirm(`确认删除这个账号？\n${item?.name || accountId}`);
  if (!ok) return;
  await apiFetch(`/accounts/${encodeURIComponent(accountId)}`, { method: "DELETE" });
  toast("账号已删除");
  await refreshAccounts({ quiet: true });
}

async function queryTask(id, options = {}) {
  if (state.queryingTaskIds.has(id) && !options.deferRender) return state.results[id];
  state.queryingTaskIds.add(id);
  if (!options.deferRender) renderTaskTable();
  try {
    const data = await apiFetch(`/tasks/${encodeURIComponent(id)}`);
    state.results[id] = data;
    if (!options.quiet) toast(`${shortId(id)} 查询完成`);
    if (!options.deferRender) {
      saveSessionResults();
      renderTaskTable();
      updateDashboardMetrics();
    }
    return data;
  } catch (error) {
    const task = state.tasks.find((item) => item.id === id) || {};
    state.results[id] = { error: clientSafeText(error.message, task) };
    if (!options.deferRender) {
      saveSessionResults();
      renderTaskTable();
      updateDashboardMetrics();
    }
    if (!options.quiet) toast(`${shortId(id)} 查询失败：${error.message}`, "error");
    throw error;
  } finally {
    state.queryingTaskIds.delete(id);
    if (!options.deferRender) renderTaskTable();
  }
}

async function queryVisibleTasks() {
  if (els.queryVisibleTasks.disabled) return;
  const ids = pageTasks().tasks.map((task) => task.id);
  if (!ids.length) return;
  setBusy(els.queryVisibleTasks, true, "查询中");
  let success = 0;
  let failed = 0;
  ids.forEach((id) => state.queryingTaskIds.add(id));
  renderTaskTable();
  try {
    await runPool(ids, 5, async (id) => {
      try {
        state.queryingTaskIds.delete(id);
        await queryTask(id, { quiet: true, deferRender: true });
        success += 1;
      } catch (_) {
        failed += 1;
      }
    });
    saveSessionResults();
    renderTaskTable();
    updateDashboardMetrics();
    toast(`本页查询完成：成功 ${success}，失败 ${failed}`, failed ? "error" : "info");
  } finally {
    ids.forEach((id) => state.queryingTaskIds.delete(id));
    renderTaskTable();
    setBusy(els.queryVisibleTasks, false);
  }
}

async function runPool(items, limit, worker) {
  let index = 0;
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (index < items.length) {
      const current = items[index];
      index += 1;
      await worker(current);
    }
  });
  await Promise.all(runners);
}

async function deleteTask(id) {
  if (state.deletingTaskIds.has(id)) return;
  state.deletingTaskIds.add(id);
  renderTaskTable();
  try {
    const task = state.tasks.find((item) => item.id === id);
    const status = getTaskStatus(task || {});
    const isRunning = status.className === "running";
    if (String(task?.status || "") === "success" || status.className === "success") {
      const ok = window.confirm(`确认删除成功任务 ${shortId(id)}？删除后任务记录会从列表移除。`);
      if (!ok) return;
    }
    const data = await apiFetch(`/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (data.cancelable === false) {
      toast(data.detail || "已进入生成中，无法取消", "error");
      return;
    }
    if (isRunning) {
      state.tasks = state.tasks.map((item) => item.id === id ? { ...item, status: "canceled", error: "用户取消生成" } : item);
      state.results[id] = { code: "0", text: "用户取消生成", url: "" };
    } else {
      state.tasks = state.tasks.filter((task) => task.id !== id);
      delete state.results[id];
    }
    saveSessionResults();
    await refreshTasks({ quiet: true });
    updateDashboardMetrics();
    toast(`${shortId(id)} ${isRunning ? "已取消" : "已删除"}`);
  } catch (error) {
    toast(`操作失败：${error.message}`, "error");
  } finally {
    state.deletingTaskIds.delete(id);
    renderTaskTable();
  }
}

async function clearTasks() {
  if (!state.tasks.length) return;
  const ok = window.confirm(`确认删除当前列表中的 ${state.tasks.length} 条任务？生成中的任务会被后端拒绝删除。`);
  if (!ok) return;

  setBusy(els.clearTasks, true, "删除中");
  try {
    const data = await apiFetch("/tasks", { method: "DELETE" });
    state.results = {};
    saveSessionResults();
    await refreshTasks({ quiet: true });
    const skipped = Array.isArray(data.skipped) ? data.skipped.length : 0;
    toast(`清空完成：删除 ${data.deleted || 0}${skipped ? `，保留生成中 ${skipped}` : ""}`);
  } catch (error) {
    toast(`清空失败：${error.message}`, "error");
  } finally {
    setBusy(els.clearTasks, false);
  }
}

async function deleteFailedTasks() {
  const removableCount = state.tasks.filter((task) => ["failed", "canceled"].includes(String(task.status || ""))).length;
  if (!removableCount) {
    toast("当前没有失败或取消任务");
    return;
  }
  const ok = window.confirm(`确认删除 ${removableCount} 条失败/取消任务？`);
  if (!ok) return;

  setBusy(els.deleteFailedTasks, true, "删除中");
  try {
    const data = await apiFetch("/tasks-failed", { method: "DELETE" });
    await refreshTasks({ quiet: true });
    toast(`失败/取消任务删除完成：${data.deleted || 0}`);
  } catch (error) {
    toast(`删除失败任务失败：${error.message}`, "error");
  } finally {
    setBusy(els.deleteFailedTasks, false);
  }
}

function setSegmentValue(group, key, value) {
  group.querySelectorAll("button").forEach((button) => {
    button.classList.toggle("active", button.dataset[key] === value);
  });
  if (els.ratioValue) els.ratioValue.textContent = value;
}

function renderImages() {
  if (!state.images.length) {
    els.imageList.innerHTML = "";
    return;
  }
  els.imageList.innerHTML = "";
  state.images.forEach((file, index) => {
    const row = document.createElement("div");
    row.className = "image-item";
    const img = document.createElement("img");
    img.className = "thumb";
    img.alt = file.name;
    img.src = URL.createObjectURL(file);
    img.onload = () => URL.revokeObjectURL(img.src);

    const meta = document.createElement("div");
    meta.className = "image-meta";
    meta.innerHTML = `<strong>${escapeHtml(file.name)}</strong><span>${escapeHtml(formatBytes(file.size))}</span>`;

    const button = document.createElement("button");
    button.className = "icon-button";
    button.type = "button";
    button.textContent = "移除";
    button.disabled = state.submitting;
    button.addEventListener("click", () => {
      if (state.submitting) return;
      state.images.splice(index, 1);
      renderImages();
    });

    row.append(img, meta, button);
    els.imageList.appendChild(row);
  });
}

async function setImages(files) {
  if (state.submitting) return;
  const incoming = Array.from(files || []).filter((file) => file.type.startsWith("image/"));
  const keys = new Set(await Promise.all(state.images.map(fileMd5)));
  const unique = [];
  let duplicateCount = 0;

  for (const file of incoming) {
    const key = await fileMd5(file);
    if (keys.has(key)) {
      duplicateCount += 1;
      continue;
    }
    keys.add(key);
    unique.push(file);
  }

  const remaining = Math.max(0, MAX_IMAGE_COUNT - state.images.length);
  const accepted = unique.slice(0, remaining);
  const overflow = unique.length - accepted.length;
  state.images = state.images.concat(accepted);
  els.imageInput.value = "";

  if (duplicateCount) {
    toast(`已跳过 ${duplicateCount} 张重复图片`);
  }
  if (overflow > 0) {
    toast(`最多支持 ${MAX_IMAGE_COUNT} 张图片，已忽略 ${overflow} 张`, "error");
  }
  renderImages();
}

function resetSubmitForm(options = {}) {
  if (state.submitting && !options.force) return;
  els.promptInput.value = "";
  state.ratio = "9:16";
  setSegmentValue(els.ratioGroup, "ratio", state.ratio);
  state.images = [];
  els.imageInput.value = "";
  renderImages();
  els.submitState.textContent = "待提交";
}

async function submitTask(event) {
  event.preventDefault();
  if (state.submitting) return;
  const prompt = els.promptInput.value.trim();
  if (!prompt) {
    toast("提示词不能为空", "error");
    return;
  }
  if (portal === "client" && state.freeRemaining + state.points <= 0) {
    toast("视频额度已用完且积分不足，请先购买积分", "error");
    switchView("points");
    return;
  }

  const form = new FormData();
  form.append("prompt", prompt);
  form.append("ratio", state.ratio);
  form.append("platform", state.platform || "dola");
  form.append("model", state.model || "");
  state.images.forEach((file) => form.append("images", file, file.name));

  setSubmitControlsDisabled(true);
  els.submitState.textContent = "提交中";
  try {
    const data = await apiFetch("/tasks", { method: "POST", body: form });
    if (data.quota) applyAccessScope({ ...data, task_retention_days: state.taskRetentionDays, user_name: state.userName });
    els.submitState.textContent = `已提交：${shortId(data.id)}`;
    const billingText = data.billing?.free_used ? "，已扣除 1 次视频额度" : Number(data.billing?.points_used || 0) > 0 ? `，已扣除 ${data.billing.points_used} 积分` : "";
    toast(`任务已提交${billingText}：${data.id}`);
    await refreshTasks({ quiet: true });
    resetSubmitForm({ force: true });
  } catch (error) {
    els.submitState.textContent = "提交失败";
    toast(`提交失败：${error.message}`, "error");
  } finally {
    setSubmitControlsDisabled(false);
  }
}

async function copyText(value, label = "内容") {
  const text = String(value || "");
  if (!text) {
    toast("没有可复制内容", "error");
    return;
  }

  try {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        toast(`${label}已复制`);
        return;
      } catch (_) {
        // Fall through for HTTP deployments and browsers that deny clipboard permissions.
      }
    }

    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    textarea.style.top = "0";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);
    const copied = document.execCommand("copy");
    document.body.removeChild(textarea);
    if (!copied) throw new Error("copy command failed");
    toast(`${label}已复制`);
  } catch (error) {
    console.warn("copy failed", error);
    toast("复制失败，请手动选择文本复制", "error");
  }
}

function downloadVideo(url, id) {
  const value = String(url || "").trim();
  if (!value) return;
  const link = document.createElement("a");
  link.href = value;
  link.download = `${id || "video"}.mp4`;
  link.target = "_blank";
  link.rel = "noreferrer";
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function bindEvents() {
  els.loginForm.addEventListener("submit", login);
  els.adminPasswordForm?.addEventListener("submit", changeAdminPassword);
  els.clientPasswordForm?.addEventListener("submit", changeClientPassword);
  els.clientEmailForm?.addEventListener("submit", changeClientEmail);
  els.feedbackForm?.addEventListener("submit", submitFeedback);
  els.adminNotificationForm?.addEventListener("submit", submitAdminNotification);
  els.adminAnnouncementForm?.addEventListener("submit", submitAdminAnnouncement);
  els.redeemForm?.addEventListener("submit", redeemPoints);
  els.refreshTransactions?.addEventListener("click", loadTransactions);
  els.pointCardForm?.addEventListener("submit", generatePointCards);
  els.refreshPointCards?.addEventListener("click", loadPointCards);
  els.exportPointCards?.addEventListener("click", exportPointCardsCsv);
  els.pointCardTableBody?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-copy-point-card]");
    if (button) copyText(button.dataset.copyPointCard, "卡密");
  });
  els.pointCardSearch?.addEventListener("input", () => {
    window.clearTimeout(state.pointCardSearchTimer);
    state.pointCardSearchTimer = window.setTimeout(loadPointCards, 250);
  });
  els.pointCardStatusFilter?.addEventListener("change", loadPointCards);
  els.pointCardTypeFilter?.addEventListener("change", loadPointCards);
  els.copyGeneratedPointCards?.addEventListener("click", () => copyText(els.generatedPointCardCodes?.textContent || "", "卡密"));
  els.purchaseOptions?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-purchase-url]");
    if (button) window.open(button.dataset.purchaseUrl, "_blank", "noopener,noreferrer");
  });
  els.membershipList?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-membership-id]");
    if (button) purchaseMembership(button);
  });
  els.confirmAnnouncementModal?.addEventListener("click", () => closeActiveAnnouncement().catch((error) => toast(`公告状态更新失败：${error.message}`, "error")));
  els.closeAnnouncementModal?.addEventListener("click", () => closeActiveAnnouncement().catch((error) => toast(`公告状态更新失败：${error.message}`, "error")));
  els.closeEmergencyAnnouncement?.addEventListener("click", () => closeActiveAnnouncement().catch((error) => toast(`公告状态更新失败：${error.message}`, "error")));
  els.announcementLevel?.addEventListener("change", () => {
    const emergency = els.announcementLevel.value === "emergency";
    els.announcementLockField.classList.toggle("hidden", !emergency);
    if (!emergency) els.announcementLockScreen.checked = false;
  });
  els.membershipForm?.addEventListener("submit", createMembership);
  els.refreshMessages?.addEventListener("click", refreshMessageCenter);
  els.feedbackTableBody?.addEventListener("change", async (event) => {
    const select = event.target.closest("[data-feedback-status]");
    if (!select) return;
    try {
      await saveFeedbackRecord(select.dataset.feedbackStatus);
      toast("反馈状态已更新");
    } catch (error) {
      toast(`状态更新失败：${error.message}`, "error");
      await loadFeedback();
    }
  });
  els.feedbackTableBody?.addEventListener("click", async (event) => {
    const deleteButton = event.target.closest("[data-delete-feedback]");
    if (deleteButton) {
      if (!window.confirm("确认删除这条用户反馈？删除后无法恢复。")) return;
      setBusy(deleteButton, true, "删除中");
      try {
        await apiFetch(`/admin/feedback/${encodeURIComponent(deleteButton.dataset.deleteFeedback)}`, { method: "DELETE" });
        await loadFeedback();
        toast("反馈已删除");
      } catch (error) {
        toast(`反馈删除失败：${error.message}`, "error");
        setBusy(deleteButton, false);
      }
      return;
    }
    const button = event.target.closest("[data-save-feedback]");
    if (!button) return;
    setBusy(button, true, "保存中");
    try {
      await saveFeedbackRecord(button.dataset.saveFeedback);
      toast("处理状态和回复已保存");
      await loadFeedback();
    } catch (error) {
      toast(`反馈保存失败：${error.message}`, "error");
    } finally {
      setBusy(button, false);
    }
  });
  els.clientNotificationList?.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-read-notification]");
    if (!button) return;
    try {
      await apiFetch(`/notifications/${encodeURIComponent(button.dataset.readNotification)}/read`, { method: "PATCH" });
      await loadClientNotifications();
    } catch (error) {
      toast(`通知状态更新失败：${error.message}`, "error");
    }
  });
  els.adminNotificationList?.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-delete-notification]");
    const article = button?.closest("[data-notification-id]");
    if (!button || !article || !window.confirm("确认删除这条通知记录？用户端对应通知也会删除。")) return;
    setBusy(button, true, "删除中");
    try {
      await apiFetch(`/admin/notifications/${encodeURIComponent(article.dataset.notificationId)}`, { method: "DELETE" });
      await loadAdminNotifications();
      toast("通知已删除");
    } catch (error) {
      toast(`通知删除失败：${error.message}`, "error");
      setBusy(button, false);
    }
  });
  els.adminAnnouncementList?.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-toggle-announcement], [data-toggle-announcement-lock], [data-delete-announcement]");
    const article = button?.closest("[data-announcement-id]");
    if (!button || !article) return;
    try {
      if (button.hasAttribute("data-delete-announcement")) {
        if (!window.confirm("确认删除这条公告？删除后所有用户都将无法查看。")) return;
        await apiFetch(`/admin/announcements/${encodeURIComponent(article.dataset.announcementId)}`, { method: "DELETE" });
        await loadAdminAnnouncements();
        toast("公告已删除");
        return;
      }
      const body = button.hasAttribute("data-toggle-announcement-lock") ? { lock_screen: button.dataset.locked !== "true" } : { enabled: button.dataset.enabled !== "true" };
      await apiFetch(`/admin/announcements/${encodeURIComponent(article.dataset.announcementId)}`, { method: "PATCH", body });
      await loadAdminAnnouncements();
      toast("公告状态已更新");
    } catch (error) {
      toast(`公告操作失败：${error.message}`, "error");
    }
  });
  document.querySelectorAll("[data-message-tab]").forEach((button) => button.addEventListener("click", () => setMessageTab(button.dataset.messageTab)));
  els.notificationRecipients?.addEventListener("change", (event) => {
    const checkbox = event.target.closest("[data-notification-user]");
    if (!checkbox) return;
    if (checkbox.checked) state.selectedNotificationUserIds.add(checkbox.value);
    else state.selectedNotificationUserIds.delete(checkbox.value);
    updateNotificationRecipientState();
  });
  els.selectAllNotificationUsers?.addEventListener("change", () => {
    state.selectedNotificationUserIds = new Set(els.selectAllNotificationUsers.checked ? state.notificationUsers.map((item) => item.id) : []);
    els.notificationRecipients?.querySelectorAll("[data-notification-user]").forEach((checkbox) => { checkbox.checked = els.selectAllNotificationUsers.checked; });
    updateNotificationRecipientState();
  });
  els.packageForm?.addEventListener("submit", createPointPackage);
  const setClientMode = (register) => {
    state.clientRegisterMode = register;
    els.clientLoginTab?.classList.toggle("active", !register);
    els.clientRegisterTab?.classList.toggle("active", register);
    document.querySelectorAll(".client-register-only").forEach((item) => item.classList.toggle("hidden", !register || (item.classList.contains("registration-code-row") || item.querySelector("#clientEmailLocal")) && !state.registrationEmailVerificationEnabled));
    els.loginForm?.classList.toggle("register-mode", register);
    els.loginButton.textContent = register ? "注册并进入" : "登录";
  };
  els.clientLoginTab?.addEventListener("click", () => setClientMode(false));
  els.clientRegisterTab?.addEventListener("click", () => setClientMode(true));
  els.sendEmailCode?.addEventListener("click", async () => {
    if (!state.registrationEmailVerificationEnabled) return toast("邮箱注册验证未启用", "error");
    const email = selectedEmail(els.clientEmailLocal, els.clientEmailDomain);
    if (!els.clientEmailLocal?.value.trim()) return toast("请先填写邮箱账号", "error");
    setBusy(els.sendEmailCode, true, "发送中");
    try {
      await requestJson("/auth/register/email-code", "", { method: "POST", body: { email } });
      toast("验证码已发送，请检查邮箱");
    } catch (error) {
      toast(`发送失败：${error.message}`, "error");
    } finally {
      setBusy(els.sendEmailCode, false);
    }
  });
  els.forgotPasswordButton?.addEventListener("click", () => openSettingsModal(els.forgotPasswordModal, els.forgotPasswordEmail));
  els.sendForgotPasswordCode?.addEventListener("click", async () => {
    const email = els.forgotPasswordEmail?.value.trim();
    if (!email) return toast("请先填写绑定邮箱", "error");
    setBusy(els.sendForgotPasswordCode, true, "发送中");
    try {
      const data = await requestJson("/auth/password/forgot-code", "", { method: "POST", body: { email } });
      toast(data.detail || "验证码已发送，请检查邮箱");
    } catch (error) {
      toast(`发送失败：${error.message}`, "error");
    } finally {
      setBusy(els.sendForgotPasswordCode, false);
    }
  });
  els.forgotPasswordForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const email = els.forgotPasswordEmail.value.trim();
    const password = els.forgotPasswordNew.value;
    if (password !== els.forgotPasswordConfirm.value) return toast("两次输入的新密码不一致", "error");
    setBusy(els.resetPasswordButton, true, "重置中");
    try {
      const data = await requestJson("/auth/password/reset", "", { method: "POST", body: { email, email_code: els.forgotPasswordCode.value.trim(), new_password: password, confirm_password: els.forgotPasswordConfirm.value } });
      closeSettingsModal(els.forgotPasswordModal);
      els.forgotPasswordForm.reset();
      els.clientUsername.value = data.username || email;
      els.clientPassword.value = password;
      toast("密码已重置，请使用新密码登录");
    } catch (error) {
      toast(`重置失败：${error.message}`, "error");
    } finally {
      setBusy(els.resetPasswordButton, false);
    }
  });
  els.sendChangeEmailCode?.addEventListener("click", async () => {
    const email = selectedEmail(els.changeEmailLocal, els.changeEmailDomain);
    if (!els.changeEmailLocal?.value.trim()) return toast("请先填写邮箱账号", "error");
    setBusy(els.sendChangeEmailCode, true, "发送中");
    try {
      await apiFetch("/auth/email/code", { method: "POST", body: { email } });
      toast("验证码已发送到新邮箱");
    } catch (error) {
      toast(`发送失败：${error.message}`, "error");
    } finally {
      setBusy(els.sendChangeEmailCode, false);
    }
  });
  els.addRegistrationEmailDomain?.addEventListener("click", () => addEmailDomainRow());
  els.registrationEmailDomains?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-email-domain]");
    if (!button) return;
    button.closest(".email-domain-item")?.remove();
    if (!els.registrationEmailDomains.children.length) addEmailDomainRow();
  });
  els.copyClientToken?.addEventListener("click", () => copyText(els.clientTokenDisplay?.value || "", "API Token"));
  els.refreshClientToken?.addEventListener("click", async () => {
    if (!window.confirm("刷新后旧 Token 会立即失效，第三方软件需要更新为新 Token。确认刷新？")) return;
    setBusy(els.refreshClientToken, true, "刷新中");
    try {
      const data = await apiFetch("/auth/token/refresh", { method: "POST" });
      if (!data.token || data.token === state.apiToken) throw new Error("后端未返回新的 Token");
      state.apiToken = data.token;
      localStorage.setItem(portalStorageKey(TOKEN_KEY), data.token);
      els.clientTokenDisplay.value = data.token;
      applyAccessScope(data.quota ? data : await requestJson(authPath, data.token));
      await refreshTasks({ quiet: true, keepPage: true });
      toast("Token 已刷新，旧 Token 已失效");
    } catch (error) {
      toast(`刷新失败：${error.message}`, "error");
    } finally {
      setBusy(els.refreshClientToken, false);
    }
  });
  els.userTableBody?.addEventListener("click", async (event) => {
    try {
      const copyButton = event.target.closest("[data-copy-user-token]");
      if (copyButton) return copyText(copyButton.dataset.copyUserToken, "Token");
      const toggleButton = event.target.closest("[data-toggle-user]");
      if (toggleButton) {
        await apiFetch(`/users/${toggleButton.dataset.toggleUser}/status`, { method: "POST", body: { enabled: toggleButton.dataset.enabled !== "true" } });
        toast(toggleButton.dataset.enabled === "true" ? "用户已停用" : "用户已启用");
        return loadUsers();
      }
      const deleteButton = event.target.closest("[data-delete-user]");
      if (deleteButton) {
        if (!window.confirm(`确认删除用户 ${deleteButton.dataset.userName}？该用户 Token 会立即失效。`)) return;
        await apiFetch(`/users/${deleteButton.dataset.deleteUser}/delete`, { method: "POST" });
        toast("用户已删除");
        return loadUsers();
      }
      const deductButton = event.target.closest("[data-deduct-user-points]");
      if (deductButton) {
        const balance = Number(deductButton.dataset.userPointsBalance || 0);
        const amount = Number(window.prompt(`请输入扣除积分数量（当前可用 ${balance}）`, "1"));
        if (!Number.isInteger(amount * 10) || amount <= 0) return;
        if (amount > balance) return toast("扣除数量不能超过用户当前积分", "error");
        if (!window.confirm(`确认扣除 ${amount} 积分？`)) return;
        await apiFetch(`/users/${deductButton.dataset.deductUserPoints}/points/deduct`, { method: "POST", body: { amount } });
        toast(`已扣除 ${amount} 积分`);
        await Promise.all([loadUsers(), refreshTempTokens({ quiet: true })]);
        return;
      }
      const button = event.target.closest("[data-user-points]");
      if (!button) return;
      const amount = Number(window.prompt("请输入充值积分数量", "10"));
      if (!Number.isInteger(amount * 10) || amount <= 0) return;
      await apiFetch(`/users/${button.dataset.userPoints}/points`, { method: "POST", body: { amount } });
      toast("积分已充值");
      await Promise.all([loadUsers(), refreshTempTokens({ quiet: true })]);
    } catch (error) {
      toast(`用户操作失败：${error.message}`, "error");
    }
  });
  els.copyTokenCommand?.addEventListener("click", async () => {
    await copyText(els.tokenCommand?.value?.trim() || "", "命令行");
  });
  els.copyClientEntryUrl?.addEventListener("click", async () => {
    await copyText(els.clientEntryUrl?.value?.trim() || getClientEntryUrl(), "客户入口");
  });
  els.clientEntryUrl?.addEventListener("dblclick", () => {
    els.clientEntryUrl.select();
  });
  els.tokenCommand?.addEventListener("dblclick", () => {
    els.tokenCommand.select();
  });
  if (els.logoutButton) els.logoutButton.addEventListener("click", logout);
  if (els.dashboardLogoutButton) els.dashboardLogoutButton.onclick = (event) => {
    event.preventDefault();
    logout();
  };
  if (els.settingsLogoutButton) els.settingsLogoutButton.onclick = (event) => {
    event.preventDefault();
    logout();
  };
  const openSettingsModal = (modal, focusTarget) => {
    modal?.classList.remove("hidden");
    modal?.setAttribute("aria-hidden", "false");
    window.setTimeout(() => focusTarget?.focus(), 0);
  };
  els.openPasswordModal?.addEventListener("click", () => {
    els.changeAdminUsername.value = state.adminUsername || els.adminUsername?.value || "";
    openSettingsModal(els.passwordModal, els.currentAdminPassword);
  });
  els.updateRepository?.addEventListener("click", updateRepository);
  els.openClientPasswordModal?.addEventListener("click", () => openSettingsModal(els.clientPasswordModal, els.currentClientPassword));
  els.openClientEmailModal?.addEventListener("click", async () => {
    await loadEmailDomains();
    openSettingsModal(els.clientEmailModal, els.changeEmailLocal);
  });
  els.openFeedbackModal?.addEventListener("click", () => openSettingsModal(els.feedbackModal, els.feedbackContent));
  els.openProxyModal?.addEventListener("click", async () => {
    try {
      await loadProxyConfig();
      openSettingsModal(els.proxyModal, els.proxyApiUrl);
    } catch (error) {
      toast(`读取失败：${error.message}`, "error");
    }
  });
  els.openProxyModalFromNodes?.addEventListener("click", async () => {
    try {
      await loadProxyConfig();
      openSettingsModal(els.proxyModal, els.proxySubscriptionUrl);
    } catch (error) {
      toast(`读取失败：${error.message}`, "error");
    }
  });
  els.openEmailModal?.addEventListener("click", async () => {
    try {
      await loadEmailConfig();
      openSettingsModal(els.emailModal, els.registrationEmailDomains.querySelector("[data-email-domain]"));
    } catch (error) {
      toast(`读取失败：${error.message}`, "error");
    }
  });
  els.saveEmailConfig?.addEventListener("click", saveEmailConfig);
  els.openModelModal?.addEventListener("click", async () => {
    try {
      await loadPlatforms();
      openSettingsModal(els.modelModal, els.modelConfigList?.querySelector("input"));
    } catch (error) {
      toast(`读取失败：${error.message}`, "error");
    }
  });
  els.openPackageModal?.addEventListener("click", async () => {
    try {
      await loadAdminPointPackages();
      openSettingsModal(els.packageModal, els.packageName);
    } catch (error) {
      toast(`套餐读取失败：${error.message}`, "error");
    }
  });
  els.openPointCardModal?.addEventListener("click", () => {
    els.generatedPointCards.classList.add("hidden");
    els.generatedPointCardCodes.textContent = "";
    openSettingsModal(els.pointCardModal, els.pointCardPoints);
  });
  els.openMembershipModal?.addEventListener("click", async () => {
    try {
      await loadAdminMemberships();
      openSettingsModal(els.membershipModal, els.membershipName);
    } catch (error) {
      toast(`会员套餐读取失败：${error.message}`, "error");
    }
  });
  [[els.passwordModal, els.closePasswordModal, els.cancelPasswordModal], [els.clientPasswordModal, els.closeClientPasswordModal, els.cancelClientPasswordModal], [els.clientEmailModal, els.closeClientEmailModal, els.cancelClientEmailModal], [els.forgotPasswordModal, els.closeForgotPasswordModal, els.cancelForgotPasswordModal], [els.feedbackModal, els.closeFeedbackModal, els.cancelFeedbackModal], [els.proxyModal, els.closeProxyModal, els.cancelProxyModal], [els.emailModal, els.closeEmailModal, els.cancelEmailModal], [els.modelModal, els.closeModelModal, els.cancelModelModal], [els.packageModal, els.closePackageModal, els.cancelPackageModal], [els.membershipModal, els.closeMembershipModal, els.cancelMembershipModal], [els.pointCardModal, els.closePointCardModal, els.cancelPointCardModal], [els.promptPickerModal, els.closePromptPickerModal, els.cancelPromptPickerModal]].forEach(([modal, closeButton, cancelButton]) => {
    if (closeButton) closeButton.onclick = (event) => {
      event.preventDefault();
      closeSettingsModal(modal);
    };
    if (cancelButton) cancelButton.onclick = (event) => {
      event.preventDefault();
      closeSettingsModal(modal);
    };
    modal?.addEventListener("click", (event) => {
      if (event.target === modal) closeSettingsModal(modal);
    });
  });
  els.packageList?.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    const article = button?.closest("[data-package-id]");
    if (!button || !article) return;
    const item = state.pointPackages.find((entry) => entry.id === article.dataset.packageId);
    if (!item) return;
    setBusy(button, true, "处理中");
    try {
      if (button.dataset.action === "save-package") {
        await apiFetch(`/admin/points/packages/${encodeURIComponent(item.id)}`, { method: "PATCH", body: { name: article.querySelector("[data-package-name]").value.trim(), points: Number(article.querySelector("[data-package-points]").value), bonus_free_uses: Number(article.querySelector("[data-package-bonus]").value), sort_order: Number(article.querySelector("[data-package-sort]").value), payment_url: article.querySelector("[data-package-url]").value.trim() } });
        toast("套餐调整已保存");
      } else if (item.enabled) {
        await apiFetch(`/admin/points/packages/${encodeURIComponent(item.id)}`, { method: "DELETE" });
        toast("套餐已停用");
      } else {
        await apiFetch(`/admin/points/packages/${encodeURIComponent(item.id)}`, { method: "PATCH", body: { enabled: true } });
        toast("套餐已重新上架");
      }
      await loadAdminPointPackages();
    } catch (error) {
      toast(`套餐操作失败：${error.message}`, "error");
    } finally {
      setBusy(button, false);
    }
  });
  els.membershipAdminList?.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    const article = button?.closest("[data-membership-id]");
    if (!button || !article) return;
    const item = state.memberships.find((entry) => entry.id === article.dataset.membershipId);
    if (!item) return;
    setBusy(button, true, "处理中");
    try {
      if (button.dataset.action === "save-membership") {
        await apiFetch(`/admin/memberships/${encodeURIComponent(item.id)}`, { method: "PATCH", body: { name: article.querySelector("[data-membership-name]").value.trim(), points_cost: Number(article.querySelector("[data-membership-points]").value), duration_days: Number(article.querySelector("[data-membership-duration]").value), concurrency: Number(article.querySelector("[data-membership-concurrency]").value), bonus_free_uses: Number(article.querySelector("[data-membership-bonus]").value), sort_order: Number(article.querySelector("[data-membership-sort]").value), description: article.querySelector("[data-membership-description]").value.trim() } });
      } else if (item.enabled) {
        await apiFetch(`/admin/memberships/${encodeURIComponent(item.id)}`, { method: "DELETE" });
      } else {
        await apiFetch(`/admin/memberships/${encodeURIComponent(item.id)}`, { method: "PATCH", body: { enabled: true } });
      }
      await loadAdminMemberships();
      toast("会员套餐已更新");
    } catch (error) {
      toast(`会员套餐操作失败：${error.message}`, "error");
    } finally {
      setBusy(button, false);
    }
  });
  document.querySelectorAll("[data-view-shortcut]").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.viewShortcut));
  });
  els.openMyPrompts?.addEventListener("click", () => {
    state.promptPickerPage = 1;
    renderPromptPicker();
    openSettingsModal(els.promptPickerModal, els.promptPickerList?.querySelector("button"));
  });
  els.promptPickerList?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-prompt-picker-id]");
    if (!button) return;
    const item = state.prompts.find((entry) => entry.id === button.dataset.promptPickerId);
    if (!item) return;
    els.promptInput.value = item.content;
    closeSettingsModal(els.promptPickerModal);
    els.promptInput.focus();
    toast(`已使用“${item.title || "提示词"}”`);
  });
  els.promptPickerPrev?.addEventListener("click", () => { state.promptPickerPage -= 1; renderPromptPicker(); });
  els.promptPickerNext?.addEventListener("click", () => { state.promptPickerPage += 1; renderPromptPicker(); });
  els.saveCurrentPrompt?.addEventListener("click", () => {
    const value = els.promptInput?.value || "";
    addPrompt(value, value.trim().slice(0, 20) || "常用提示词");
  });
  els.addPromptButton?.addEventListener("click", () => openPromptEditor());
  els.cancelPromptEdit?.addEventListener("click", () => els.promptEditor.classList.add("hidden"));
  els.promptEditor?.addEventListener("submit", (event) => {
    event.preventDefault();
    const content = els.promptContentInput.value.trim();
    if (!content) return toast("请输入提示词内容", "error");
    const item = state.prompts.find((entry) => entry.id === state.editingPromptId);
    if (item) Object.assign(item, { title: els.promptTitleInput.value.trim() || "常用提示词", content, updated_at: new Date().toISOString() });
    else state.prompts.unshift({ id: newPromptId(), title: els.promptTitleInput.value.trim() || "常用提示词", content, updated_at: new Date().toISOString() });
    savePrompts();
    els.promptEditor.classList.add("hidden");
    renderPrompts();
    renderPromptPicker();
    toast("提示词已保存", "success");
  });
  els.promptPrevPage?.addEventListener("click", () => { state.promptPage -= 1; renderPrompts(); });
  els.promptNextPage?.addEventListener("click", () => { state.promptPage += 1; renderPrompts(); });
  let draggedPromptId = "";
  els.promptGrid?.addEventListener("dragstart", (event) => { draggedPromptId = event.target.closest("[data-prompt-id]")?.dataset.promptId || ""; });
  els.promptGrid?.addEventListener("dragover", (event) => event.preventDefault());
  els.promptGrid?.addEventListener("drop", (event) => {
    event.preventDefault();
    const targetId = event.target.closest("[data-prompt-id]")?.dataset.promptId;
    if (!draggedPromptId || !targetId || draggedPromptId === targetId) return;
    const from = state.prompts.findIndex((item) => item.id === draggedPromptId);
    const to = state.prompts.findIndex((item) => item.id === targetId);
    state.prompts.splice(to, 0, state.prompts.splice(from, 1)[0]);
    savePrompts(); renderPrompts();
  });
  els.promptGrid?.addEventListener("click", (event) => {
    const card = event.target.closest("[data-prompt-id]");
    const action = event.target.closest("[data-prompt-action]")?.dataset.promptAction;
    if (!card || !action) return;
    const item = state.prompts.find((entry) => entry.id === card.dataset.promptId);
    if (!item) return;
    if (action === "use") { els.promptInput.value = item.content; switchView("submit"); els.promptInput.focus(); }
    if (action === "edit") openPromptEditor(item);
    if (action === "delete" && window.confirm("确认删除这条提示词？")) { state.prompts = state.prompts.filter((entry) => entry.id !== item.id); savePrompts(); renderPrompts(); renderPromptPicker(); }
  });

  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });

  els.refreshTasks.addEventListener("click", () => refreshTasks());
  els.queryVisibleTasks.addEventListener("click", queryVisibleTasks);
  els.deleteFailedTasks?.addEventListener("click", deleteFailedTasks);
  els.clearTasks.addEventListener("click", clearTasks);
  els.loadProxyConfig?.addEventListener("click", async () => {
    setBusy(els.loadProxyConfig, true, "读取中");
    try {
      await loadProxyConfig();
      toast("代理配置已读取");
    } catch (error) {
      toast(`读取失败：${error.message}`, "error");
    } finally {
      setBusy(els.loadProxyConfig, false);
    }
  });
  els.saveProxyConfig?.addEventListener("click", saveProxyConfig);
  els.proxySource?.addEventListener("change", updateProxySourceFields);
  els.refreshProxyNodes?.addEventListener("click", () => loadProxyNodes(true));
  els.proxyEnabledSelect?.addEventListener("change", saveProxyMode);
  els.proxyAutoSelect?.addEventListener("change", saveProxyMode);
  els.proxyCountryFilter?.addEventListener("change", () => { state.proxyCountry = els.proxyCountryFilter.value; renderProxyNodes(); });
  els.proxyNodeGrid?.addEventListener("click", (event) => {
    const card = event.target.closest("[data-proxy-node-id]");
    if (card) selectProxyNode(card.dataset.proxyNodeId);
  });
  els.saveModelConfig?.addEventListener("click", saveModelConfig);
  els.syncQianwenModels?.addEventListener("click", syncQianwenModels);
  els.modelConfigList?.addEventListener("click", (event) => {
    const section = event.target.closest("[data-model-platform]");
    if (!section) return;
    if (event.target.closest("[data-add-model]")) {
      section.querySelector("[data-model-rows]").insertAdjacentHTML("beforeend", '<label class="model-config-row"><input type="checkbox" data-model-enabled checked><input type="text" data-model-name placeholder="模型名称"><span class="model-cost-field"><input type="number" data-model-cost min="0.1" step="0.1" value="1"><em>积分/次</em></span><button class="secondary-button" type="button" data-remove-model>删除</button></label>');
    }
    if (event.target.closest("[data-remove-model]")) event.target.closest(".model-config-row")?.remove();
  });
  els.refreshTempTokens?.addEventListener("click", () => refreshTempTokens());
  els.openCreateTokenModal?.addEventListener("click", openCreateTokenModal);
  els.refreshAccounts?.addEventListener("click", () => refreshAccounts());
  els.platformSelect?.addEventListener("change", () => {
    state.platform = els.platformSelect.value || "dola";
    renderPlatformControls();
  });
  els.modelSelect?.addEventListener("change", () => {
    const [platform, ...parts] = String(els.modelSelect.value || "").split("::");
    state.platform = platform || "dola";
    state.model = parts.join("::");
    applyAccessScope();
    els.platformSelect.value = state.platform;
  });
  els.accountForm?.addEventListener("submit", importAccount);
  els.accountTaskSearch?.addEventListener("input", () => {
    state.accountPage = 1;
    window.clearTimeout(state.accountSearchTimer);
    state.accountSearchTimer = window.setTimeout(() => refreshAccounts({ quiet: true }), 250);
  });
  els.accountPlatformFilter?.addEventListener("change", () => {
    state.accountPlatformFilter = els.accountPlatformFilter.value || "all";
    state.accountPage = 1;
    refreshAccounts({ quiet: true });
  });
  els.prevAccountPage?.addEventListener("click", () => { state.accountPage = Math.max(1, state.accountPage - 1); refreshAccounts({ quiet: true }); });
  els.nextAccountPage?.addEventListener("click", () => { state.accountPage = Math.min(state.accountTotalPages, state.accountPage + 1); refreshAccounts({ quiet: true }); });
  els.accountPageSize?.addEventListener("change", () => { state.accountPageSize = Number(els.accountPageSize.value || 20); state.accountPage = 1; refreshAccounts({ quiet: true }); });
  els.accountPlatform?.addEventListener("change", () => {
    if (els.accountQuotaLimit) els.accountQuotaLimit.value = els.accountPlatform.value === "dola" ? "1" : els.accountPlatform.value === "qianwen" ? "5" : "2";
  });
  els.accountCookieData?.addEventListener("input", updateAccountDetectedCount);
  els.accountCookieData?.addEventListener("paste", () => window.setTimeout(updateAccountDetectedCount, 0));
  els.accountTableBody?.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const id = button.dataset.id;
    const action = button.dataset.action;
    try {
      if (action === "toggle-account") {
        setBusy(button, true, "保存中");
        await toggleAccountById(id);
      }
      if (action === "edit-quota") {
        await editAccountQuota(id);
      }
      if (action === "reset-quota") {
        await resetAccountQuota(id);
      }
      if (action === "delete-account") {
        await deleteAccountById(id);
      }
    } catch (error) {
      toast(`操作失败：${error.message}`, "error");
    } finally {
      if (action === "toggle-account") setBusy(button, false);
    }
  });
  els.editWorkers.addEventListener("click", openWorkersModal);
  els.closeWorkersModal.addEventListener("click", closeWorkersModal);
  els.cancelWorkersModal.addEventListener("click", closeWorkersModal);
  els.workersModal.addEventListener("click", (event) => {
    if (event.target === els.workersModal) closeWorkersModal();
  });
  els.closeTextModal.addEventListener("click", closeTextModal);
  els.confirmTextModal.addEventListener("click", closeTextModal);
  els.copyTextModal.addEventListener("click", async () => {
    await copyText(state.modalText, "完整文本");
  });
  els.textModal.addEventListener("click", (event) => {
    if (event.target === els.textModal) closeTextModal();
  });
  els.closeVideoModal.addEventListener("click", closeVideoModal);
  els.confirmVideoModal.addEventListener("click", closeVideoModal);
  els.copyVideoUrl.addEventListener("click", async () => {
    await copyText(state.modalVideoUrl, "视频 URL");
  });
  els.videoModal.addEventListener("click", (event) => {
    if (event.target === els.videoModal) closeVideoModal();
  });
  els.workersInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") saveWorkersConfig();
    if (event.key === "Escape") closeWorkersModal();
  });
  els.saveWorkers.addEventListener("click", saveWorkersConfig);
  els.closeCreateTokenModal?.addEventListener("click", closeCreateTokenModal);
  els.cancelCreateTokenModal?.addEventListener("click", closeCreateTokenModal);
  els.createTokenModal?.addEventListener("click", (event) => {
    if (event.target === els.createTokenModal) closeCreateTokenModal();
  });
  els.confirmCreateTokens?.addEventListener("click", createTempTokens);
  els.createTokenCount?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") createTempTokens();
    if (event.key === "Escape") closeCreateTokenModal();
  });
  els.createTokenLimit?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") createTempTokens();
    if (event.key === "Escape") closeCreateTokenModal();
  });

  els.tempTokenTableBody?.addEventListener("change", async (event) => {
    const input = event.target.closest("[data-token-remark], [data-token-concurrency], [data-token-retention]");
    if (!input) return;
    const id = input.dataset.tokenRemark || input.dataset.tokenConcurrency || input.dataset.tokenRetention;
    try {
      await saveTempTokenLimit(id);
    } catch (error) {
      toast(`保存失败：${error.message}`, "error");
    }
  });

  els.tempTokenTableBody?.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const id = button.dataset.id;
    const action = button.dataset.action;
    try {
      if (action === "copy-temp-token") {
        const item = state.tempTokens.find((token) => token.id === id);
        if (item?.token) await copyText(item.token, "临时 Token");
      }
      if (action === "save-temp-token") {
        setBusy(button, true, "保存中");
        await saveTempTokenLimit(id);
      }
      if (action === "delete-temp-token") {
        await deleteTempTokenById(id);
      }
    } catch (error) {
      toast(`操作失败：${error.message}`, "error");
    } finally {
      if (action === "save-temp-token") setBusy(button, false);
    }
  });

  els.ratioGroup.addEventListener("click", (event) => {
    const trigger = event.target.closest(".ratio-trigger");
    if (trigger) {
      els.ratioGroup.classList.toggle("open");
      return;
    }

    const button = event.target.closest("button[data-ratio]");
    if (!button) return;
    state.ratio = button.dataset.ratio;
    setSegmentValue(els.ratioGroup, "ratio", state.ratio);
    els.ratioGroup.classList.remove("open");
  });

  document.addEventListener("click", (event) => {
    if (!els.ratioGroup.contains(event.target)) {
      els.ratioGroup.classList.remove("open");
    }
  });

  els.taskForm.addEventListener("submit", submitTask);
  els.resetSubmit?.addEventListener("click", resetSubmitForm);
  els.clearImages?.addEventListener("click", () => {
    if (state.submitting) return;
    state.images = [];
    els.imageInput.value = "";
    renderImages();
  });
  els.imageInput.addEventListener("change", () => {
    if (state.submitting) return;
    setImages(els.imageInput.files).catch((error) => {
      toast(`图片读取失败：${error.message}`, "error");
      els.imageInput.value = "";
    });
  });

  els.taskSearch.addEventListener("input", () => {
    state.page = 1;
    window.clearTimeout(state.taskSearchTimer);
    state.taskSearchTimer = window.setTimeout(() => refreshTasks({ quiet: true, keepPage: true }), 250);
  });
  els.prevPage.addEventListener("click", () => {
    state.page = Math.max(1, state.page - 1);
    refreshTasks({ quiet: true, keepPage: true });
  });
  els.nextPage.addEventListener("click", () => {
    state.page = Math.min(state.taskTotalPages, state.page + 1);
    refreshTasks({ quiet: true, keepPage: true });
  });
  els.taskPageSize?.addEventListener("change", () => {
    state.pageSize = Number.parseInt(els.taskPageSize.value, 10) || 50;
    state.page = 1;
    refreshTasks({ quiet: true, keepPage: true });
  });
  els.prevUserPage?.addEventListener("click", () => { state.userPage = Math.max(1, state.userPage - 1); loadUsers(); });
  els.nextUserPage?.addEventListener("click", () => { state.userPage = Math.min(state.userTotalPages, state.userPage + 1); loadUsers(); });
  els.userPageSize?.addEventListener("change", () => { state.userPageSize = Number.parseInt(els.userPageSize.value, 10) || 20; state.userPage = 1; loadUsers(); });
  const runUserSearch = () => { state.userSearch = els.userSearch?.value.trim() || ""; state.userPage = 1; loadUsers(); };
  els.searchUsers?.addEventListener("click", runUserSearch);
  els.userSearch?.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); runUserSearch(); } });
  els.clearUserSearch?.addEventListener("click", () => { if (els.userSearch) els.userSearch.value = ""; state.userSearch = ""; state.userPage = 1; loadUsers(); });
  els.prevQuotaPage?.addEventListener("click", () => {
    state.quotaPage -= 1;
    renderTempTokenTable();
  });
  els.nextQuotaPage?.addEventListener("click", () => {
    state.quotaPage += 1;
    renderTempTokenTable();
  });
  els.quotaPageSize?.addEventListener("change", () => {
    state.quotaPageSize = Number.parseInt(els.quotaPageSize.value, 10) || 50;
    state.quotaPage = 1;
    renderTempTokenTable();
  });

  els.taskTableBody.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const id = button.dataset.id;
    const action = button.dataset.action;
    if (action === "show-result") {
      const task = state.tasks.find((item) => item.id === id);
      const status = task ? getTaskStatus(task) : null;
      const text = status?.text || task?.error || "";
      openTextModal(text);
      return;
    }
    if (action === "query") {
      setBusy(button, true, "查询中");
      try {
        await queryTask(id);
      } catch (_) {
      } finally {
        setBusy(button, false);
      }
    }
    if (action === "copy-id") {
      await copyText(id, "任务 ID");
    }
    if (action === "copy-prompt") {
      const task = state.tasks.find((item) => item.id === id);
      const prompt = String(task?.prompt || task?.prompt_preview || "").trim();
      if (prompt) await copyText(prompt, "提示词");
    }
    if (action === "copy-url") {
      const task = state.tasks.find((item) => item.id === id);
      const url = task ? getTaskStatus(task).url : "";
      if (url) await copyText(url, "视频 URL");
    }
    if (action === "open-video") {
      const task = state.tasks.find((item) => item.id === id);
      const url = task ? getTaskStatus(task).url : "";
      if (url) openVideoModal(url);
    }
    if (action === "download-video") {
      const task = state.tasks.find((item) => item.id === id);
      const url = task ? getTaskStatus(task).url : "";
      if (url) downloadVideo(url, id);
    }
    if (action === "delete") {
      await deleteTask(id);
    }
  });
  els.videoLibrary?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const id = button.dataset.id;
    if (button.dataset.action === "download-video") {
      const task = state.tasks.find((item) => item.id === id);
      const url = task ? getTaskStatus(task).url : "";
      if (url) downloadVideo(url, id);
    }
    if (button.dataset.action === "delete-video") deleteVideoTasks([id]);
  });
  els.videoLibrary?.addEventListener("change", (event) => {
    const checkbox = event.target.closest("[data-video-select]");
    if (!checkbox) return;
    if (checkbox.checked) state.selectedVideoIds.add(checkbox.dataset.videoSelect);
    else state.selectedVideoIds.delete(checkbox.dataset.videoSelect);
    renderVideoLibrary();
  });
  els.selectAllVideos?.addEventListener("change", () => {
    const cutoff = Date.now() - state.taskRetentionDays * 86400000;
    const ids = state.tasks.filter((task) => {
      const completedAt = Date.parse(task.finished_at || task.updated_at || "");
      const hidden = portal === "client" ? task.video_hidden_for_client : task.video_hidden_for_admin;
      return !hidden && getTaskStatus(task).url && (!Number.isFinite(completedAt) || completedAt >= cutoff);
    }).map((task) => task.id);
    ids.forEach((id) => els.selectAllVideos.checked ? state.selectedVideoIds.add(id) : state.selectedVideoIds.delete(id));
    renderVideoLibrary();
  });
  els.deleteSelectedVideos?.addEventListener("click", () => deleteVideoTasks(Array.from(state.selectedVideoIds)));
}

async function init() {
  const params = new URLSearchParams(window.location.search);
  const tokenFromUrl = params.get("token") || "";
  const savedToken = portal === "client" ? tokenFromUrl || localStorage.getItem(portalStorageKey(TOKEN_KEY)) || "" : "";
  els.loginToken.value = savedToken;

  applyPortalText();
  if (portal === "client") {
    try {
      await loadEmailDomains();
    } catch (error) {
      toast(`邮箱后缀读取失败：${error.message}`, "error");
    }
  }
  bindEvents();
  renderImages();
  renderTaskTable();
  updateAccountDetectedCount();
  updateDashboardMetrics();

  if (sessionStorage.getItem(portalStorageKey(AUTH_KEY)) === "1" && (savedToken || portal === "admin")) {
    try {
      const health = await requestJson(authPath, savedToken);
      state.apiToken = savedToken;
      loadPrompts();
      applyAccessScope(health);
      clearTokenFromUrl();
      showApp();
      await refreshDashboard();
    } catch (error) {
      state.apiToken = "";
      sessionStorage.removeItem(portalStorageKey(AUTH_KEY));
      showLogin("登录已失效");
      toast(`自动登录失败：${error.message}`, "error");
    }
    return;
  }

  showLogin(savedToken ? "等待进入" : "等待输入");
}

init();
