const API = '';
let refreshInterval;
let prevNet = null;
let prevNetTime = null;
let authToken = null;

// ---- WebSocket ----
let ws = null;
let wsConnected = false;
let wsProgressCallbacks = {};

function connectWs() {
    if (ws) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.onopen = () => {
        handleWsConnected();
    };
    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            if (msg.type === 'progress' && wsProgressCallbacks[msg.task]) {
                wsProgressCallbacks[msg.task](msg);
            } else if (msg.type === 'inference_operation') {
                handleInferenceOperationEvent(msg.operation, msg);
            } else if (msg.type === 'model_job') {
                handleModelJobEvent(msg.job, msg);
            }
        } catch (e) {}
    };
    ws.onclose = () => {
        handleWsDisconnected();
        ws = null;
        // Reconnect after a short delay
        setTimeout(() => { if (authToken || document.cookie.includes('inframatik_session')) connectWs(); }, 3000);
    };
    ws.onerror = () => { handleWsDisconnected(); };
}

function onWsProgress(task, callback) {
    wsProgressCallbacks[task] = callback;
}

async function handleWsConnected() {
    wsConnected = true;
    updateInferencePolling();
    await resyncInferenceAfterWsReconnect();
}

function handleWsDisconnected() {
    wsConnected = false;
    updateInferencePolling();
}

async function resyncInferenceAfterWsReconnect() {
    if (currentAppView !== 'inference' || !hasActiveInferenceActivity()) return;
    await refreshInferenceActivity();
}

// ---- Cluster state ----
let isMaster = false;
let nodeRole = null;
let selfNodeId = null;
let selectedNodeId = null;
let nodes = [];
let sidebarInterval = null;
let currentTunnelId = null;
let cfPolicies = [];
let cfSectionLoaded = false;
let machineHostname = window.location.hostname || '';
let installSourceMasterUrl = '';
let workerSkipCf = {
    'setup-worker': false,
    'init-worker': false,
};
let currentAppView = 'main';
let lastSystemData = null;
let refreshInFlight = false;
let refreshQueued = false;
let refreshQueuedForceCf = false;
let refreshGeneration = 0;
let priorityRefreshes = 0;
let inferenceModelData = null;
let inferenceStorageData = null;
let inferenceLaunchersData = [];
let inferenceProfilesData = [];
let inferenceOperationsData = [];
let inferenceSystemData = null;
let pendingInferenceProfileActions = new Map();
let pendingInferenceInstanceActions = new Map();
let inferenceFailureLogFetches = new Set();
let profileDetailCache = new Map();
let profileDetailModes = new Map();
let profileOutputCache = new Map();
let operationLogOutputCache = new Map();
let inferenceJobsTimer = null;
let activeInferenceTab = 'profiles';
const ACTIVE_MODEL_JOB_STATES = new Set(['queued', 'running', 'hashing', 'verifying']);
const ACTIVE_INFERENCE_OPERATION_STATES = new Set(['queued', 'running']);

// ---- Helpers ----

function shouldShowLocalCfSection() {
    return nodeRole === 'master' || nodeRole === 'standalone' || nodeRole === 'worker';
}

function syncAppViewChrome() {
    const configured = nodeRole && nodeRole !== 'unconfigured';
    const settingsAvailable = nodeRole && nodeRole !== 'unconfigured' && nodeRole !== 'worker';
    if (!configured && currentAppView === 'inference') {
        currentAppView = 'main';
    }
    if (!settingsAvailable && currentAppView === 'settings') {
        currentAppView = 'main';
    }

    const mainView = document.getElementById('main-view');
    const inferenceView = document.getElementById('inference-view');
    const settingsView = document.getElementById('settings-view');
    const mainTab = document.getElementById('main-view-tab');
    const inferenceTab = document.getElementById('inference-view-tab');
    const settingsTab = document.getElementById('settings-view-tab');
    const appNav = document.getElementById('app-nav');
    const sidebar = document.getElementById('sidebar');

    if (mainView) mainView.style.display = currentAppView === 'main' ? '' : 'none';
    if (inferenceView) inferenceView.style.display = currentAppView === 'inference' ? '' : 'none';
    if (settingsView) settingsView.style.display = currentAppView === 'settings' ? '' : 'none';
    if (mainTab) {
        if (currentAppView === 'main') mainTab.classList.add('active');
        else mainTab.classList.remove('active');
    }
    if (inferenceTab) {
        if (currentAppView === 'inference') inferenceTab.classList.add('active');
        else inferenceTab.classList.remove('active');
        inferenceTab.style.display = configured ? '' : 'none';
    }
    if (settingsTab) {
        if (currentAppView === 'settings') settingsTab.classList.add('active');
        else settingsTab.classList.remove('active');
        settingsTab.style.display = settingsAvailable ? '' : 'none';
    }
    if (appNav) appNav.style.display = configured ? '' : 'none';
    if (sidebar) {
        if (isMaster && currentAppView === 'main') sidebar.classList.add('visible');
        else sidebar.classList.remove('visible');
    }
}

async function showAppView(view) {
    if (view === 'settings' && nodeRole === 'worker') {
        view = 'main';
    }
    if (view === 'inference' && (!nodeRole || nodeRole === 'unconfigured')) {
        view = 'main';
    }
    currentAppView = view === 'settings' || view === 'inference' ? view : 'main';
    syncAppViewChrome();
    if (currentAppView === 'settings') {
        stopRefreshLoop();
        stopSidebarLoop();
        stopInferencePolling();
        await loadSettingsView();
    } else if (currentAppView === 'inference') {
        stopRefreshLoop();
        stopSidebarLoop();
        await loadInferenceView();
    } else if (selectedNodeId) {
        stopInferencePolling();
        await Promise.all([
            isMaster ? startSidebarLoop() : Promise.resolve(),
            startRefreshLoop(),
        ]);
    }
}

function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return (bytes / Math.pow(1024, i)).toFixed(i > 1 ? 1 : 0) + ' ' + units[i];
}

function formatRate(bytesPerSec) {
    if (bytesPerSec < 1024) return bytesPerSec.toFixed(0) + ' B/s';
    if (bytesPerSec < 1048576) return (bytesPerSec / 1024).toFixed(1) + ' KB/s';
    return (bytesPerSec / 1048576).toFixed(1) + ' MB/s';
}

function formatSeconds(seconds) {
    const value = Number(seconds || 0);
    if (!Number.isFinite(value) || value <= 0) return '0s';
    if (value < 60) return `${Math.round(value)}s`;
    const minutes = Math.floor(value / 60);
    const remainder = Math.round(value % 60);
    if (minutes < 60) return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    const minuteRemainder = minutes % 60;
    return minuteRemainder ? `${hours}h ${minuteRemainder}m` : `${hours}h`;
}

function progressColor(pct) {
    if (pct >= 90) return 'red';
    if (pct >= 70) return 'yellow';
    return 'green';
}

function coreColor(pct) {
    if (pct >= 90) return 'var(--red)';
    if (pct >= 60) return 'var(--yellow)';
    if (pct >= 30) return 'var(--accent)';
    return 'var(--green)';
}

function tempColor(c) {
    if (c >= 85) return 'var(--red)';
    if (c >= 70) return 'var(--yellow)';
    return 'var(--green)';
}

function setHtmlIfChanged(el, html) {
    if (!el || el._inframatikHtml === html) return;
    el.innerHTML = html;
    el._inframatikHtml = html;
}

function setElementHtml(id, html) {
    setHtmlIfChanged(document.getElementById(id), html);
}

function setElementText(id, text) {
    const el = document.getElementById(id);
    const value = String(text ?? '');
    if (!el || el.textContent === value) return;
    el.textContent = value;
    el._inframatikHtml = null;
}

function makeRefreshContext() {
    return {
        generation: ++refreshGeneration,
        nodeId: selectedNodeId,
    };
}

function isRefreshCurrent(context) {
    if (!context) return currentAppView === 'main';
    return currentAppView === 'main'
        && context.generation === refreshGeneration
        && context.nodeId === selectedNodeId;
}

async function api(method, path, body, extraHeaders) {
    const headers = { 'Content-Type': 'application/json', ...extraHeaders };
    if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
    const opts = { method, headers, credentials: 'same-origin' };
    if (body) opts.body = JSON.stringify(body);
    const resp = await fetch(API + path, opts);
    if (resp.status === 401 && !path.startsWith('/api/auth/')) {
        // Session expired or invalid — show login
        showLogin();
        throw new Error('Session expired');
    }
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        const detail = err.detail || 'Request failed';
        const message = typeof detail === 'string'
            ? detail
            : (detail.message || JSON.stringify(detail));
        const error = new Error(message);
        error.detail = detail;
        error.status = resp.status;
        throw error;
    }
    return resp.json();
}

// Build the API path, rewriting through the proxy when viewing a remote node on the master
function nodePathFor(nodeId, path) {
    if (isMaster && nodeId && nodeId !== selfNodeId) {
        // Rewrite /api/system -> /api/nodes/{id}/system
        // Rewrite /api/services -> /api/nodes/{id}/services
        // Rewrite /api/services/foo/start -> /api/nodes/{id}/services/foo/start
        // Rewrite /api/tunnel -> /api/nodes/{id}/tunnel
        // Rewrite /api/models -> /api/nodes/{id}/models
        // Rewrite /api/inference -> /api/nodes/{id}/inference
        // Rewrite /api/ports/next -> /api/ports/next (local only, don't proxy)
        if (path.startsWith('/api/system') || path.startsWith('/api/services') || path.startsWith('/api/tunnel') || path.startsWith('/api/models') || path.startsWith('/api/inference')) {
            // Strip /api prefix: /api/system -> /system, then build /api/nodes/{id}/system
            const subpath = path.slice(4); // remove '/api'
            return `/api/nodes/${nodeId}${subpath}`;
        }
    }
    return path;
}

function nodePath(path) {
    return nodePathFor(selectedNodeId, path);
}

// ---- Tabs ----

document.addEventListener('click', (e) => {
    const profileActionButton = e.target.closest('[data-profile-action]');
    if (profileActionButton) {
        e.preventDefault();
        runProfileAction(
            profileActionButton.dataset.profileId || '',
            profileActionButton.dataset.profileAction || '',
            profileActionButton
        );
        return;
    }
    const instanceActionButton = e.target.closest('[data-instance-action]');
    if (instanceActionButton) {
        e.preventDefault();
        runInstanceAction(
            instanceActionButton.dataset.profileId || '',
            Number(instanceActionButton.dataset.instanceIndex),
            instanceActionButton.dataset.instanceAction || '',
            instanceActionButton
        );
        return;
    }
    if (e.target.classList.contains('tab')) {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
        e.target.classList.add('active');
        const tabName = e.target.dataset.tab;
        const panel = document.getElementById('tab-' + tabName);
        if (panel) panel.classList.add('active');
        renderActiveSystemTab(tabName);
    }
    if (e.target.classList.contains('tunnel-tab')) {
        document.querySelectorAll('.tunnel-tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tunnel-tab-content').forEach(t => {
            t.classList.remove('active');
            t.style.display = 'none';
        });
        e.target.classList.add('active');
        const panel = document.getElementById('tunnel-tab-' + e.target.dataset.tunnelTab);
        panel.classList.add('active');
        panel.style.display = '';
    }
    if (e.target.classList.contains('inference-tab')) {
        setInferenceTab(e.target.dataset.inferenceTab || 'models');
        if (currentAppView === 'inference') refreshActiveInferenceTab();
    }
});

document.addEventListener('change', (e) => {
    if (e.target && e.target.id === 'profile-engine') {
        renderProfileSelects();
        renderProfileEngineFields();
    }
});

document.addEventListener('input', (e) => {
    if (e.target && e.target.id === 'profile-gpus') {
        renderInferenceGpuHints();
    }
});

// ---- Cluster init ----

async function initCluster() {
    try {
        const info = await api('GET', '/api/node/info');
        nodeRole = info.role || null;
        if (info.machine_hostname && typeof info.machine_hostname === 'string') {
            machineHostname = info.machine_hostname.trim();
        }
        installSourceMasterUrl = typeof info.install_source_master_url === 'string'
            ? info.install_source_master_url.trim()
            : '';
        if (info.role === 'unconfigured') {
            // First run — show setup modal
            document.getElementById('setup-modal').classList.add('active');
            syncAppViewChrome();
            connectWs();
            return false;
        }
        if (info.role === 'master') {
            isMaster = true;
            selfNodeId = info.node_id;
            selectedNodeId = info.node_id;
            syncAppViewChrome();
            setElementHtml('topbar-title', `${esc(info.node_name)} <span>/ inframatik</span>`);
            await startSidebarLoop();
            await updateCurrentTunnelId(info.node_id);
        } else if (info.node_name) {
            // Standalone or worker — show node name in topbar
            selfNodeId = info.node_id;
            selectedNodeId = info.node_id;
            setElementHtml('topbar-title', `${esc(info.node_name)} <span>/ inframatik</span>`);
            syncAppViewChrome();
            if (info.role === 'standalone') {
                await updateCurrentTunnelId(info.node_id);
            }
        }
    } catch (e) {
        // Endpoint not available — just continue
    }
    return true;
}

// ---- First-run setup modal ----

let setupRole = null;
let setupCf = { enabled: false, token: null, account_id: null, zone_id: null, zone_name: null, zones: [], default_policy_id: null };

function _hideAllSetupSteps() {
    for (const id of ['setup-choose', 'setup-cf-prompt', 'setup-cf-consent', 'setup-cf-config', 'setup-cf-details', 'setup-name', 'setup-worker']) {
        document.getElementById(id).style.display = 'none';
    }
}

function showSetupChoose() {
    _hideAllSetupSteps();
    document.getElementById('setup-choose').style.display = '';
    setupCf = { enabled: false, token: null, account_id: null, zone_id: null, zone_name: null, zones: [], default_policy_id: null };
}

function showSetupForm(role) {
    setupRole = role;
    _hideAllSetupSteps();

    if (role === 'worker') {
        document.getElementById('setup-worker').style.display = '';
        document.getElementById('setup-worker-name').value = defaultWorkerNodeName();
        resetWorkerMasterFields('setup-worker', installSourceMasterUrl);
        document.getElementById('setup-worker-token').value = '';
        document.getElementById('setup-worker-error').textContent = '';
        setWorkerSkipCf('setup-worker', false);
        hideWorkerEnrollProgress('setup-worker');
        const backBtn = document.getElementById('setup-worker-back-btn');
        const submitBtn = document.getElementById('setup-worker-submit-btn');
        if (backBtn) backBtn.disabled = false;
        if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Register'; }
        setWorkerCfButtonsDisabled('setup-worker', false);
    } else {
        // Standalone or Master → CF prompt
        const title = role === 'master' ? 'Set as Master' : 'Standalone Setup';
        document.getElementById('setup-cf-title').textContent = title;
        document.getElementById('setup-cf-prompt').style.display = '';
    }
}

function showSetupCfPrompt() {
    _hideAllSetupSteps();
    document.getElementById('setup-cf-prompt').style.display = '';
}

function showSetupCfToken() {
    // "Connect Cloudflare" clicked — show consent first
    _hideAllSetupSteps();
    document.getElementById('setup-cf-consent').style.display = '';
}

function showSetupCfConsent() {
    _hideAllSetupSteps();
    document.getElementById('setup-cf-consent').style.display = '';
    // Reset to initial state
    document.getElementById('setup-cf-install-info').style.display = '';
    document.getElementById('setup-cf-install-progress').style.display = 'none';
    document.getElementById('setup-cf-install-error').textContent = '';
    const btn = document.getElementById('setup-cf-install-btn');
    btn.disabled = false;
    btn.innerHTML = 'Install &amp; Continue';
    document.getElementById('setup-cf-install-back').disabled = false;
}

async function installCloudflared() {
    const btn = document.getElementById('setup-cf-install-btn');
    const backBtn = document.getElementById('setup-cf-install-back');
    const errEl = document.getElementById('setup-cf-install-error');
    const statusEl = document.getElementById('setup-cf-install-status');
    const barEl = document.getElementById('setup-cf-install-bar');
    errEl.textContent = '';

    // Switch to progress view
    document.getElementById('setup-cf-install-info').style.display = 'none';
    document.getElementById('setup-cf-install-progress').style.display = '';
    btn.disabled = true;
    btn.textContent = 'Installing...';
    backBtn.disabled = true;

    // Animate progress bar (indeterminate-ish since we don't know exact progress)
    let progress = 0;
    const progressTimer = setInterval(() => {
        progress = Math.min(progress + (90 - progress) * 0.08, 90);
        barEl.style.width = progress + '%';
    }, 200);

    // Listen for WebSocket progress
    onWsProgress('cloudflared-install', (msg) => {
        statusEl.textContent = msg.message;
        if (msg.done && !msg.error) {
            clearInterval(progressTimer);
            barEl.style.width = '100%';
        }
    });

    try {
        await api('POST', '/api/cf/service/install');
        delete wsProgressCallbacks['cloudflared-install'];
        clearInterval(progressTimer);
        barEl.style.width = '100%';
        statusEl.textContent = 'Installed!';
        await new Promise(r => setTimeout(r, 600));
        showSetupCfTokenStep();
    } catch (e) {
        delete wsProgressCallbacks['cloudflared-install'];
        clearInterval(progressTimer);
        barEl.style.width = '0%';
        document.getElementById('setup-cf-install-progress').style.display = 'none';
        document.getElementById('setup-cf-install-info').style.display = '';
        errEl.textContent = e.message;
        btn.disabled = false;
        btn.innerHTML = 'Install &amp; Continue';
        backBtn.disabled = false;
    }
}

function showSetupCfTokenStep() {
    _hideAllSetupSteps();
    document.getElementById('setup-cf-config').style.display = '';
    document.getElementById('setup-cf-token').value = '';
    document.getElementById('setup-cf-token').disabled = false;
    document.getElementById('setup-cf-error').textContent = '';
    document.getElementById('setup-cf-perms').innerHTML = '';
    const btn = document.getElementById('setup-cf-validate-btn');
    btn.disabled = false;
    btn.textContent = 'Validate';
}

function skipSetupCf() {
    setupCf.enabled = false;
    showSetupNameStep();
}

function _renderPermCheck(permsEl, name, status) {
    // status: 'pending', 'pass', 'fail'
    const icon = status === 'pass' ? '✓' : status === 'fail' ? '✗' : '·';
    const existing = permsEl.querySelector(`[data-perm="${name}"]`);
    const html = `<div class="perm-item ${status}" data-perm="${name}"><span class="perm-icon">${icon}</span>${esc(name)}</div>`;
    if (existing) {
        existing.outerHTML = html;
    } else {
        permsEl.insertAdjacentHTML('beforeend', html);
    }
}

async function validateSetupCfToken() {
    const token = document.getElementById('setup-cf-token').value.trim();
    const errEl = document.getElementById('setup-cf-error');
    const permsEl = document.getElementById('setup-cf-perms');
    const btn = document.getElementById('setup-cf-validate-btn');
    errEl.textContent = '';

    if (!token) { errEl.textContent = 'API token is required.'; return; }

    btn.disabled = true;
    btn.textContent = 'Validating...';
    document.getElementById('setup-cf-token').disabled = true;

    // Show pending permission checks
    permsEl.innerHTML = '<div class="perm-list"></div>';
    const listEl = permsEl.querySelector('.perm-list');
    _renderPermCheck(listEl, 'Cloudflare Tunnel: Edit', 'pending');
    _renderPermCheck(listEl, 'Zone DNS: Edit', 'pending');
    _renderPermCheck(listEl, 'Access: Apps and Policies: Edit', 'pending');

    try {
        const data = await api('POST', '/api/cf/setup/validate-token', { token });

        // All permissions passed (validate-token checks all three)
        _renderPermCheck(listEl, 'Cloudflare Tunnel: Edit', 'pass');
        _renderPermCheck(listEl, 'Zone DNS: Edit', 'pass');
        _renderPermCheck(listEl, 'Access: Apps and Policies: Edit', 'pass');

        setupCf.token = token;

        // Auto-select account if only one
        if (data.accounts.length === 1) {
            setupCf.account_id = data.accounts[0].id;
        } else {
            // Multiple accounts — let user pick, then advance
            setupCf._accounts = data.accounts;
            permsEl.insertAdjacentHTML('beforeend', `
                <div class="form-group" style="margin-top:12px">
                    <label>Account</label>
                    <select id="setup-cf-account">
                        ${data.accounts.map(a => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('')}
                    </select>
                </div>`);
            btn.textContent = 'Continue';
            btn.disabled = false;
            btn.onclick = () => {
                setupCf.account_id = document.getElementById('setup-cf-account').value;
                showSetupCfDetails();
            };
            return;
        }

        // Auto-advance after short delay so user sees the green checks
        btn.textContent = 'Continuing...';
        await new Promise(r => setTimeout(r, 800));
        await showSetupCfDetails();
    } catch (e) {
        // Parse permission failures from error message
        const msg = e.message || '';
        if (msg.includes('missing permissions')) {
            const missing = msg.split('missing permissions: ')[1] || '';
            const missingList = missing.split(', ');
            for (const perm of ['Cloudflare Tunnel: Edit', 'Zone DNS: Edit', 'Access: Apps and Policies: Edit']) {
                const failed = missingList.some(m => perm.toLowerCase().includes(m.toLowerCase()) || m.toLowerCase().includes(perm.toLowerCase()));
                _renderPermCheck(listEl, perm, failed ? 'fail' : 'pass');
            }
            errEl.textContent = 'Token is missing required permissions. Update it in the Cloudflare dashboard and try again.';
        } else {
            _renderPermCheck(listEl, 'Cloudflare Tunnel: Edit', 'fail');
            _renderPermCheck(listEl, 'Zone DNS: Edit', 'fail');
            _renderPermCheck(listEl, 'Access: Apps and Policies: Edit', 'fail');
            errEl.textContent = e.message;
        }
        document.getElementById('setup-cf-token').disabled = false;
        btn.disabled = false;
        btn.textContent = 'Validate';
    }
}

async function showSetupCfDetails() {
    _hideAllSetupSteps();
    document.getElementById('setup-cf-details').style.display = '';
    document.getElementById('setup-cf-details-error').textContent = '';
    document.getElementById('setup-cf-details-warning').textContent = '';
    document.getElementById('setup-cf-admin-email').value = '';

    const policyEl = document.getElementById('setup-cf-policy');
    policyEl.innerHTML = '<option>Loading...</option>';

    try {
        // Fetch zones (needed for later hostname step)
        const data = await api('POST', '/api/cf/setup/zones', {
            token: setupCf.token, account_id: setupCf.account_id,
        });
        if (!data.zones || data.zones.length === 0) {
            document.getElementById('setup-cf-details-error').textContent = 'No domains found. Add a domain in Cloudflare first.';
            return;
        }
        setupCf.zones = data.zones;
        // Auto-select first zone (user picks specific domain in the node name step)
        setupCf.zone_id = data.zones[0].id;
        setupCf.zone_name = data.zones[0].name;

        // Fetch policies
        let policies = [];
        try {
            const pData = await api('POST', '/api/cf/setup/policies', {
                token: setupCf.token, account_id: setupCf.account_id,
            });
            policies = pData.policies || [];
        } catch (e) { /* optional */ }
        setupCf._policies = policies;

        // Default to "Create new" — always require Access protection
        policyEl.innerHTML =
            '<option value="__create__">Create new policy for my email</option>' +
            policies.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');

        // Live policy coverage check
        policyEl.addEventListener('change', _checkSetupPolicyCoverage);
        document.getElementById('setup-cf-admin-email').addEventListener('input', _checkSetupPolicyCoverage);
    } catch (e) {
        document.getElementById('setup-cf-details-error').textContent = e.message;
    }
}

function _policyCoversEmail(policy, email) {
    if (!email || !policy.include) return false;
    const lower = email.toLowerCase();
    const domain = lower.split('@')[1] || '';
    for (const rule of policy.include) {
        if (rule.email && rule.email.email && rule.email.email.toLowerCase() === lower) return true;
        if (rule.email_domain && rule.email_domain.domain && domain.endsWith(rule.email_domain.domain.toLowerCase())) return true;
    }
    return false;
}

// loadSetupCfZones removed — replaced by showSetupCfDetails()

function _checkSetupPolicyCoverage() {
    const warnEl = document.getElementById('setup-cf-details-warning');
    if (!warnEl) return;
    warnEl.textContent = '';

    const email = (document.getElementById('setup-cf-admin-email').value || '').trim();
    const policyId = (document.getElementById('setup-cf-policy').value || '');

    if (!email || !policyId || policyId === '__create__' || policyId === '') return;

    const policy = (setupCf._policies || []).find(p => p.id === policyId);
    if (policy && !_policyCoversEmail(policy, email)) {
        warnEl.textContent = `Warning: "${policy.name}" does not include ${email}. You may be locked out.`;
    }
}

async function finishSetupCf() {
    const errEl = document.getElementById('setup-cf-details-error');
    errEl.textContent = '';

    const email = (document.getElementById('setup-cf-admin-email').value || '').trim();
    if (!email || !email.includes('@')) {
        errEl.textContent = 'A valid admin email is required.';
        return;
    }
    setupCf.admin_email = email;

    // zone_id and zone_name already set by showSetupCfDetails()
    let policyId = document.getElementById('setup-cf-policy').value;

    if (policyId === '__create__') {
        // Create a simple policy allowing this admin email
        try {
            const data = await api('POST', '/api/cf/setup/create-policy', {
                token: setupCf.token, account_id: setupCf.account_id,
                name: 'inframatik admin',
                email_domain: email.split('@')[1],
            });
            policyId = data.id;
        } catch (e) {
            errEl.textContent = 'Failed to create policy: ' + e.message;
            return;
        }
    } else if (policyId) {
        // Check coverage one more time
        const policy = (setupCf._policies || []).find(p => p.id === policyId);
        if (policy && !_policyCoversEmail(policy, email)) {
            errEl.textContent = `"${policy.name}" does not cover ${email}. Choose a different policy or create a new one.`;
            return;
        }
    }

    setupCf.default_policy_id = policyId || null;
    setupCf.enabled = true;
    showSetupNameStep();
}

function showSetupNameStep() {
    _hideAllSetupSteps();
    document.getElementById('setup-name').style.display = '';
    document.getElementById('setup-error').textContent = '';

    const title = setupRole === 'master' ? 'Set as Master' : 'Standalone Setup';
    document.getElementById('setup-name-title').textContent = title;

    if (setupCf.enabled && setupCf.zones.length > 0) {
        // Show name + domain picker
        document.getElementById('setup-name-with-domain').style.display = '';
        document.getElementById('setup-name-plain').style.display = 'none';

        // Prefill with machine hostname
        const defaultName = (machineHostname || '').split('.')[0].toLowerCase().replace(/[^a-z0-9-]/g, '') || '';
        document.getElementById('setup-node-name').value = defaultName;

        const domainSel = document.getElementById('setup-domain-select');
        domainSel.innerHTML = setupCf.zones.map(z =>
            `<option value="${esc(z.name)}" ${z.id === setupCf.zone_id ? 'selected' : ''}>${esc(z.name)}</option>`
        ).join('');

        // Live preview
        const updatePreview = () => {
            const name = document.getElementById('setup-node-name').value.trim();
            const domain = domainSel.value;
            const preview = document.getElementById('setup-hostname-preview');
            preview.textContent = name ? `Dashboard: ${name}.${domain}` : '';
        };
        document.getElementById('setup-node-name').addEventListener('input', updatePreview);
        domainSel.addEventListener('change', updatePreview);
        updatePreview(); // Show preview immediately with prefilled value
    } else {
        // Plain name, no domain
        document.getElementById('setup-name-with-domain').style.display = 'none';
        document.getElementById('setup-name-plain').style.display = '';
        document.getElementById('setup-node-name-plain').value = '';
    }
}

function setupNameBack() {
    showSetupCfPrompt();
}

const WORKER_ENROLL_PROGRESS = {
    contacting_master: 10,
    saving_worker_config: 25,
    saving_cloudflare_config: 38,
    skipping_cloudflare: 45,
    creating_tunnel: 50,
    initializing_tunnel: 58,
    getting_token: 66,
    installing_cloudflared: 80,
    cloudflared_ready: 88,
    reporting_master: 94,
    complete: 100,
};

function showWorkerEnrollProgress(prefix, message, pct) {
    const progressEl = document.getElementById(`${prefix}-progress`);
    const textEl = document.getElementById(`${prefix}-progress-text`);
    const barEl = document.getElementById(`${prefix}-progress-bar`);
    if (!progressEl || !textEl || !barEl) return;
    progressEl.style.display = '';
    textEl.textContent = message;
    if (pct !== undefined) barEl.style.width = pct + '%';
}

function hideWorkerEnrollProgress(prefix) {
    const progressEl = document.getElementById(`${prefix}-progress`);
    const barEl = document.getElementById(`${prefix}-progress-bar`);
    if (progressEl) progressEl.style.display = 'none';
    if (barEl) barEl.style.width = '0%';
}

function bindWorkerEnrollProgress(prefix) {
    let fallbackPct = 10;
    onWsProgress('worker-enroll', (msg) => {
        const mappedPct = WORKER_ENROLL_PROGRESS[msg.step];
        fallbackPct = mappedPct !== undefined ? mappedPct : Math.min(fallbackPct + 8, 95);
        showWorkerEnrollProgress(prefix, msg.message, msg.done && !msg.error ? 100 : fallbackPct);
    });
}

function setWorkerSkipCf(prefix, skip) {
    workerSkipCf[prefix] = Boolean(skip);
    const useCfBtn = document.getElementById(`${prefix}-use-cf-btn`);
    const localOnlyBtn = document.getElementById(`${prefix}-local-only-btn`);
    if (useCfBtn) {
        useCfBtn.classList.toggle('primary', !workerSkipCf[prefix]);
        useCfBtn.setAttribute('aria-pressed', String(!workerSkipCf[prefix]));
    }
    if (localOnlyBtn) {
        localOnlyBtn.classList.toggle('primary', workerSkipCf[prefix]);
        localOnlyBtn.setAttribute('aria-pressed', String(workerSkipCf[prefix]));
    }
}

function setWorkerCfButtonsDisabled(prefix, disabled) {
    const useCfBtn = document.getElementById(`${prefix}-use-cf-btn`);
    const localOnlyBtn = document.getElementById(`${prefix}-local-only-btn`);
    if (useCfBtn) useCfBtn.disabled = disabled;
    if (localOnlyBtn) localOnlyBtn.disabled = disabled;
}

function defaultWorkerNodeName() {
    return (machineHostname || '').split('.')[0].trim();
}

function splitMasterUrlForFields(masterUrl) {
    try {
        const parsed = new URL(masterUrl);
        if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) return null;
        return {
            host: parsed.hostname,
            port: parsed.port || (parsed.protocol === 'https:' ? '443' : '80'),
        };
    } catch (e) {
        return null;
    }
}

function resetWorkerMasterFields(prefix, masterUrl = '') {
    const hostEl = document.getElementById(`${prefix}-master-host`);
    const portEl = document.getElementById(`${prefix}-master-port`);
    const source = splitMasterUrlForFields(masterUrl);
    if (hostEl) hostEl.value = source ? source.host : '';
    if (portEl) portEl.value = source ? source.port : '9000';
}

function getWorkerMasterUrl(prefix) {
    const hostEl = document.getElementById(`${prefix}-master-host`);
    const portEl = document.getElementById(`${prefix}-master-port`);
    let host = (hostEl?.value || '').trim();
    let port = (portEl?.value || '').trim() || '9000';
    let scheme = 'http';

    if (!host) {
        return { error: 'Master IP or hostname is required.' };
    }

    if (/^https?:\/\//i.test(host)) {
        try {
            const parsed = new URL(host);
            scheme = parsed.protocol.replace(':', '').toLowerCase();
            host = parsed.hostname;
            if (parsed.port) port = parsed.port;
            if ((parsed.pathname && parsed.pathname !== '/') || parsed.search || parsed.hash) {
                return { error: 'Enter only the master IP or hostname and port.' };
            }
        } catch (e) {
            return { error: 'Enter a valid master IP or hostname.' };
        }
    } else {
        host = host.replace(/^\/+|\/+$/g, '');
        if (/[/?#]/.test(host)) {
            return { error: 'Enter only the master IP or hostname and port.' };
        }

        const bracketedIpv6 = host.match(/^\[([^\]]+)\](?::(\d+))?$/);
        const hostWithPort = host.match(/^([^:]+):(\d+)$/);
        if (bracketedIpv6) {
            host = bracketedIpv6[1];
            if (bracketedIpv6[2]) port = bracketedIpv6[2];
        } else if (hostWithPort) {
            host = hostWithPort[1];
            port = hostWithPort[2];
        }
    }

    if (!/^\d+$/.test(port)) {
        return { error: 'Master port must be a number.' };
    }
    const portNum = Number(port);
    if (portNum < 1 || portNum > 65535) {
        return { error: 'Master port must be between 1 and 65535.' };
    }

    const hostForUrl = host.includes(':') && !host.startsWith('[') ? `[${host}]` : host;
    return { url: `${scheme}://${hostForUrl}:${portNum}` };
}

async function submitSetupFinal() {
    const errEl = document.getElementById('setup-error');
    errEl.textContent = '';

    // Get node name
    let name;
    if (setupCf.enabled && setupCf.zones.length > 0) {
        name = document.getElementById('setup-node-name').value.trim();
    } else {
        name = document.getElementById('setup-node-name-plain').value.trim();
    }
    if (!name) { errEl.textContent = 'Node name is required.'; return; }

    const endpoint = setupRole === 'master'
        ? '/api/config/init-master'
        : '/api/config/init-standalone';

    const submitBtn = document.getElementById('setup-submit-btn');
    const backBtn = document.getElementById('setup-name-back-btn');
    const progressEl = document.getElementById('setup-progress');
    const progressText = document.getElementById('setup-progress-text');
    const progressBar = document.getElementById('setup-progress-bar');

    submitBtn.disabled = true;
    submitBtn.textContent = 'Setting up...';
    backBtn.disabled = true;

    function showProgress(msg, pct) {
        progressEl.style.display = '';
        progressText.textContent = msg;
        if (pct !== undefined) progressBar.style.width = pct + '%';
    }

    function hideProgress() {
        progressEl.style.display = 'none';
        progressBar.style.width = '0%';
    }

    try {
        showProgress('Creating node...', 10);
        await api('POST', endpoint, { name });

        if (setupCf.enabled) {
            try {
                showProgress('Saving Cloudflare configuration...', 20);
                await api('POST', '/api/cf/setup/save', {
                    token: setupCf.token,
                    account_id: setupCf.account_id,
                    zone_id: setupCf.zone_id,
                    default_policy_id: setupCf.default_policy_id,
                });

                const domain = document.getElementById('setup-domain-select').value;
                const hostname = `${name}.${domain}`;

                // Listen for progress via WebSocket
                let stepCount = 0;
                onWsProgress('dashboard-access', (msg) => {
                    stepCount++;
                    const pct = Math.min(30 + stepCount * 10, 90);
                    showProgress(msg.message, pct);
                });

                showProgress('Setting up dashboard access...', 30);
                await api('POST', '/api/config/dashboard-access', { hostname });
                delete wsProgressCallbacks['dashboard-access'];
            } catch (cfErr) {
                delete wsProgressCallbacks['dashboard-access'];
                hideProgress();
                errEl.textContent = 'Note: Cloudflare setup failed (' + cfErr.message + '). You can configure it in Settings.';
                await new Promise(r => setTimeout(r, 3000));
            }
        }

        showProgress('Done! Redirecting to dashboard...', 100);
        await new Promise(r => setTimeout(r, 1000));
        location.reload();
    } catch (e) {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Get Started';
        backBtn.disabled = false;
        hideProgress();
        errEl.textContent = e.message;
    }
}

async function submitSetupWorker() {
    const name = document.getElementById('setup-worker-name').value.trim();
    const token = document.getElementById('setup-worker-token').value.trim();
    const errEl = document.getElementById('setup-worker-error');
    errEl.textContent = '';
    const masterAddress = getWorkerMasterUrl('setup-worker');
    if (!name || !token) { errEl.textContent = 'All fields are required.'; return; }
    if (masterAddress.error) { errEl.textContent = masterAddress.error; return; }

    const regBtn = document.getElementById('setup-worker-submit-btn');
    const backBtn = document.getElementById('setup-worker-back-btn');
    if (regBtn) { regBtn.disabled = true; regBtn.textContent = 'Registering...'; }
    if (backBtn) backBtn.disabled = true;
    setWorkerCfButtonsDisabled('setup-worker', true);
    bindWorkerEnrollProgress('setup-worker');
    showWorkerEnrollProgress('setup-worker', 'Contacting master...', 5);

    try {
        const result = await api('POST', '/api/config/enroll-worker', {
            name,
            master_url: masterAddress.url,
            token,
            skip_cf: workerSkipCf['setup-worker'],
        });
        delete wsProgressCallbacks['worker-enroll'];
        if (result.cf_tunnel_error) {
            showWorkerEnrollProgress('setup-worker', 'Registered, but Cloudflare tunnel setup needs attention.', 100);
            errEl.textContent = 'Registered, but Cloudflare tunnel setup needs attention: ' + result.cf_tunnel_error;
            setTimeout(() => location.reload(), 3000);
            return;
        }
        showWorkerEnrollProgress('setup-worker', 'Done! Redirecting to dashboard...', 100);
        await new Promise(r => setTimeout(r, 600));
        location.reload();
    } catch (e) {
        delete wsProgressCallbacks['worker-enroll'];
        hideWorkerEnrollProgress('setup-worker');
        errEl.textContent = e.message;
        if (regBtn) { regBtn.disabled = false; regBtn.textContent = 'Register'; }
        if (backBtn) backBtn.disabled = false;
        setWorkerCfButtonsDisabled('setup-worker', false);
    }
}

async function refreshSidebar() {
    if (!isMaster) return;
    try {
        nodes = await api('GET', '/api/nodes');
        renderSidebar(nodes);
        if (currentAppView === 'inference') renderInferenceNodePicker();
    } catch (e) {
        console.error('Failed to fetch nodes:', e);
    }
}

function stopSidebarLoop() {
    if (sidebarInterval) {
        clearInterval(sidebarInterval);
        sidebarInterval = null;
    }
}

function startSidebarLoop() {
    if (!isMaster) return Promise.resolve();
    stopSidebarLoop();
    const initial = refreshSidebar();
    sidebarInterval = setInterval(refreshSidebar, 15000);
    return initial;
}

function renderSidebar(nodeList) {
    const el = document.getElementById('sidebar-nodes');
    setHtmlIfChanged(el, nodeList.map(node => {
        const isSelected = node.node_id === selectedNodeId;
        const statusClass = node.status === 'online' ? 'green' : 'red';
        const tag = node.is_self ? 'local' : '';
        return `
        <div class="sidebar-node ${isSelected ? 'active' : ''}" onclick="selectNode('${esc(node.node_id)}')">
            <span class="status-dot ${statusClass}"></span>
            <span class="sidebar-node-name">${esc(node.node_name)}</span>
            ${tag ? `<span class="sidebar-node-tag">${tag}</span>` : ''}
        </div>`;
    }).join(''));
}

function selectNode(nodeId) {
    if (nodeId === selectedNodeId) return;
    selectedNodeId = nodeId;

    // Reset network rate state so we don't show bogus deltas
    prevNet = null;
    prevNetTime = null;

    // Update sidebar highlight
    renderSidebar(nodes);

    // Update topbar title
    const node = nodes.find(n => n.node_id === nodeId);
    if (node) {
        setElementHtml('topbar-title', `${esc(node.node_name)} <span>/ inframatik</span>`);
    }

    cfSectionLoaded = false;
    if (currentAppView === 'inference') {
        showInferencePendingState();
        loadInferenceView();
    } else {
        showNodeSwitchPendingState();
        refreshAll({ forceCf: true, priority: true });
    }
}

function showNodeSwitchPendingState() {
    setElementText('uptime', 'Loading...');
    setElementHtml('host-bar', '<span>Loading node...</span>');
    setElementHtml('services-list', '<div class="empty-state">Loading services...</div>');

    const tunnelDot = document.getElementById('tunnel-dot');
    const tunnelText = document.getElementById('tunnel-text');
    if (tunnelDot) tunnelDot.className = 'status-dot yellow';
    if (tunnelText) tunnelText.textContent = 'Tunnel: loading...';

    const cfStatus = document.getElementById('cf-tunnel-status');
    if (cfStatus) {
        cfStatus.textContent = 'Loading';
        cfStatus.style.color = 'var(--yellow)';
    }
}

// ---- System metrics ----

async function refreshSystem(context = null) {
    try {
        const path = nodePathFor(context ? context.nodeId : selectedNodeId, '/api/system');
        const data = await api('GET', path);
        if (isRefreshCurrent(context)) renderSystem(data);
        return data;
    } catch (e) {
        console.error('Failed to fetch system metrics:', e);
        return null;
    }
}

function renderSystem(d) {
    lastSystemData = d;
    setElementText('uptime', d.uptime);

    // Host info bar
    if (d.host) {
        setElementHtml('host-bar',
            `<span>${d.host.distro}</span>` +
            `<span>${d.host.cpu_model}</span>` +
            `<span>${d.cpu.count} cores</span>` +
            `<span>${formatBytes(d.memory.total)} RAM</span>`);
    }

    // CPU
    setElementHtml('cpu-value', `${d.cpu.percent}<span class="unit">%</span>`);
    setElementText('cpu-sub', `${d.cpu.count} cores @ ${d.cpu.freq_mhz || '?'} MHz`);
    const cpuBar = document.getElementById('cpu-bar');
    cpuBar.style.width = d.cpu.percent + '%';
    cpuBar.className = 'progress-fill ' + progressColor(d.cpu.percent);

    // CPU per-core
    const coresEl = document.getElementById('cpu-cores');
    if (d.cpu.per_cpu) {
        setHtmlIfChanged(coresEl, d.cpu.per_cpu.map(pct =>
            `<div class="cpu-core-bar" style="height:${Math.max(pct, 3)}%;background:${coreColor(pct)}" title="${pct}%"></div>`
        ).join(''));
    }

    // Memory
    setElementHtml('mem-value', `${d.memory.percent}<span class="unit">%</span>`);
    setElementText('mem-sub', `${formatBytes(d.memory.used)} / ${formatBytes(d.memory.total)}`);
    const memBar = document.getElementById('mem-bar');
    memBar.style.width = d.memory.percent + '%';
    memBar.className = 'progress-fill ' + progressColor(d.memory.percent);

    // Disk (primary /)
    const rootDisk = (d.disks || []).find(dk => dk.mount === '/');
    if (rootDisk) {
        setElementHtml('disk-value', `${rootDisk.percent}<span class="unit">%</span>`);
        setElementText('disk-sub', `${formatBytes(rootDisk.used)} / ${formatBytes(rootDisk.total)}`);
        const diskBar = document.getElementById('disk-bar');
        diskBar.style.width = rootDisk.percent + '%';
        diskBar.className = 'progress-fill ' + progressColor(rootDisk.percent);
    }

    // Network rate
    const now = Date.now();
    if (prevNet && prevNetTime) {
        const dt = (now - prevNetTime) / 1000;
        if (dt > 0) {
            const upRate = (d.network.bytes_sent - prevNet.bytes_sent) / dt;
            const downRate = (d.network.bytes_recv - prevNet.bytes_recv) / dt;
            setElementHtml('net-rate', `<span style="font-size:16px">&darr;</span> ${formatRate(downRate)}`);
            setElementHtml('net-rate-sub', `<span>&uarr;</span> ${formatRate(upRate)} &middot; ${formatBytes(d.network.bytes_recv)} total`);
        }
    } else {
        setElementHtml('net-rate', `<span style="font-size:16px">&darr;</span> ${formatBytes(d.network.bytes_recv)}`);
        setElementHtml('net-rate-sub', `<span>&uarr;</span> ${formatBytes(d.network.bytes_sent)} total`);
    }
    prevNet = d.network;
    prevNetTime = now;

    // Load
    setElementText('load-value', d.load['1min'].toFixed(2));
    setElementText('load-sub', `${d.load['5min'].toFixed(2)} / ${d.load['15min'].toFixed(2)} (5m/15m)`);

    // Temperatures
    if (d.temps && d.temps.cpu !== undefined) {
        const cpuTemp = d.temps.cpu;
        setElementHtml('temp-value', `${cpuTemp.toFixed(0)}<span class="unit">&deg;C</span>`);
        let sub = `CPU ${cpuTemp.toFixed(1)}&deg;C`;
        if (d.temps.nvme !== undefined) sub += ` &middot; NVMe ${d.temps.nvme.toFixed(0)}&deg;C`;
        setElementHtml('temp-sub', sub);
    }

    renderActiveSystemTab();
}

function getActiveSystemTab() {
    const tab = document.querySelector('.tab.active');
    if (tab && tab.dataset && tab.dataset.tab) return tab.dataset.tab;
    return 'overview';
}

function renderActiveSystemTab(tabName) {
    if (!lastSystemData) return;

    const activeTab = tabName || getActiveSystemTab();
    if (activeTab === 'gpus') {
        renderGpus(lastSystemData.gpus || []);
    } else if (activeTab === 'processes') {
        renderProcesses(lastSystemData.processes || []);
    } else if (activeTab === 'network') {
        renderNetInterfaces((lastSystemData.network && lastSystemData.network.interfaces) || []);
    } else if (activeTab === 'storage') {
        renderStorage(lastSystemData.disks || []);
    }
}

// ---- GPUs ----

function renderGpus(gpus) {
    const el = document.getElementById('gpu-cards');
    if (gpus.length === 0) {
        setHtmlIfChanged(el, '<div class="empty-state">No GPUs detected</div>');
        return;
    }
    setHtmlIfChanged(el, gpus.map(gpu => {
        const memPct = gpu.mem_total_mb > 0 ? (gpu.mem_used_mb / gpu.mem_total_mb * 100) : 0;
        return `
        <div class="metric-card gpu-card">
            <div class="metric-label">GPU ${gpu.index} — ${gpu.name}</div>
            <div class="gpu-stats">
                <div class="gpu-stat">
                    <span class="gpu-stat-label">Util</span>
                    <span class="gpu-stat-value">${gpu.util_percent}%</span>
                    <div class="progress-bar"><div class="progress-fill ${progressColor(gpu.util_percent)}" style="width:${gpu.util_percent}%"></div></div>
                </div>
                <div class="gpu-stat">
                    <span class="gpu-stat-label">VRAM</span>
                    <span class="gpu-stat-value">${formatBytes(gpu.mem_used_mb * 1048576)} / ${formatBytes(gpu.mem_total_mb * 1048576)}</span>
                    <div class="progress-bar"><div class="progress-fill ${progressColor(memPct)}" style="width:${memPct}%"></div></div>
                </div>
                <div class="gpu-stat-row">
                    <span style="color:${tempColor(gpu.temp_c)}">${gpu.temp_c}&deg;C</span>
                    <span>${gpu.power_w.toFixed(0)}W</span>
                </div>
            </div>
        </div>`;
    }).join(''));
}

// ---- Processes ----

function renderProcesses(procs) {
    const el = document.getElementById('process-table');
    if (procs.length === 0) {
        setHtmlIfChanged(el, '<div class="empty-state">No process data</div>');
        return;
    }
    setHtmlIfChanged(el, `
        <div class="proc-header">
            <span class="proc-pid">PID</span>
            <span class="proc-name">Name</span>
            <span class="proc-cpu">CPU %</span>
            <span class="proc-mem">MEM %</span>
        </div>
        ${procs.map(p => `
        <div class="proc-row">
            <span class="proc-pid">${p.pid}</span>
            <span class="proc-name">${esc(p.name)}</span>
            <span class="proc-cpu">${p.cpu.toFixed(1)}</span>
            <span class="proc-mem">${p.mem.toFixed(1)}</span>
        </div>`).join('')}
    `);
}

// ---- Network interfaces ----

function renderNetInterfaces(interfaces) {
    const el = document.getElementById('net-cards');
    if (interfaces.length === 0) {
        setHtmlIfChanged(el, '<div class="empty-state">No active network interfaces</div>');
        return;
    }
    setHtmlIfChanged(el, interfaces.map(iface => `
        <div class="metric-card">
            <div class="metric-label">${esc(iface.name)}</div>
            <div class="metric-value" style="font-size:18px">${iface.ip || 'No IP'}</div>
            <div class="metric-sub">${iface.speed_mbps ? iface.speed_mbps + ' Mbps' : ''}</div>
            <div class="net-iface-stats">
                <span>&darr; ${formatBytes(iface.bytes_recv)}</span>
                <span>&uarr; ${formatBytes(iface.bytes_sent)}</span>
            </div>
        </div>
    `).join(''));
}

// ---- Storage ----

function renderStorage(disks) {
    const el = document.getElementById('storage-cards');
    if (disks.length === 0) {
        setHtmlIfChanged(el, '<div class="empty-state">No disks found</div>');
        return;
    }
    setHtmlIfChanged(el, disks.map(dk => `
        <div class="metric-card">
            <div class="metric-label">${esc(dk.mount)} <span style="color:var(--text-muted);font-size:10px">${esc(dk.device)}</span></div>
            <div class="metric-value">${dk.percent}<span class="unit">%</span></div>
            <div class="metric-sub">${formatBytes(dk.used)} / ${formatBytes(dk.total)} (${dk.fstype})</div>
            <div class="progress-bar"><div class="progress-fill ${progressColor(dk.percent)}" style="width:${dk.percent}%"></div></div>
        </div>
    `).join(''));
}

// ---- Tunnel ----

async function refreshTunnel(context = null) {
    try {
        const path = nodePathFor(context ? context.nodeId : selectedNodeId, '/api/tunnel');
        const data = await api('GET', path);
        if (isRefreshCurrent(context)) renderTunnel(data);
        return data;
    } catch (e) {
        if (isRefreshCurrent(context)) renderTunnel({ connected: false, detail: 'unreachable' });
        return null;
    }
}

function renderTunnel(d) {
    const dot = document.getElementById('tunnel-dot');
    const text = document.getElementById('tunnel-text');
    dot.className = 'status-dot ' + (d.connected ? 'green' : 'red');
    text.textContent = d.connected ? `Tunnel: ${d.detail}` : 'Tunnel: disconnected';
}

// ---- Services ----

async function refreshServices(context = null) {
    try {
        const path = nodePathFor(context ? context.nodeId : selectedNodeId, '/api/services');
        const data = await api('GET', path);
        if (isRefreshCurrent(context)) renderServices(data);
        return data;
    } catch (e) {
        console.error('Failed to fetch services:', e);
        return null;
    }
}

function renderServices(services) {
    const el = document.getElementById('services-list');
    if (services.length === 0) {
        setHtmlIfChanged(el, "<div class=\"empty-state\">No services registered yet. Add one to get started or use 'inframatik init' in the root directory of your repo.</div>");
        return;
    }

    setHtmlIfChanged(el, services.map(svc => {
        const statusClass = svc.status === 'active' ? 'active' : svc.status === 'failed' ? 'failed' : 'inactive';
        const link = svc.hostname
            ? `<a href="https://${svc.hostname}" target="_blank">${svc.hostname}</a>`
            : svc.lan
                ? `<a href="http://${window.location.hostname}:${svc.port}" target="_blank">LAN :${svc.port}</a>`
                : '';
        const badge = svc.lan ? '<span class="service-badge lan">LAN</span>' : svc.hostname ? '<span class="service-badge cf">CF</span>' : '';
        const isRunning = svc.status === 'active';

        return `
        <div class="service-card">
            <div class="service-status ${statusClass}"></div>
            <div class="service-info">
                <div class="service-name">${esc(svc.name)} ${badge}</div>
                <div class="service-meta">
                    <span class="service-port">:${svc.port}</span>
                    ${link}
                </div>
            </div>
            <div class="service-actions">
                ${isRunning
                    ? `<button class="btn" onclick="svcAction('${esc(svc.name)}','restart')">Restart</button>
                       <button class="btn danger" onclick="svcAction('${esc(svc.name)}','stop')">Stop</button>`
                    : `<button class="btn success" onclick="svcAction('${esc(svc.name)}','start')">Start</button>`
                }
                <button class="btn" onclick="showLogs('${esc(svc.name)}')">Logs</button>
                <button class="btn danger" onclick="deleteSvc('${esc(svc.name)}')">Remove</button>
            </div>
        </div>`;
    }).join(''));
}

function esc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

function jsArg(value) {
    return esc(JSON.stringify(String(value || '')));
}

async function svcAction(name, action) {
    try {
        await api('POST', nodePath(`/api/services/${name}/${action}`));
        await refreshServices();
    } catch (e) {
        alert(`Failed to ${action} ${name}: ${e.message}`);
    }
}

async function deleteSvc(name) {
    if (!confirm(`Remove service "${name}"? This will stop it and delete the systemd unit.`)) return;
    try {
        await api('DELETE', nodePath(`/api/services/${name}`));
        await refreshServices();
    } catch (e) {
        alert(`Failed to remove ${name}: ${e.message}`);
    }
}

async function showLogs(name) {
    const overlay = document.getElementById('logs-modal');
    document.getElementById('logs-title').textContent = `Logs: ${name}`;
    document.getElementById('logs-content').textContent = 'Loading...';
    overlay.classList.add('active');

    try {
        const data = await api('GET', nodePath(`/api/services/${name}/logs`));
        document.getElementById('logs-content').textContent = data.logs || 'No logs available.';
        const el = document.getElementById('logs-content');
        el.scrollTop = el.scrollHeight;
    } catch (e) {
        document.getElementById('logs-content').textContent = `Error: ${e.message}`;
    }
}

function closeLogs() {
    document.getElementById('logs-modal').classList.remove('active');
}

// ---- New service modal ----

async function openNewService() {
    document.getElementById('new-svc-modal').classList.add('active');
    document.getElementById('svc-name').value = '';
    document.getElementById('svc-command').value = '';
    document.getElementById('svc-workdir').value = '';
    document.getElementById('svc-hostname').value = '';
    document.getElementById('svc-lan').checked = false;
    document.getElementById('svc-error').textContent = '';

    try {
        const data = await api('GET', '/api/ports/next');
        document.getElementById('svc-port-preview').textContent = `Will be assigned port ${data.port}`;
    } catch (e) {
        document.getElementById('svc-port-preview').textContent = 'No ports available';
    }
}

function closeNewService() {
    document.getElementById('new-svc-modal').classList.remove('active');
}

async function submitNewService() {
    const name = document.getElementById('svc-name').value.trim();
    const command = document.getElementById('svc-command').value.trim();
    const working_dir = document.getElementById('svc-workdir').value.trim();
    const hostname = document.getElementById('svc-hostname').value.trim() || null;
    const lan = document.getElementById('svc-lan').checked;
    const errEl = document.getElementById('svc-error');
    errEl.textContent = '';

    if (!name || !command || !working_dir) {
        errEl.textContent = 'Name, command, and working directory are required.';
        return;
    }

    try {
        await api('POST', nodePath('/api/services'), { name, command, working_dir, hostname, lan });
        closeNewService();
        await refreshServices();
    } catch (e) {
        errEl.textContent = e.message;
    }
}

// ---- Inference models ----

const PROFILE_EDITOR_SECTIONS = ['basics', 'runtime', 'placement', 'exposure', 'engine', 'advanced'];

function modelNodePath(path) {
    return nodePathFor(selectedNodeId, path);
}

function setInferenceTab(tab) {
    activeInferenceTab = ['profiles', 'models', 'launchers', 'jobs', 'storage'].includes(tab) ? tab : 'profiles';
    document.querySelectorAll('.inference-tab').forEach(t => {
        if (t.dataset.inferenceTab === activeInferenceTab) t.classList.add('active');
        else t.classList.remove('active');
    });
    document.querySelectorAll('.inference-tab-content').forEach(t => t.classList.remove('active'));
    const panel = document.getElementById('inference-tab-' + activeInferenceTab);
    if (panel) panel.classList.add('active');
}

function setProfileEditorSection(section) {
    const active = PROFILE_EDITOR_SECTIONS.includes(section) ? section : 'basics';
    document.querySelectorAll('[data-profile-editor-section]').forEach(tab => {
        const selected = tab.dataset.profileEditorSection === active;
        tab.classList.toggle('active', selected);
        tab.setAttribute('aria-selected', selected ? 'true' : 'false');
    });
    document.querySelectorAll('[data-profile-editor-panel]').forEach(panel => {
        if (panel.dataset.profileEditorPanel === active) panel.classList.add('active');
        else panel.classList.remove('active');
    });
}

function profileIssueSection(field) {
    const fieldText = String(field || '');
    const key = fieldText.split('.')[0];
    if (['id', 'display_name', 'engine', 'engine_launcher_id', 'launcher_id', 'model', 'model_ref'].includes(key)) return 'basics';
    if (key === 'deployment') return 'placement';
    if (key === 'exposure') return 'exposure';
    if (key === 'engine_config') return 'engine';
    if (key === 'advanced') return 'advanced';
    if (key === 'raw_args' || key === 'env') return 'advanced';
    if (key === 'common') {
        if (fieldText.includes('host') || fieldText.includes('api_key')) return 'exposure';
        return 'runtime';
    }
    return 'basics';
}

function clearProfileEditorIssueBadges() {
    document.querySelectorAll('[data-profile-editor-section]').forEach(tab => {
        tab.classList.remove('has-blockers');
        tab.classList.remove('has-warnings');
        const badge = tab.querySelector('.profile-editor-issue-badge');
        if (!badge) return;
        if (badge.parentNode && typeof badge.parentNode.removeChild === 'function') {
            badge.parentNode.removeChild(badge);
        } else if (typeof badge.remove === 'function') {
            badge.remove();
        }
    });
}

function updateProfileEditorIssueBadges(plan) {
    plan = plan || {};
    clearProfileEditorIssueBadges();
    const counts = new Map(PROFILE_EDITOR_SECTIONS.map(section => [section, { blockers: 0, warnings: 0 }]));
    (plan.blockers || []).forEach(issue => {
        const section = profileIssueSection(issue && issue.field);
        counts.get(section).blockers += 1;
    });
    (plan.warnings || []).forEach(issue => {
        const section = profileIssueSection(issue && issue.field);
        counts.get(section).warnings += 1;
    });
    document.querySelectorAll('[data-profile-editor-section]').forEach(tab => {
        const section = tab.dataset.profileEditorSection;
        const count = counts.get(section) || { blockers: 0, warnings: 0 };
        const total = count.blockers + count.warnings;
        if (!total) return;
        const hasBlockers = count.blockers > 0;
        const badge = document.createElement('span');
        badge.className = `profile-editor-issue-badge ${hasBlockers ? 'red' : 'yellow'}`;
        badge.textContent = String(hasBlockers ? count.blockers : count.warnings);
        badge.title = hasBlockers
            ? `${count.blockers} blocker${count.blockers === 1 ? '' : 's'}`
            : `${count.warnings} warning${count.warnings === 1 ? '' : 's'}`;
        tab.classList.add(hasBlockers ? 'has-blockers' : 'has-warnings');
        tab.appendChild(badge);
    });
}

function setInferenceError(message) {
    setElementText('inference-error', message || '');
}

function setInferenceStatus(message) {
    setElementText('inference-status', message || '');
}

function selectedNodeLabel() {
    const node = nodes.find(n => n.node_id === selectedNodeId || n.config_node_id === selectedNodeId);
    if (node) return node.node_name || selectedNodeId;
    return selectedNodeId || 'local node';
}

function renderInferenceNodePicker() {
    const picker = document.getElementById('inference-node-picker');
    const select = document.getElementById('inference-node-select');
    if (!picker || !select) return;
    if (!isMaster) {
        picker.style.display = 'none';
        return;
    }
    picker.style.display = '';
    setHtmlIfChanged(select, (nodes || []).map(node => {
        const status = node.status ? ` (${node.status})` : '';
        return `<option value="${esc(node.node_id)}">${esc(node.node_name || node.node_id)}${esc(status)}</option>`;
    }).join(''));
    select.value = selectedNodeId || '';
}

function showInferencePendingState() {
    setElementHtml('model-storage-summary', `
        <div class="metric-card">
            <div class="metric-label">Models</div>
            <div class="metric-value">--</div>
            <div class="metric-sub">Loading inventory</div>
        </div>
    `);
    setElementHtml('models-list', '<div class="empty-state">Loading models...</div>');
    setElementHtml('model-jobs-list', '<div class="empty-state">Loading jobs...</div>');
    setElementHtml('launchers-list', '<div class="empty-state">Loading launchers...</div>');
    setElementHtml('inference-profiles-list', '<div class="empty-state">Loading profiles...</div>');
    setElementHtml('inference-operations-list', '<div class="empty-state">Loading operations...</div>');
}

async function loadInferenceView() {
    if (isMaster && (!nodes || nodes.length === 0)) {
        await refreshSidebar();
    }
    renderInferenceNodePicker();
    const subtitle = document.getElementById('inference-page-subtitle');
    if (subtitle) subtitle.textContent = `${selectedNodeLabel()} · node-local inference storage`;
    showInferencePendingState();
    await refreshActiveInferenceTab();
}

async function refreshActiveInferenceTab() {
    if (activeInferenceTab === 'profiles') {
        await refreshInferenceProfiles();
    } else if (activeInferenceTab === 'launchers') {
        await refreshInferenceLaunchers();
    } else if (activeInferenceTab === 'jobs') {
        await refreshInferenceJobs();
    } else {
        await refreshInferenceModels();
    }
}

async function refreshInferenceProfiles() {
    const nodeId = selectedNodeId;
    if (!nodeId) return;
    setInferenceError('');
    try {
        const overview = await api('GET', modelNodePath('/api/inference/overview'));
        if (currentAppView !== 'inference' || nodeId !== selectedNodeId) return;
        const profiles = overview.profiles || {};
        const models = overview.models || { artifacts: [], jobs: [] };
        const launchers = overview.launchers || {};
        const operations = overview.operations || {};
        inferenceProfilesData = profiles.profiles || [];
        inferenceModelData = models;
        inferenceLaunchersData = launchers.launchers || [];
        inferenceOperationsData = operations.operations || [];
        inferenceSystemData = overview.system || null;
        const partialErrors = overview.partial_errors || {};
        const partialKeys = Object.keys(partialErrors);
        const statusEl = document.getElementById('inference-status');
        if (partialKeys.length) {
            setInferenceStatus(`Loaded with limited data: ${partialKeys.join(', ')}`);
        } else if (statusEl && statusEl.textContent.startsWith('Loaded with limited data:')) {
            setInferenceStatus('');
        }
        renderProfileSelects();
        renderInferenceGpuHints();
        renderInferenceProfiles(inferenceProfilesData);
        renderInferenceOperations(inferenceOperationsData);
        hydrateVisibleInferenceFailures(nodeId);
        updateInferencePolling();
    } catch (e) {
        setInferenceError(e.message);
        stopInferencePolling();
    }
}

async function refreshInferenceJobs() {
    const nodeId = selectedNodeId;
    if (!nodeId) return;
    setInferenceError('');
    try {
        const [models, operations] = await Promise.all([
            api('GET', modelNodePath('/api/models')),
            api('GET', modelNodePath('/api/inference/operations')),
        ]);
        if (currentAppView !== 'inference' || nodeId !== selectedNodeId) return;
        inferenceModelData = models;
        inferenceOperationsData = operations.operations || [];
        renderInferenceOperations(inferenceOperationsData);
        renderModelJobs(models.jobs || []);
        hydrateVisibleInferenceFailures(nodeId);
        updateInferencePolling();
    } catch (e) {
        setInferenceError(e.message);
        stopInferencePolling();
    }
}

function profileStateBadge(state) {
    const value = state || 'stopped';
    let color = '';
    if (['running', 'healthy'].includes(value)) color = 'green';
    else if (['starting', 'queued', 'restarting', 'degraded'].includes(value)) color = 'yellow';
    else if (['failed', 'unhealthy'].includes(value)) color = 'red';
    return `<span class="model-badge ${color}">${esc(value)}</span>`;
}

function renderInferenceGpuHints() {
    const el = document.getElementById('profile-gpu-hints');
    if (!el) return;
    const gpus = (inferenceSystemData && inferenceSystemData.gpus) || [];
    if (!gpus.length) {
        setHtmlIfChanged(el, '<div class="profile-gpu-empty">No GPU facts available for this node.</div>');
        return;
    }
    const selected = new Set(parseProfileGpuIds());
    setHtmlIfChanged(el, gpus.map((gpu, fallbackIndex) => {
        const id = gpu.index ?? fallbackIndex;
        const active = selected.has(Number(id)) ? ' active' : '';
        const total = Number(gpu.mem_total_mb || 0);
        const used = Number(gpu.mem_used_mb || 0);
        const free = Math.max(0, total - used);
        const name = gpu.name || `GPU ${id}`;
        const mem = total ? `${formatBytes(free * 1048576)} free / ${formatBytes(total * 1048576)}` : 'VRAM unknown';
        return `
            <button type="button" class="profile-gpu-chip${active}" onclick="toggleProfileGpu(${Number(id)})">
                <span>${esc(id)}</span>
                <strong>${esc(name)}</strong>
                <small>${esc(mem)}</small>
            </button>
        `;
    }).join(''));
}

function toggleProfileGpu(gpuId) {
    const el = document.getElementById('profile-gpus');
    if (!el || !Number.isInteger(gpuId)) return;
    const current = new Set(parseProfileGpuIds());
    if (current.has(gpuId)) current.delete(gpuId);
    else current.add(gpuId);
    el.value = Array.from(current).sort((a, b) => a - b).join(',');
    renderInferenceGpuHints();
}

function profileConfigChips(profile) {
    const common = profile.common || {};
    const deployment = profile.deployment || {};
    const exposure = profile.exposure || {};
    const chips = [];
    if (common.context_length) chips.push(`ctx ${common.context_length}`);
    if (common.dtype) chips.push(`dtype ${common.dtype}`);
    if (common.quantization) chips.push(`quant ${common.quantization}`);
    if (common.kv_cache_dtype) chips.push(`KV ${common.kv_cache_dtype}`);
    if (common.tensor_parallel) chips.push(`TP ${common.tensor_parallel}`);
    if (common.pipeline_parallel) chips.push(`PP ${common.pipeline_parallel}`);
    if (common.data_parallel) chips.push(`DP ${common.data_parallel}`);
    if (common.expert_parallel) {
        const expert = typeof common.expert_parallel === 'object' ? common.expert_parallel.size || 'on' : common.expert_parallel;
        chips.push(`EP ${expert}`);
    }
    if (common.context_parallel) {
        const cp = common.context_parallel;
        chips.push(`CP ${cp.decode_size || cp.prefill_size || cp.attn_cp_size || 'on'}`);
    }
    if (common.gpu_memory_utilization) chips.push(`VRAM ${common.gpu_memory_utilization}`);
    if (common.max_concurrent_requests) chips.push(`seqs ${common.max_concurrent_requests}`);
    if (common.max_batch_tokens) chips.push(`batch ${common.max_batch_tokens}`);
    if (common.max_prefill_tokens) chips.push(`prefill ${common.max_prefill_tokens}`);
    if (deployment.mode) chips.push(deployment.mode);
    if (exposure.mode) chips.push(exposure.mode);
    return chips;
}

const STRUCTURED_COMMON_KEYS = [
    'served_model_name',
    'context_length',
    'dtype',
    'quantization',
    'kv_cache_dtype',
    'kv_cache_memory_bytes',
    'gpu_memory_utilization',
    'cpu_offload_gb',
    'tensor_parallel',
    'pipeline_parallel',
    'data_parallel',
    'expert_parallel',
    'context_parallel',
    'max_concurrent_requests',
    'max_batch_tokens',
    'max_prefill_tokens',
    'startup_grace_seconds',
    'trust_remote_code',
    'enable_prefix_caching',
    'reasoning_parser',
    'tool_call_parser',
    'enable_auto_tool_choice',
    'chat_template',
    'log_level',
    'speculative',
    'gpu_ids',
];

const STRUCTURED_ENGINE_KEYS = {
    vllm: [
        'load_format',
        'all2all_backend',
        'expert_placement_strategy',
        'distributed_executor_backend',
        'api_server_count',
        'data_parallel_size_local',
        'data_parallel_start_rank',
        'data_parallel_address',
        'data_parallel_rpc_port',
        'context_parallel_backend',
        'max_num_partial_prefills',
        'max_long_partial_prefills',
        'long_prefill_token_threshold',
        'scheduling_policy',
        'moe_backend',
        'linear_backend',
        'kv_offloading_size',
        'kv_offloading_backend',
        'offload_backend',
        'chat_template_content_format',
        'reasoning_parser_plugin',
        'tool_parser_plugin',
        'eplb_config',
        'compilation_config',
        'attention_config',
        'enable_expert_parallel',
        'enable_ep_weight_filter',
        'enable_eplb',
        'enable_dbo',
    ],
    sglang: [
        'load_format',
        'page_size',
        'ep_size',
        'attn_cp_size',
        'chunked_prefill_size',
        'load_balance_method',
        'moe_a2a_backend',
        'moe_runner_backend',
        'torchao_config',
        'dsa_prefill_cp_mode',
        'sampling_defaults',
        'cuda_graph_config',
        'hicache',
        'grammar_backend',
        'enable_dp_attention',
        'enable_dsa_prefill_context_parallel',
    ],
    llama_cpp: [
        'n_gpu_layers',
        'main_gpu',
        'split_mode',
        'tensor_split',
        'threads',
        'threads_batch',
        'batch_size',
        'ubatch_size',
        'cache_type_k',
        'cache_type_v',
        'mmproj_ref',
        'flash_attention',
    ],
};

function profileModelOptions() {
    const artifacts = (inferenceModelData && inferenceModelData.artifacts) || [];
    if (!artifacts.length) return '<option value="">No models</option>';
    return '<option value="">Select model</option>' + artifacts.map(model => {
        const snapshot = model.active_snapshot || '';
        const value = `${model.id}@${snapshot}`;
        const label = `${model.display_name || model.manifest_display_name || model.id}${snapshot ? ` @ ${snapshot}` : ''}`;
        return `<option value="${esc(value)}">${esc(label)}</option>`;
    }).join('');
}

function profileLauncherOptions(engine = '') {
    const launchers = inferenceLaunchersData || [];
    const filtered = engine ? launchers.filter(item => item.engine === engine) : launchers;
    if (!filtered.length) return '<option value="">No launchers</option>';
    return '<option value="">Select launcher</option>' + filtered.map(launcher => (
        `<option value="${esc(launcher.id)}">${esc(launcher.display_name || launcher.id)} · ${esc(launcher.engine)}</option>`
    )).join('');
}

function renderProfileSelects() {
    const engineEl = document.getElementById('profile-engine');
    const launcherEl = document.getElementById('profile-launcher');
    const modelEl = document.getElementById('profile-model');
    if (launcherEl) {
        const current = launcherEl.value;
        launcherEl.innerHTML = profileLauncherOptions(engineEl ? engineEl.value : '');
        if (current) launcherEl.value = current;
    }
    if (modelEl) {
        const current = modelEl.value;
        modelEl.innerHTML = profileModelOptions();
        if (current) modelEl.value = current;
    }
    renderProfileEngineFields();
}

function renderLauncherState() {
    renderProfileSelects();
    if (currentAppView === 'inference' && activeInferenceTab === 'launchers') {
        renderLaunchers(inferenceLaunchersData || []);
    }
}

function patchInferenceLauncher(launcher) {
    if (!launcher || !launcher.id) return false;
    const existing = inferenceLaunchersData || [];
    const next = [
        launcher,
        ...existing.filter(item => item.id !== launcher.id),
    ].sort((a, b) => String(a.engine || '').localeCompare(String(b.engine || '')) || String(a.id || '').localeCompare(String(b.id || '')));
    inferenceLaunchersData = next;
    renderLauncherState();
    return true;
}

function removeInferenceLauncher(launcherId) {
    if (!launcherId || !inferenceLaunchersData) return false;
    const before = inferenceLaunchersData.length;
    inferenceLaunchersData = inferenceLaunchersData.filter(item => item.id !== launcherId);
    renderLauncherState();
    return inferenceLaunchersData.length !== before;
}

function renderProfileEngineFields() {
    const engine = (document.getElementById('profile-engine') || {}).value || 'vllm';
    const wanted = engine === 'llama.cpp' ? 'llama' : engine;
    document.querySelectorAll('.engine-field').forEach(section => {
        section.style.display = section.classList.contains(`engine-field-${wanted}`) ? '' : 'none';
    });
}

function parseProfileModelValue(value) {
    const [artifactId, snapshot] = String(value || '').split('@');
    return artifactId ? { artifact_id: artifactId, snapshot: snapshot || null } : null;
}

function parseProfileGpuIds() {
    const raw = modelOptionalValue('profile-gpus');
    if (!raw) return [];
    return raw.split(',').map(part => part.trim()).filter(Boolean).map(part => Number(part)).filter(Number.isInteger);
}

function profileTextAreaLines(id) {
    const el = document.getElementById(id);
    if (!el) return [];
    return el.value.split('\n').map(line => line.trim()).filter(Boolean);
}

function profileEnvFromTextArea() {
    const env = {};
    profileTextAreaLines('profile-env').forEach(line => {
        const idx = line.indexOf('=');
        if (idx > 0) env[line.slice(0, idx).trim()] = line.slice(idx + 1);
    });
    return env;
}

function profileJsonValue(id, label) {
    const raw = modelOptionalValue(id);
    if (!raw) return {};
    let parsed;
    try {
        parsed = JSON.parse(raw);
    } catch (e) {
        throw new Error(`${label} must be valid JSON: ${e.message}`);
    }
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
        throw new Error(`${label} must be a JSON object.`);
    }
    return parsed;
}

function normalizeEngineConfigJson(engine, value) {
    const config = value && typeof value === 'object' ? value : {};
    const known = ['vllm', 'sglang', 'llama.cpp', 'llama_cpp', 'llamacpp'];
    if (known.some(key => Object.prototype.hasOwnProperty.call(config, key))) return config;
    return Object.keys(config).length ? { [engine]: config } : {};
}

function jsonForTextarea(value) {
    if (!value || typeof value !== 'object' || !Object.keys(value).length) return '';
    return JSON.stringify(value, null, 2);
}

function omitKeys(value, keys) {
    const result = { ...(value || {}) };
    keys.forEach(key => delete result[key]);
    return result;
}

function profileNumberValue(id) {
    const raw = modelOptionalValue(id);
    if (raw === null) return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
}

function profileBooleanValue(id) {
    const el = document.getElementById(id);
    return Boolean(el && el.checked);
}

function setProfileValue(id, value) {
    const el = document.getElementById(id);
    if (el) el.value = value === undefined || value === null ? '' : value;
}

function setProfileChecked(id, value) {
    const el = document.getElementById(id);
    if (el) el.checked = Boolean(value);
}

function clearProfileValues(ids) {
    ids.forEach(id => setProfileValue(id, ''));
}

function clearProfileChecks(ids) {
    ids.forEach(id => setProfileChecked(id, false));
}

function cleanObject(value) {
    const result = {};
    Object.entries(value || {}).forEach(([key, item]) => {
        if (item === null || item === undefined || item === '') return;
        if (Array.isArray(item) && item.length === 0) return;
        if (typeof item === 'object' && !Array.isArray(item) && Object.keys(item).length === 0) return;
        result[key] = item;
    });
    return result;
}

function engineConfigKey(engine) {
    return engine === 'llama.cpp' ? 'llama_cpp' : engine;
}

function getEngineSpecificConfig(engineConfig, engine) {
    const config = engineConfig || {};
    if (engine === 'llama.cpp') {
        return config.llama_cpp || config['llama.cpp'] || config.llamacpp || {};
    }
    return config[engine] || {};
}

function profileTensorSplitValue() {
    const raw = modelOptionalValue('profile-llama-tensor-split');
    if (!raw) return null;
    return raw.split(',').map(part => part.trim()).filter(Boolean).map(part => {
        const value = Number(part);
        return Number.isFinite(value) ? value : part;
    });
}

function structuredCommonConfig() {
    const contextParallel = cleanObject({
        decode_size: profileNumberValue('profile-context-parallel-decode'),
        prefill_size: profileNumberValue('profile-context-parallel-prefill'),
    });
    const speculative = cleanObject({
        model: modelOptionalValue('profile-speculative-model'),
        num_tokens: profileNumberValue('profile-speculative-tokens'),
    });
    return cleanObject({
        served_model_name: modelOptionalValue('profile-served-name'),
        context_length: profileNumberValue('profile-context'),
        dtype: modelOptionalValue('profile-dtype'),
        quantization: modelOptionalValue('profile-quantization'),
        kv_cache_dtype: modelOptionalValue('profile-kv-cache-dtype'),
        kv_cache_memory_bytes: modelOptionalValue('profile-kv-cache-bytes'),
        gpu_memory_utilization: profileNumberValue('profile-gpu-memory-utilization'),
        cpu_offload_gb: profileNumberValue('profile-cpu-offload-gb'),
        tensor_parallel: profileNumberValue('profile-tensor-parallel'),
        pipeline_parallel: profileNumberValue('profile-pipeline-parallel'),
        data_parallel: profileNumberValue('profile-data-parallel'),
        expert_parallel: profileNumberValue('profile-expert-parallel'),
        context_parallel: contextParallel,
        max_concurrent_requests: profileNumberValue('profile-max-concurrent'),
        max_batch_tokens: profileNumberValue('profile-max-batch-tokens'),
        max_prefill_tokens: profileNumberValue('profile-max-prefill-tokens'),
        startup_grace_seconds: profileNumberValue('profile-startup-grace'),
        trust_remote_code: profileBooleanValue('profile-trust-remote-code') ? true : null,
        enable_prefix_caching: profileBooleanValue('profile-prefix-caching') ? true : null,
        reasoning_parser: modelOptionalValue('profile-reasoning-parser'),
        tool_call_parser: modelOptionalValue('profile-tool-call-parser'),
        enable_auto_tool_choice: profileBooleanValue('profile-auto-tool-choice') ? true : null,
        chat_template: modelOptionalValue('profile-chat-template'),
        log_level: modelOptionalValue('profile-log-level'),
        speculative,
    });
}

function structuredEngineConfig(engine) {
    if (engine === 'vllm') {
        return cleanObject({
            load_format: modelOptionalValue('profile-vllm-load-format'),
            all2all_backend: modelOptionalValue('profile-vllm-all2all-backend'),
            expert_placement_strategy: modelOptionalValue('profile-vllm-expert-placement'),
            distributed_executor_backend: modelOptionalValue('profile-vllm-distributed-executor'),
            api_server_count: profileNumberValue('profile-vllm-api-server-count'),
            data_parallel_size_local: profileNumberValue('profile-vllm-dp-local-size'),
            data_parallel_start_rank: profileNumberValue('profile-vllm-dp-start-rank'),
            data_parallel_address: modelOptionalValue('profile-vllm-dp-address'),
            data_parallel_rpc_port: profileNumberValue('profile-vllm-dp-rpc-port'),
            context_parallel_backend: modelOptionalValue('profile-vllm-context-backend'),
            max_num_partial_prefills: profileNumberValue('profile-vllm-partial-prefills'),
            max_long_partial_prefills: profileNumberValue('profile-vllm-long-partial-prefills'),
            long_prefill_token_threshold: profileNumberValue('profile-vllm-long-prefill-threshold'),
            scheduling_policy: modelOptionalValue('profile-vllm-scheduling-policy'),
            moe_backend: modelOptionalValue('profile-vllm-moe-backend'),
            linear_backend: modelOptionalValue('profile-vllm-linear-backend'),
            kv_offloading_size: modelOptionalValue('profile-vllm-kv-offloading-size'),
            kv_offloading_backend: modelOptionalValue('profile-vllm-kv-offloading-backend'),
            offload_backend: modelOptionalValue('profile-vllm-offload-backend'),
            chat_template_content_format: modelOptionalValue('profile-vllm-chat-template-format'),
            reasoning_parser_plugin: modelOptionalValue('profile-vllm-reasoning-plugin'),
            tool_parser_plugin: modelOptionalValue('profile-vllm-tool-plugin'),
            eplb_config: profileJsonValue('profile-vllm-eplb-config', 'vLLM EPLB Config'),
            compilation_config: profileJsonValue('profile-vllm-compilation-config', 'vLLM Compilation Config'),
            attention_config: profileJsonValue('profile-vllm-attention-config', 'vLLM Attention Config'),
            enable_expert_parallel: profileBooleanValue('profile-vllm-expert-parallel') ? true : null,
            enable_ep_weight_filter: profileBooleanValue('profile-vllm-ep-weight-filter') ? true : null,
            enable_eplb: profileBooleanValue('profile-vllm-eplb') ? true : null,
            enable_dbo: profileBooleanValue('profile-vllm-dbo') ? true : null,
        });
    }
    if (engine === 'sglang') {
        return cleanObject({
            load_format: modelOptionalValue('profile-sglang-load-format'),
            page_size: profileNumberValue('profile-sglang-page-size'),
            ep_size: profileNumberValue('profile-sglang-ep-size'),
            attn_cp_size: profileNumberValue('profile-sglang-attn-cp-size'),
            chunked_prefill_size: profileNumberValue('profile-sglang-chunked-prefill-size'),
            load_balance_method: modelOptionalValue('profile-sglang-load-balance-method'),
            moe_a2a_backend: modelOptionalValue('profile-sglang-moe-a2a-backend'),
            moe_runner_backend: modelOptionalValue('profile-sglang-moe-runner-backend'),
            torchao_config: modelOptionalValue('profile-sglang-torchao-config'),
            dsa_prefill_cp_mode: modelOptionalValue('profile-sglang-dsa-cp-mode'),
            sampling_defaults: profileJsonValue('profile-sglang-sampling-defaults', 'SGLang Sampling Defaults'),
            cuda_graph_config: profileJsonValue('profile-sglang-cuda-graph-config', 'SGLang CUDA Graph Config'),
            hicache: profileJsonValue('profile-sglang-hicache', 'SGLang HiCache Config'),
            grammar_backend: modelOptionalValue('profile-sglang-grammar-backend'),
            enable_dp_attention: profileBooleanValue('profile-sglang-dp-attention') ? true : null,
            enable_dsa_prefill_context_parallel: profileBooleanValue('profile-sglang-dsa-prefill-cp') ? true : null,
        });
    }
    if (engine === 'llama.cpp') {
        return cleanObject({
            n_gpu_layers: profileNumberValue('profile-llama-gpu-layers'),
            main_gpu: profileNumberValue('profile-llama-main-gpu'),
            split_mode: modelOptionalValue('profile-llama-split-mode'),
            tensor_split: profileTensorSplitValue(),
            threads: profileNumberValue('profile-llama-threads'),
            threads_batch: profileNumberValue('profile-llama-threads-batch'),
            batch_size: profileNumberValue('profile-llama-batch-size'),
            ubatch_size: profileNumberValue('profile-llama-ubatch-size'),
            cache_type_k: modelOptionalValue('profile-llama-cache-type-k'),
            cache_type_v: modelOptionalValue('profile-llama-cache-type-v'),
            mmproj_ref: modelOptionalValue('profile-llama-mmproj-ref'),
            flash_attention: profileBooleanValue('profile-llama-flash-attn') ? true : null,
        });
    }
    return {};
}

function mergeEngineConfig(engine, structured, rawConfig) {
    const key = engineConfigKey(engine);
    const raw = rawConfig && typeof rawConfig === 'object' ? rawConfig : {};
    const existing = getEngineSpecificConfig(raw, engine);
    const result = { ...raw };
    result[key] = cleanObject({ ...existing, ...structured });
    if (!Object.keys(result[key]).length) delete result[key];
    if (engine === 'llama.cpp') {
        delete result['llama.cpp'];
        delete result.llamacpp;
    }
    return cleanObject(result);
}

function buildProfileDraft() {
    const id = modelOptionalValue('profile-id');
    const displayName = modelOptionalValue('profile-display-name');
    const engine = document.getElementById('profile-engine').value;
    const launcherId = document.getElementById('profile-launcher').value;
    const model = parseProfileModelValue(document.getElementById('profile-model').value);
    const deploymentMode = document.getElementById('profile-deployment-mode').value || 'single';
    const replicas = Number(document.getElementById('profile-replicas').value || 1);
    const port = Number(document.getElementById('profile-port').value || 0);
    const gpuIds = parseProfileGpuIds();
    const common = {
        ...profileJsonValue('profile-common-json', 'Common JSON'),
        ...structuredCommonConfig(),
        gpu_ids: gpuIds.length ? gpuIds : null,
    };
    Object.keys(common).forEach(key => common[key] === null && delete common[key]);
    const rawEngineConfig = normalizeEngineConfigJson(
        engine,
        profileJsonValue('profile-engine-json', 'Engine JSON')
    );
    const engineConfig = mergeEngineConfig(engine, structuredEngineConfig(engine), rawEngineConfig);
    const deployment = {
        mode: deploymentMode,
        replicas: deploymentMode === 'replicated' ? Math.max(1, replicas || 1) : 1,
        gpu_policy: {
            mode: document.getElementById('profile-gpu-policy').value || 'profile',
            claim_mode: document.getElementById('profile-gpu-claim').value || 'exclusive',
            gpu_ids: gpuIds,
        },
        port_policy: port
            ? { mode: 'explicit', ports: [port] }
            : { mode: deploymentMode === 'replicated' ? 'contiguous' : 'auto' },
    };
    const exposure = {
        mode: document.getElementById('profile-exposure-mode').value || 'local',
        hostname: modelOptionalValue('profile-hostname'),
    };
    const advanced = {
        args: profileTextAreaLines('profile-raw-args'),
        env: profileEnvFromTextArea(),
    };
    return {
        id,
        display_name: displayName,
        engine,
        engine_launcher_id: launcherId,
        model,
        common,
        engine_config: engineConfig,
        deployment,
        exposure,
        advanced,
    };
}

function resetProfileForm() {
    ['profile-edit-id', 'profile-id', 'profile-display-name', 'profile-served-name', 'profile-context',
        'profile-dtype', 'profile-quantization', 'profile-kv-cache-dtype', 'profile-kv-cache-bytes',
        'profile-gpu-memory-utilization', 'profile-cpu-offload-gb', 'profile-tensor-parallel',
        'profile-pipeline-parallel', 'profile-data-parallel', 'profile-expert-parallel',
        'profile-context-parallel-decode', 'profile-context-parallel-prefill', 'profile-max-concurrent',
        'profile-max-batch-tokens', 'profile-max-prefill-tokens', 'profile-startup-grace',
        'profile-reasoning-parser', 'profile-tool-call-parser', 'profile-chat-template',
        'profile-speculative-model', 'profile-speculative-tokens', 'profile-log-level',
        'profile-port', 'profile-gpus', 'profile-hostname',
        'profile-vllm-load-format', 'profile-vllm-all2all-backend', 'profile-vllm-expert-placement',
        'profile-vllm-api-server-count', 'profile-vllm-dp-local-size', 'profile-vllm-dp-start-rank',
        'profile-vllm-dp-address', 'profile-vllm-dp-rpc-port', 'profile-vllm-partial-prefills',
        'profile-vllm-long-partial-prefills', 'profile-vllm-long-prefill-threshold',
        'profile-vllm-scheduling-policy', 'profile-vllm-moe-backend', 'profile-vllm-linear-backend',
        'profile-vllm-distributed-executor', 'profile-vllm-context-backend',
        'profile-vllm-kv-offloading-size', 'profile-vllm-kv-offloading-backend',
        'profile-vllm-offload-backend', 'profile-vllm-chat-template-format',
        'profile-vllm-reasoning-plugin', 'profile-vllm-tool-plugin',
        'profile-vllm-eplb-config', 'profile-vllm-compilation-config', 'profile-vllm-attention-config',
        'profile-sglang-load-format', 'profile-sglang-page-size',
        'profile-sglang-ep-size', 'profile-sglang-attn-cp-size', 'profile-sglang-chunked-prefill-size',
        'profile-sglang-load-balance-method', 'profile-sglang-moe-a2a-backend',
        'profile-sglang-moe-runner-backend', 'profile-sglang-torchao-config', 'profile-sglang-dsa-cp-mode',
        'profile-sglang-grammar-backend', 'profile-sglang-sampling-defaults',
        'profile-sglang-cuda-graph-config', 'profile-sglang-hicache',
        'profile-llama-gpu-layers', 'profile-llama-main-gpu', 'profile-llama-split-mode',
        'profile-llama-tensor-split', 'profile-llama-threads', 'profile-llama-threads-batch',
        'profile-llama-batch-size', 'profile-llama-ubatch-size', 'profile-llama-cache-type-k',
        'profile-llama-cache-type-v', 'profile-llama-mmproj-ref', 'profile-raw-args', 'profile-env',
        'profile-common-json', 'profile-engine-json'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    ['profile-trust-remote-code', 'profile-prefix-caching', 'profile-auto-tool-choice',
        'profile-vllm-expert-parallel', 'profile-vllm-ep-weight-filter', 'profile-vllm-eplb', 'profile-vllm-dbo',
        'profile-sglang-dp-attention', 'profile-sglang-dsa-prefill-cp',
        'profile-llama-flash-attn'].forEach(id => setProfileChecked(id, false));
    const idEl = document.getElementById('profile-id');
    if (idEl) idEl.disabled = false;
    const engineEl = document.getElementById('profile-engine');
    if (engineEl) engineEl.value = 'vllm';
    const replicasEl = document.getElementById('profile-replicas');
    if (replicasEl) replicasEl.value = '1';
    const deploymentEl = document.getElementById('profile-deployment-mode');
    if (deploymentEl) deploymentEl.value = 'single';
    const gpuPolicyEl = document.getElementById('profile-gpu-policy');
    if (gpuPolicyEl) gpuPolicyEl.value = 'profile';
    const claimEl = document.getElementById('profile-gpu-claim');
    if (claimEl) claimEl.value = 'exclusive';
    const exposureEl = document.getElementById('profile-exposure-mode');
    if (exposureEl) exposureEl.value = 'local';
    renderProfileSelects();
    renderProfileEngineFields();
    setProfileEditorSection('basics');
    clearProfileEditorIssueBadges();
    syncProfileSaveRestartButton(null);
    setElementHtml('profile-preview-panel', '<div class="empty-state">No preview yet.</div>');
}

function profileCanSaveRestart(profile) {
    const state = String((profile && (profile.state || profile.status)) || '').toLowerCase();
    return ['running', 'starting', 'restarting', 'active'].includes(state);
}

function syncProfileSaveRestartButton(profile) {
    const btn = document.getElementById('profile-save-restart-btn');
    if (!btn) return;
    btn.style.display = profileCanSaveRestart(profile) ? '' : 'none';
}

function fillProfileForm(profile) {
    resetProfileForm();
    setProfileEditorSection('basics');
    document.getElementById('profile-edit-id').value = profile.id || '';
    document.getElementById('profile-id').value = profile.id || '';
    document.getElementById('profile-id').disabled = true;
    document.getElementById('profile-display-name').value = profile.display_name || '';
    document.getElementById('profile-engine').value = profile.engine || 'vllm';
    renderProfileSelects();
    document.getElementById('profile-launcher').value = profile.engine_launcher_id || '';
    const snapshot = profile.model && profile.model.snapshot ? `@${profile.model.snapshot}` : '@';
    document.getElementById('profile-model').value = profile.model && profile.model.artifact_id ? `${profile.model.artifact_id}${snapshot}` : '';
    const common = profile.common || {};
    const contextParallel = common.context_parallel || {};
    const speculative = common.speculative || {};
    setProfileValue('profile-served-name', common.served_model_name);
    setProfileValue('profile-context', common.context_length);
    setProfileValue('profile-dtype', common.dtype);
    setProfileValue('profile-quantization', common.quantization);
    setProfileValue('profile-kv-cache-dtype', common.kv_cache_dtype);
    setProfileValue('profile-kv-cache-bytes', common.kv_cache_memory_bytes);
    setProfileValue('profile-gpu-memory-utilization', common.gpu_memory_utilization);
    setProfileValue('profile-cpu-offload-gb', common.cpu_offload_gb);
    setProfileValue('profile-tensor-parallel', common.tensor_parallel);
    setProfileValue('profile-pipeline-parallel', common.pipeline_parallel);
    setProfileValue('profile-data-parallel', common.data_parallel);
    setProfileValue('profile-expert-parallel', typeof common.expert_parallel === 'object' ? common.expert_parallel.size : common.expert_parallel);
    setProfileValue('profile-context-parallel-decode', contextParallel.decode_size);
    setProfileValue('profile-context-parallel-prefill', contextParallel.prefill_size);
    setProfileValue('profile-max-concurrent', common.max_concurrent_requests);
    setProfileValue('profile-max-batch-tokens', common.max_batch_tokens);
    setProfileValue('profile-max-prefill-tokens', common.max_prefill_tokens);
    setProfileValue('profile-startup-grace', common.startup_grace_seconds);
    setProfileValue('profile-reasoning-parser', common.reasoning_parser);
    setProfileValue('profile-tool-call-parser', common.tool_call_parser);
    setProfileValue('profile-chat-template', common.chat_template);
    setProfileValue('profile-log-level', common.log_level);
    setProfileValue('profile-speculative-model', speculative.model);
    setProfileValue('profile-speculative-tokens', speculative.num_tokens);
    setProfileChecked('profile-trust-remote-code', common.trust_remote_code);
    setProfileChecked('profile-prefix-caching', common.enable_prefix_caching);
    setProfileChecked('profile-auto-tool-choice', common.enable_auto_tool_choice);
    document.getElementById('profile-port').value = profile.instances && profile.instances[0] ? profile.instances[0].port || '' : '';
    const deployment = profile.deployment || {};
    const gpuPolicy = deployment.gpu_policy || {};
    document.getElementById('profile-gpus').value = (gpuPolicy.gpu_ids || common.gpu_ids || []).join(',');
    document.getElementById('profile-deployment-mode').value = deployment.mode || 'single';
    document.getElementById('profile-replicas').value = deployment.replicas || (profile.instances || []).length || 1;
    document.getElementById('profile-gpu-policy').value = gpuPolicy.mode || 'profile';
    document.getElementById('profile-gpu-claim').value = gpuPolicy.claim_mode || 'exclusive';
    const exposure = profile.exposure || {};
    document.getElementById('profile-exposure-mode').value = exposure.mode || 'local';
    document.getElementById('profile-hostname').value = exposure.hostname || '';
    const advanced = profile.advanced || {};
    document.getElementById('profile-raw-args').value = (advanced.args || []).join('\n');
    document.getElementById('profile-env').value = Object.entries(advanced.env || {}).map(([key, value]) => `${key}=${value}`).join('\n');
    document.getElementById('profile-common-json').value = jsonForTextarea(omitKeys(common, STRUCTURED_COMMON_KEYS));
    const engineConfig = profile.engine_config || {};
    const engineSpecific = getEngineSpecificConfig(engineConfig, profile.engine || 'vllm');
    setProfileValue('profile-vllm-load-format', engineSpecific.load_format);
    setProfileValue('profile-vllm-all2all-backend', engineSpecific.all2all_backend);
    setProfileValue('profile-vllm-expert-placement', engineSpecific.expert_placement_strategy);
    setProfileValue('profile-vllm-distributed-executor', engineSpecific.distributed_executor_backend);
    setProfileValue('profile-vllm-api-server-count', engineSpecific.api_server_count);
    setProfileValue('profile-vllm-dp-local-size', engineSpecific.data_parallel_size_local);
    setProfileValue('profile-vllm-dp-start-rank', engineSpecific.data_parallel_start_rank);
    setProfileValue('profile-vllm-dp-address', engineSpecific.data_parallel_address);
    setProfileValue('profile-vllm-dp-rpc-port', engineSpecific.data_parallel_rpc_port);
    setProfileValue('profile-vllm-context-backend', engineSpecific.context_parallel_backend);
    setProfileValue('profile-vllm-partial-prefills', engineSpecific.max_num_partial_prefills);
    setProfileValue('profile-vllm-long-partial-prefills', engineSpecific.max_long_partial_prefills);
    setProfileValue('profile-vllm-long-prefill-threshold', engineSpecific.long_prefill_token_threshold);
    setProfileValue('profile-vllm-scheduling-policy', engineSpecific.scheduling_policy);
    setProfileValue('profile-vllm-moe-backend', engineSpecific.moe_backend);
    setProfileValue('profile-vllm-linear-backend', engineSpecific.linear_backend);
    setProfileValue('profile-vllm-kv-offloading-size', engineSpecific.kv_offloading_size);
    setProfileValue('profile-vllm-kv-offloading-backend', engineSpecific.kv_offloading_backend);
    setProfileValue('profile-vllm-offload-backend', engineSpecific.offload_backend);
    setProfileValue('profile-vllm-chat-template-format', engineSpecific.chat_template_content_format);
    setProfileValue('profile-vllm-reasoning-plugin', engineSpecific.reasoning_parser_plugin);
    setProfileValue('profile-vllm-tool-plugin', engineSpecific.tool_parser_plugin);
    setProfileValue('profile-vllm-eplb-config', jsonForTextarea(engineSpecific.eplb_config));
    setProfileValue('profile-vllm-compilation-config', jsonForTextarea(engineSpecific.compilation_config));
    setProfileValue('profile-vllm-attention-config', jsonForTextarea(engineSpecific.attention_config));
    setProfileChecked('profile-vllm-expert-parallel', engineSpecific.enable_expert_parallel);
    setProfileChecked('profile-vllm-ep-weight-filter', engineSpecific.enable_ep_weight_filter);
    setProfileChecked('profile-vllm-eplb', engineSpecific.enable_eplb);
    setProfileChecked('profile-vllm-dbo', engineSpecific.enable_dbo);
    setProfileValue('profile-sglang-load-format', engineSpecific.load_format);
    setProfileValue('profile-sglang-page-size', engineSpecific.page_size);
    setProfileValue('profile-sglang-ep-size', engineSpecific.ep_size);
    setProfileValue('profile-sglang-attn-cp-size', engineSpecific.attn_cp_size);
    setProfileValue('profile-sglang-chunked-prefill-size', engineSpecific.chunked_prefill_size);
    setProfileValue('profile-sglang-load-balance-method', engineSpecific.load_balance_method);
    setProfileValue('profile-sglang-moe-a2a-backend', engineSpecific.moe_a2a_backend);
    setProfileValue('profile-sglang-moe-runner-backend', engineSpecific.moe_runner_backend);
    setProfileValue('profile-sglang-torchao-config', engineSpecific.torchao_config);
    setProfileValue('profile-sglang-dsa-cp-mode', engineSpecific.dsa_prefill_cp_mode);
    setProfileValue('profile-sglang-grammar-backend', engineSpecific.grammar_backend);
    setProfileValue('profile-sglang-sampling-defaults', jsonForTextarea(engineSpecific.sampling_defaults));
    setProfileValue('profile-sglang-cuda-graph-config', jsonForTextarea(engineSpecific.cuda_graph_config));
    setProfileValue('profile-sglang-hicache', jsonForTextarea(engineSpecific.hicache));
    setProfileChecked('profile-sglang-dp-attention', engineSpecific.enable_dp_attention);
    setProfileChecked('profile-sglang-dsa-prefill-cp', engineSpecific.enable_dsa_prefill_context_parallel);
    setProfileValue('profile-llama-gpu-layers', engineSpecific.n_gpu_layers);
    setProfileValue('profile-llama-main-gpu', engineSpecific.main_gpu);
    setProfileValue('profile-llama-split-mode', engineSpecific.split_mode);
    setProfileValue('profile-llama-tensor-split', Array.isArray(engineSpecific.tensor_split) ? engineSpecific.tensor_split.join(',') : engineSpecific.tensor_split);
    setProfileValue('profile-llama-threads', engineSpecific.threads);
    setProfileValue('profile-llama-threads-batch', engineSpecific.threads_batch);
    setProfileValue('profile-llama-batch-size', engineSpecific.batch_size);
    setProfileValue('profile-llama-ubatch-size', engineSpecific.ubatch_size);
    setProfileValue('profile-llama-cache-type-k', engineSpecific.cache_type_k);
    setProfileValue('profile-llama-cache-type-v', engineSpecific.cache_type_v);
    setProfileValue('profile-llama-mmproj-ref', engineSpecific.mmproj_ref);
    setProfileChecked('profile-llama-flash-attn', engineSpecific.flash_attention);
    const selectedEngine = profile.engine || 'vllm';
    if (selectedEngine !== 'vllm') {
        clearProfileValues([
            'profile-vllm-load-format', 'profile-vllm-all2all-backend', 'profile-vllm-expert-placement',
            'profile-vllm-api-server-count', 'profile-vllm-dp-local-size', 'profile-vllm-dp-start-rank',
            'profile-vllm-dp-address', 'profile-vllm-dp-rpc-port', 'profile-vllm-partial-prefills',
            'profile-vllm-long-partial-prefills', 'profile-vllm-long-prefill-threshold',
            'profile-vllm-scheduling-policy', 'profile-vllm-moe-backend', 'profile-vllm-linear-backend',
            'profile-vllm-distributed-executor', 'profile-vllm-context-backend',
            'profile-vllm-kv-offloading-size', 'profile-vllm-kv-offloading-backend',
            'profile-vllm-offload-backend', 'profile-vllm-chat-template-format',
            'profile-vllm-reasoning-plugin', 'profile-vllm-tool-plugin',
            'profile-vllm-eplb-config', 'profile-vllm-compilation-config', 'profile-vllm-attention-config',
        ]);
        clearProfileChecks(['profile-vllm-expert-parallel', 'profile-vllm-ep-weight-filter', 'profile-vllm-eplb', 'profile-vllm-dbo']);
    }
    if (selectedEngine !== 'sglang') {
        clearProfileValues([
            'profile-sglang-load-format', 'profile-sglang-page-size', 'profile-sglang-ep-size',
            'profile-sglang-attn-cp-size', 'profile-sglang-chunked-prefill-size',
            'profile-sglang-load-balance-method', 'profile-sglang-moe-a2a-backend',
            'profile-sglang-moe-runner-backend', 'profile-sglang-torchao-config',
            'profile-sglang-dsa-cp-mode', 'profile-sglang-grammar-backend',
            'profile-sglang-sampling-defaults', 'profile-sglang-cuda-graph-config',
            'profile-sglang-hicache',
        ]);
        clearProfileChecks(['profile-sglang-dp-attention', 'profile-sglang-dsa-prefill-cp']);
    }
    if (selectedEngine !== 'llama.cpp') {
        clearProfileValues([
            'profile-llama-gpu-layers', 'profile-llama-main-gpu', 'profile-llama-split-mode',
            'profile-llama-tensor-split', 'profile-llama-threads', 'profile-llama-threads-batch',
            'profile-llama-batch-size', 'profile-llama-ubatch-size', 'profile-llama-cache-type-k',
            'profile-llama-cache-type-v', 'profile-llama-mmproj-ref',
        ]);
        clearProfileChecks(['profile-llama-flash-attn']);
    }
    document.getElementById('profile-engine-json').value = jsonForTextarea(omitKeys(
        engineSpecific,
        STRUCTURED_ENGINE_KEYS[engineConfigKey(profile.engine || 'vllm')] || []
    ));
    renderProfileEngineFields();
    syncProfileSaveRestartButton(profile);
}

async function previewInferenceProfile() {
    setInferenceError('');
    let draft;
    try {
        draft = buildProfileDraft();
    } catch (e) {
        setInferenceError(e.message);
        return;
    }
    if (!draft.engine_launcher_id || !draft.model) {
        setInferenceError('Launcher and model are required.');
        return;
    }
    try {
        const editId = modelOptionalValue('profile-edit-id');
        const body = editId ? { ...draft, existing_profile_id: editId } : draft;
        const preview = await api('POST', modelNodePath('/api/inference/profiles/preview'), body);
        renderProfilePreview(preview);
    } catch (e) {
        setInferenceError(e.message);
    }
}

async function saveInferenceProfile(options = {}) {
    setInferenceError('');
    let draft;
    try {
        draft = buildProfileDraft();
    } catch (e) {
        setInferenceError(e.message);
        return;
    }
    if (!draft.engine_launcher_id || !draft.model) {
        setInferenceError('Launcher and model are required.');
        return;
    }
    try {
        const editId = modelOptionalValue('profile-edit-id');
        const restartAfterSave = Boolean(options.restart && editId);
        const result = editId
            ? await api('PUT', modelNodePath(`/api/inference/profiles/${encodeURIComponent(editId)}`), draft)
            : await api('POST', modelNodePath('/api/inference/profiles'), draft);
        const savedProfile = result.profile || null;
        const savedProfileId = (savedProfile && savedProfile.id) || editId || draft.id;
        setInferenceStatus(restartAfterSave ? `Updated profile ${savedProfileId}; queuing restart...` : editId ? `Updated profile ${savedProfileId}.` : 'Profile saved.');
        patchInferenceProfile(savedProfile);
        resetProfileForm();
        renderProfilePreview(result.plan || {});
        if (restartAfterSave) {
            await runProfileAction(savedProfileId, 'restart');
        }
    } catch (e) {
        setInferenceError(e.message);
    }
}

function renderProfilePreview(plan) {
    updateProfileEditorIssueBadges(plan);
    const blockers = plan.blockers || [];
    const warnings = plan.warnings || [];
    const instances = plan.resolved_instances || [];
    const portPlan = plan.port_plan || {};
    const gpuPlan = plan.gpu_plan || {};
    const cloudflarePlan = plan.cloudflare_plan || {};
    const restart = plan.restart_required || {};
    const units = ((plan.systemd_preview || {}).units || []);
    const allocatedPorts = (portPlan.allocated || []).join(', ') || '--';
    const gpuAssignments = (gpuPlan.assignments || [])
        .map(item => `#${item.index}: ${(item.gpu_ids || []).join(',') || 'none'}`)
        .join(' / ') || '--';
    const cfResources = (cloudflarePlan.resources || [])
        .map(resource => resource.kind + (resource.hostname ? ` ${resource.hostname}` : ''))
        .join(' / ') || 'none';
    const restartFields = (restart.fields || []).join(', ') || '--';
    setElementHtml('profile-preview-panel', `
        <div class="profile-preview-title">
            Preview
            <span>
                ${restart.required ? '<span class="model-badge yellow">restart required</span>' : ''}
                ${plan.valid_for_save ? '<span class="model-badge green">Valid</span>' : '<span class="model-badge red">Blocked</span>'}
            </span>
        </div>
        ${blockers.length ? `<div class="profile-issue-list">${blockers.map(item => `<div class="model-job-error">${esc(item.message || item)}</div>`).join('')}</div>` : ''}
        ${warnings.length ? `<div class="profile-issue-list">${warnings.map(item => `<div class="profile-warning">${esc(item.message || item)}</div>`).join('')}</div>` : ''}
        <div class="profile-preview-facts">
            <div><span>Ports</span><code>${esc(allocatedPorts)}</code><small>${esc(portPlan.mode || '--')} · ${esc(portPlan.range || 'inference')} ${portPlan.range_start ? `${esc(portPlan.range_start)}-${esc(portPlan.range_end)}` : ''}</small></div>
            <div><span>GPU Plan</span><code>${esc(gpuAssignments)}</code><small>${esc(gpuPlan.mode || '--')} · ${esc(gpuPlan.claim_mode || 'exclusive')}</small></div>
            <div><span>Cloudflare</span><code>${esc(cloudflarePlan.would_provision ? 'would provision' : 'no changes')}</code><small>${esc(cfResources)}</small></div>
            <div><span>Systemd</span><code>${esc(units.length ? `${units.length} unit${units.length === 1 ? '' : 's'}` : 'no units')}</code><small>${esc((units.map(unit => unit.name).join(' / ')) || '--')}</small></div>
            <div><span>Restart</span><code>${esc(restart.required ? 'required' : 'not required')}</code><small>${esc(restartFields)}</small></div>
        </div>
        <div class="profile-preview-grid">
            ${instances.map(instance => `
                <div class="profile-instance-pill">
                    <span>${esc(instance.unit || `instance ${instance.index}`)}</span>
                    <code>${esc(instance.host)}:${esc(instance.port)}</code>
                    <span>GPU ${(instance.gpu_ids || []).join(',') || 'none'}</span>
                </div>
            `).join('') || '<div class="empty-state">No instances resolved.</div>'}
        </div>
        <div class="profile-preview-section">
            <div class="launcher-card-title">Command Preview</div>
            ${renderCommandPreview(plan)}
        </div>
        ${renderSystemdPreview(plan)}
        ${renderCloudflarePreview(plan)}
    `);
}

function renderInferenceProfiles(profiles) {
    const el = document.getElementById('inference-profiles-list');
    if (!el) return;
    if (!profiles.length) {
        setHtmlIfChanged(el, '<div class="empty-state">No inference profiles yet.</div>');
        return;
    }
    const html = profiles.map(profile => {
        const instances = profile.instances || [];
        const instanceText = instances.map(item => `${item.host}:${item.port}${item.gpu_ids && item.gpu_ids.length ? ` · GPU ${item.gpu_ids.join(',')}` : ''}`).join(' / ') || 'no instances';
        const configChips = profileConfigChips(profile);
        const profileOps = (inferenceOperationsData || []).filter(item => item.profile_id === profile.id);
        const op = profileOps.find(item => ACTIVE_INFERENCE_OPERATION_STATES.has(item.state));
        const recentOp = op || profileOps.find(item => ['failed', 'failed_interrupted', 'succeeded', 'canceled'].includes(item.state));
        const pendingAction = pendingInferenceProfileActions.get(profile.id);
        const busy = Boolean(op || pendingAction);
        const failedOp = recentOp && ['failed', 'failed_interrupted'].includes(recentOp.state) ? recentOp : null;
        const profileIdArg = jsArg(profile.id);
        const labelArg = jsArg(profile.display_name || profile.id);
        const profileIdData = esc(profile.id);
        const disabled = busy ? ' disabled' : '';
        return `
            <div class="profile-card" id="profile-card-${esc(profile.id)}">
                <div class="launcher-card-header">
                    <div>
                        <div class="launcher-card-title">${esc(profile.display_name || profile.id)}</div>
                        <div class="launcher-card-meta">${esc(profile.id)} · ${esc(profile.engine)} · ${esc(profile.engine_launcher_id || '--')}</div>
                    </div>
                    <div>${profileStateBadge(op ? op.state : profile.state)}</div>
                </div>
                <div class="profile-card-line">${esc(profile.model ? `${profile.model.artifact_id}@${profile.model.snapshot || ''}` : '--')}</div>
                <div class="profile-card-line">${esc(instanceText)}</div>
                ${configChips.length ? `<div class="profile-config-chips">${configChips.map(chip => `<span>${esc(chip)}</span>`).join('')}</div>` : ''}
                ${profile.restart_required ? '<div class="profile-warning">Restart required for saved changes.</div>' : ''}
                ${renderProfileOperationPanel(op || failedOp, pendingAction, { context: `profile-${profile.id}` })}
                <div class="model-actions profile-actions">
                    <button type="button" class="btn" onclick="editInferenceProfile(${profileIdArg})">Edit</button>
                    <button type="button" class="btn" onclick="loadProfileDetails(${profileIdArg})">Details</button>
                    <button type="button" class="btn primary" data-profile-id="${profileIdData}" data-profile-action="start"${disabled}>Start</button>
                    <button type="button" class="btn" data-profile-id="${profileIdData}" data-profile-action="stop"${disabled}>Stop</button>
                    <button type="button" class="btn" data-profile-id="${profileIdData}" data-profile-action="restart"${disabled}>Restart</button>
                    <button type="button" class="btn" onclick="loadProfileConnect(${profileIdArg})">Connect</button>
                    <button type="button" class="btn" onclick="loadProfileTest(${profileIdArg})">Test</button>
                    <button type="button" class="btn" onclick="loadProfileHealth(${profileIdArg})">Health</button>
                    <button type="button" class="btn" onclick="loadProfileLogs(${profileIdArg})">Logs</button>
                    <button type="button" class="btn" onclick="exportInferenceProfile(${profileIdArg})">Export</button>
                    <button type="button" class="btn danger" onclick="deleteInferenceProfile(${profileIdArg},${labelArg})">Delete</button>
                </div>
                <div class="profile-card-detail" id="profile-detail-${esc(profile.id)}"></div>
            </div>
        `;
    }).join('');
    setHtmlIfChanged(el, html);
    restoreProfileDetails();
}

function operationResultDetail(operation) {
    const result = operation && operation.result;
    if (!result || typeof result !== 'object') return {};
    return result.cause && typeof result.cause === 'object' ? result.cause : result;
}

function operationFailureMessage(operation) {
    const detail = operationResultDetail(operation);
    return detail.message || operation.error || 'Operation failed.';
}

function operationFailureLogs(operation) {
    const detail = operationResultDetail(operation);
    return detail.logs || '';
}

function operationRuntimeStatus(operation) {
    return operation && operation.runtime_status && typeof operation.runtime_status === 'object'
        ? operation.runtime_status
        : {};
}

function operationStateColor(state) {
    if (state === 'succeeded') return 'green';
    if (ACTIVE_INFERENCE_OPERATION_STATES.has(state)) return 'yellow';
    if (state === 'canceled') return '';
    return 'red';
}

function operationStepLabel(value) {
    return String(value || '').replace(/_/g, ' ');
}

function renderOperationSteps(operation) {
    const steps = (operation && operation.steps) || [];
    if (!steps.length) return '';
    return `
        <div class="profile-operation-steps">
            ${steps.map(step => `
                <span class="profile-operation-step ${esc(step.state || 'pending')}">${esc(operationStepLabel(step.name))}</span>
            `).join('')}
        </div>
    `;
}

function renderOperationFacts(detail) {
    detail = detail || {};
    const elapsed = detail.elapsed_seconds !== undefined && detail.elapsed_seconds !== null
        ? formatSeconds(detail.elapsed_seconds)
        : '';
    const timeout = detail.timeout_seconds !== undefined && detail.timeout_seconds !== null
        ? formatSeconds(detail.timeout_seconds)
        : '';
    const instanceLabel = detail.instance_index !== undefined && detail.instance_index !== null
        ? `${detail.wait_position && detail.wait_total ? `${detail.wait_position}/${detail.wait_total} · ` : ''}#${detail.instance_index}`
        : '';
    const facts = [
        instanceLabel ? ['Instance', instanceLabel] : null,
        detail.unit ? ['Unit', detail.unit] : null,
        detail.host && detail.port ? ['Target', `${detail.host}:${detail.port}`] : null,
        detail.systemd_state ? ['Systemd', detail.systemd_state] : null,
        detail.tcp_reachable !== undefined ? ['TCP', detail.tcp_reachable ? 'reachable' : 'waiting'] : null,
        detail.restart_count !== undefined && detail.restart_count !== null ? ['Restarts', detail.restart_count] : null,
        elapsed ? ['Elapsed', timeout ? `${elapsed} / ${timeout}` : elapsed] : null,
    ].filter(Boolean);
    if (!facts.length) return '';
    return `
        <div class="profile-operation-facts">
            ${facts.map(([label, value]) => `
                <div><span>${esc(label)}</span><code>${esc(value)}</code></div>
            `).join('')}
        </div>
    `;
}

function operationLogOutputId(operation, context = 'panel') {
    if (!operation || !operation.id) return '';
    const cleanContext = String(context || 'panel').replace(/[^a-zA-Z0-9_-]/g, '-');
    const cleanId = String(operation.id || '').replace(/[^a-zA-Z0-9_-]/g, '-');
    return `profile-operation-log-output-${cleanContext}-${cleanId}`;
}

function operationLogButton(operation, detail = {}, options = {}) {
    if (!operation || !operation.profile_id) return '';
    const targetId = options.logTargetId || operationLogOutputId(operation, options.context);
    if (!targetId) return '';
    const profileIdArg = jsArg(operation.profile_id);
    const targetIdArg = jsArg(targetId);
    const instanceIndex = operation.instance_index !== null && operation.instance_index !== undefined
        ? operation.instance_index
        : detail.instance_index;
    if (instanceIndex !== null && instanceIndex !== undefined) {
        const numericIndex = Number(instanceIndex);
        if (Number.isInteger(numericIndex)) {
            return `<button class="btn" type="button" onclick="loadOperationLogs(${profileIdArg}, ${numericIndex}, ${targetIdArg})">Logs</button>`;
        }
    }
    return `<button class="btn" type="button" onclick="loadOperationLogs(${profileIdArg}, null, ${targetIdArg})">Logs</button>`;
}

function renderProfileOperationPanel(operation, pendingAction = '', options = {}) {
    if (!operation) {
        if (!pendingAction) return '';
        return `
            <div class="profile-operation-panel active">
                <div class="profile-operation-head">
                    <div>
                        <div class="profile-operation-title">${esc(profileActionLabel(pendingAction))} queued</div>
                        <div class="profile-operation-sub">Waiting for operation record</div>
                    </div>
                    <span class="model-badge yellow">queued</span>
                </div>
                <div class="progress-bar"><div class="progress-fill yellow" style="width:8%"></div></div>
            </div>
        `;
    }
    const progress = Math.max(0, Math.min(100, Number(operation.progress || 0)));
    const color = operationStateColor(operation.state);
    const failed = ['failed', 'failed_interrupted'].includes(operation.state);
    const detail = failed ? operationResultDetail(operation) : operationRuntimeStatus(operation);
    const message = failed ? operationFailureMessage(operation) : '';
    const logs = failed ? operationFailureLogs(operation) : '';
    const panelClass = failed ? 'failed' : ACTIVE_INFERENCE_OPERATION_STATES.has(operation.state) ? 'active' : 'complete';
    const operationIdArg = jsArg(operation.id);
    const logTargetId = operationLogOutputId(operation, options.context);
    const logButton = operationLogButton(operation, detail, { ...options, logTargetId });
    const cachedLogOutput = logTargetId ? operationLogOutputCache.get(logTargetId) || '' : '';
    return `
        <div class="profile-operation-panel ${panelClass}">
            <div class="profile-operation-head">
                <div>
                    <div class="profile-operation-title">${esc(operationStepLabel(operation.kind || 'operation'))}</div>
                    <div class="profile-operation-sub">${esc(operationStepLabel(operation.current_step || operation.state || '--'))}</div>
                </div>
                <div class="profile-operation-actions">
                    <span class="model-badge ${color}">${esc(operation.state || 'unknown')}</span>
                    ${logButton}
                    ${operation.state === 'queued' ? `<button class="btn danger" type="button" onclick="cancelInferenceOperation(${operationIdArg})">Cancel</button>` : ''}
                </div>
            </div>
            <div class="progress-bar"><div class="progress-fill ${color}" style="width:${progress}%"></div></div>
            ${renderOperationSteps(operation)}
            ${renderOperationFacts(detail)}
            ${failed ? `
                <div class="profile-operation-error">${esc(message)}</div>
                ${logs ? `<pre class="profile-log-view profile-diagnostic-log">${esc(logs)}</pre>` : ''}
            ` : ''}
            ${logTargetId ? `<div class="profile-operation-log-output" id="${esc(logTargetId)}">${cachedLogOutput}</div>` : ''}
        </div>
    `;
}

function operationNeedsFailureLogHydration(operation) {
    return operation
        && operation.id
        && operation.profile_id
        && ['failed', 'failed_interrupted'].includes(operation.state)
        && !operationFailureLogs(operation);
}

function operationWithHydratedLogs(operation, logs) {
    const existing = operation && operation.result && typeof operation.result === 'object'
        ? { ...operation.result }
        : {};
    if (existing.cause && typeof existing.cause === 'object') {
        existing.cause = {
            ...existing.cause,
            message: existing.cause.message || operation.error || 'Operation failed.',
            logs,
        };
    } else {
        existing.message = existing.message || operation.error || 'Operation failed.';
        existing.logs = logs;
    }
    return { ...operation, result: existing };
}

async function hydrateInferenceFailureDiagnostics(operation, nodeId = selectedNodeId) {
    if (!operationNeedsFailureLogHydration(operation)) return;
    const key = `${nodeId || 'local'}:${operation.id}`;
    if (inferenceFailureLogFetches.has(key)) return;
    inferenceFailureLogFetches.add(key);
    try {
        const data = await api('GET', nodePathFor(nodeId, `/api/inference/profiles/${encodeURIComponent(operation.profile_id)}/logs?lines=120`));
        const logs = data.logs || '';
        if (!logs) return;
        mergeInferenceOperation(operationWithHydratedLogs(operation, logs), { hydrateLogs: false, suppressTerminalStatus: true });
    } catch (e) {
        // Log hydration is best-effort; the operation record remains authoritative.
    }
}

function hydrateVisibleInferenceFailures(nodeId = selectedNodeId) {
    (inferenceOperationsData || []).forEach(operation => {
        hydrateInferenceFailureDiagnostics(operation, nodeId);
    });
}

function profileDetailId(profileId) {
    return `profile-detail-${profileId}`;
}

function profileDetailOutputId(profileId) {
    return `profile-detail-output-${profileId}`;
}

function setProfileDetail(profileId, html, mode = 'custom') {
    profileDetailCache.set(profileId, html);
    profileDetailModes.set(profileId, mode);
    if (mode !== 'details') profileOutputCache.delete(profileId);
    setElementHtml(profileDetailId(profileId), html);
    restoreProfileOutput(profileId);
}

function restoreProfileDetails() {
    profileDetailCache.forEach((html, profileId) => {
        const el = document.getElementById(profileDetailId(profileId));
        if (el) {
            setHtmlIfChanged(el, html);
            restoreProfileOutput(profileId);
        }
    });
}

function clearProfileDetail(profileId) {
    profileDetailCache.delete(profileId);
    profileDetailModes.delete(profileId);
    profileOutputCache.delete(profileId);
    setElementHtml(profileDetailId(profileId), '');
}

function restoreProfileOutput(profileId) {
    if (!profileOutputCache.has(profileId)) return;
    const output = document.getElementById(profileDetailOutputId(profileId));
    if (output) setHtmlIfChanged(output, profileOutputCache.get(profileId));
}

function setProfileOutput(profileId, html) {
    const output = document.getElementById(profileDetailOutputId(profileId));
    if (output) {
        profileOutputCache.set(profileId, html);
        setHtmlIfChanged(output, html);
        return;
    }
    setProfileDetail(profileId, html, 'custom');
}

function instanceActionKey(profileId, instanceIndex) {
    return `${profileId}:${instanceIndex}`;
}

function profileById(profileId) {
    return inferenceProfilesData.find(item => item.id === profileId) || null;
}

function patchInferenceProfile(profile) {
    if (!profile || !profile.id) return false;
    let found = false;
    inferenceProfilesData = (inferenceProfilesData || []).map(item => {
        if (item.id !== profile.id) return item;
        found = true;
        return profile;
    });
    if (!found) inferenceProfilesData = [profile, ...(inferenceProfilesData || [])];
    if (currentAppView === 'inference' && activeInferenceTab === 'profiles') {
        renderInferenceProfiles(inferenceProfilesData);
    }
    renderProfileSelects();
    return true;
}

function removeInferenceProfile(profileId) {
    if (!profileId) return false;
    const before = (inferenceProfilesData || []).length;
    inferenceProfilesData = (inferenceProfilesData || []).filter(item => item.id !== profileId);
    pendingInferenceProfileActions.delete(profileId);
    Array.from(pendingInferenceInstanceActions.keys()).forEach(key => {
        if (String(key).startsWith(`${profileId}:`)) pendingInferenceInstanceActions.delete(key);
    });
    clearProfileDetail(profileId);
    if (currentAppView === 'inference' && activeInferenceTab === 'profiles') {
        renderInferenceProfiles(inferenceProfilesData);
    }
    renderProfileSelects();
    return inferenceProfilesData.length !== before;
}

function profileInstanceGpuText(instance) {
    const ids = instance.gpu_ids || [];
    if (!ids.length) return 'none';
    const gpus = (inferenceSystemData && inferenceSystemData.gpus) || [];
    return ids.map(id => {
        const gpu = gpus.find(item => Number(item.index) === Number(id));
        if (!gpu) return `GPU ${id}`;
        const total = Number(gpu.mem_total_mb || 0);
        const used = Number(gpu.mem_used_mb || 0);
        const free = Math.max(0, total - used);
        const mem = total ? ` · ${formatBytes(free * 1048576)} free` : '';
        const util = gpu.util_percent !== undefined ? ` · ${gpu.util_percent}% util` : '';
        return `GPU ${id} ${gpu.name || ''}${mem}${util}`;
    }).join(' / ');
}

function profileSummaryFacts(profile) {
    const common = profile.common || {};
    const deployment = profile.deployment || {};
    const exposure = profile.exposure || {};
    const gpuPolicy = deployment.gpu_policy || {};
    const facts = [
        ['Engine', profile.engine || '--'],
        ['Launcher', profile.engine_launcher_id || '--'],
        ['Model', profile.model ? `${profile.model.artifact_id}@${profile.model.snapshot || ''}` : '--'],
        ['Endpoint', exposure.mode === 'cloudflare' ? (exposure.hostname || (profile.cloudflare || {}).hostname || 'Cloudflare') : (exposure.mode || 'local')],
        ['Context', common.context_length || '--'],
        ['DType', common.dtype || '--'],
        ['Quantization', common.quantization || '--'],
        ['KV Cache', `${common.kv_cache_dtype || '--'}${common.kv_cache_memory_bytes ? ` · ${common.kv_cache_memory_bytes}` : ''}`],
        ['Parallelism', `TP ${common.tensor_parallel || 1} · PP ${common.pipeline_parallel || 1} · DP ${common.data_parallel || 1} · EP ${typeof common.expert_parallel === 'object' ? common.expert_parallel.size || 'on' : common.expert_parallel || 1}`],
        ['Capacity', `${common.max_concurrent_requests || '--'} seqs · ${common.max_batch_tokens || '--'} batch tokens`],
        ['Memory', `${common.gpu_memory_utilization || '--'} GPU target · ${common.cpu_offload_gb || 0}GB CPU offload`],
        ['Deployment', deployment.mode || 'single'],
        ['GPU claim', gpuPolicy.claim_mode || 'exclusive'],
    ];
    return facts.map(([label, value]) => `
        <div><span>${esc(label)}</span><code>${esc(value)}</code></div>
    `).join('');
}

function renderProfileIssues(plan) {
    const blockers = plan.blockers || [];
    const warnings = plan.warnings || [];
    return `
        ${blockers.length ? `<div class="profile-issue-list">${blockers.map(item => `<div class="model-job-error">${esc(item.message || item)}</div>`).join('')}</div>` : ''}
        ${warnings.length ? `<div class="profile-issue-list">${warnings.map(item => `<div class="profile-warning">${esc(item.message || item)}</div>`).join('')}</div>` : ''}
    `;
}

function renderCommandEnv(command) {
    const env = command.env || {};
    const rows = Object.entries(env);
    if (!rows.length) return '';
    return `
        <div class="profile-command-env">
            ${rows.map(([key, value]) => `<div><span>${esc(key)}</span><code>${esc(value)}</code></div>`).join('')}
        </div>
    `;
}

function renderCommandPreview(plan) {
    const commands = plan.command_preview || [];
    if (!commands.length) return '<div class="empty-state compact">No command preview available.</div>';
    return commands.map(command => `
        <div class="profile-command-block">
            <div class="launcher-card-meta">${esc(command.index !== undefined ? `instance ${command.index}` : 'command')}</div>
            <pre class="profile-log-view">${esc((command.argv || []).join(' ') || 'No command rendered.')}</pre>
            ${renderCommandEnv(command)}
        </div>
    `).join('');
}

function renderSystemdPreview(plan) {
    const units = ((plan.systemd_preview || {}).units || []);
    if (!units.length) return '';
    return `
        <details class="profile-command-preview profile-preview-section">
            <summary>Systemd Unit Preview</summary>
            ${units.map(unit => `
                <div class="profile-command-block">
                    <div class="launcher-card-meta">${esc(unit.name || `unit ${unit.index}`)}</div>
                    <pre class="profile-log-view">${esc(unit.content || '')}</pre>
                </div>
            `).join('')}
        </details>
    `;
}

function renderCloudflarePreview(plan) {
    const cloudflare = plan.cloudflare_plan || {};
    const resources = cloudflare.resources || [];
    const warnings = cloudflare.warnings || [];
    const blockers = cloudflare.blockers || [];
    if (!cloudflare.would_provision && !resources.length && !warnings.length && !blockers.length) return '';
    return `
        <div class="profile-preview-section">
            <div class="connect-section-header compact">
                <div>
                    <div class="launcher-card-title">Cloudflare Plan</div>
                    <div class="launcher-card-meta">${cloudflare.would_provision ? 'Provision on save/reconcile' : 'No Cloudflare resources planned'}</div>
                </div>
                ${connectBadge(cloudflare.mode || 'cloudflare', cloudflare.would_provision ? 'yellow' : '')}
            </div>
            ${blockers.length ? `<div class="profile-issue-list">${blockers.map(item => `<div class="model-job-error">${esc(item)}</div>`).join('')}</div>` : ''}
            ${warnings.length ? `<div class="profile-issue-list">${warnings.map(item => `<div class="profile-warning">${esc(item)}</div>`).join('')}</div>` : ''}
            <div class="profile-preview-resource-grid">
                ${resources.map(resource => `
                    <div>
                        <span>${esc(resource.kind || 'resource')}</span>
                        <code>${esc(resource.hostname || resource.secret || '--')}</code>
                    </div>
                `).join('') || '<div class="empty-state compact">No Cloudflare resources planned.</div>'}
            </div>
        </div>
    `;
}

function renderProfileInstanceRows(profile, healthData) {
    const profileIdData = esc(profile.id);
    const activeOp = (inferenceOperationsData || []).find(item => item.profile_id === profile.id && ACTIVE_INFERENCE_OPERATION_STATES.has(item.state));
    const healthByIndex = new Map((healthData.instances || []).map(item => [Number(item.index), item]));
    const rows = (profile.instances || []).map(instance => {
        const index = Number(instance.index || 0);
        const health = healthByIndex.get(index) || instance;
        const pending = pendingInferenceInstanceActions.get(instanceActionKey(profile.id, index));
        const disabled = activeOp || pending ? ' disabled' : '';
        const profileIdArg = jsArg(profile.id);
        const indexArg = Number(index);
        return `
            <div class="profile-instance-row">
                <div>
                    <strong>#${esc(index)}</strong>
                    <span>${profileStateBadge(health.health || instance.state || 'unknown')}</span>
                </div>
                <div><span>Endpoint</span><code>${esc(instance.host || '127.0.0.1')}:${esc(instance.port || '--')}</code></div>
                <div><span>GPU</span><code>${esc(profileInstanceGpuText(instance))}</code></div>
                <div><span>Unit</span><code>${esc(instance.unit || '--')}</code></div>
                <div><span>Systemd</span><code>${esc(health.systemd_state || '--')} · TCP ${health.tcp_reachable ? 'yes' : 'no'}</code></div>
                <div class="model-actions">
                    <button type="button" class="btn" data-profile-id="${profileIdData}" data-instance-index="${indexArg}" data-instance-action="start"${disabled}>Start</button>
                    <button type="button" class="btn" data-profile-id="${profileIdData}" data-instance-index="${indexArg}" data-instance-action="stop"${disabled}>Stop</button>
                    <button type="button" class="btn" data-profile-id="${profileIdData}" data-instance-index="${indexArg}" data-instance-action="restart"${disabled}>Restart</button>
                    <button type="button" class="btn" onclick="loadProfileLogs(${profileIdArg}, ${indexArg})">Logs</button>
                    <button type="button" class="btn" onclick="loadProfileTest(${profileIdArg}, ${indexArg})">Test</button>
                </div>
            </div>
        `;
    }).join('');
    return rows || '<div class="empty-state compact">No resolved instances.</div>';
}

function renderProfileDetail(profile, healthData, plan, partialErrors = {}) {
    const profileIdArg = jsArg(profile.id);
    const chips = profileConfigChips(profile);
    const errorKeys = Object.keys(partialErrors || {});
    const partialWarning = errorKeys.length
        ? `<div class="profile-warning">Some live detail could not be loaded: ${esc(errorKeys.join(', '))}</div>`
        : '';
    return `
        <div class="profile-detail-panel">
            <div class="profile-detail-header">
                <div>
                    <div class="launcher-card-title">${esc(profile.display_name || profile.id)}</div>
                    <div class="launcher-card-meta">${esc(profile.id)} · ${esc(profile.engine || '--')} · ${esc(profile.engine_launcher_id || '--')}</div>
                </div>
                <div class="connect-status-row">
                    ${profileStateBadge(healthData.health || profile.state)}
                    ${chips.slice(0, 4).map(chip => `<span class="model-badge">${esc(chip)}</span>`).join('')}
                </div>
            </div>
            <div class="profile-detail-actions">
                <button type="button" class="btn" onclick="loadProfileDetails(${profileIdArg})">Refresh</button>
                <button type="button" class="btn" onclick="loadProfileTest(${profileIdArg})">Test</button>
                <button type="button" class="btn" onclick="loadProfileLogs(${profileIdArg})">Logs</button>
                <button type="button" class="btn" onclick="loadProfileConnect(${profileIdArg})">Connect</button>
                <button type="button" class="btn" onclick="exportInferenceProfile(${profileIdArg})">Export</button>
                <button type="button" class="btn" onclick="clearProfileDetail(${profileIdArg})">Close</button>
            </div>
            ${partialWarning}
            ${profile.restart_required ? `<div class="profile-warning">Restart required: ${esc((profile.restart_required_fields || []).join(', ') || 'saved runtime changes')}</div>` : ''}
            <div class="connect-facts profile-detail-facts">${profileSummaryFacts(profile)}</div>
            <div class="connect-section-header compact">
                <div class="launcher-card-title">Instances</div>
            </div>
            <div class="profile-instance-table">${renderProfileInstanceRows(profile, healthData)}</div>
            ${renderProfileIssues(plan)}
            <details class="profile-command-preview">
                <summary>Command Preview</summary>
                ${renderCommandPreview(plan)}
            </details>
            <div class="profile-detail-output" id="${esc(profileDetailOutputId(profile.id))}"></div>
        </div>
    `;
}

async function loadProfileDetails(profileId) {
    setProfileDetail(profileId, '<div class="empty-state compact">Loading profile details...</div>', 'details');
    try {
        const detail = await api('GET', modelNodePath(`/api/inference/profiles/${encodeURIComponent(profileId)}/detail`));
        const profile = detail.profile || profileById(profileId) || { id: profileId, instances: [] };
        const healthData = detail.instances || { instances: profile.instances || [], health: profile.state || 'unknown' };
        const plan = detail.plan || { blockers: [], warnings: [], command_preview: [], systemd_preview: { units: [] } };
        setProfileDetail(profileId, renderProfileDetail(profile, healthData, plan, detail.partial_errors || {}), 'details');
    } catch (e) {
        setProfileDetail(profileId, `<div class="model-job-error">${esc(e.message)}</div>`, 'details');
    }
}

function renderProfileHealthOutput(data) {
    return `
        <div class="profile-health-summary">${profileStateBadge(data.health)}</div>
        ${(data.instances || []).map(item => `
            <div class="profile-card-line">${esc(item.unit)} · ${esc(item.systemd_state)} · TCP ${item.tcp_reachable ? 'yes' : 'no'} · ${esc(item.health || 'unknown')}</div>
        `).join('') || '<div class="empty-state compact">No instances.</div>'}
    `;
}

function renderProfileTestForm(profileId, selectedInstance = null, result = null, error = '') {
    const profile = profileById(profileId) || {};
    const options = (profile.instances || []).map(instance => {
        const index = Number(instance.index || 0);
        return `<option value="${index}" ${selectedInstance !== null && Number(selectedInstance) === index ? 'selected' : ''}>Instance ${index} · ${esc(instance.host || '127.0.0.1')}:${esc(instance.port || '--')}</option>`;
    }).join('');
    return `
        <div class="profile-test-panel">
            <div class="connect-section-header">
                <div>
                    <div class="launcher-card-title">Manual Test</div>
                    <div class="launcher-card-meta">Local instance request from the selected node</div>
                </div>
            </div>
            <div class="profile-test-grid">
                <select id="profile-test-instance-${esc(profileId)}">
                    ${options || '<option value="">Default instance</option>'}
                </select>
                <select id="profile-test-method-${esc(profileId)}">
                    <option value="GET">GET</option>
                    <option value="POST">POST</option>
                </select>
                <input type="text" id="profile-test-path-${esc(profileId)}" value="/v1/models" autocomplete="off">
            </div>
            <textarea id="profile-test-body-${esc(profileId)}" rows="5" placeholder='{"model":"...","messages":[{"role":"user","content":"ping"}]}'></textarea>
            <div class="profile-detail-actions">
                <button type="button" class="btn primary" onclick="runProfileTest(${jsArg(profileId)})">Run Test</button>
            </div>
            ${error ? `<div class="model-job-error">${esc(error)}</div>` : ''}
            ${result ? `
                <div class="profile-test-result">
                    <div class="connect-facts">
                        <div><span>Status</span><code>${esc(result.status_code || '--')}</code></div>
                        <div><span>Latency</span><code>${esc(result.latency_ms !== undefined ? `${result.latency_ms}ms` : '--')}</code></div>
                        <div><span>URL</span><code>${esc(result.url || '--')}</code></div>
                        <div><span>Target</span><code>${esc(result.target_mode || '--')} · instance ${esc(result.instance_index ?? '--')}</code></div>
                    </div>
                    <pre class="profile-log-view">${esc(result.body_preview || '')}</pre>
                </div>
            ` : ''}
        </div>
    `;
}

async function loadProfileTest(profileId, instanceIndex = null) {
    setProfileOutput(profileId, renderProfileTestForm(profileId, instanceIndex));
}

async function runProfileTest(profileId) {
    const selected = document.getElementById(`profile-test-instance-${profileId}`);
    const methodEl = document.getElementById(`profile-test-method-${profileId}`);
    const pathEl = document.getElementById(`profile-test-path-${profileId}`);
    const bodyEl = document.getElementById(`profile-test-body-${profileId}`);
    const instanceValue = selected ? selected.value : '';
    const payload = {
        method: methodEl ? methodEl.value : 'GET',
        path: pathEl ? pathEl.value : '/v1/models',
        timeout: 60,
    };
    if (instanceValue !== '') payload.instance = Number(instanceValue);
    const bodyText = bodyEl ? bodyEl.value.trim() : '';
    try {
        if (bodyText) payload.body = JSON.parse(bodyText);
    } catch (e) {
        setProfileOutput(profileId, renderProfileTestForm(profileId, instanceValue === '' ? null : Number(instanceValue), null, `Request body must be valid JSON: ${e.message}`));
        return;
    }
    setProfileOutput(profileId, '<div class="empty-state compact">Running test request...</div>');
    try {
        const data = await api('POST', modelNodePath(`/api/inference/profiles/${encodeURIComponent(profileId)}/test`), payload);
        setProfileOutput(profileId, renderProfileTestForm(profileId, data.instance_index, data));
    } catch (e) {
        setProfileOutput(profileId, renderProfileTestForm(profileId, instanceValue === '' ? null : Number(instanceValue), null, e.message));
    }
}

async function exportInferenceProfile(profileId) {
    try {
        const data = await api('GET', modelNodePath(`/api/inference/profiles/${encodeURIComponent(profileId)}/export`));
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${profileId}-inference-profile.json`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        setInferenceStatus(`Exported profile ${profileId}.`);
    } catch (e) {
        setInferenceError(e.message);
    }
}

function editInferenceProfile(profileId) {
    const profile = inferenceProfilesData.find(item => item.id === profileId);
    if (!profile) return;
    fillProfileForm(profile);
    setInferenceStatus(`Editing ${profile.id}.`);
}

async function deleteInferenceProfile(profileId, label) {
    if (!confirm(`Delete inference profile "${label}"?`)) return;
    setInferenceError('');
    try {
        const result = await api('DELETE', modelNodePath(`/api/inference/profiles/${encodeURIComponent(profileId)}`));
        const deletedId = (result && result.deleted) || profileId;
        removeInferenceProfile(deletedId);
        setInferenceStatus(`Deleted profile ${deletedId}.`);
    } catch (e) {
        setInferenceError(e.message);
    }
}

function isTerminalInferenceOperation(operation) {
    return operation && ['succeeded', 'failed', 'failed_interrupted', 'canceled'].includes(operation.state);
}

function isLocalInferenceNode(nodeId) {
    return !isMaster || !nodeId || nodeId === selfNodeId;
}

function selectedInferenceNodeIds() {
    const ids = new Set();
    if (selectedNodeId) ids.add(String(selectedNodeId));
    if (isLocalInferenceNode(selectedNodeId) && selfNodeId) ids.add(String(selfNodeId));
    const node = nodes.find(item => item.node_id === selectedNodeId || item.config_node_id === selectedNodeId);
    if (node) {
        if (node.node_id) ids.add(String(node.node_id));
        if (node.config_node_id) ids.add(String(node.config_node_id));
    }
    return ids;
}

function websocketEventMatchesSelectedNode(event) {
    const eventIds = [event && event.node_id, event && event.real_node_id].filter(Boolean).map(String);
    if (!eventIds.length) return isLocalInferenceNode(selectedNodeId);
    const selectedIds = selectedInferenceNodeIds();
    return eventIds.some(id => selectedIds.has(id));
}

function shouldUseInferenceOperationWs(nodeId) {
    return wsConnected;
}

function mergeInferenceOperation(operation, options = {}) {
    if (!operation || !operation.id) return;
    inferenceOperationsData = [
        operation,
        ...(inferenceOperationsData || []).filter(item => item.id !== operation.id),
    ];
    if (operation.profile_id && isTerminalInferenceOperation(operation)) {
        pendingInferenceProfileActions.delete(operation.profile_id);
        if (operation.instance_index !== null && operation.instance_index !== undefined) {
            pendingInferenceInstanceActions.delete(instanceActionKey(operation.profile_id, operation.instance_index));
        }
        if (!options.suppressTerminalStatus) {
            if (operation.state === 'succeeded') {
                setInferenceStatus(`${operation.kind || 'Operation'} completed for ${operation.profile_id}.`);
            } else if (operation.state === 'failed') {
                setInferenceError(operationFailureMessage(operation));
            }
        }
    }
    if (currentAppView === 'inference' && activeInferenceTab === 'profiles' && !options.suppressProfileRender) {
        renderInferenceProfiles(inferenceProfilesData);
    }
    renderInferenceOperations(inferenceOperationsData);
    if (options.hydrateLogs !== false) {
        hydrateInferenceFailureDiagnostics(operation);
    }
    updateInferencePolling();
}

function operationProfileState(operation) {
    const result = operation && operation.result && typeof operation.result === 'object' ? operation.result : {};
    if (result.state) return result.state;
    const kind = String(operation && operation.kind || '');
    if (!operation || operation.state !== 'succeeded') {
        return operation && operation.state === 'failed' && (kind.includes('start') || kind.includes('restart')) ? 'failed' : null;
    }
    if (kind.includes('stop')) return 'stopped';
    if (kind.includes('start') || kind.includes('restart')) return 'running';
    return null;
}

function operationInstanceState(operation) {
    if (!operation || operation.state === 'canceled') return null;
    const kind = String(operation.kind || '');
    if (operation.state === 'failed' && (kind.includes('start') || kind.includes('restart'))) return 'failed';
    if (operation.state !== 'succeeded') return null;
    if (kind.includes('stop')) return 'stopped';
    if (kind.includes('start') || kind.includes('restart')) return 'running';
    return null;
}

function operationInstanceIndexes(operation) {
    if (!operation) return [];
    if (operation.instance_index !== null && operation.instance_index !== undefined) {
        return [Number(operation.instance_index)].filter(Number.isInteger);
    }
    const result = operation.result && typeof operation.result === 'object' ? operation.result : {};
    return (result.instances || [])
        .map(item => Number(item && item.index))
        .filter(Number.isInteger);
}

function patchProfileFromOperation(operation) {
    if (!operation || !operation.profile_id || !isTerminalInferenceOperation(operation)) return false;
    const profileState = operationProfileState(operation);
    const instanceState = operationInstanceState(operation);
    if (!profileState && !instanceState) return false;
    let changed = false;
    const indexes = new Set(operationInstanceIndexes(operation));
    inferenceProfilesData = (inferenceProfilesData || []).map(profile => {
        if (profile.id !== operation.profile_id) return profile;
        changed = true;
        const next = { ...profile };
        if (profileState) next.state = profileState;
        if (instanceState && Array.isArray(profile.instances)) {
            const shouldUpdateAll = !indexes.size && !String(operation.kind || '').startsWith('instance_');
            next.instances = profile.instances.map(instance => {
                const index = Number(instance && instance.index);
                if (!shouldUpdateAll && !indexes.has(index)) return instance;
                return { ...instance, state: instanceState };
            });
        }
        return next;
    });
    if (changed && currentAppView === 'inference' && activeInferenceTab === 'profiles') {
        renderInferenceProfiles(inferenceProfilesData);
    }
    return changed;
}

function handleInferenceOperationEvent(operation, event = {}) {
    if (!websocketEventMatchesSelectedNode(event)) return;
    const terminal = isTerminalInferenceOperation(operation);
    mergeInferenceOperation(operation, { suppressProfileRender: terminal });
    if (terminal && currentAppView === 'inference') {
        const patched = patchProfileFromOperation(operation);
        if (!patched && activeInferenceTab === 'profiles') renderInferenceProfiles(inferenceProfilesData);
        if (operation.profile_id && profileDetailModes.get(operation.profile_id) === 'details') {
            loadProfileDetails(operation.profile_id);
        }
    }
}

function mergeInferenceOperationSnapshot(operations, nodeId = selectedNodeId) {
    const list = Array.isArray(operations) ? operations : [];
    inferenceOperationsData = list;
    let needsProfileRender = false;
    list.forEach(operation => {
        if (!isTerminalInferenceOperation(operation) || !operation.profile_id) return;
        pendingInferenceProfileActions.delete(operation.profile_id);
        if (operation.instance_index !== null && operation.instance_index !== undefined) {
            pendingInferenceInstanceActions.delete(instanceActionKey(operation.profile_id, operation.instance_index));
        }
        const patched = patchProfileFromOperation(operation);
        if (!patched && currentAppView === 'inference' && activeInferenceTab === 'profiles') {
            needsProfileRender = true;
        }
        if (operation.profile_id && profileDetailModes.get(operation.profile_id) === 'details') {
            loadProfileDetails(operation.profile_id);
        }
    });
    if (needsProfileRender) renderInferenceProfiles(inferenceProfilesData);
    renderInferenceOperations(inferenceOperationsData);
    hydrateVisibleInferenceFailures(nodeId);
    updateInferencePolling();
}

function modelJobIsTerminal(job) {
    return job && ['ready', 'failed', 'failed_interrupted', 'canceled'].includes(job.state);
}

function mergeModelArtifactFromJob(job) {
    const artifact = job && job.artifact && typeof job.artifact === 'object' ? job.artifact : null;
    if (!artifact || !artifact.id) return false;
    if (!inferenceModelData) inferenceModelData = { artifacts: [], jobs: [] };
    const artifacts = [
        artifact,
        ...((inferenceModelData.artifacts || []).filter(item => item.id !== artifact.id)),
    ].sort((a, b) => String(a.id || '').localeCompare(String(b.id || '')));
    inferenceModelData = { ...inferenceModelData, artifacts };
    renderProfileSelects();
    return true;
}

function renderModelInventoryState() {
    renderProfileSelects();
    if (currentAppView !== 'inference') return;
    if (['models', 'storage'].includes(activeInferenceTab)) {
        renderInferenceSummary(inferenceModelData || { artifacts: [], jobs: [] }, inferenceStorageData || {});
        renderModelInventory((inferenceModelData && inferenceModelData.artifacts) || []);
    }
}

function renderPolledModelState() {
    if (currentAppView !== 'inference' || !inferenceModelData) return;
    if (activeInferenceTab === 'jobs') {
        renderModelJobs(inferenceModelData.jobs || []);
    } else if (['models', 'storage'].includes(activeInferenceTab)) {
        renderInferenceSummary(inferenceModelData, inferenceStorageData || {});
        renderModelInventory(inferenceModelData.artifacts || []);
        renderModelJobs(inferenceModelData.jobs || []);
    }
}

function removeModelArtifactLocal(artifactId) {
    if (!artifactId || !inferenceModelData) return false;
    const before = (inferenceModelData.artifacts || []).length;
    inferenceModelData = {
        ...inferenceModelData,
        artifacts: (inferenceModelData.artifacts || []).filter(item => item.id !== artifactId),
    };
    renderModelInventoryState();
    return (inferenceModelData.artifacts || []).length !== before;
}

function patchModelVerification(result) {
    if (!result || !result.artifact_id || !inferenceModelData) return false;
    const snapshotId = result.snapshot;
    const state = result.valid ? 'ready' : 'degraded';
    let changed = false;
    inferenceModelData = {
        ...inferenceModelData,
        artifacts: (inferenceModelData.artifacts || []).map(artifact => {
            if (artifact.id !== result.artifact_id) return artifact;
            changed = true;
            const snapshots = { ...(artifact.snapshots || {}) };
            if (snapshotId) {
                snapshots[snapshotId] = {
                    ...(snapshots[snapshotId] || {}),
                    state,
                    last_verified_at: Math.floor(Date.now() / 1000),
                };
            }
            const next = { ...artifact, snapshots };
            if (!snapshotId || artifact.active_snapshot === snapshotId) next.active_snapshot_state = state;
            return next;
        }),
    };
    if (changed) renderModelInventoryState();
    return changed;
}

function mergeModelJob(job) {
    if (!job || !job.id) return;
    if (!inferenceModelData) inferenceModelData = { artifacts: [], jobs: [] };
    const jobs = [
        job,
        ...((inferenceModelData.jobs || []).filter(item => item.id !== job.id)),
    ];
    inferenceModelData = { ...inferenceModelData, jobs };
    const artifactChanged = mergeModelArtifactFromJob(job);
    if (currentAppView === 'inference') {
        if (activeInferenceTab === 'jobs') renderModelJobs(jobs);
        if (['models', 'storage'].includes(activeInferenceTab)) {
            renderInferenceSummary(inferenceModelData, inferenceStorageData || {});
            if (artifactChanged) renderModelInventory(inferenceModelData.artifacts || []);
            renderModelJobs(jobs);
        }
    }
    if (job.state === 'ready' && !artifactChanged && currentAppView === 'inference') {
        refreshActiveInferenceTab();
    }
    updateInferencePolling();
}

function handleModelJobEvent(job, event = {}) {
    if (!websocketEventMatchesSelectedNode(event)) return;
    mergeModelJob(job);
}

function profileActionLabel(action) {
    if (action === 'start') return 'Start';
    if (action === 'stop') return 'Stop';
    if (action === 'restart') return 'Restart';
    return action || 'Action';
}

async function surfaceActiveInferenceOperation(error, nodeId = selectedNodeId) {
    const detail = error && error.detail;
    const activeId = detail && detail.active_operation_id;
    if (!activeId) return false;
    try {
        const operation = await api('GET', nodePathFor(nodeId, `/api/inference/operations/${encodeURIComponent(activeId)}`));
        mergeInferenceOperation(operation);
        setInferenceError(`${detail.message || 'An inference operation is already active.'} ${operation.kind || detail.kind || ''} ${operation.current_step || operation.state || ''}`.trim());
        return true;
    } catch (_ignored) {
        return false;
    }
}

async function runProfileAction(profileId, action, button) {
    if (!profileId || !['start', 'stop', 'restart'].includes(action)) return;
    const nodeId = selectedNodeId;
    setInferenceError('');
    setInferenceStatus(`${profileActionLabel(action)} queued for ${profileId}...`);
    pendingInferenceProfileActions.set(profileId, action);
    if (button) button.disabled = true;
    renderInferenceProfiles(inferenceProfilesData);
    try {
        const operation = await api('POST', nodePathFor(nodeId, `/api/inference/profiles/${encodeURIComponent(profileId)}/${action}`));
        mergeInferenceOperation(operation);
        setInferenceStatus(`${profileActionLabel(action)} operation queued for ${profileId}.`);
    } catch (e) {
        pendingInferenceProfileActions.delete(profileId);
        if (button) button.disabled = false;
        const surfaced = await surfaceActiveInferenceOperation(e, nodeId);
        if (!surfaced) setInferenceError(e.message);
        renderInferenceProfiles(inferenceProfilesData);
    }
}

async function runInstanceAction(profileId, instanceIndex, action, button) {
    if (!profileId || !Number.isInteger(instanceIndex) || !['start', 'stop', 'restart'].includes(action)) return;
    const nodeId = selectedNodeId;
    const key = instanceActionKey(profileId, instanceIndex);
    setInferenceError('');
    setInferenceStatus(`${profileActionLabel(action)} queued for ${profileId}[${instanceIndex}]...`);
    pendingInferenceInstanceActions.set(key, action);
    if (button) button.disabled = true;
    renderInferenceProfiles(inferenceProfilesData);
    try {
        const operation = await api(
            'POST',
            nodePathFor(nodeId, `/api/inference/profiles/${encodeURIComponent(profileId)}/instances/${encodeURIComponent(instanceIndex)}/${action}`)
        );
        mergeInferenceOperation(operation);
        setInferenceStatus(`${profileActionLabel(action)} operation queued for ${profileId}[${instanceIndex}].`);
    } catch (e) {
        pendingInferenceInstanceActions.delete(key);
        if (button) button.disabled = false;
        const surfaced = await surfaceActiveInferenceOperation(e, nodeId);
        if (!surfaced) setInferenceError(e.message);
        renderInferenceProfiles(inferenceProfilesData);
    }
}

async function cancelInferenceOperation(operationId) {
    if (!operationId) return;
    if (!confirm('Cancel this queued inference operation?')) return;
    setInferenceError('');
    try {
        const operation = await api('POST', modelNodePath(`/api/inference/operations/${encodeURIComponent(operationId)}/cancel`));
        mergeInferenceOperation(operation);
        setInferenceStatus(`Canceled operation ${operationId}.`);
    } catch (e) {
        setInferenceError(e.message);
    }
}

function profileLogRequest(profileId, instanceIndex = null) {
    const label = instanceIndex === null ? 'profile' : `instance ${instanceIndex}`;
    const path = instanceIndex === null
        ? `/api/inference/profiles/${encodeURIComponent(profileId)}/logs?lines=120`
        : `/api/inference/profiles/${encodeURIComponent(profileId)}/instances/${encodeURIComponent(instanceIndex)}/logs?lines=180`;
    return { label, path };
}

function renderProfileLogOutput(label, logs) {
    return `
        <div class="connect-section-header compact">
            <div class="launcher-card-title">${esc(label)} logs</div>
        </div>
        <pre class="profile-log-view">${esc(logs || 'No logs.')}</pre>
    `;
}

async function loadProfileLogs(profileId, instanceIndex = null) {
    const detail = document.getElementById(`profile-detail-${profileId}`);
    const request = profileLogRequest(profileId, instanceIndex);
    if (detail) setProfileOutput(profileId, `<div class="empty-state compact">Loading ${esc(request.label)} logs...</div>`);
    try {
        const data = await api('GET', modelNodePath(request.path));
        const html = renderProfileLogOutput(request.label, data.logs || '');
        if (detail) setProfileOutput(profileId, html);
    } catch (e) {
        if (detail) setProfileOutput(profileId, `<div class="model-job-error">${esc(e.message)}</div>`);
    }
}

async function loadOperationLogs(profileId, instanceIndex = null, targetId = '') {
    const target = targetId ? document.getElementById(targetId) : null;
    const request = profileLogRequest(profileId, instanceIndex);
    const loadingHtml = `<div class="empty-state compact">Loading ${esc(request.label)} logs...</div>`;
    if (target) {
        operationLogOutputCache.set(targetId, loadingHtml);
        setHtmlIfChanged(target, loadingHtml);
    }
    try {
        const data = await api('GET', modelNodePath(request.path));
        const html = renderProfileLogOutput(request.label, data.logs || '');
        if (target) {
            operationLogOutputCache.set(targetId, html);
            setHtmlIfChanged(target, html);
        } else {
            await loadProfileLogs(profileId, instanceIndex);
        }
    } catch (e) {
        if (target) {
            const errorHtml = `<div class="model-job-error">${esc(e.message)}</div>`;
            operationLogOutputCache.set(targetId, errorHtml);
            setHtmlIfChanged(target, errorHtml);
        } else {
            setInferenceError(e.message);
        }
    }
}

async function loadProfileHealth(profileId) {
    const detail = document.getElementById(`profile-detail-${profileId}`);
    if (detail) setProfileOutput(profileId, '<div class="empty-state compact">Checking health...</div>');
    try {
        const data = await api('GET', modelNodePath(`/api/inference/profiles/${encodeURIComponent(profileId)}/health`));
        if (detail) setProfileOutput(profileId, renderProfileHealthOutput(data));
    } catch (e) {
        if (detail) setProfileOutput(profileId, `<div class="model-job-error">${esc(e.message)}</div>`);
    }
}

function connectBadge(label, state = '') {
    return `<span class="model-badge ${esc(state)}">${esc(label)}</span>`;
}

function copyButton(value, label = 'Copy') {
    const text = String(value || '');
    if (!text || text === '--') return '';
    return `<button type="button" class="btn copy-btn" data-copy="${esc(text)}" onclick="copyText(this.dataset.copy, this)">${esc(label)}</button>`;
}

function renderClientBundle(bundle, secrets = {}) {
    if (!bundle) {
        return '<div class="empty-state">No client bundle available.</div>';
    }
    if (bundle.requires_instance) {
        return `<div class="profile-warning">${esc(bundle.message || 'Select an instance to render a bundle.')}</div>`;
    }
    const examples = bundle.examples || {};
    const headers = bundle.headers || {};
    const missing = (bundle.secret_state && bundle.secret_state.missing_secret_actions) || [];
    const headerRows = Object.entries(headers);
    const oneTimeRows = [
        secrets.engine_api_key ? ['Engine API key', secrets.engine_api_key] : null,
        secrets.client_secret ? ['Cloudflare Client Secret', secrets.client_secret] : null,
    ].filter(Boolean);
    const exampleRows = [
        ['curl', examples.curl],
        ['Python', examples.python_openai],
        ['LiteLLM', examples.litellm],
    ];
    return `
        <div class="profile-connect-panel">
            <div class="connect-section-header">
                <div>
                    <div class="launcher-card-title">${esc(bundle.name || 'Connection')}</div>
                    <div class="launcher-card-meta">${esc(bundle.id || 'default')} · ${esc((bundle.target || {}).type || 'profile')}</div>
                </div>
                ${connectBadge(bundle.exposure_mode || 'local')}
            </div>
            <div class="connect-facts">
                <div>
                    <span>Base URL</span>
                    <div class="connect-copy-row"><code>${esc(bundle.base_url || '--')}</code>${copyButton(bundle.base_url)}</div>
                </div>
                <div>
                    <span>Model</span>
                    <div class="connect-copy-row"><code>${esc(bundle.model || '--')}</code>${copyButton(bundle.model)}</div>
                </div>
            </div>
            ${headerRows.length ? `
                <div class="connect-section-header compact">
                    <div class="launcher-card-title">Required Headers</div>
                </div>
                <div class="profile-secret-strip">
                    ${headerRows.map(([key, value]) => `
                        <div><span>${esc(key)}</span><div class="connect-copy-row"><code>${esc(value)}</code>${copyButton(value)}</div></div>
                    `).join('')}
                </div>
            ` : '<div class="profile-card-line">No auth headers configured for this bundle.</div>'}
            ${oneTimeRows.length ? `
                <div class="profile-one-time-secret">
                    <div class="launcher-card-title">Shown Once</div>
                    ${oneTimeRows.map(([label, value]) => `<div><span>${esc(label)}</span><div class="connect-copy-row"><code>${esc(value)}</code>${copyButton(value)}</div></div>`).join('')}
                </div>
            ` : ''}
            ${missing.length ? `<div class="profile-card-line">Missing one-time values: ${esc(missing.join(', '))}</div>` : ''}
            <div class="connect-section-header compact">
                <div class="launcher-card-title">Examples</div>
            </div>
            <div class="client-example-grid">
                ${exampleRows.map(([label, value]) => `
                    <div class="client-example-card">
                        <div class="client-example-head">
                            <div class="launcher-card-meta">${esc(label)}</div>
                            ${copyButton(value)}
                        </div>
                        <pre class="profile-log-view">${esc(value || '')}</pre>
                    </div>
                `).join('')}
            </div>
        </div>
    `;
}

function renderInstanceBundleOptions(bundles) {
    const items = Array.isArray(bundles) ? bundles : [];
    if (!items.length) return '';
    return `
        <div class="profile-connect-panel instance-bundle-panel">
            <div class="connect-section-header compact">
                <div>
                    <div class="launcher-card-title">Instance Endpoints</div>
                    <div class="launcher-card-meta">Replicated profiles expose one local endpoint per resolved instance.</div>
                </div>
            </div>
            <div class="instance-bundle-list">
                ${items.map(bundle => {
                    const instance = bundle.instance || {};
                    const target = bundle.target || {};
                    const index = instance.index ?? target.instance_index ?? '--';
                    const gpuText = (instance.gpu_ids || []).length ? `GPU ${(instance.gpu_ids || []).join(',')}` : 'GPU none';
                    const state = instance.state || 'planned';
                    return `
                        <div class="instance-bundle-row">
                            <div class="instance-bundle-main">
                                <div>
                                    <div class="launcher-card-title">Instance ${esc(index)}</div>
                                    <div class="launcher-card-meta">${esc(gpuText)} · ${esc(state)} · ${esc(instance.unit || '--')}</div>
                                </div>
                                ${connectBadge(bundle.exposure_mode || 'local')}
                            </div>
                            <div class="connect-copy-row">
                                <code>${esc(bundle.base_url || '--')}</code>
                                ${copyButton(bundle.base_url)}
                            </div>
                        </div>
                    `;
                }).join('')}
            </div>
        </div>
    `;
}

function renderCloudflareCleanupRecords(profileId, records) {
    const items = Array.isArray(records) ? records : [];
    if (!items.length) return '';
    const profileIdArg = jsArg(profileId);
    return `
        <div class="profile-connect-panel cleanup-record-panel">
            <div class="connect-section-header compact">
                <div>
                    <div class="launcher-card-title">Cloudflare Cleanup Pending</div>
                    <div class="launcher-card-meta">External resources need retry or local cleanup metadata can be forgotten.</div>
                </div>
                ${connectBadge(`${items.length} pending`, 'yellow')}
            </div>
            <div class="cleanup-record-list">
                ${items.map(record => {
                    const recordIdArg = jsArg(record.id);
                    const payload = record.payload || {};
                    const target = payload.hostname || payload.id || '--';
                    return `
                        <div class="cleanup-record-row">
                            <div>
                                <div class="launcher-card-title">${esc(record.kind || 'cloudflare')}</div>
                                <div class="launcher-card-meta">${esc(target)} · attempts ${esc(record.attempts || 0)}</div>
                                ${record.error ? `<div class="model-job-error">${esc(record.error)}</div>` : ''}
                            </div>
                            <div class="model-actions">
                                <button class="btn" onclick="retryInferenceCleanup(${profileIdArg},${recordIdArg})">Retry</button>
                                <button class="btn danger" onclick="forgetInferenceCleanup(${profileIdArg},${recordIdArg})">Forget</button>
                            </div>
                        </div>
                    `;
                }).join('')}
            </div>
        </div>
    `;
}

function renderConnectionPosture(profileId, context) {
    const exposure = context.exposure || {};
    const mode = exposure.mode || 'local';
    const isCloudflare = mode === 'cloudflare' || context.cfResourcesConfigured;
    const isLan = mode === 'lan';
    const profileIdArg = jsArg(profileId);
    const hasEngineKey = Boolean(context.hasEngineKey);
    const cfReady = Boolean(context.cfResourcesReady);
    const activeTokenCount = (context.activeTokens || []).length;
    const cleanupCount = (context.cleanupRecords || []).length;
    const items = [
        {
            label: 'Exposure',
            state: isCloudflare ? 'Cloudflare tunnel' : isLan ? 'LAN' : 'Local only',
            tone: isCloudflare ? (cfReady ? 'green' : 'yellow') : isLan ? 'yellow' : 'green',
            detail: isCloudflare
                ? (cfReady ? `Public hostname ${context.endpointHostname || '--'} is provisioned.` : 'Cloudflare hostname or Access resources still need provisioning.')
                : isLan
                    ? 'The model server is reachable on the node network.'
                    : 'The model server binds locally on the node.',
        },
        {
            label: 'Engine API key',
            state: hasEngineKey ? 'Configured' : 'Missing',
            tone: hasEngineKey ? 'green' : (isLan || isCloudflare ? 'yellow' : ''),
            detail: hasEngineKey
                ? 'Clients use an OpenAI-compatible Authorization bearer token.'
                : (isLan || isCloudflare ? 'Recommended for LAN and public endpoints.' : 'Optional for local-only endpoints.'),
            action: hasEngineKey
                ? `<button class="btn" onclick="rotateProfileApiKey(${profileIdArg})">Rotate</button>`
                : `<button class="btn" onclick="rotateProfileApiKey(${profileIdArg})">Generate</button>`,
        },
    ];
    if (isCloudflare) {
        items.push({
            label: 'Cloudflare Access',
            state: cfReady ? 'Service Auth ready' : 'Needs provisioning',
            tone: cfReady ? 'green' : 'yellow',
            detail: cfReady
                ? 'Cloudflare Access policy is attached to this inference hostname.'
                : 'Provision the endpoint to create or reconcile DNS, route, Access app, and policy.',
            action: `<button class="btn" onclick="provisionProfileCloudflare(${profileIdArg})">${context.cfResourcesConfigured ? 'Reconcile' : 'Provision'}</button>`,
        });
        items.push({
            label: 'Cloudflare clients',
            state: `${activeTokenCount} active`,
            tone: activeTokenCount ? 'green' : 'yellow',
            detail: activeTokenCount
                ? 'Active service-token clients can call through Cloudflare.'
                : 'Generate a client to receive a Client ID and one-time Client Secret.',
            action: `<button class="btn" onclick="generateProfileCfToken(${profileIdArg})" ${cfReady ? '' : 'disabled'}>Generate Client</button>`,
        });
    }
    if (cleanupCount) {
        items.push({
            label: 'Cleanup',
            state: `${cleanupCount} pending`,
            tone: 'yellow',
            detail: 'Cloudflare resources have retryable cleanup records below.',
        });
    }
    return `
        <div class="profile-connect-panel connect-posture-panel">
            <div class="connect-section-header compact">
                <div>
                    <div class="launcher-card-title">Security Posture</div>
                    <div class="launcher-card-meta">Auth layers and next actions for this endpoint</div>
                </div>
            </div>
            <div class="connect-posture-grid">
                ${items.map(item => `
                    <div class="connect-posture-item">
                        <div class="connect-posture-head">
                            <span>${esc(item.label)}</span>
                            ${connectBadge(item.state, item.tone || '')}
                        </div>
                        <div class="connect-posture-detail">${esc(item.detail)}</div>
                        ${item.action ? `<div class="connect-posture-action">${item.action}</div>` : ''}
                    </div>
                `).join('')}
            </div>
        </div>
    `;
}

function renderProfileConnect(profileId, data, secrets = {}) {
    const profile = inferenceProfilesData.find(item => item.id === profileId) || {};
    const exposure = profile.exposure || {};
    const cloudflare = profile.cloudflare || {};
    const bundle = data.default || data.client_bundle || data || {};
    const instanceBundles = Array.isArray(data.instance_bundles) ? data.instance_bundles : [];
    const cleanupRecords = Array.isArray(data.cleanup_records) ? data.cleanup_records : [];
    const tokens = (cloudflare.service_tokens || []);
    const activeTokens = tokens.filter(token => (token.state || 'active') === 'active');
    const hasOwnedTokens = tokens.some(token => token.owned_by_inframatik && (token.state || 'active') === 'active');
    const hasEngineKey = Boolean((bundle.secret_state || {}).engine_api_key_configured);
    const endpointHostname = cloudflare.hostname || exposure.hostname || '';
    const cfResourcesReady = Boolean(cloudflare.hostname && cloudflare.access_app_id && cloudflare.access_policy_id);
    const cfResourcesConfigured = Boolean(cloudflare.hostname || cloudflare.access_app_id || cloudflare.access_policy_id);
    const canGenerateClient = Boolean(cloudflare.access_policy_id);
    const cloudflareFacts = [
        ['Hostname', endpointHostname || '--'],
        ['Tunnel', cloudflare.tunnel_id || '--'],
        ['Access App', cloudflare.access_app_id || '--'],
        ['Policy', cloudflare.access_policy_id || '--'],
    ];
    const tokenRows = tokens.map(token => {
        const profileIdArg = jsArg(profileId);
        const tokenIdArg = jsArg(token.id);
        const state = token.state || 'active';
        const owned = Boolean(token.owned_by_inframatik);
        const active = state === 'active';
        const ownershipLabel = owned ? 'inframatik owned' : 'external';
        const guidance = active
            ? owned
                ? 'New client secrets can be rotated here and are shown once.'
                : 'Detach removes this token from the profile policy; inframatik cannot recover its client secret.'
            : 'Retired clients are no longer accepted by this profile policy.';
        return `
            <div class="profile-token-row">
                <div>
                    <div class="launcher-card-title">${esc(token.name || token.id)}</div>
                    <div class="launcher-card-meta">${esc(token.client_id || '--')} · ${esc(state)} · ${esc(ownershipLabel)}</div>
                    <div class="profile-token-guidance">${esc(guidance)}</div>
                </div>
                <div class="model-actions">
                    ${active && owned ? `<button class="btn" onclick="rotateProfileCfToken(${profileIdArg},${tokenIdArg})">Rotate</button>` : ''}
                    ${active ? `<button class="btn ${owned ? 'danger' : ''}" onclick="retireProfileCfToken(${profileIdArg},${tokenIdArg})">${owned ? 'Retire' : 'Detach'}</button>` : '<span class="model-badge">retired</span>'}
                </div>
            </div>
        `;
    }).join('');
    const profileIdArg = jsArg(profileId);
    const html = `
        <div class="connect-hero">
            <div>
                <div class="launcher-card-title">API Connection</div>
                <div class="launcher-card-meta">${esc(profile.display_name || profile.id || profileId)}</div>
            </div>
            <div class="connect-status-row">
                ${connectBadge(exposure.mode || 'local')}
                ${connectBadge(hasEngineKey ? 'engine key ready' : 'engine key missing', hasEngineKey ? 'green' : 'yellow')}
                ${connectBadge(cfResourcesReady ? 'cloudflare ready' : endpointHostname ? 'hostname set' : 'local only', cfResourcesReady ? 'green' : endpointHostname ? 'yellow' : '')}
                ${connectBadge(`${activeTokens.length} active client${activeTokens.length === 1 ? '' : 's'}`, activeTokens.length ? 'green' : '')}
            </div>
        </div>
        ${renderConnectionPosture(profileId, {
            exposure,
            cloudflare,
            bundle,
            hasEngineKey,
            cfResourcesReady,
            cfResourcesConfigured,
            activeTokens,
            endpointHostname,
            cleanupRecords,
        })}
        <div class="connect-action-grid">
            <section>
                <div class="connect-section-header">
                    <div>
                        <div class="launcher-card-title">Engine Auth</div>
                        <div class="launcher-card-meta">OpenAI-compatible bearer token</div>
                    </div>
                    ${connectBadge(hasEngineKey ? 'configured' : 'missing', hasEngineKey ? 'green' : 'yellow')}
                </div>
                <button class="btn" onclick="rotateProfileApiKey(${profileIdArg})">${hasEngineKey ? 'Rotate' : 'Generate'} API Key</button>
            </section>
            <section>
                <div class="connect-section-header">
                    <div>
                        <div class="launcher-card-title">Cloudflare Endpoint</div>
                        <div class="launcher-card-meta">${esc(endpointHostname || 'No hostname configured')}</div>
                    </div>
                    ${connectBadge(cfResourcesReady ? 'provisioned' : endpointHostname ? 'pending' : 'not configured', cfResourcesReady ? 'green' : endpointHostname ? 'yellow' : '')}
                </div>
                <div class="connect-inline-form">
                    <input type="text" id="profile-cf-hostname-${esc(profileId)}" value="${esc(endpointHostname)}" placeholder="llm.example.com" autocomplete="off">
                    <button class="btn" onclick="provisionProfileCloudflare(${profileIdArg})">${cfResourcesConfigured ? 'Reconcile' : 'Provision'}</button>
                </div>
                <div class="connect-mini-facts">
                    ${cloudflareFacts.map(([label, value]) => `<div><span>${esc(label)}</span><code>${esc(value)}</code></div>`).join('')}
                </div>
                <div class="model-actions">
                    ${cfResourcesConfigured && hasOwnedTokens ? `
                        <label class="profile-cf-removal-option">
                            <input type="checkbox" id="profile-cf-delete-owned-${esc(profileId)}">
                            <span>Delete inframatik-owned clients if unreferenced</span>
                        </label>
                    ` : ''}
                    ${cfResourcesConfigured ? `<button class="btn danger" onclick="removeProfileCloudflare(${profileIdArg})">Remove Endpoint</button>` : ''}
                </div>
                <div class="model-job-error" id="profile-cf-hostname-error-${esc(profileId)}"></div>
            </section>
            <section>
                <div class="connect-section-header">
                    <div>
                        <div class="launcher-card-title">Cloudflare Clients</div>
                        <div class="launcher-card-meta">Service-token credentials for callers</div>
                    </div>
                    ${connectBadge(canGenerateClient ? 'policy ready' : 'needs endpoint', canGenerateClient ? 'green' : 'yellow')}
                </div>
                <button class="btn" onclick="generateProfileCfToken(${profileIdArg})" ${canGenerateClient ? '' : 'disabled'}>Generate New Client</button>
            </section>
        </div>
        ${renderClientBundle(bundle, secrets)}
        ${renderInstanceBundleOptions(instanceBundles)}
        ${renderCloudflareCleanupRecords(profileId, cleanupRecords)}
        <div class="connect-section-header compact">
            <div class="launcher-card-title">Cloudflare Service Tokens</div>
        </div>
        ${tokens.length ? `<div class="profile-token-list">${tokenRows}</div>` : '<div class="empty-state compact">No Cloudflare service-token clients attached.</div>'}
    `;
    setProfileDetail(profileId, html, 'connect');
}

async function loadProfileConnect(profileId) {
    setProfileDetail(profileId, '<div class="empty-state compact">Loading connection bundle...</div>', 'connect');
    try {
        const data = await api('GET', modelNodePath(`/api/inference/profiles/${encodeURIComponent(profileId)}/client-bundles`));
        renderProfileConnect(profileId, data);
    } catch (e) {
        setProfileDetail(profileId, `<div class="model-job-error">${esc(e.message)}</div>`, 'connect');
    }
}

async function rotateProfileApiKey(profileId) {
    try {
        const data = await api('POST', modelNodePath(`/api/inference/profiles/${encodeURIComponent(profileId)}/api-key`), { render_bundle: true });
        patchInferenceProfile(data.profile);
        renderProfileConnect(profileId, data, { engine_api_key: data.engine_api_key });
    } catch (e) {
        setInferenceError(e.message);
    }
}

async function provisionProfileCloudflare(profileId) {
    const profile = inferenceProfilesData.find(item => item.id === profileId) || {};
    const exposure = profile.exposure || {};
    const cloudflare = profile.cloudflare || {};
    const input = document.getElementById(`profile-cf-hostname-${profileId}`);
    const errEl = document.getElementById(`profile-cf-hostname-error-${profileId}`);
    const hostname = (input && input.value.trim()) || cloudflare.hostname || exposure.hostname || '';
    if (errEl) errEl.textContent = '';
    if (!hostname) {
        if (errEl) errEl.textContent = 'Hostname is required.';
        else setInferenceError('Hostname is required.');
        return;
    }
    try {
        const data = await api('POST', modelNodePath(`/api/inference/profiles/${encodeURIComponent(profileId)}/cloudflare/exposure`), {
            hostname,
            render_bundle: true,
        });
        patchInferenceProfile(data.profile);
        renderProfileConnect(profileId, data, { client_secret: data.client_secret });
        setInferenceStatus(`Cloudflare endpoint ready for ${profileId}.`);
    } catch (e) {
        setInferenceError(e.message);
    }
}

async function removeProfileCloudflare(profileId) {
    if (!confirm('Remove Cloudflare exposure for this profile?')) return;
    const deleteOwnedEl = document.getElementById(`profile-cf-delete-owned-${profileId}`);
    const deleteOwned = Boolean(deleteOwnedEl && deleteOwnedEl.checked);
    try {
        const data = await api('DELETE', modelNodePath(`/api/inference/profiles/${encodeURIComponent(profileId)}/cloudflare/exposure?delete_owned_tokens=${deleteOwned ? 'true' : 'false'}`));
        patchInferenceProfile(data.profile);
        await loadProfileConnect(profileId);
        const warnings = data.warnings || [];
        setInferenceStatus(warnings.length ? `Removed Cloudflare exposure with ${warnings.length} cleanup warning(s).` : 'Removed Cloudflare exposure.');
    } catch (e) {
        setInferenceError(e.message);
    }
}

async function retryInferenceCleanup(profileId, recordId) {
    try {
        await api('POST', modelNodePath(`/api/inference/cleanup/${encodeURIComponent(recordId)}/retry`));
        setInferenceStatus('Cloudflare cleanup retry completed.');
        await loadProfileConnect(profileId);
    } catch (e) {
        setInferenceError(e.message);
        await loadProfileConnect(profileId);
    }
}

async function forgetInferenceCleanup(profileId, recordId) {
    if (!confirm('Forget this local cleanup record without calling Cloudflare?')) return;
    try {
        await api('DELETE', modelNodePath(`/api/inference/cleanup/${encodeURIComponent(recordId)}`));
        setInferenceStatus('Forgot Cloudflare cleanup record.');
        await loadProfileConnect(profileId);
    } catch (e) {
        setInferenceError(e.message);
    }
}

async function generateProfileCfToken(profileId) {
    try {
        const data = await api('POST', modelNodePath(`/api/inference/profiles/${encodeURIComponent(profileId)}/cloudflare/service-tokens`), {
            render_bundle: true,
        });
        patchInferenceProfile(data.profile);
        renderProfileConnect(profileId, data, { client_secret: data.client_secret });
    } catch (e) {
        setInferenceError(e.message);
    }
}

async function rotateProfileCfToken(profileId, tokenId) {
    if (!confirm('Rotate this Cloudflare client secret? Existing clients using the previous secret may fail until updated.')) return;
    try {
        const data = await api('POST', modelNodePath(`/api/inference/profiles/${encodeURIComponent(profileId)}/cloudflare/service-tokens/${encodeURIComponent(tokenId)}/rotate`), {
            render_bundle: true,
        });
        patchInferenceProfile(data.profile);
        renderProfileConnect(profileId, data, { client_secret: data.client_secret });
    } catch (e) {
        setInferenceError(e.message);
    }
}

async function retireProfileCfToken(profileId, tokenId) {
    if (!confirm('Retire this Cloudflare client from the profile policy?')) return;
    try {
        const data = await api('DELETE', modelNodePath(`/api/inference/profiles/${encodeURIComponent(profileId)}/cloudflare/service-tokens/${encodeURIComponent(tokenId)}`));
        patchInferenceProfile(data.profile);
        await loadProfileConnect(profileId);
    } catch (e) {
        setInferenceError(e.message);
    }
}

function renderInferenceOperations(operations) {
    const el = document.getElementById('inference-operations-list');
    if (!el) return;
    if (!operations.length) {
        setHtmlIfChanged(el, '<div class="empty-state">No inference operations yet.</div>');
        return;
    }
    setHtmlIfChanged(el, operations.slice(0, 20).map(op => {
        const progress = Math.max(0, Math.min(100, Number(op.progress || 0))).toFixed(0);
        return `
            <div class="inference-operation-row">
                <div class="inference-operation-context">
                    <div>
                        <div class="model-job-title">${esc(op.profile_id || op.id)}</div>
                        <div class="model-job-sub">${esc(op.id || 'operation')}</div>
                    </div>
                    <div class="model-job-sub">${progress}%</div>
                </div>
                ${renderProfileOperationPanel(op, '', { context: 'operations' })}
            </div>
        `;
    }).join(''));
}

async function refreshInferenceLaunchers() {
    const nodeId = selectedNodeId;
    if (!nodeId) return;
    setInferenceError('');
    try {
        const data = await api('GET', modelNodePath('/api/inference/launchers'));
        if (currentAppView !== 'inference' || nodeId !== selectedNodeId) return;
        inferenceLaunchersData = data.launchers || [];
        renderLaunchers(inferenceLaunchersData);
    } catch (e) {
        setInferenceError(e.message);
    }
}

function resetLauncherForm() {
    const editId = document.getElementById('launcher-edit-id');
    if (editId) editId.value = '';
    ['launcher-id', 'launcher-display-name', 'launcher-executable', 'launcher-venv-path', 'launcher-working-dir'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    const engine = document.getElementById('launcher-engine');
    if (engine) engine.value = 'vllm';
    setElementHtml('launcher-arg-rows', '');
    setElementHtml('launcher-env-rows', '');
}

function setLauncherBaseArgs(args) {
    setElementHtml('launcher-arg-rows', '');
    (args || []).forEach(arg => addLauncherArgRow(arg));
}

function launcherModuleArgs(engine) {
    if (engine === 'vllm') return ['-m', 'vllm.entrypoints.openai.api_server'];
    if (engine === 'sglang') return ['-m', 'sglang.launch_server'];
    return [];
}

function applyLauncherPreset(preset) {
    const engine = document.getElementById('launcher-engine');
    if (preset === 'vllm-module') {
        if (engine) engine.value = 'vllm';
        setLauncherBaseArgs(launcherModuleArgs('vllm'));
    } else if (preset === 'sglang-module') {
        if (engine) engine.value = 'sglang';
        setLauncherBaseArgs(launcherModuleArgs('sglang'));
    } else if (preset === 'direct-binary') {
        setLauncherBaseArgs([]);
    }
    setInferenceStatus('Launcher base args updated.');
}

function normalizedVenvPath(value) {
    return String(value || '').trim().replace(/\/+$/, '');
}

function pythonExecutableFromVenv(value) {
    const venvPath = normalizedVenvPath(value);
    return venvPath ? `${venvPath}/bin/python` : '';
}

function deriveVenvPathFromPythonExecutable(value) {
    return String(value || '').trim().replace(/\/bin\/python(?:\d+(?:\.\d+)?)?$/, '');
}

function syncLauncherVenvFromExecutable() {
    const executable = document.getElementById('launcher-executable');
    const venv = document.getElementById('launcher-venv-path');
    if (!executable || !venv) return;
    const inferred = deriveVenvPathFromPythonExecutable(executable.value || '');
    if (inferred && inferred !== (executable.value || '').trim()) {
        venv.value = inferred;
        setInferenceStatus('Python venv inferred from executable.');
        return;
    }
    setInferenceError('Executable does not look like a Python venv path.');
}

function applyLauncherVenvPreset(engineName) {
    const engine = engineName === 'sglang' ? 'sglang' : 'vllm';
    const venv = document.getElementById('launcher-venv-path');
    const executable = document.getElementById('launcher-executable');
    const python = pythonExecutableFromVenv(venv ? venv.value : '');
    if (!python) {
        setInferenceError('Python venv path is required.');
        return;
    }
    setInferenceError('');
    if (executable) executable.value = python;
    const engineEl = document.getElementById('launcher-engine');
    if (engineEl) engineEl.value = engine;
    setLauncherBaseArgs(launcherModuleArgs(engine));
    setInferenceStatus(`${engine === 'vllm' ? 'vLLM' : 'SGLang'} launcher set to ${python}.`);
}

function addLauncherArgRow(value = '') {
    const el = document.getElementById('launcher-arg-rows');
    if (!el) return;
    const row = document.createElement('div');
    row.className = 'launcher-arg-row';
    row.innerHTML = `
        <input type="text" class="launcher-arg-input" value="${esc(value)}" placeholder="argv token" autocomplete="off">
        <button class="btn danger" type="button" onclick="this.parentElement.remove()">Remove</button>
    `;
    el.appendChild(row);
}

function addLauncherEnvRow(key = '', value = '') {
    const el = document.getElementById('launcher-env-rows');
    if (!el) return;
    const row = document.createElement('div');
    row.className = 'launcher-env-row';
    row.innerHTML = `
        <input type="text" class="launcher-env-key" value="${esc(key)}" placeholder="KEY" autocomplete="off">
        <input type="text" class="launcher-env-value" value="${esc(value)}" placeholder="value" autocomplete="off">
        <button class="btn danger" type="button" onclick="this.parentElement.remove()">Remove</button>
    `;
    el.appendChild(row);
}

function collectLauncherBaseArgs() {
    return Array.from(document.querySelectorAll('.launcher-arg-input'))
        .map(input => input.value.trim())
        .filter(Boolean);
}

function collectLauncherEnv(editing) {
    const rows = Array.from(document.querySelectorAll('.launcher-env-row'));
    const pairs = rows.map(row => ({
        key: (row.querySelector('.launcher-env-key') || {}).value?.trim() || '',
        value: (row.querySelector('.launcher-env-value') || {}).value || '',
    })).filter(pair => pair.key);
    if (editing && pairs.length && pairs.every(pair => !pair.value)) {
        return null;
    }
    const env = {};
    pairs.forEach(pair => {
        if (pair.value) env[pair.key] = pair.value;
    });
    return pairs.length ? env : (editing ? null : {});
}

async function submitLauncherForm() {
    const editId = modelOptionalValue('launcher-edit-id');
    const body = {
        id: modelOptionalValue('launcher-id'),
        display_name: modelOptionalValue('launcher-display-name'),
        engine: document.getElementById('launcher-engine').value,
        executable: modelOptionalValue('launcher-executable'),
        base_args: collectLauncherBaseArgs(),
        working_dir: modelOptionalValue('launcher-working-dir'),
    };
    const env = collectLauncherEnv(!!editId);
    if (env !== null) body.env = env;
    if (!body.executable || !body.engine) {
        setInferenceError('Engine and executable are required.');
        return;
    }
    setInferenceError('');
    try {
        if (editId) {
            delete body.id;
            Object.keys(body).forEach(key => body[key] === null && delete body[key]);
            const launcher = await api('PUT', modelNodePath(`/api/inference/launchers/${encodeURIComponent(editId)}`), body);
            patchInferenceLauncher(launcher);
            setInferenceStatus(`Updated launcher ${editId}.`);
        } else {
            const launcher = await api('POST', modelNodePath('/api/inference/launchers'), body);
            patchInferenceLauncher(launcher);
            setInferenceStatus('Launcher saved.');
        }
        resetLauncherFormAndEnableId();
    } catch (e) {
        setInferenceError(e.message);
    }
}

function renderLaunchers(launchers) {
    const el = document.getElementById('launchers-list');
    if (!el) return;
    if (!launchers.length) {
        setHtmlIfChanged(el, '<div class="empty-state">No engine launchers configured yet.</div>');
        return;
    }
    setHtmlIfChanged(el, launchers.map(launcher => {
        const preview = (launcher.command_preview || [launcher.executable, ...(launcher.base_args || [])]).join(' ');
        const envKeys = (launcher.redacted_env_keys || []).join(', ') || 'none';
        const moduleName = launcherModuleName(launcher);
        const mode = moduleName ? `module ${moduleName}` : 'direct command';
        return `
            <div class="launcher-card">
                <div class="launcher-card-header">
                    <div>
                        <div class="launcher-card-title">${esc(launcher.display_name || launcher.id)}</div>
                        <div class="launcher-card-meta">${esc(launcher.id)} · ${esc(launcher.engine)} · ${esc(mode)} · env: ${esc(envKeys)}</div>
                    </div>
                    <div class="model-actions">
                        <button class="btn" onclick="editLauncher('${esc(launcher.id)}')">Edit</button>
                        <button class="btn" onclick="validateLauncher('${esc(launcher.id)}')">Validate</button>
                        <button class="btn danger" onclick="deleteLauncher('${esc(launcher.id)}','${esc(launcher.display_name || launcher.id)}')">Delete</button>
                    </div>
                </div>
                <div class="launcher-card-meta">${esc(launcher.executable)}${launcher.working_dir ? ` · cwd ${esc(launcher.working_dir)}` : ''}</div>
                <div class="launcher-command-preview">${esc(preview)}</div>
                <div class="launcher-validation" id="launcher-validation-${esc(launcher.id)}"></div>
            </div>
        `;
    }).join(''));
}

function editLauncher(launcherId) {
    const launcher = inferenceLaunchersData.find(item => item.id === launcherId);
    if (!launcher) return;
    document.getElementById('launcher-edit-id').value = launcher.id;
    document.getElementById('launcher-id').value = launcher.id;
    document.getElementById('launcher-id').disabled = true;
    document.getElementById('launcher-display-name').value = launcher.display_name || '';
    document.getElementById('launcher-engine').value = launcher.engine || 'vllm';
    document.getElementById('launcher-executable').value = launcher.executable || '';
    const venvEl = document.getElementById('launcher-venv-path');
    if (venvEl) {
        const inferred = deriveVenvPathFromPythonExecutable(launcher.executable || '');
        venvEl.value = inferred && inferred !== (launcher.executable || '') ? inferred : '';
    }
    document.getElementById('launcher-working-dir').value = launcher.working_dir || '';
    setElementHtml('launcher-arg-rows', '');
    (launcher.base_args || []).forEach(arg => addLauncherArgRow(arg));
    setElementHtml('launcher-env-rows', '');
    (launcher.redacted_env_keys || []).forEach(key => addLauncherEnvRow(key, ''));
    setInferenceStatus(`Editing ${launcher.id}.`);
}

function resetLauncherFormAndEnableId() {
    resetLauncherForm();
    const idEl = document.getElementById('launcher-id');
    if (idEl) idEl.disabled = false;
}

function launcherModuleName(launcher) {
    const args = launcher && launcher.base_args;
    if (!Array.isArray(args)) return '';
    const moduleFlagIndex = args.indexOf('-m');
    if (moduleFlagIndex === -1 || !args[moduleFlagIndex + 1]) return '';
    return args[moduleFlagIndex + 1];
}

async function validateLauncher(launcherId) {
    const resultEl = document.getElementById(`launcher-validation-${launcherId}`);
    if (resultEl) resultEl.innerHTML = '<div class="launcher-validation-panel">Checking executable and runtime...</div>';
    try {
        const result = await api('POST', modelNodePath(`/api/inference/launchers/${encodeURIComponent(launcherId)}/validate?runtime=true`));
        if (resultEl) resultEl.innerHTML = renderLauncherValidation(result);
    } catch (e) {
        if (resultEl) {
            resultEl.innerHTML = `<div class="launcher-validation-panel failed"><div class="model-job-error">${esc(e.message)}</div></div>`;
        }
    }
}

function launcherValidationBadge(label, ok) {
    return `<span class="model-badge ${ok ? 'green' : 'red'}">${esc(label)}</span>`;
}

function renderLauncherValidation(result) {
    const runtime = result.runtime || {};
    const executable = result.executable || {};
    const workingDir = result.working_dir || null;
    const errors = result.errors || [];
    const command = (runtime.command_preview || []).join(' ');
    const runtimeChecked = runtime.checked === true;
    const runtimeOk = runtime.valid === true;
    return `
        <div class="launcher-validation-panel ${result.valid ? 'valid' : 'failed'}">
            <div class="launcher-validation-head">
                <div>
                    <div class="launcher-card-title">Validation</div>
                    <div class="launcher-card-meta">${runtimeChecked ? `runtime ${runtime.elapsed_ms || 0}ms` : 'path only'}</div>
                </div>
                <div class="connect-status-row">
                    ${launcherValidationBadge('path', Boolean(executable.exists && executable.is_file && executable.executable))}
                    ${runtimeChecked ? launcherValidationBadge('runtime', runtimeOk) : ''}
                </div>
            </div>
            <div class="launcher-validation-grid">
                <div><span>Executable</span><code>${esc(executable.path || '--')}</code></div>
                <div><span>Working Dir</span><code>${esc(workingDir ? workingDir.path : 'default')}</code></div>
                <div><span>Exit Code</span><code>${esc(runtimeChecked ? (runtime.code ?? 'timeout') : '--')}</code></div>
            </div>
            ${command ? `<div class="launcher-command-preview">${esc(command)}</div>` : ''}
            ${errors.length ? `<div class="launcher-validation-errors">${errors.map(error => `<div>${esc(error)}</div>`).join('')}</div>` : ''}
            ${runtime.output ? `<pre class="launcher-validation-output">${esc(runtime.output)}</pre>` : ''}
        </div>
    `;
}

async function deleteLauncher(launcherId, displayName) {
    if (!confirm(`Delete launcher "${displayName}"?`)) return;
    setInferenceError('');
    try {
        const result = await api('DELETE', modelNodePath(`/api/inference/launchers/${encodeURIComponent(launcherId)}`));
        removeInferenceLauncher((result && result.deleted) || launcherId);
        setInferenceStatus(`Deleted launcher ${launcherId}.`);
    } catch (e) {
        const detail = e.detail;
        if (detail && detail.requires_force) {
            const refs = (detail.references || []).map(ref => ref.name || ref.profile_id).join(', ');
            if (confirm(`Stopped profiles reference this launcher: ${refs}. Delete anyway?`)) {
                const result = await api('DELETE', modelNodePath(`/api/inference/launchers/${encodeURIComponent(launcherId)}?force_stopped_references=true`));
                removeInferenceLauncher((result && result.deleted) || launcherId);
                setInferenceStatus(`Deleted launcher ${launcherId}.`);
                return;
            }
        }
        setInferenceError(e.message);
    }
}

function stopInferencePolling() {
    if (inferenceJobsTimer) {
        clearInterval(inferenceJobsTimer);
        inferenceJobsTimer = null;
    }
}

function hasActiveInferenceModelJob() {
    return inferenceModelData
        && Array.isArray(inferenceModelData.jobs)
        && inferenceModelData.jobs.some(job => ACTIVE_MODEL_JOB_STATES.has(job.state));
}

function hasActiveInferenceOperation() {
    return Array.isArray(inferenceOperationsData)
        && inferenceOperationsData.some(op => ACTIVE_INFERENCE_OPERATION_STATES.has(op.state));
}

function hasActiveInferenceActivity() {
    return hasActiveInferenceModelJob() || hasActiveInferenceOperation();
}

async function refreshInferenceActivity() {
    const nodeId = selectedNodeId;
    if (!nodeId || currentAppView !== 'inference') return;
    const needsModels = hasActiveInferenceModelJob();
    const needsOperations = hasActiveInferenceOperation();
    if (!needsModels && !needsOperations) {
        updateInferencePolling();
        return;
    }

    setInferenceError('');
    try {
        const [models, operations] = await Promise.all([
            needsModels ? api('GET', modelNodePath('/api/models')) : Promise.resolve(null),
            needsOperations ? api('GET', modelNodePath('/api/inference/operations')) : Promise.resolve(null),
        ]);
        if (currentAppView !== 'inference' || nodeId !== selectedNodeId) return;
        if (models) {
            inferenceModelData = models;
            renderPolledModelState();
        }
        if (operations) {
            mergeInferenceOperationSnapshot(operations.operations || [], nodeId);
        } else {
            updateInferencePolling();
        }
    } catch (e) {
        setInferenceError(e.message);
        stopInferencePolling();
    }
}

function updateInferencePolling() {
    const hasActiveModelJob = hasActiveInferenceModelJob();
    const hasActiveOperation = hasActiveInferenceOperation();
    const needsOperationPoll = hasActiveOperation && !shouldUseInferenceOperationWs(selectedNodeId);
    const needsModelJobPoll = hasActiveModelJob && !shouldUseInferenceOperationWs(selectedNodeId);
    const shouldPoll = currentAppView === 'inference' && (needsModelJobPoll || needsOperationPoll);
    if (shouldPoll && !inferenceJobsTimer) {
        inferenceJobsTimer = setInterval(refreshInferenceActivity, 2500);
    } else if (!shouldPoll && inferenceJobsTimer) {
        stopInferencePolling();
    }
}

async function refreshInferenceModels() {
    const nodeId = selectedNodeId;
    if (!nodeId) return;
    setInferenceError('');
    try {
        const [models, storage] = await Promise.all([
            api('GET', modelNodePath('/api/models')),
            api('GET', modelNodePath('/api/models/storage')),
        ]);
        if (currentAppView !== 'inference' || nodeId !== selectedNodeId) return;
        inferenceModelData = models;
        inferenceStorageData = storage;
        renderInferenceSummary(models, storage);
        renderModelInventory(models.artifacts || []);
        renderModelJobs(models.jobs || []);
        renderModelStorageInfo(storage);
        updateInferencePolling();
    } catch (e) {
        setInferenceError(e.message);
        stopInferencePolling();
    }
}

function renderInferenceSummary(models, storage) {
    const artifacts = models.artifacts || [];
    const jobs = models.jobs || [];
    const activeJobs = jobs.filter(job => ACTIVE_MODEL_JOB_STATES.has(job.state));
    const disk = storage && storage.disk;
    const root = (storage && storage.root) || models.store_root || '--';
    setElementHtml('model-storage-summary', `
        <div class="metric-card">
            <div class="metric-label">Models</div>
            <div class="metric-value">${artifacts.length}</div>
            <div class="metric-sub">${activeJobs.length} active job${activeJobs.length === 1 ? '' : 's'}</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Store Free</div>
            <div class="metric-value" style="font-size:22px">${disk ? formatBytes(disk.free) : '--'}</div>
            <div class="metric-sub">${disk ? `${formatBytes(disk.used)} used / ${formatBytes(disk.total)}` : 'Disk usage unavailable'}</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Store Root</div>
            <div class="metric-value mono-sm" style="font-size:13px;overflow-wrap:anywhere">${esc(root)}</div>
            <div class="metric-sub">Node-local path</div>
        </div>
    `);
}

function modelStateBadge(state) {
    const value = state || 'unknown';
    const color = value === 'ready' ? 'green' : ACTIVE_MODEL_JOB_STATES.has(value) ? 'yellow' : value === 'degraded' ? 'yellow' : value === 'unknown' ? '' : 'red';
    return `<span class="model-badge ${color}">${esc(value)}</span>`;
}

function modelSourceLabel(source) {
    if (!source || !source.type) return '--';
    if (source.type === 'huggingface') return `HF ${source.repo || '--'}${source.revision ? ` @ ${source.revision}` : ''}`;
    if (source.type === 'url') {
        const host = String(source.url || '').replace(/^https?:\/\//, '').split('/')[0] || source.filename || '--';
        return `URL ${host}`;
    }
    if (source.type === 'local') return `Local ${source.path || '--'}`;
    return source.type;
}

function renderModelInventory(artifacts) {
    const el = document.getElementById('models-list');
    if (!artifacts.length) {
        setHtmlIfChanged(el, '<div class="empty-state">No managed models yet.</div>');
        return;
    }

    const rows = artifacts.map(artifact => {
        const snapshot = artifact.active_snapshot || '--';
        const snap = (artifact.snapshots || {})[artifact.active_snapshot] || {};
        const state = artifact.active_snapshot_state || snap.state || 'unknown';
        const display = artifact.manifest_display_name || artifact.display_name || artifact.id;
        const locationBadge = artifact.current_root === false
            ? '<span class="model-badge yellow">Previous root</span>'
            : artifact.path_exists === false
                ? '<span class="model-badge red">Missing path</span>'
                : '<span class="model-badge green">Current root</span>';
        const format = artifact.format || '--';
        const source = modelSourceLabel(artifact.source);
        const size = typeof artifact.size_bytes === 'number' ? formatBytes(artifact.size_bytes) : '--';
        return `
            <div class="model-table-row">
                <div>
                    <div class="model-name">${esc(display)}</div>
                    <div class="model-meta">${esc(artifact.id)} · ${artifact.files_count || 0} files</div>
                </div>
                <div><span class="model-badge">${esc(format)}</span></div>
                <div>
                    <div class="model-meta">${esc(snapshot)}</div>
                    <div class="model-meta">${size}</div>
                </div>
                <div>${modelStateBadge(state)}<div style="margin-top:4px">${locationBadge}</div></div>
                <div class="model-source">${esc(source)}</div>
                <div class="model-actions">
                    <button class="btn" onclick="verifyModelArtifact('${esc(artifact.id)}','${esc(snapshot)}')">Verify</button>
                    <button class="btn danger" onclick="deleteModelArtifact('${esc(artifact.id)}','${esc(display)}')">Delete</button>
                </div>
            </div>`;
    }).join('');

    setHtmlIfChanged(el, `
        <div class="model-table">
            <div class="model-table-row model-table-header">
                <div>Model</div>
                <div>Format</div>
                <div>Snapshot</div>
                <div>State</div>
                <div>Source</div>
                <div style="text-align:right">Actions</div>
            </div>
            ${rows}
        </div>
    `);
}

function modelJobStateBadge(state) {
    if (state === 'ready') return '<span class="model-badge green">Ready</span>';
    if (ACTIVE_MODEL_JOB_STATES.has(state)) return `<span class="model-badge yellow">${esc(state)}</span>`;
    if (state === 'canceled') return '<span class="model-badge">Canceled</span>';
    return `<span class="model-badge red">${esc(state || 'failed')}</span>`;
}

function modelJobProgressClass(job, progress) {
    if (job.state === 'ready') return 'green';
    if (['failed', 'failed_interrupted'].includes(job.state)) return 'red';
    if (ACTIVE_MODEL_JOB_STATES.has(job.state)) return 'yellow';
    return progressColor(progress);
}

function modelJobCleanup(job) {
    return (job && job.cleanup && typeof job.cleanup === 'object') ? job.cleanup : {};
}

function modelJobStagingCleaned(job) {
    const cleanup = modelJobCleanup(job);
    return !!cleanup.staging_removed_at || cleanup.staging_removed === true;
}

function modelJobActivityLabel(job) {
    if (!job) return '';
    if (job.current_file) return job.current_file;
    if (job.cleanup_error) return `Cleanup issue: ${job.cleanup_error}`;
    if (modelJobStagingCleaned(job)) return 'Staging cleaned';
    if (job.state === 'ready') return 'Artifact committed';
    if (job.state === 'failed_interrupted') return 'Interrupted by restart';
    return job.state || '';
}

function modelJobProgressMetrics(job, done, total) {
    if (!job) return '';
    const startedAt = Number(job.started_at || 0);
    if (!startedAt) return '';
    const finishedAt = Number(job.finished_at || 0);
    const now = finishedAt || (Date.now() / 1000);
    const elapsed = Math.max(0, now - startedAt);
    if (!elapsed) return '';
    const parts = [`elapsed ${formatSeconds(elapsed)}`];
    if (done > 0) {
        const rate = done / elapsed;
        if (rate > 0) {
            parts.push(`${ACTIVE_MODEL_JOB_STATES.has(job.state) ? '' : 'avg '}${formatRate(rate)}`);
            if (ACTIVE_MODEL_JOB_STATES.has(job.state) && total > done) {
                parts.push(`ETA ${formatSeconds((total - done) / rate)}`);
            }
        }
    }
    return parts.join(' · ');
}

function renderModelJobs(jobs) {
    const el = document.getElementById('model-jobs-list');
    if (!jobs.length) {
        setHtmlIfChanged(el, '<div class="empty-state">No model jobs yet.</div>');
        return;
    }
    setHtmlIfChanged(el, jobs.map(job => {
        const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
        const isHashing = ['hashing', 'verifying'].includes(job.state);
        const bytes = Number(isHashing ? (job.hash_total_bytes || job.total_bytes || 0) : (job.total_bytes || job.hash_total_bytes || 0));
        const done = Number(isHashing ? (job.hashed_bytes || 0) : (job.downloaded_bytes || 0));
        const barClass = modelJobProgressClass(job, progress);
        const active = ACTIVE_MODEL_JOB_STATES.has(job.state);
        const stagingCleaned = modelJobStagingCleaned(job);
        const canClean = ['failed', 'failed_interrupted', 'canceled'].includes(job.state) && job.staging_path && !stagingCleaned;
        const actions = [
            active ? `<button class="btn" onclick="cancelModelJob('${esc(job.id)}')">Cancel</button>` : '',
            canClean ? `<button class="btn danger" onclick="cleanModelJobStaging('${esc(job.id)}','${esc(job.staging_path)}')">Clean staging</button>` : '',
        ].filter(Boolean).join('');
        const cleanupBadge = stagingCleaned ? '<span class="model-badge green">Staging cleaned</span>' : '';
        const activityLabel = modelJobActivityLabel(job);
        const progressMetrics = modelJobProgressMetrics(job, done, bytes);
        return `
            <div class="model-job-row">
                <div class="model-job-main">
                    <div>
                        <div class="model-job-title">${esc(job.artifact_id || job.id)}</div>
                        <div class="model-job-sub">${esc(job.kind || 'model')} · ${esc(job.snapshot || '--')} · ${esc(modelSourceLabel(job.source))}</div>
                    </div>
                    <div class="model-job-badges">${modelJobStateBadge(job.state)}${cleanupBadge}</div>
                    <div class="model-job-sub">${bytes ? `${formatBytes(done)} / ${formatBytes(bytes)}` : `${progress.toFixed(0)}%`}</div>
                    <div class="model-actions">${actions}</div>
                </div>
                <div class="model-job-progress">
                    <div class="progress-bar"><div class="progress-fill ${barClass}" style="width:${progress}%"></div></div>
                    <div class="model-job-sub">${esc(activityLabel)}</div>
                    ${progressMetrics ? `<div class="model-job-metrics">${esc(progressMetrics)}</div>` : ''}
                </div>
                ${job.error ? `<div class="model-job-error">${esc(job.error)}</div>` : ''}
            </div>`;
    }).join(''));
}

function renderModelStorageInfo(storage) {
    if (!storage) return;
    const input = document.getElementById('model-store-root-input');
    if (input && document.activeElement !== input) input.value = storage.root || '';
    const disk = storage.disk;
    const active = storage.active_jobs || [];
    setElementHtml('model-storage-info', `
        <div><span class="settings-info-label">Root:</span> ${esc(storage.root || '--')}</div>
        <div><span class="settings-info-label">Registry:</span> ${esc(storage.registry_path || '--')}</div>
        <div><span class="settings-info-label">Active Jobs:</span> ${active.length}</div>
        <div><span class="settings-info-label">Max Download:</span> ${formatBytes(storage.max_download_bytes || 0)}</div>
        <div><span class="settings-info-label">Disk:</span> ${disk ? `${formatBytes(disk.free)} free` : '--'}</div>
        <div><span class="settings-info-label">Import Roots:</span> ${esc((storage.allowlist_roots || []).join(', ') || '--')}</div>
    `);
}

function modelOptionalValue(id) {
    const el = document.getElementById(id);
    const value = el ? el.value.trim() : '';
    return value || null;
}

function showStartedModelJob(job, message) {
    setInferenceStatus(message);
    setInferenceTab('jobs');
    mergeModelJob(job);
    renderInferenceOperations(inferenceOperationsData);
}

async function submitModelImport() {
    const path = modelOptionalValue('model-import-path');
    if (!path) {
        setInferenceError('Local import path is required.');
        return;
    }
    setInferenceError('');
    setInferenceStatus('Starting import...');
    try {
        const job = await api('POST', modelNodePath('/api/models/import'), {
            path,
            artifact_id: modelOptionalValue('model-import-artifact'),
            display_name: modelOptionalValue('model-import-name'),
            snapshot: modelOptionalValue('model-import-snapshot'),
        });
        showStartedModelJob(job, 'Import job started.');
    } catch (e) {
        setInferenceError(e.message);
        setInferenceStatus('');
    }
}

async function submitModelUrlDownload() {
    const url = modelOptionalValue('model-url-url');
    if (!url) {
        setInferenceError('URL is required.');
        return;
    }
    const source = {
        type: 'url',
        url,
        sha256: modelOptionalValue('model-url-sha256'),
        extract: !!document.getElementById('model-url-extract').checked,
    };
    setInferenceError('');
    setInferenceStatus('Starting download...');
    try {
        const job = await api('POST', modelNodePath('/api/models/download'), {
            source,
            artifact_id: modelOptionalValue('model-url-artifact'),
            snapshot: modelOptionalValue('model-url-snapshot'),
        });
        showStartedModelJob(job, 'Download job started.');
    } catch (e) {
        setInferenceError(e.message);
        setInferenceStatus('');
    }
}

async function submitModelHfDownload() {
    const repo = modelOptionalValue('model-hf-repo');
    if (!repo) {
        setInferenceError('Hugging Face repo is required.');
        return;
    }
    const tokenEl = document.getElementById('model-hf-token');
    const source = {
        type: 'huggingface',
        repo,
        revision: modelOptionalValue('model-hf-revision') || 'main',
        preset: document.getElementById('model-hf-preset').value || 'full',
        token: tokenEl ? tokenEl.value.trim() || null : null,
    };
    setInferenceError('');
    setInferenceStatus('Starting Hugging Face download...');
    try {
        const job = await api('POST', modelNodePath('/api/models/download'), {
            source,
            artifact_id: modelOptionalValue('model-hf-artifact'),
        });
        if (tokenEl) tokenEl.value = '';
        showStartedModelJob(job, 'Download job started.');
    } catch (e) {
        setInferenceError(e.message);
        setInferenceStatus('');
    }
}

async function verifyModelArtifact(artifactId, snapshot) {
    setInferenceError('');
    setInferenceStatus(`Verifying ${artifactId}...`);
    try {
        const query = snapshot && snapshot !== '--' ? `?snapshot=${encodeURIComponent(snapshot)}` : '';
        const result = await api('POST', modelNodePath(`/api/models/${artifactId}/verify${query}`));
        setInferenceStatus(result.valid ? `${artifactId} verified.` : `${artifactId} has verification issues.`);
        patchModelVerification(result);
    } catch (e) {
        setInferenceError(e.message);
        setInferenceStatus('');
    }
}

async function deleteModelArtifact(artifactId, displayName) {
    if (!confirm(`Delete model "${displayName}" from this node?`)) return;
    setInferenceError('');
    setInferenceStatus('');
    try {
        const result = await api('DELETE', modelNodePath(`/api/models/${artifactId}`));
        removeModelArtifactLocal((result && result.deleted) || artifactId);
        setInferenceStatus(`${artifactId} deleted.`);
    } catch (e) {
        const detail = e.detail;
        if (detail && detail.requires_force) {
            const refs = (detail.references || []).map(ref => ref.name || ref.profile_id).join(', ');
            if (confirm(`Stopped profiles reference this model: ${refs}. Delete anyway?`)) {
                const result = await api('DELETE', modelNodePath(`/api/models/${artifactId}?force_stopped_references=true`));
                removeModelArtifactLocal((result && result.deleted) || artifactId);
                setInferenceStatus(`${artifactId} deleted.`);
                return;
            }
        }
        setInferenceError(e.message);
    }
}

async function cancelModelJob(jobId) {
    try {
        const job = await api('POST', modelNodePath(`/api/models/jobs/${jobId}/cancel`));
        mergeModelJob(job);
    } catch (e) {
        setInferenceError(e.message);
    }
}

async function cleanModelJobStaging(jobId, stagingPath) {
    if (!confirm(`Delete staging files for ${jobId}?\n\n${stagingPath}`)) return;
    try {
        const result = await api('DELETE', modelNodePath(`/api/models/jobs/${jobId}/staging`));
        setInferenceStatus(`Cleaned staging for ${jobId}.`);
        if (result && result.job) mergeModelJob(result.job);
    } catch (e) {
        setInferenceError(e.message);
    }
}

async function updateModelStoreRoot() {
    const root = modelOptionalValue('model-store-root-input');
    if (!root) {
        setInferenceError('Model store root is required.');
        return;
    }
    if (!confirm('Update the model store root for this node? Existing artifacts will not be moved.')) return;
    try {
        await api('PUT', modelNodePath('/api/models/storage'), { root });
        setInferenceStatus('Model store root updated.');
        await refreshInferenceModels();
    } catch (e) {
        setInferenceError(e.message);
    }
}

// ---- Settings page ----

function openSettings() {
    document.getElementById('settings-error').textContent = '';
    showAppView('settings');
}

function closeSettings() {
    showAppView('main');
}

async function loadSettingsView() {
    // Hide all settings panels
    document.getElementById('settings-unconfigured').style.display = 'none';
    document.getElementById('settings-standalone').style.display = 'none';
    document.getElementById('settings-init-master').style.display = 'none';
    document.getElementById('settings-init-worker').style.display = 'none';
    document.getElementById('settings-master').style.display = 'none';

    try {
        const config = await api('GET', '/api/config');
        const subtitleEl = document.getElementById('settings-page-subtitle');
        if (subtitleEl) {
            const label = config.node_name ? `${config.node_name} · ${config.role}` : config.role;
            subtitleEl.textContent = label || 'Cluster, Cloudflare, tokens, and updates.';
        }

        if (config.role === 'master') {
            document.getElementById('settings-master').style.display = 'block';
            document.getElementById('master-info-name').textContent = config.node_name;
            const liveNodes = await loadSettingsNodes();
            const localServices = await loadLocalServicesForSettings();
            renderMasterWorkers(config.workers || {}, liveNodes, config.cf_configured);
            renderWorkerCfSyncActions(config.workers || {}, config.cf_configured);
            renderEnrollmentTokens(config.enrollment_tokens || []);
            renderCfSetup('master-cf-setup', config.cf_configured);
            renderServiceTokens('master-service-tokens', config.service_tokens || [], localServices);
            await renderDashboardAccess(
                'master-dashboard-access',
                config.dashboard_hostname,
                config.dashboard_zone_id,
                config.dashboard_zone_name,
                config.cf_configured,
                config.cf_zone_id,
            );
            loadDeployInfo();
        } else if (config.role === 'worker') {
            await showAppView('main');
        } else if (config.role === 'standalone') {
            document.getElementById('settings-standalone').style.display = 'block';
            document.getElementById('standalone-info-name').textContent = config.node_name;
            const localServices = await loadLocalServicesForSettings();
            renderCfSetup('standalone-cf-setup', config.cf_configured);
            renderServiceTokens('standalone-service-tokens', config.service_tokens || [], localServices);
            await renderDashboardAccess(
                'standalone-dashboard-access',
                config.dashboard_hostname,
                config.dashboard_zone_id,
                config.dashboard_zone_name,
                config.cf_configured,
                config.cf_zone_id,
            );
        } else {
            document.getElementById('settings-unconfigured').style.display = 'block';
        }
    } catch (e) {
        document.getElementById('settings-unconfigured').style.display = 'block';
    }
}

async function loadLocalServicesForSettings() {
    try {
        return await api('GET', '/api/services');
    } catch (e) {
        return [];
    }
}

async function loadSettingsNodes() {
    try {
        const liveNodes = await api('GET', '/api/nodes');
        nodes = liveNodes;
        renderSidebar(liveNodes);
        return liveNodes;
    } catch (e) {
        return nodes || [];
    }
}

// ---- CF Setup Wizard ----

let cfWizardState = {};

function renderCfSetup(containerId, isConfigured) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (isConfigured) {
        el.innerHTML = `
            <div class="settings-subsection-header">Cloudflare</div>
            <div class="settings-info">
                <div><span class="settings-info-label">Status:</span> Configured</div>
            </div>
            <div class="form-actions">
                <button class="btn danger" onclick="clearCfSetup('${containerId}')">Reconfigure</button>
            </div>`;
        return;
    }
    cfWizardState = {};
    el.innerHTML = `
        <div class="settings-subsection-header">Cloudflare</div>
        <p class="settings-desc">Connect to Cloudflare for tunnels, DNS, and Zero Trust Access.</p>
        <div class="cf-token-instructions">
            <p>In the Cloudflare dashboard, go to Manage Account → Account API Tokens, then click Create Token.</p>
            <ol>
                <li>Add an account policy for Entire Account: Access (Read/Edit), Access: Organizations, Identity Providers, and Groups (Read/Edit), Argo Tunnel (Legacy) (Read/Edit).</li>
                <li>Add a domain policy, changing the Entire Account dropdown to All Domains or Specified Domains: DNS (Read/Edit), Zones (Read).</li>
            </ol>
        </div>
        <div class="form-group">
            <label>API Token</label>
            <input type="password" id="cf-wiz-token" placeholder="Paste your Cloudflare API token" autocomplete="off">
        </div>
        <div class="form-actions">
            <button class="btn primary" onclick="cfWizardValidateToken('${containerId}')">Validate</button>
        </div>
        <div class="form-error" id="cf-wiz-error"></div>
        <div id="cf-wiz-steps"></div>`;
}

async function cfWizardValidateToken(containerId) {
    const token = document.getElementById('cf-wiz-token').value.trim();
    const errEl = document.getElementById('cf-wiz-error');
    errEl.textContent = '';
    if (!token) { errEl.textContent = 'API token is required.'; return; }

    try {
        const data = await api('POST', '/api/cf/setup/validate-token', { token });
        cfWizardState.token = token;
        cfWizardState.accounts = data.accounts;

        if (data.accounts.length === 1) {
            cfWizardState.account_id = data.accounts[0].id;
            cfWizardState.account_name = data.accounts[0].name;
            await cfWizardLoadZones(containerId);
        } else {
            document.getElementById('cf-wiz-steps').innerHTML = `
                <div class="form-group">
                    <label>Account</label>
                    <select id="cf-wiz-account">
                        ${data.accounts.map(a => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('')}
                    </select>
                </div>
                <div class="form-actions">
                    <button class="btn primary" onclick="cfWizardSelectAccount('${containerId}')">Next</button>
                </div>`;
        }
    } catch (e) {
        errEl.textContent = e.message;
    }
}

async function cfWizardSelectAccount(containerId) {
    const sel = document.getElementById('cf-wiz-account');
    cfWizardState.account_id = sel.value;
    cfWizardState.account_name = sel.options[sel.selectedIndex].text;
    await cfWizardLoadZones(containerId);
}

async function cfWizardLoadZones(containerId) {
    const errEl = document.getElementById('cf-wiz-error');
    errEl.textContent = '';
    try {
        const data = await api('POST', '/api/cf/setup/zones', {
            token: cfWizardState.token,
            account_id: cfWizardState.account_id,
        });
        if (data.zones.length === 0) {
            errEl.textContent = 'No domains found in this account. Add a domain in Cloudflare first.';
            return;
        }
        cfWizardState.zones = data.zones;

        document.getElementById('cf-wiz-steps').innerHTML = `
            <div class="form-group">
                <label>Domain</label>
                <select id="cf-wiz-zone">
                    ${data.zones.map(z => `<option value="${esc(z.id)}" data-name="${esc(z.name)}">${esc(z.name)}</option>`).join('')}
                </select>
            </div>
            <div class="form-actions">
                <button class="btn primary" onclick="cfWizardSelectZone('${containerId}')">Next</button>
            </div>`;
    } catch (e) {
        errEl.textContent = e.message;
    }
}

async function cfWizardSelectZone(containerId) {
    const sel = document.getElementById('cf-wiz-zone');
    cfWizardState.zone_id = sel.value;
    cfWizardState.zone_name = sel.options[sel.selectedIndex].dataset.name;

    const errEl = document.getElementById('cf-wiz-error');
    errEl.textContent = '';
    try {
        const data = await api('POST', '/api/cf/setup/policies', {
            token: cfWizardState.token,
            account_id: cfWizardState.account_id,
        });
        const policies = data.policies || [];

        const policyOptions = policies.map(p =>
            `<option value="${esc(p.id)}">${esc(p.name)}</option>`
        ).join('');

        document.getElementById('cf-wiz-steps').innerHTML = `
            <div class="form-group">
                <label>Access Policy <span class="label-hint">(optional)</span></label>
                <select id="cf-wiz-policy">
                    <option value="">None — skip Access protection</option>
                    ${policyOptions}
                    <option value="__create__">Create new policy...</option>
                </select>
            </div>
            <div id="cf-wiz-new-policy" style="display:none">
                <div class="form-group">
                    <label>Policy Name</label>
                    <input type="text" id="cf-wiz-policy-name" placeholder="e.g. Allow company" autocomplete="off">
                </div>
                <div class="form-group">
                    <label>Allow Email Domain</label>
                    <input type="text" id="cf-wiz-policy-domain" placeholder="${esc(cfWizardState.zone_name)}" value="${esc(cfWizardState.zone_name)}" autocomplete="off">
                </div>
            </div>
            <div class="form-actions">
                <button class="btn primary" onclick="cfWizardSave('${containerId}')">Save</button>
            </div>`;

        document.getElementById('cf-wiz-policy').addEventListener('change', (e) => {
            document.getElementById('cf-wiz-new-policy').style.display =
                e.target.value === '__create__' ? '' : 'none';
        });
    } catch (e) {
        errEl.textContent = e.message;
    }
}

async function cfWizardSave(containerId) {
    const errEl = document.getElementById('cf-wiz-error');
    errEl.textContent = '';

    let policyId = document.getElementById('cf-wiz-policy').value;

    // Create new policy if requested
    if (policyId === '__create__') {
        const policyName = document.getElementById('cf-wiz-policy-name').value.trim();
        const policyDomain = document.getElementById('cf-wiz-policy-domain').value.trim();
        if (!policyName || !policyDomain) {
            errEl.textContent = 'Policy name and email domain are required.';
            return;
        }
        try {
            const result = await api('POST', '/api/cf/setup/create-policy', {
                token: cfWizardState.token,
                account_id: cfWizardState.account_id,
                name: policyName,
                email_domain: policyDomain,
            });
            policyId = result.id;
        } catch (e) {
            errEl.textContent = 'Failed to create policy: ' + e.message;
            return;
        }
    }

    // Save config
    try {
        await api('POST', '/api/cf/setup/save', {
            token: cfWizardState.token,
            account_id: cfWizardState.account_id,
            zone_id: cfWizardState.zone_id,
            default_policy_id: policyId || null,
        });
        cfWizardState = {};
        await loadSettingsView();
    } catch (e) {
        errEl.textContent = e.message;
    }
}

async function clearCfSetup(containerId) {
    if (!confirm('Remove Cloudflare configuration? Existing tunnels and routes will not be deleted from Cloudflare.')) return;
    try {
        await api('DELETE', '/api/cf/setup');
        await loadSettingsView();
    } catch (e) {
        alert('Failed to clear CF config: ' + e.message);
    }
}

function sanitizeSubdomain(raw) {
    const normalized = (raw || '')
        .toLowerCase()
        .replace(/[^a-z0-9-]+/g, '-')
        .replace(/^-+|-+$/g, '')
        .replace(/-{2,}/g, '-');
    if (!normalized) return '';
    return normalized.slice(0, 63);
}

function updateDashboardHostnamePreview() {
    const previewEl = document.getElementById('dashboard-access-preview');
    const subdomainEl = document.getElementById('dashboard-access-subdomain');
    const zoneEl = document.getElementById('dashboard-access-zone');
    if (!previewEl || !subdomainEl || !zoneEl) return;
    const subdomain = subdomainEl.value.trim().toLowerCase();
    const zoneName = zoneEl.options[zoneEl.selectedIndex]?.dataset.name || '';
    previewEl.textContent = subdomain && zoneName
        ? `${subdomain}.${zoneName}`
        : '--';
}

async function loadDashboardZoneOptions(defaultZoneId, defaultZoneName) {
    const errEl = document.getElementById('dashboard-access-error');
    const zoneEl = document.getElementById('dashboard-access-zone');
    if (!zoneEl) return;
    try {
        const data = await api('GET', '/api/cf/zones');
        const zones = data.zones || [];
        if (zones.length === 0) {
            throw new Error('No active Cloudflare domains found.');
        }
        zoneEl.innerHTML = zones.map(z =>
            `<option value="${esc(z.id)}" data-name="${esc(z.name)}">${esc(z.name)}</option>`
        ).join('');

        let preferred = defaultZoneId || '';
        if (preferred && zones.some(z => z.id === preferred)) {
            zoneEl.value = preferred;
        } else if (defaultZoneName) {
            const match = zones.find(z => z.name === defaultZoneName);
            if (match) zoneEl.value = match.id;
        }
        if (!zoneEl.value && zones.length > 0) {
            zoneEl.value = zones[0].id;
        }
        updateDashboardHostnamePreview();
    } catch (e) {
        if (errEl) errEl.textContent = e.message;
        zoneEl.innerHTML = '<option value="">Unavailable</option>';
    }
}

async function renderDashboardAccess(
    containerId,
    hostname,
    dashboardZoneId,
    dashboardZoneName,
    cfConfigured,
    defaultCfZoneId,
) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (hostname) {
        const zoneDetail = dashboardZoneName
            ? `<div><span class="settings-info-label">Domain:</span> ${esc(dashboardZoneName)}</div>`
            : '';
        el.innerHTML = `
            <div class="settings-subsection-header">Dashboard Access</div>
            <div class="settings-info">
                <div><span class="settings-info-label">Hostname:</span> <a href="https://${esc(hostname)}" target="_blank">${esc(hostname)}</a></div>
                ${zoneDetail}
            </div>
            <div class="form-actions">
                <button class="btn danger" onclick="disableDashboardAccess()">Remove</button>
            </div>`;
    } else {
        if (!cfConfigured) {
            el.innerHTML = `
                <div class="settings-subsection-header">Dashboard Access</div>
                <p class="settings-desc">Configure Cloudflare first to enable dashboard access.</p>`;
            return;
        }
        const defaultSubdomain = esc(sanitizeSubdomain(machineHostname) || 'dashboard');
        el.innerHTML = `
            <div class="settings-subsection-header">Dashboard Access</div>
            <p class="settings-desc">Choose a subdomain and Cloudflare domain for dashboard access.</p>
            <div class="form-group">
                <label>Subdomain</label>
                <input type="text" id="dashboard-access-subdomain" placeholder="dash" value="${defaultSubdomain}" autocomplete="off">
            </div>
            <div class="form-group">
                <label>Domain</label>
                <select id="dashboard-access-zone"><option value="">Loading domains...</option></select>
                <div class="label-hint" id="dashboard-access-preview-wrap">Preview: <code id="dashboard-access-preview">--</code></div>
            </div>
            <div class="form-error" id="dashboard-access-error"></div>
            <div class="form-actions">
                <button class="btn primary" onclick="enableDashboardAccess()">Enable</button>
            </div>`;

        const subdomainEl = document.getElementById('dashboard-access-subdomain');
        const zoneEl = document.getElementById('dashboard-access-zone');
        subdomainEl.addEventListener('input', updateDashboardHostnamePreview);
        zoneEl.addEventListener('change', updateDashboardHostnamePreview);
        await loadDashboardZoneOptions(defaultCfZoneId || dashboardZoneId, dashboardZoneName);
    }
}

async function enableDashboardAccess() {
    const subdomainEl = document.getElementById('dashboard-access-subdomain');
    const zoneEl = document.getElementById('dashboard-access-zone');
    const errEl = document.getElementById('dashboard-access-error');
    if (!subdomainEl || !zoneEl || !errEl) return;
    const subdomain = subdomainEl.value.trim().toLowerCase();
    const zoneId = zoneEl.value;
    errEl.textContent = '';
    if (!subdomain) { errEl.textContent = 'Subdomain is required.'; return; }
    if (subdomain.includes('.')) { errEl.textContent = 'Subdomain must be a single DNS label.'; return; }
    if (!zoneId) { errEl.textContent = 'Domain selection is required.'; return; }
    try {
        await api('POST', '/api/config/dashboard-access', { subdomain, zone_id: zoneId });
        await loadSettingsView();
    } catch (e) {
        errEl.textContent = e.message;
    }
}

async function disableDashboardAccess() {
    if (!confirm('Remove dashboard from Cloudflare Access?')) return;
    try {
        await api('DELETE', '/api/config/dashboard-access');
        await loadSettingsView();
    } catch (e) {
        alert('Failed to remove dashboard access: ' + e.message);
    }
}

function findWorkerNodeInfo(nodeId, worker, liveNodes) {
    return (liveNodes || []).find(n =>
        n.config_node_id === nodeId ||
        n.node_id === nodeId ||
        (worker.address && n.address === worker.address)
    );
}

function renderMasterWorkers(workers, liveNodes = nodes, cfConfigured = false) {
    const el = document.getElementById('master-workers-list');
    const entries = Object.entries(workers);
    if (entries.length === 0) {
        el.innerHTML = '<div class="settings-empty">No workers configured yet.</div>';
        return;
    }
    el.innerHTML = entries.map(([nodeId, w]) => {
        const nodeInfo = findWorkerNodeInfo(nodeId, w, liveNodes);
        const status = nodeInfo ? nodeInfo.status : 'offline';
        const statusClass = status === 'online' ? 'green' : 'red';
        const statusText = status === 'online' ? 'Online' : 'Offline';
        const cfBadge = w.tunnel_id
            ? '<span class="worker-cf-badge">CF</span>'
            : w.cf_opt_out
                ? '<span class="worker-status-label">Local only</span>'
                : cfConfigured
                    ? `<button class="btn" onclick="setupWorkerTunnel('${esc(nodeId)}', '${esc(w.name)}')">Setup Tunnel</button>`
                    : '<span class="worker-status-label">Local only</span>';
        return `
        <div class="master-worker-row">
            <span class="status-dot ${statusClass}"></span>
            <span class="master-worker-name">${esc(w.name)}</span>
            <span class="master-worker-address">${esc(w.address)}</span>
            <span class="worker-status-label ${statusClass}">${statusText}</span>
            ${cfBadge}
            <button class="btn danger" onclick="removeWorker('${esc(nodeId)}', '${esc(w.name)}')">Remove</button>
        </div>`;
    }).join('');
}

function renderWorkerCfSyncActions(workers, cfConfigured) {
    const actionsEl = document.getElementById('worker-cf-sync-actions');
    const resultsEl = document.getElementById('worker-cf-sync-results');
    if (!actionsEl) return;
    if (resultsEl) resultsEl.innerHTML = '';

    const missing = Object.values(workers || {}).filter(w => !w.tunnel_id && !w.cf_opt_out);
    if (!cfConfigured || missing.length === 0) {
        actionsEl.innerHTML = '';
        return;
    }

    actionsEl.innerHTML = `
        <button class="btn primary" onclick="setupMissingWorkerTunnels()">
            Sync Cloudflare to Workers
        </button>`;
}

function renderEnrollmentTokens(tokens) {
    const el = document.getElementById('enrollment-tokens');
    if (!el) return;
    if (!tokens || tokens.length === 0) {
        el.innerHTML = '<p class="settings-desc">Generate a token to enroll a new worker. The token is single-use.</p>';
        return;
    }

    // Backward/forward-compatible token shape support:
    // - old: ["enroll-abc..."]
    // - new: [{token, created_at, expires_at}]
    const items = tokens
        .map((entry) => {
            if (typeof entry === 'string') {
                return { token: entry, created_at: null, expires_at: null };
            }
            if (!entry || typeof entry !== 'object') return null;
            const token = typeof entry.token === 'string' ? entry.token : '';
            if (!token) return null;
            return {
                token,
                created_at: entry.created_at ?? null,
                expires_at: entry.expires_at ?? null,
            };
        })
        .filter(Boolean);

    if (items.length === 0) {
        el.innerHTML = '<p class="settings-desc">Generate a token to enroll a new worker. The token is single-use.</p>';
        return;
    }

    el.innerHTML = items.map((t) => {
        const created = t.created_at
            ? new Date(t.created_at * 1000).toLocaleString()
            : 'unknown';
        const expires = t.expires_at
            ? new Date(t.expires_at * 1000).toLocaleString()
            : 'unknown';
        return `
        <div class="master-worker-row">
            <code>${esc(t.token)}</code>
            <span class="master-worker-address">created ${esc(created)} · expires ${esc(expires)}</span>
            <button class="btn" onclick="copyText('${esc(t.token)}', this)">Copy</button>
            <button class="btn danger" onclick="cancelEnrollmentToken('${esc(t.token)}')">Cancel</button>
        </div>
    `;
    }).join('') + '<p class="settings-desc" style="margin-top:8px">Run on the new machine: <code>curl ... | bash -s -- --enroll TOKEN</code></p>';
}

function copyText(text, btn) {
    const value = String(text || '');
    const markCopied = () => {
        const orig = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(() => btn.textContent = orig, 1500);
    };
    const fallbackCopy = () => {
        const textarea = document.createElement('textarea');
        textarea.value = value;
        textarea.setAttribute('readonly', '');
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        const ok = document.execCommand && document.execCommand('copy');
        document.body.removeChild(textarea);
        if (!ok) throw new Error('Copy failed');
    };
    const clipboard = typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText
        ? navigator.clipboard.writeText(value)
        : Promise.reject(new Error('Clipboard API unavailable'));
    clipboard.then(markCopied).catch(() => {
        try {
            fallbackCopy();
            markCopied();
        } catch (e) {
            alert('Copy failed. Select the value manually.');
        }
    });
}

async function generateEnrollmentToken() {
    try {
        const result = await api('POST', '/api/config/enrollment-tokens');
        await loadSettingsView();
    } catch (e) {
        alert('Failed to generate token: ' + e.message);
    }
}

async function cancelEnrollmentToken(token) {
    try {
        await api('DELETE', `/api/config/enrollment-tokens/${token}`);
        await loadSettingsView();
    } catch (e) {
        alert('Failed to cancel token: ' + e.message);
    }
}

// ---- Service Tokens ----

function normalizeServiceTokenItems(tokens) {
    return (tokens || [])
        .map((entry) => {
            if (!entry || typeof entry !== 'object') return null;
            const service = typeof entry.service === 'string' ? entry.service : '';
            if (!service) return null;
            return {
                service,
                token_id: typeof entry.token_id === 'string' ? entry.token_id : '',
                capability: typeof entry.capability === 'string' ? entry.capability : 'deploy',
                created_at: entry.created_at ?? null,
                expires_at: entry.expires_at ?? null,
            };
        })
        .filter(Boolean);
}

function serviceTokenDate(ts) {
    return ts ? new Date(ts * 1000).toLocaleDateString() : '--';
}

function serviceTokenArg(value) {
    return encodeURIComponent(value || '');
}

function renderServiceTokenItem(containerId, t) {
    const createdDate = serviceTokenDate(t.created_at);
    const expiresDate = serviceTokenDate(t.expires_at);
    return `
        <div class="service-token-item">
            <span class="service-token-pill">${esc(t.capability)}</span>
            <span class="master-worker-address">created ${esc(createdDate)} · expires ${esc(expiresDate)}</span>
            <button class="btn danger" onclick="revokeServiceToken('${containerId}', '${esc(t.token_id || '')}', '${serviceTokenArg(t.service)}', true)">Revoke</button>
        </div>`;
}

function renderGeneratedServiceTokens(containerId, results) {
    const box = document.getElementById(`${containerId}-new-token`);
    if (!box) return;
    const generated = Array.isArray(results) ? results : [results];
    box.style.display = '';
    box.innerHTML = `
        <p class="settings-desc">Copy ${generated.length === 1 ? 'this token' : 'these tokens'} now. Token values will not be shown again.</p>
        ${generated.map((r) => `
            <div class="service-token-secret-row">
                <span class="service-token-name">${esc(r.service || 'service')}</span>
                <code>${esc(r.token || '')}</code>
                <button class="btn" onclick="copyText(this.parentElement.querySelector('code').textContent, this)">Copy</button>
            </div>
        `).join('')}`;
}

function renderServiceTokens(containerId, tokens, services = []) {
    const el = document.getElementById(containerId);
    if (!el) return;

    const items = normalizeServiceTokenItems(tokens);
    const serviceRows = (services || [])
        .filter((svc) => svc && typeof svc.name === 'string' && svc.name)
        .map((svc) => {
            const serviceTokens = items.filter((t) => t.service === svc.name);
            const status = svc.status || 'unknown';
            const statusClass = status === 'active' ? 'green' : 'red';
            const tokenHtml = serviceTokens.length > 0
                ? serviceTokens.map((t) => renderServiceTokenItem(containerId, t)).join('')
                : `<div class="service-token-item empty">
                    <span class="master-worker-address">No token generated</span>
                    <button class="btn primary" onclick="generateServiceTokenForService('${containerId}', '${serviceTokenArg(svc.name)}', true)">Generate Token</button>
                </div>`;
            return `
                <div class="service-token-row">
                    <div class="service-token-main">
                        <span class="status-dot ${statusClass}"></span>
                        <span class="service-token-name">${esc(svc.name)}</span>
                        <span class="service-token-status">${esc(status)}</span>
                    </div>
                    <div class="service-token-list">${tokenHtml}</div>
                </div>`;
        })
        .join('');

    const serviceNames = new Set((services || []).map((svc) => svc && svc.name).filter(Boolean));
    const orphanTokens = items.filter((t) => !serviceNames.has(t.service));
    const orphanRows = orphanTokens.map((t) => `
        <div class="service-token-row">
            <div class="service-token-main">
                <span class="status-dot red"></span>
                <span class="service-token-name">${esc(t.service)}</span>
                <span class="service-token-status">not registered</span>
            </div>
            <div class="service-token-list">${renderServiceTokenItem(containerId, t)}</div>
        </div>
    `).join('');

    const emptyText = services.length === 0 && items.length === 0
        ? '<p class="settings-desc">No services are registered on this node yet.</p>'
        : '';

    el.innerHTML = `
        <div class="settings-subsection-header">Service Tokens</div>
        <p class="settings-desc">Tokens are scoped to a service and stay valid whether that service is running or stopped.</p>
        <div id="${containerId}-new-token" class="service-token-secret" style="display:none"></div>
        ${emptyText}
        <div class="service-token-rows">
            ${serviceRows}
            ${orphanRows}
        </div>
        <div class="form-group" style="margin-top:12px">
            <input type="text" id="${containerId}-service-name" placeholder="Manual service name" autocomplete="off">
        </div>
        <div class="form-actions">
            <button class="btn primary" onclick="generateServiceToken('${containerId}')">Generate Manual Token</button>
        </div>`;
}

async function generateServiceTokenForService(containerId, service, encoded = false) {
    if (encoded) service = decodeURIComponent(service);
    if (!service) { alert('Service name is required.'); return; }
    try {
        const result = await api('POST', '/api/config/service-tokens', { service });
        await loadSettingsView();
        renderGeneratedServiceTokens(containerId, result);
    } catch (e) {
        alert('Failed to generate token: ' + e.message);
    }
}

async function generateServiceToken(containerId) {
    const nameEl = document.getElementById(`${containerId}-service-name`);
    const service = nameEl.value.trim();
    if (!service) { alert('Service name is required.'); return; }
    try {
        const result = await api('POST', '/api/config/service-tokens', { service });
        nameEl.value = '';
        await loadSettingsView();
        renderGeneratedServiceTokens(containerId, result);
    } catch (e) {
        alert('Failed to generate token: ' + e.message);
    }
}

async function revokeServiceToken(containerId, tokenId, serviceName, encoded = false) {
    if (encoded) serviceName = decodeURIComponent(serviceName);
    if (!tokenId) {
        alert('This token cannot be revoked from the dashboard because it has no token ID.');
        return;
    }
    const label = serviceName ? ` for "${serviceName}"` : '';
    if (!confirm(`Revoke this service token${label}?`)) return;
    try {
        await api('DELETE', `/api/config/service-tokens/by-id/${encodeURIComponent(tokenId)}`);
        await loadSettingsView();
    } catch (e) {
        alert('Failed to revoke token: ' + e.message);
    }
}

function showSettingsHome() {
    document.getElementById('settings-init-master').style.display = 'none';
    document.getElementById('settings-init-worker').style.display = 'none';
    document.getElementById('settings-error').textContent = '';
    // Reload to show correct panel (standalone vs unconfigured)
    loadSettingsView();
}

function showInitMaster() {
    document.getElementById('settings-unconfigured').style.display = 'none';
    document.getElementById('settings-standalone').style.display = 'none';
    document.getElementById('settings-init-master').style.display = 'block';
    document.getElementById('init-master-name').value = '';
    document.getElementById('settings-error').textContent = '';
}

function showInitWorker() {
    document.getElementById('settings-unconfigured').style.display = 'none';
    document.getElementById('settings-standalone').style.display = 'none';
    document.getElementById('settings-init-worker').style.display = 'block';
    document.getElementById('init-worker-name').value = defaultWorkerNodeName();
    resetWorkerMasterFields('init-worker', installSourceMasterUrl);
    document.getElementById('init-worker-token').value = '';
    setWorkerSkipCf('init-worker', false);
    document.getElementById('settings-error').textContent = '';
    hideWorkerEnrollProgress('init-worker');
    document.getElementById('init-worker-back-btn').disabled = false;
    setWorkerCfButtonsDisabled('init-worker', false);
    const btn = document.getElementById('init-worker-submit-btn');
    btn.disabled = false;
    btn.textContent = 'Register';
}

async function submitInitMaster() {
    const name = document.getElementById('init-master-name').value.trim();
    const errEl = document.getElementById('settings-error');
    errEl.textContent = '';
    if (!name) { errEl.textContent = 'Node name is required.'; return; }

    try {
        await api('POST', '/api/config/init-master', { name });
        // Reload the page to pick up master mode (lifespan tasks, sidebar, etc.)
        location.reload();
    } catch (e) {
        errEl.textContent = e.message;
    }
}

async function submitInitWorker() {
    const name = document.getElementById('init-worker-name').value.trim();
    const token = document.getElementById('init-worker-token').value.trim();
    const errEl = document.getElementById('settings-error');
    errEl.textContent = '';
    const masterAddress = getWorkerMasterUrl('init-worker');
    if (!name || !token) { errEl.textContent = 'All fields are required.'; return; }
    if (masterAddress.error) { errEl.textContent = masterAddress.error; return; }
    const backBtn = document.getElementById('init-worker-back-btn');
    const submitBtn = document.getElementById('init-worker-submit-btn');
    backBtn.disabled = true;
    submitBtn.disabled = true;
    setWorkerCfButtonsDisabled('init-worker', true);
    submitBtn.textContent = 'Registering...';
    bindWorkerEnrollProgress('init-worker');
    showWorkerEnrollProgress('init-worker', 'Contacting master...', 5);

    try {
        const result = await api('POST', '/api/config/enroll-worker', {
            name,
            master_url: masterAddress.url,
            token,
            skip_cf: workerSkipCf['init-worker'],
        });
        delete wsProgressCallbacks['worker-enroll'];
        if (result.cf_tunnel_error) {
            showWorkerEnrollProgress('init-worker', 'Registered, but Cloudflare tunnel setup needs attention.', 100);
            errEl.textContent = 'Registered, but Cloudflare tunnel setup needs attention: ' + result.cf_tunnel_error;
            setTimeout(() => location.reload(), 3000);
            return;
        }
        showWorkerEnrollProgress('init-worker', 'Done! Redirecting to dashboard...', 100);
        await new Promise(r => setTimeout(r, 600));
        location.reload();
    } catch (e) {
        delete wsProgressCallbacks['worker-enroll'];
        hideWorkerEnrollProgress('init-worker');
        errEl.textContent = e.message;
        backBtn.disabled = false;
        submitBtn.disabled = false;
        setWorkerCfButtonsDisabled('init-worker', false);
        submitBtn.textContent = 'Register';
    }
}

async function removeWorker(nodeId, name) {
    if (!confirm(`Remove worker "${name}"?`)) return;
    try {
        await api('DELETE', `/api/config/workers/${nodeId}`);
        // If we were viewing this node, switch back to self
        if (selectedNodeId === nodeId) {
            selectNode(selfNodeId);
        }
        await loadSettingsView();
        await refreshSidebar();
    } catch (e) {
        alert(`Failed to remove worker: ${e.message}`);
    }
}

async function resetNode() {
    if (!confirm('Reset this node to standalone mode? This will remove all cluster configuration.')) return;
    try {
        const config = await api('GET', '/api/config');
        const headers = config.api_key ? { 'X-Api-Key': config.api_key } : {};
        await api('POST', '/api/config/reset', null, headers);
        location.reload();
    } catch (e) {
        document.getElementById('settings-error').textContent = e.message;
    }
}

// ---- CF Tunnel management ----

async function updateCurrentTunnelId(nodeId) {
    try {
        const config = await api('GET', '/api/config');
        if (nodeId === selfNodeId) {
            currentTunnelId = config.tunnel_id || null;
        } else {
            const worker = (config.workers || {})[nodeId];
            currentTunnelId = worker ? worker.tunnel_id : null;
        }
    } catch (e) {
        currentTunnelId = null;
    }
}

async function refreshCfSection(context = null, options = {}) {
    if (!shouldShowLocalCfSection()) return;
    try {
        const nodeId = context ? context.nodeId : selectedNodeId;
        let tunnelId = null;
        if (isMaster) {
            // Resolve tunnel_id from the nodes list (includes tunnel_id for all nodes)
            const node = nodes.find(n => n.node_id === nodeId);
            tunnelId = node ? (node.tunnel_id || null) : null;
        } else {
            await updateCurrentTunnelId(selfNodeId);
            tunnelId = currentTunnelId;
        }
        if (!isRefreshCurrent(context)) return;
        currentTunnelId = tunnelId;

        // Show the section header
        document.getElementById('tunnel-section-header').style.display = '';
        // Show the active tab content
        const activeTab = document.querySelector('.tunnel-tab-content.active');
        if (activeTab) activeTab.style.display = '';

        let tunnelData = options.tunnelData || null;
        if (!tunnelData) {
            try {
                tunnelData = await api('GET', nodePathFor(nodeId, '/api/tunnel'));
            } catch (e) {
                tunnelData = { connected: false, connections: 0, detail: 'unreachable' };
            }
        }
        if (!isRefreshCurrent(context)) return;
        renderCfStatus(tunnelData);
        await refreshCfServiceControl(context);
        if (!isRefreshCurrent(context)) return;

        const accessAppsPromise = api('GET', '/api/cf/access/apps');
        const policiesPromise = api('GET', '/api/cf/access/policies');

        // If this node has no tunnel configured, show empty state for routes
        if (!currentTunnelId) {
            renderCfRoutes([]);
            const [accessApps, policies] = await Promise.all([accessAppsPromise, policiesPromise]);
            if (!isRefreshCurrent(context)) return;
            loadCfPolicies(policies);
            renderCfAccessApps(accessApps);
            return;
        }

        const [routes, accessApps, policies] = await Promise.all([
            api('GET', `/api/cf/routes?tunnel_id=${currentTunnelId}`),
            accessAppsPromise,
            policiesPromise,
        ]);
        if (!isRefreshCurrent(context)) return;
        loadCfPolicies(policies);
        renderCfRoutes(routes);
        renderCfAccessApps(accessApps);
    } catch (e) {
        console.error('CF section error:', e);
    }
}

function renderCfStatus(d) {
    const statusEl = document.getElementById('cf-tunnel-status');
    statusEl.textContent = d.connected ? 'Connected' : 'Disconnected';
    statusEl.style.color = d.connected ? 'var(--green)' : 'var(--red)';
    document.getElementById('cf-tunnel-connections').textContent = d.connections || '0';
    document.getElementById('cf-tunnel-locations').textContent =
        (d.locations || []).join(', ') || 'No edge connections';
    document.getElementById('cf-tunnel-id').textContent = currentTunnelId || 'Not configured';
    document.getElementById('cf-tunnel-name').textContent =
        currentTunnelId ? `Tunnel ${currentTunnelId.slice(0, 8)}...` : '--';
}

function cfServiceEndpoint(kind, lines = null, nodeId = selectedNodeId) {
    let base = `/api/cf/service/${kind}`;
    if (isMaster && nodeId && nodeId !== selfNodeId) {
        base = `/api/nodes/${nodeId}/cf/service/${kind}`;
    }
    if (lines !== null) {
        return `${base}?lines=${encodeURIComponent(lines)}`;
    }
    return base;
}

function renderCfServiceStatus(status) {
    const stateEl = document.getElementById('cf-service-state');
    const versionEl = document.getElementById('cf-service-version');
    const enabledEl = document.getElementById('cf-service-enabled');
    const resultEl = document.getElementById('cf-service-result');
    const pidEl = document.getElementById('cf-service-pid');
    if (!stateEl || !versionEl || !enabledEl || !resultEl || !pidEl) return;

    const activeState = status.active_state || 'unknown';
    const subState = status.sub_state || '';
    const stateLabel = subState && subState !== activeState
        ? `${activeState} (${subState})`
        : activeState;

    stateEl.textContent = stateLabel;
    if (activeState === 'active') stateEl.style.color = 'var(--green)';
    else if (activeState === 'failed') stateEl.style.color = 'var(--red)';
    else if (activeState === 'activating' || activeState === 'deactivating') stateEl.style.color = 'var(--yellow)';
    else stateEl.style.color = 'var(--text-secondary)';

    versionEl.textContent = status.binary_version || '--';
    enabledEl.textContent = status.unit_file_state || 'unknown';
    resultEl.textContent = status.result || '--';
    pidEl.textContent = status.main_pid ? String(status.main_pid) : '--';
}

async function refreshCfServiceControl(context = null) {
    const errEl = document.getElementById('cf-service-error');
    const logsEl = document.getElementById('cf-service-logs');
    if (!errEl || !logsEl) return;

    errEl.textContent = '';
    const nodeId = context ? context.nodeId : selectedNodeId;
    try {
        const status = await api('GET', cfServiceEndpoint('status', null, nodeId));
        if (isRefreshCurrent(context)) renderCfServiceStatus(status);
    } catch (e) {
        if (isRefreshCurrent(context)) errEl.textContent = `Status unavailable: ${e.message}`;
    }

    try {
        const data = await api('GET', cfServiceEndpoint('logs', 80, nodeId));
        if (isRefreshCurrent(context)) logsEl.textContent = data.logs || 'No recent logs.';
    } catch (e) {
        if (!isRefreshCurrent(context)) return;
        logsEl.textContent = 'Logs unavailable.';
        errEl.textContent = errEl.textContent
            ? `${errEl.textContent} Logs unavailable: ${e.message}`
            : `Logs unavailable: ${e.message}`;
    }
}

async function restartCfService() {
    const errEl = document.getElementById('cf-service-error');
    if (errEl) errEl.textContent = '';
    try {
        await api('POST', cfServiceEndpoint('restart'));
        await refreshCfServiceControl();
    } catch (e) {
        if (errEl) errEl.textContent = e.message;
    }
}

async function updateCfService() {
    const errEl = document.getElementById('cf-service-error');
    const inputEl = document.getElementById('cf-service-update-version');
    if (errEl) errEl.textContent = '';
    const version = inputEl ? inputEl.value.trim() : '';
    const payload = version ? { version } : {};
    try {
        await api('POST', cfServiceEndpoint('update'), payload);
        if (inputEl) inputEl.value = '';
        await refreshCfServiceControl();
    } catch (e) {
        if (errEl) errEl.textContent = e.message;
    }
}

function renderCfRoutes(routes) {
    const el = document.getElementById('cf-routes-table');
    if (!routes || routes.length === 0) {
        el.innerHTML = '<div class="empty-state">No ingress routes configured</div>';
        return;
    }
    el.innerHTML = `
        <div class="cf-table-header">
            <span>Hostname</span>
            <span>Service</span>
            <span></span>
        </div>
        ${routes.map(r => `
        <div class="cf-table-row">
            <span><a href="https://${esc(r.hostname)}" target="_blank">${esc(r.hostname)}</a></span>
            <span>${esc(r.service)}</span>
            <span><button class="btn danger" onclick="removeCfRoute('${esc(r.hostname)}')">Remove</button></span>
        </div>`).join('')}
    `;
}

function buildCfPolicyOptions(selectedId = '', selectedName = '') {
    const options = [];
    if (selectedId && !cfPolicies.some(p => p.id === selectedId)) {
        options.push(`<option value="${esc(selectedId)}" selected>${esc(selectedName || selectedId)}</option>`);
    }
    for (const policy of cfPolicies) {
        const selected = policy.id === selectedId ? ' selected' : '';
        options.push(`<option value="${esc(policy.id)}"${selected}>${esc(policy.name || policy.id)}</option>`);
    }
    return options.join('');
}

function renderCfPolicySelect() {
    const select = document.getElementById('cf-access-policy');
    if (!select) return;
    if (cfPolicies.length === 0) {
        select.innerHTML = '<option value="">No policies found</option>';
        return;
    }
    select.innerHTML = '<option value="">Select policy</option>' + buildCfPolicyOptions();
}

function renderCfAccessApps(apps) {
    const el = document.getElementById('cf-access-table');
    if (!apps || apps.length === 0) {
        el.innerHTML = '<div class="empty-state">No Access applications</div>';
        return;
    }
    el.innerHTML = `
        <div class="cf-table-header cf-table-5col">
            <span>Name</span>
            <span>Domain</span>
            <span>Policy</span>
            <span></span>
            <span></span>
        </div>
        ${apps.map(a => {
            const currentPolicy = (a.policies && a.policies[0]) ? a.policies[0] : null;
            const selectId = `cf-access-policy-app-${a.id}`;
            return `
            <div class="cf-table-row cf-table-5col">
                <span>${esc(a.name)}</span>
                <span><a href="https://${esc(a.domain)}" target="_blank">${esc(a.domain)}</a></span>
                <span>
                    <select id="${esc(selectId)}" class="cf-table-inline-select">
                        ${buildCfPolicyOptions(
                            currentPolicy ? currentPolicy.id : '',
                            currentPolicy ? (currentPolicy.name || currentPolicy.id) : 'No policy'
                        )}
                    </select>
                </span>
                <span><button class="btn" onclick="assignCfAccessAppPolicy('${esc(a.id)}', '${esc(selectId)}')">Apply</button></span>
                <span><button class="btn danger" onclick="removeCfAccessApp('${esc(a.domain)}')">Remove</button></span>
            </div>`;
        }).join('')}
    `;
}

function renderCfPolicies(policies) {
    const el = document.getElementById('cf-policy-list');
    if (!el) return;
    if (!policies || policies.length === 0) {
        el.innerHTML = '<div class="empty-state">No reusable policies found</div>';
        return;
    }
    el.innerHTML = policies.map(policy => {
        const members = Array.isArray(policy.members) ? policy.members : [];
        const memberRows = members.length > 0
            ? members.map(member => `
                <div class="cf-policy-member-row">
                    <code>${esc(member.value)}</code>
                    <button class="btn danger" onclick="removeCfPolicyMember('${esc(policy.id)}', '${encodeURIComponent(member.value)}')">Remove</button>
                </div>
            `).join('')
            : '<div class="cf-policy-empty">No email or domain rules on this policy.</div>';
        return `
            <div class="cf-policy-card">
                <div class="cf-policy-card-header">
                    <div>
                        <div class="cf-policy-card-title">${esc(policy.name || policy.id)}</div>
                        <div class="cf-policy-card-meta">
                            ${esc(policy.decision || 'allow')} policy · ${members.length} member${members.length === 1 ? '' : 's'}
                        </div>
                    </div>
                    <button class="btn danger" onclick="removeCfPolicy('${esc(policy.id)}')">Delete Policy</button>
                </div>
                <div class="cf-policy-members">${memberRows}</div>
                <div class="cf-add-form-row cf-policy-edit-row">
                    <div class="form-group">
                        <label>Add Email or Domain</label>
                        <input
                            type="text"
                            id="cf-policy-member-${esc(policy.id)}"
                            placeholder="alice@example.com or team.example.com"
                            autocomplete="off"
                        >
                    </div>
                    <button class="btn primary" onclick="addCfPolicyMember('${esc(policy.id)}')">Add</button>
                </div>
                <div class="form-error" id="cf-policy-error-${esc(policy.id)}"></div>
            </div>
        `;
    }).join('');
}

function loadCfPolicies(policies = null) {
    try {
        cfPolicies = Array.isArray(policies) ? policies : [];
        renderCfPolicySelect();
        renderCfPolicies(cfPolicies);
    } catch (e) {
        console.error('Failed to load policies:', e);
    }
}

async function createCfPolicy() {
    const nameEl = document.getElementById('cf-policy-create-name');
    const valueEl = document.getElementById('cf-policy-create-value');
    const errEl = document.getElementById('cf-policy-create-error');
    const name = nameEl ? nameEl.value.trim() : '';
    const value = valueEl ? valueEl.value.trim() : '';
    if (errEl) errEl.textContent = '';
    if (!name || !value) {
        if (errEl) errEl.textContent = 'Policy name and initial email/domain are required.';
        return;
    }
    try {
        const result = await api('POST', '/api/cf/access/policies', { name, value });
        if (nameEl) nameEl.value = '';
        if (valueEl) valueEl.value = '';
        await refreshCfSection();
        const select = document.getElementById('cf-access-policy');
        if (select && result && result.policy && result.policy.id) {
            select.value = result.policy.id;
        }
    } catch (e) {
        if (errEl) errEl.textContent = e.message;
    }
}

async function removeCfPolicy(policyId) {
    const policy = cfPolicies.find(p => p.id === policyId);
    const label = policy && policy.name ? `"${policy.name}"` : policyId;
    if (!confirm(`Delete Access policy ${label}?`)) return;
    const errEl = document.getElementById('cf-policy-create-error');
    if (errEl) errEl.textContent = '';
    try {
        await api('DELETE', `/api/cf/access/policies/${policyId}`);
        await refreshCfSection();
    } catch (e) {
        if (errEl) errEl.textContent = e.message;
    }
}

async function addCfRoute() {
    const hostname = document.getElementById('cf-route-hostname').value.trim();
    const service = document.getElementById('cf-route-service').value.trim();
    const errEl = document.getElementById('cf-route-error');
    errEl.textContent = '';
    if (!hostname || !service) { errEl.textContent = 'Both fields are required.'; return; }
    try {
        await api('POST', '/api/cf/routes', { hostname, service, tunnel_id: currentTunnelId });
        document.getElementById('cf-route-hostname').value = '';
        document.getElementById('cf-route-service').value = '';
        await refreshCfSection();
    } catch (e) {
        errEl.textContent = e.message;
    }
}

async function removeCfRoute(hostname) {
    if (!confirm(`Remove route for ${hostname}?`)) return;
    try {
        const params = currentTunnelId ? `?tunnel_id=${currentTunnelId}` : '';
        await api('DELETE', `/api/cf/routes/${hostname}${params}`);
        await refreshCfSection();
    } catch (e) {
        alert('Failed to remove route: ' + e.message);
    }
}

async function addCfAccessApp() {
    const name = document.getElementById('cf-access-name').value.trim();
    const hostname = document.getElementById('cf-access-hostname').value.trim();
    const policyId = document.getElementById('cf-access-policy').value;
    const errEl = document.getElementById('cf-access-error');
    errEl.textContent = '';
    if (!name || !hostname || !policyId) { errEl.textContent = 'All fields including policy are required.'; return; }
    try {
        await api('POST', '/api/cf/access/apps', { name, hostname, policy_id: policyId });
        document.getElementById('cf-access-name').value = '';
        document.getElementById('cf-access-hostname').value = '';
        await refreshCfSection();
    } catch (e) {
        errEl.textContent = e.message;
    }
}

async function assignCfAccessAppPolicy(appId, selectId) {
    const select = document.getElementById(selectId);
    const policyId = select ? select.value : '';
    const errEl = document.getElementById('cf-access-error');
    errEl.textContent = '';
    if (!policyId) { errEl.textContent = 'Choose a policy before assigning it.'; return; }
    try {
        await api('PUT', `/api/cf/access/apps/${appId}/policy`, { policy_id: policyId });
        await refreshCfSection();
    } catch (e) {
        errEl.textContent = e.message;
    }
}

async function removeCfAccessApp(hostname) {
    if (!confirm(`Delete Access app for ${hostname}?`)) return;
    try {
        await api('DELETE', `/api/cf/access/apps/${hostname}`);
        await refreshCfSection();
    } catch (e) {
        alert('Failed to delete Access app: ' + e.message);
    }
}

async function addCfPolicyMember(policyId) {
    const inputEl = document.getElementById(`cf-policy-member-${policyId}`);
    const errEl = document.getElementById(`cf-policy-error-${policyId}`);
    const value = inputEl ? inputEl.value.trim() : '';
    if (errEl) errEl.textContent = '';
    if (!value) {
        if (errEl) errEl.textContent = 'Enter an email or literal email domain.';
        return;
    }
    try {
        await api('POST', `/api/cf/access/policies/${policyId}/members`, { value });
        if (inputEl) inputEl.value = '';
        await refreshCfSection();
    } catch (e) {
        if (errEl) errEl.textContent = e.message;
    }
}

async function removeCfPolicyMember(policyId, encodedValue) {
    const value = decodeURIComponent(encodedValue);
    if (!confirm(`Remove ${value} from this policy?`)) return;
    const errEl = document.getElementById(`cf-policy-error-${policyId}`);
    if (errEl) errEl.textContent = '';
    try {
        await api('DELETE', `/api/cf/access/policies/${policyId}/members`, { value });
        await refreshCfSection();
    } catch (e) {
        if (errEl) errEl.textContent = e.message;
    }
}

async function setupWorkerTunnel(nodeId, nodeName) {
    if (!confirm(`Create a Cloudflare tunnel for "${nodeName}" and start cloudflared on the worker?`)) return;
    bindWorkerCfSetupProgress();
    showWorkerCfSetupProgress(`Setting up Cloudflare tunnel for ${nodeName}...`, 8);
    try {
        const result = await api('POST', `/api/nodes/${nodeId}/cf/setup`, {});
        delete wsProgressCallbacks['worker-cf-setup'];
        showWorkerCfSetupProgress(`Tunnel ready for ${nodeName}.`, 100);
        await loadSettingsView();
        renderWorkerCfSetupResults({ [nodeId]: { ...result, name: nodeName } });
    } catch (e) {
        delete wsProgressCallbacks['worker-cf-setup'];
        renderWorkerCfSetupError(e.message);
    }
}

function showWorkerCfSetupProgress(message, pct) {
    const progressEl = document.getElementById('worker-cf-sync-progress');
    const textEl = document.getElementById('worker-cf-sync-progress-text');
    const barEl = document.getElementById('worker-cf-sync-progress-bar');
    if (!progressEl || !textEl || !barEl) return;
    progressEl.style.display = '';
    textEl.textContent = message;
    if (pct !== undefined) barEl.style.width = pct + '%';
}

function hideWorkerCfSetupProgress() {
    const progressEl = document.getElementById('worker-cf-sync-progress');
    const barEl = document.getElementById('worker-cf-sync-progress-bar');
    if (progressEl) progressEl.style.display = 'none';
    if (barEl) barEl.style.width = '0%';
}

function bindWorkerCfSetupProgress() {
    let stepCount = 0;
    onWsProgress('worker-cf-setup', (msg) => {
        stepCount += 1;
        const pct = msg.done ? 100 : Math.min(10 + stepCount * 12, 92);
        showWorkerCfSetupProgress(msg.message || 'Setting up worker Cloudflare...', pct);
    });
}

function renderWorkerCfSetupResults(results) {
    const el = document.getElementById('worker-cf-sync-results');
    if (!el) return;
    const entries = Object.entries(results || {});
    if (entries.length === 0) {
        el.innerHTML = '<div class="settings-desc">No workers needed Cloudflare setup.</div>';
        return;
    }
    el.innerHTML = entries.map(([nodeId, result]) => {
        const ok = result.status && result.status !== 'error';
        const name = result.name || result.tunnel_name || nodeId;
        const detail = ok
            ? `Tunnel ${result.tunnel_id || 'ready'}`
            : (result.detail || 'Setup failed');
        return `<div class="settings-desc" style="color:${ok ? 'var(--green)' : 'var(--red)'}">${esc(name)}: ${esc(detail)}</div>`;
    }).join('');
}

function renderWorkerCfSetupError(message) {
    const el = document.getElementById('worker-cf-sync-results');
    if (el) el.innerHTML = `<div class="settings-desc" style="color:var(--red)">${esc(message)}</div>`;
}

async function setupMissingWorkerTunnels() {
    if (!confirm('Create Cloudflare tunnels for all workers missing one and start cloudflared on each worker?')) return;
    bindWorkerCfSetupProgress();
    showWorkerCfSetupProgress('Setting up Cloudflare on workers...', 5);
    try {
        const result = await api('POST', '/api/cf/workers/setup', {});
        delete wsProgressCallbacks['worker-cf-setup'];
        showWorkerCfSetupProgress('Worker Cloudflare setup finished.', 100);
        await loadSettingsView();
        renderWorkerCfSetupResults(result.workers || {});
    } catch (e) {
        delete wsProgressCallbacks['worker-cf-setup'];
        hideWorkerCfSetupProgress();
        renderWorkerCfSetupError(e.message);
    }
}

// ---- Deploy / update ----

async function loadDeployInfo() {
    try {
        const ver = await api('GET', '/api/node/version');
        const el = document.getElementById('deploy-version');
        if (el) el.textContent = ver.summary || 'unknown';
    } catch (e) {
        const el = document.getElementById('deploy-version');
        if (el) el.textContent = 'unknown';
    }
}

async function deployToWorkers() {
    const resultsEl = document.getElementById('deploy-results');
    resultsEl.innerHTML = '<div class="settings-desc" style="color:var(--accent)">Deploying...</div>';
    try {
        const data = await api('POST', '/api/update/deploy');
        const entries = Object.entries(data.workers || {});
        if (entries.length === 0) {
            resultsEl.innerHTML = '<div class="settings-desc">No workers to deploy to.</div>';
            return;
        }
        resultsEl.innerHTML = entries.map(([id, r]) => {
            const ok = r.status === 'updated';
            const color = ok ? 'var(--green)' : 'var(--red)';
            const msg = ok ? 'Update sent, restarting' : (r.detail || 'Failed');
            return `<div class="master-worker-row"><span class="status-dot ${ok ? 'green' : 'red'}"></span><span class="master-worker-name">${esc(r.name)}</span><span class="master-worker-address" style="color:${color}">${msg}</span></div>`;
        }).join('');
        setTimeout(() => refreshDeployWorkerStatuses(entries), 3000);
    } catch (e) {
        resultsEl.innerHTML = `<div class="settings-desc" style="color:var(--red)">${esc(e.message)}</div>`;
    }
}

async function refreshDeployWorkerStatuses(entries, attempt = 0) {
    const resultsEl = document.getElementById('deploy-results');
    if (!resultsEl) return;
    try {
        const [config, liveNodes] = await Promise.all([
            api('GET', '/api/config'),
            loadSettingsNodes(),
        ]);
        if (config.role === 'master') {
            renderMasterWorkers(config.workers || {}, liveNodes);
        }
        let waiting = false;
        resultsEl.innerHTML = entries.map(([id, r]) => {
            if (r.status !== 'updated') {
                return `<div class="master-worker-row"><span class="status-dot red"></span><span class="master-worker-name">${esc(r.name)}</span><span class="master-worker-address" style="color:var(--red)">${esc(r.detail || 'Failed')}</span></div>`;
            }
            const node = (liveNodes || []).find(n => n.config_node_id === id || n.node_id === id);
            const online = node && node.status === 'online';
            if (!online && attempt < 6) waiting = true;
            const statusClass = online ? 'green' : 'yellow';
            const color = online ? 'var(--green)' : 'var(--yellow)';
            const msg = online ? 'Online after restart' : 'Waiting for heartbeat';
            return `<div class="master-worker-row"><span class="status-dot ${statusClass}"></span><span class="master-worker-name">${esc(r.name)}</span><span class="master-worker-address" style="color:${color}">${msg}</span></div>`;
        }).join('');
        if (waiting) {
            setTimeout(() => refreshDeployWorkerStatuses(entries, attempt + 1), 3000);
        }
    } catch (e) {
        if (attempt < 6) {
            setTimeout(() => refreshDeployWorkerStatuses(entries, attempt + 1), 3000);
        }
    }
}

async function updateMasterFromGit() {
    if (!confirm('Update the master from git and restart inframatik?')) return;
    const resultEl = document.getElementById('git-update-results');
    if (resultEl) {
        resultEl.innerHTML = '<div class="settings-desc" style="color:var(--accent)">Updating from git...</div>';
    }
    try {
        const data = await api('POST', '/api/update/git');
        const detail = esc(data.detail || 'Updated from git.');
        if (resultEl) {
            resultEl.innerHTML = `<div class="settings-desc" style="color:var(--green)">${detail}<br>Restarting master...</div>`;
        }
        setTimeout(() => location.reload(), 3000);
    } catch (e) {
        if (resultEl) {
            resultEl.innerHTML = `<div class="settings-desc" style="color:var(--red)">${esc(e.message)}</div>`;
        } else {
            alert('Git update failed: ' + e.message);
        }
    }
}

async function deployRestart() {
    if (!confirm('Restart the master? The page will reload in a few seconds.')) return;
    try {
        await api('POST', '/api/update/deploy-self');
        setTimeout(() => location.reload(), 3000);
    } catch (e) {
        alert('Failed to restart: ' + e.message);
    }
}

// ---- Refresh loop ----

function renderNodeSnapshot(snapshot, context) {
    if (!snapshot || !isRefreshCurrent(context)) return;
    if (snapshot.system) renderSystem(snapshot.system);
    if (snapshot.tunnel) renderTunnel(snapshot.tunnel);
    if (Array.isArray(snapshot.services)) renderServices(snapshot.services);
}

async function refreshNodeData(context) {
    if (isMaster && context && context.nodeId) {
        try {
            const snapshot = await api('GET', `/api/nodes/${context.nodeId}/snapshot`);
            renderNodeSnapshot(snapshot, context);
            return snapshot;
        } catch (e) {
            console.error('Failed to fetch node snapshot:', e);
        }
    }

    const [system, tunnel, services] = await Promise.all([
        refreshSystem(context),
        refreshTunnel(context),
        refreshServices(context),
    ]);
    return { system, tunnel, services };
}

async function refreshAll(options = {}) {
    if (currentAppView !== 'main') return;
    const priority = !!options.priority;
    if (!priority && (refreshInFlight || priorityRefreshes > 0)) {
        refreshQueued = true;
        refreshQueuedForceCf = refreshQueuedForceCf || !!options.forceCf;
        return;
    }

    const context = makeRefreshContext();
    if (priority) priorityRefreshes += 1;
    else refreshInFlight = true;

    try {
        const snapshot = await refreshNodeData(context);
        if (isRefreshCurrent(context) && shouldShowLocalCfSection() && (!cfSectionLoaded || options.forceCf)) {
            cfSectionLoaded = true;
            const cfRefresh = refreshCfSection(context, { tunnelData: snapshot ? snapshot.tunnel : null });
            if (!priority) await cfRefresh;
        }
    } finally {
        if (priority) priorityRefreshes = Math.max(0, priorityRefreshes - 1);
        else refreshInFlight = false;

        if (!refreshInFlight && priorityRefreshes === 0 && refreshQueued) {
            const forceCf = refreshQueuedForceCf;
            refreshQueued = false;
            refreshQueuedForceCf = false;
            await refreshAll({ forceCf });
        }
    }
}

function stopRefreshLoop() {
    if (refreshInterval) {
        clearInterval(refreshInterval);
        refreshInterval = null;
    }
}

function startRefreshLoop() {
    stopRefreshLoop();
    const initial = refreshAll();
    refreshInterval = setInterval(refreshAll, 5000);
    return initial;
}

async function loadVersionTag() {
    try {
        const ver = await api('GET', '/api/node/version');
        const el = document.getElementById('version-tag');
        el.textContent = ver.summary || '';
        el.title = ver.branch ? `${ver.branch} @ ${ver.commit}` : '';
    } catch (e) {}
}

async function loadUserInfo() {
    try {
        const me = await api('GET', '/api/auth/me');
        const el = document.getElementById('user-email');
        if (me.email) {
            el.textContent = me.email;
            el.style.display = '';
        }
    } catch (e) {}
}

// ---- Auth flow ----

function showLogin() {
    document.getElementById('login-screen').style.display = '';
    document.getElementById('dashboard-content').style.display = 'none';
}

function showDashboard() {
    document.getElementById('login-screen').style.display = 'none';
    document.getElementById('dashboard-content').style.display = '';
    connectWs();
}

async function checkAuthAndInit() {
    try {
        const status = await fetch('/api/auth/status').then(r => r.json());
        if (!status.has_password) {
            // First run — show set password form
            document.getElementById('login-screen').style.display = '';
            document.getElementById('dashboard-content').style.display = 'none';
            document.getElementById('login-form').style.display = 'none';
            document.getElementById('set-password-form').style.display = '';
            return;
        }
        // Validate existing cookie session against an authenticated endpoint
        const resp = await fetch('/api/config', {
            credentials: 'same-origin',
        });
        if (resp.status === 401) {
            showLogin();
            return;
        }
    } catch (e) {
        // Can't reach server — show dashboard anyway (might work)
    }
    showDashboard();
    const configured = await initCluster();
    if (configured) {
        loadVersionTag();
        loadUserInfo();
        startRefreshLoop();
    }
}

async function submitLogin() {
    const password = document.getElementById('login-password').value;
    const errEl = document.getElementById('login-error');
    errEl.textContent = '';
    if (!password) { errEl.textContent = 'Password is required.'; return; }
    try {
        const resp = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || 'Login failed');
        document.getElementById('login-password').value = '';
        showDashboard();
        const configured = await initCluster();
        if (configured) {
            loadVersionTag();
            loadUserInfo();
            startRefreshLoop();
        }
    } catch (e) {
        errEl.textContent = e.message;
    }
}

async function submitSetPassword() {
    const pw = document.getElementById('set-password-input').value;
    const confirm = document.getElementById('set-password-confirm').value;
    const errEl = document.getElementById('set-password-error');
    errEl.textContent = '';
    if (!pw || pw.length < 8) { errEl.textContent = 'Password must be at least 8 characters.'; return; }
    if (pw !== confirm) { errEl.textContent = 'Passwords do not match.'; return; }
    try {
        const resp = await fetch('/api/auth/set-password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: pw }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || 'Failed to set password');
        // Auto-login after setting password
        const loginResp = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: pw }),
        });
        await loginResp.json();
        if (loginResp.ok) {
            authToken = null;
        }
        showDashboard();
        const configured = await initCluster();
        if (configured) {
            loadVersionTag();
            loadUserInfo();
            startRefreshLoop();
        }
    } catch (e) {
        errEl.textContent = e.message;
    }
}

document.addEventListener('DOMContentLoaded', checkAuthAndInit);

// Close modals on escape
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closeLogs();
        closeNewService();
        closeSettings();
    }
});
