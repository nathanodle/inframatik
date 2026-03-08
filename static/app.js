const API = '';
let refreshInterval;
let prevNet = null;
let prevNetTime = null;
let authToken = null;

// ---- WebSocket ----
let ws = null;
let wsProgressCallbacks = {};

function connectWs() {
    if (ws) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            if (msg.type === 'progress' && wsProgressCallbacks[msg.task]) {
                wsProgressCallbacks[msg.task](msg);
            }
        } catch (e) {}
    };
    ws.onclose = () => {
        ws = null;
        // Reconnect after a short delay
        setTimeout(() => { if (authToken || document.cookie.includes('inframatik_session')) connectWs(); }, 3000);
    };
    ws.onerror = () => {};
}

function onWsProgress(task, callback) {
    wsProgressCallbacks[task] = callback;
}

// ---- Cluster state ----
let isMaster = false;
let selfNodeId = null;
let selectedNodeId = null;
let nodes = [];
let sidebarInterval = null;
let currentTunnelId = null;
let cfPolicies = [];
let cfSectionLoaded = false;
let machineHostname = window.location.hostname || '';

// ---- Helpers ----

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
        throw new Error(err.detail || 'Request failed');
    }
    return resp.json();
}

// Build the API path, rewriting through the proxy when viewing a remote node on the master
function nodePath(path) {
    if (isMaster && selectedNodeId && selectedNodeId !== selfNodeId) {
        // Rewrite /api/system -> /api/nodes/{id}/system
        // Rewrite /api/services -> /api/nodes/{id}/services
        // Rewrite /api/services/foo/start -> /api/nodes/{id}/services/foo/start
        // Rewrite /api/tunnel -> /api/nodes/{id}/tunnel
        // Rewrite /api/ports/next -> /api/ports/next (local only, don't proxy)
        if (path.startsWith('/api/system') || path.startsWith('/api/services') || path.startsWith('/api/tunnel')) {
            // Strip /api prefix: /api/system -> /system, then build /api/nodes/{id}/system
            const subpath = path.slice(4); // remove '/api'
            return `/api/nodes/${selectedNodeId}${subpath}`;
        }
    }
    return path;
}

// ---- Tabs ----

document.addEventListener('click', (e) => {
    if (e.target.classList.contains('tab')) {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
        e.target.classList.add('active');
        document.getElementById('tab-' + e.target.dataset.tab).classList.add('active');
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
});

// ---- Cluster init ----

async function initCluster() {
    try {
        const info = await api('GET', '/api/node/info');
        if (info.machine_hostname && typeof info.machine_hostname === 'string') {
            machineHostname = info.machine_hostname.trim();
        }
        if (info.role === 'unconfigured') {
            // First run — show setup modal
            document.getElementById('setup-modal').classList.add('active');
            connectWs();
            return false;
        }
        if (info.role === 'master') {
            isMaster = true;
            selfNodeId = info.node_id;
            selectedNodeId = info.node_id;
            document.getElementById('sidebar').classList.add('visible');
            document.getElementById('topbar-title').innerHTML =
                `${esc(info.node_name)} <span>/ inframatik</span>`;
            await refreshSidebar();
            sidebarInterval = setInterval(refreshSidebar, 15000);
            await updateCurrentTunnelId(info.node_id);
        } else if (info.node_name) {
            // Standalone or worker — show node name in topbar
            selfNodeId = info.node_id;
            document.getElementById('topbar-title').innerHTML =
                `${esc(info.node_name)} <span>/ inframatik</span>`;
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
    for (const id of ['setup-choose', 'setup-cf-prompt', 'setup-cf-consent', 'setup-cf-config', 'setup-name', 'setup-worker']) {
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
        document.getElementById('setup-worker-name').value = '';
        document.getElementById('setup-worker-master').value = '';
        document.getElementById('setup-worker-token').value = '';
        document.getElementById('setup-worker-error').textContent = '';
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
}

function showSetupCfTokenStep() {
    _hideAllSetupSteps();
    document.getElementById('setup-cf-config').style.display = '';
    document.getElementById('setup-cf-token').value = '';
    document.getElementById('setup-cf-error').textContent = '';
    document.getElementById('setup-cf-steps').innerHTML = '';
}

function skipSetupCf() {
    setupCf.enabled = false;
    showSetupNameStep();
}

async function validateSetupCfToken() {
    const token = document.getElementById('setup-cf-token').value.trim();
    const errEl = document.getElementById('setup-cf-error');
    errEl.textContent = '';
    if (!token) { errEl.textContent = 'API token is required.'; return; }

    // Loading state
    const validateBtn = document.querySelector('#setup-cf-config .btn.primary');
    if (validateBtn) { validateBtn.disabled = true; validateBtn.textContent = 'Validating...'; }

    try {
        const data = await api('POST', '/api/cf/setup/validate-token', { token });
        setupCf.token = token;

        // Hide token input + validate button, show success
        document.getElementById('setup-cf-token').disabled = true;
        // Replace validate button with checkmark
        const btns = document.getElementById('setup-cf-config').querySelectorAll('.form-actions')[0];
        if (btns) btns.innerHTML = '<button class="btn" onclick="showSetupCfConsent()">Back</button> <span style="color:var(--green)">✓ Token valid</span>';

        // Auto-select account if only one
        let account_id;
        if (data.accounts.length === 1) {
            account_id = data.accounts[0].id;
        } else {
            document.getElementById('setup-cf-steps').innerHTML = `
                <div class="form-group">
                    <label>Account</label>
                    <select id="setup-cf-account">
                        ${data.accounts.map(a => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('')}
                    </select>
                </div>
                <div class="form-actions">
                    <button class="btn primary" onclick="selectSetupCfAccount()">Next</button>
                </div>`;
            return;
        }

        setupCf.account_id = account_id;
        await loadSetupCfZones();
    } catch (e) {
        errEl.textContent = e.message;
        if (validateBtn) { validateBtn.disabled = false; validateBtn.textContent = 'Validate'; }
    }
}

async function selectSetupCfAccount() {
    const sel = document.getElementById('setup-cf-account');
    setupCf.account_id = sel.value;
    await loadSetupCfZones();
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

async function loadSetupCfZones() {
    const errEl = document.getElementById('setup-cf-error');
    try {
        const data = await api('POST', '/api/cf/setup/zones', {
            token: setupCf.token, account_id: setupCf.account_id,
        });

        if (data.zones.length === 0) {
            errEl.textContent = 'No domains found. Add a domain in Cloudflare first.';
            return;
        }
        setupCf.zones = data.zones;

        // Load policies too
        let policies = [];
        try {
            const pData = await api('POST', '/api/cf/setup/policies', {
                token: setupCf.token, account_id: setupCf.account_id,
            });
            policies = pData.policies || [];
        } catch (e) { /* policies are optional */ }
        setupCf._policies = policies;

        const zoneOptions = data.zones.map(z =>
            `<option value="${esc(z.id)}" data-name="${esc(z.name)}">${esc(z.name)}</option>`
        ).join('');

        const policyOptions = policies.map(p =>
            `<option value="${esc(p.id)}">${esc(p.name)}</option>`
        ).join('');

        document.getElementById('setup-cf-steps').innerHTML = `
            <div class="form-group">
                <label>Admin Email</label>
                <input type="email" id="setup-cf-admin-email" placeholder="you@company.com" autocomplete="off">
                <div class="label-hint" style="margin-top:4px">Used for Zero Trust access. Must be covered by the Access policy.</div>
            </div>
            <div class="form-group">
                <label>Domain</label>
                <select id="setup-cf-zone">${zoneOptions}</select>
            </div>
            <div class="form-group">
                <label>Access Policy</label>
                <select id="setup-cf-policy">
                    <option value="">None — skip Access protection</option>
                    ${policyOptions}
                    <option value="__create__">Create new policy for my email</option>
                </select>
            </div>
            <div class="form-error" id="setup-cf-policy-warning"></div>
            <div class="form-actions">
                <button class="btn primary" onclick="finishSetupCf()">Continue</button>
            </div>`;

        // Warn if selected policy doesn't cover admin email
        document.getElementById('setup-cf-policy').addEventListener('change', () => {
            _checkSetupPolicyCoverage();
        });
        document.getElementById('setup-cf-admin-email').addEventListener('input', () => {
            _checkSetupPolicyCoverage();
        });
    } catch (e) {
        errEl.textContent = e.message;
    }
}

function _checkSetupPolicyCoverage() {
    const warnEl = document.getElementById('setup-cf-policy-warning');
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
    const errEl = document.getElementById('setup-cf-error');
    errEl.textContent = '';

    const email = (document.getElementById('setup-cf-admin-email').value || '').trim();
    if (!email || !email.includes('@')) {
        errEl.textContent = 'A valid admin email is required.';
        return;
    }
    setupCf.admin_email = email;

    const zoneSel = document.getElementById('setup-cf-zone');
    setupCf.zone_id = zoneSel.value;
    setupCf.zone_name = zoneSel.options[zoneSel.selectedIndex].dataset.name;

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
        document.getElementById('setup-node-name').value = '';

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

    // Disable submit button
    const submitBtn = document.getElementById('setup-submit-btn');
    submitBtn.disabled = true;
    submitBtn.textContent = 'Setting up...';

    // Show progress area
    const progressEl = document.getElementById('setup-progress');

    try {
        // 1. Create role
        await api('POST', endpoint, { name });

        // 2. If CF enabled, save CF config + enable dashboard access
        if (setupCf.enabled) {
            try {
                progressEl.style.display = '';
                progressEl.textContent = 'Saving Cloudflare configuration...';

                await api('POST', '/api/cf/setup/save', {
                    token: setupCf.token,
                    account_id: setupCf.account_id,
                    zone_id: setupCf.zone_id,
                    default_policy_id: setupCf.default_policy_id,
                });

                const domain = document.getElementById('setup-domain-select').value;
                const hostname = `${name}.${domain}`;

                // Listen for progress via WebSocket
                let cfDone = false;
                onWsProgress('dashboard-access', (msg) => {
                    progressEl.textContent = msg.message;
                    if (msg.done) cfDone = true;
                });

                progressEl.textContent = 'Setting up dashboard access...';
                await api('POST', '/api/config/dashboard-access', { hostname });
                // API returns when done, but WS may have already shown progress
                delete wsProgressCallbacks['dashboard-access'];
            } catch (cfErr) {
                delete wsProgressCallbacks['dashboard-access'];
                progressEl.style.display = 'none';
                errEl.textContent = 'Note: Cloudflare setup failed (' + cfErr.message + '). You can configure it in Settings.';
                await new Promise(r => setTimeout(r, 3000));
            }
        }

        progressEl.style.display = '';
        progressEl.textContent = 'Done! Redirecting to dashboard...';
        await new Promise(r => setTimeout(r, 1000));
        location.reload();
    } catch (e) {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Get Started';
        progressEl.style.display = 'none';
        errEl.textContent = e.message;
    }
}

async function submitSetupWorker() {
    const name = document.getElementById('setup-worker-name').value.trim();
    const master_url = document.getElementById('setup-worker-master').value.trim().replace(/\/+$/, '');
    const token = document.getElementById('setup-worker-token').value.trim();
    const errEl = document.getElementById('setup-worker-error');
    errEl.textContent = '';
    if (!name || !master_url || !token) { errEl.textContent = 'All fields are required.'; return; }

    const regBtn = document.querySelector('#setup-worker .btn.primary');
    if (regBtn) { regBtn.disabled = true; regBtn.textContent = 'Registering...'; }

    try {
        // Determine our address for the master
        const address = `http://${window.location.hostname}:9000`;

        // Enroll with master — master validates token and returns api_key
        const resp = await fetch(`${master_url}/api/nodes/enroll`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token, node_name: name, address }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
            throw new Error(err.detail || 'Enrollment failed');
        }
        const result = await resp.json();

        // Configure locally as worker with credentials from master
        await api('POST', '/api/config/init-worker', {
            name,
            master_url,
            api_key: result.api_key,
            update_public_key: result.signing_public_key || null,
        });
        location.reload();
    } catch (e) {
        errEl.textContent = e.message;
        if (regBtn) { regBtn.disabled = false; regBtn.textContent = 'Register'; }
    }
}

async function refreshSidebar() {
    if (!isMaster) return;
    try {
        nodes = await api('GET', '/api/nodes');
        renderSidebar(nodes);
    } catch (e) {
        console.error('Failed to fetch nodes:', e);
    }
}

function renderSidebar(nodeList) {
    const el = document.getElementById('sidebar-nodes');
    el.innerHTML = nodeList.map(node => {
        const isSelected = node.node_id === selectedNodeId;
        const statusClass = node.status === 'online' ? 'green' : 'red';
        const tag = node.is_self ? 'local' : '';
        return `
        <div class="sidebar-node ${isSelected ? 'active' : ''}" onclick="selectNode('${esc(node.node_id)}')">
            <span class="status-dot ${statusClass}"></span>
            <span class="sidebar-node-name">${esc(node.node_name)}</span>
            ${tag ? `<span class="sidebar-node-tag">${tag}</span>` : ''}
        </div>`;
    }).join('');
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
        document.getElementById('topbar-title').innerHTML =
            `${esc(node.node_name)} <span>/ inframatik</span>`;
    }

    refreshAll();
    if (cfSectionLoaded) refreshCfSection();
}

// ---- System metrics ----

async function refreshSystem() {
    try {
        const data = await api('GET', nodePath('/api/system'));
        renderSystem(data);
    } catch (e) {
        console.error('Failed to fetch system metrics:', e);
    }
}

function renderSystem(d) {
    document.getElementById('uptime').textContent = d.uptime;

    // Host info bar
    if (d.host) {
        document.getElementById('host-bar').innerHTML =
            `<span>${d.host.distro}</span>` +
            `<span>${d.host.cpu_model}</span>` +
            `<span>${d.cpu.count} cores</span>` +
            `<span>${formatBytes(d.memory.total)} RAM</span>`;
    }

    // CPU
    document.getElementById('cpu-value').innerHTML = `${d.cpu.percent}<span class="unit">%</span>`;
    document.getElementById('cpu-sub').textContent = `${d.cpu.count} cores @ ${d.cpu.freq_mhz || '?'} MHz`;
    const cpuBar = document.getElementById('cpu-bar');
    cpuBar.style.width = d.cpu.percent + '%';
    cpuBar.className = 'progress-fill ' + progressColor(d.cpu.percent);

    // CPU per-core
    const coresEl = document.getElementById('cpu-cores');
    if (d.cpu.per_cpu) {
        coresEl.innerHTML = d.cpu.per_cpu.map(pct =>
            `<div class="cpu-core-bar" style="height:${Math.max(pct, 3)}%;background:${coreColor(pct)}" title="${pct}%"></div>`
        ).join('');
    }

    // Memory
    document.getElementById('mem-value').innerHTML = `${d.memory.percent}<span class="unit">%</span>`;
    document.getElementById('mem-sub').textContent = `${formatBytes(d.memory.used)} / ${formatBytes(d.memory.total)}`;
    const memBar = document.getElementById('mem-bar');
    memBar.style.width = d.memory.percent + '%';
    memBar.className = 'progress-fill ' + progressColor(d.memory.percent);

    // Disk (primary /)
    const rootDisk = d.disks.find(dk => dk.mount === '/');
    if (rootDisk) {
        document.getElementById('disk-value').innerHTML = `${rootDisk.percent}<span class="unit">%</span>`;
        document.getElementById('disk-sub').textContent = `${formatBytes(rootDisk.used)} / ${formatBytes(rootDisk.total)}`;
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
            document.getElementById('net-rate').innerHTML = `<span style="font-size:16px">&darr;</span> ${formatRate(downRate)}`;
            document.getElementById('net-rate-sub').innerHTML = `<span>&uarr;</span> ${formatRate(upRate)} &middot; ${formatBytes(d.network.bytes_recv)} total`;
        }
    } else {
        document.getElementById('net-rate').innerHTML = `<span style="font-size:16px">&darr;</span> ${formatBytes(d.network.bytes_recv)}`;
        document.getElementById('net-rate-sub').innerHTML = `<span>&uarr;</span> ${formatBytes(d.network.bytes_sent)} total`;
    }
    prevNet = d.network;
    prevNetTime = now;

    // Load
    document.getElementById('load-value').textContent = d.load['1min'].toFixed(2);
    document.getElementById('load-sub').textContent = `${d.load['5min'].toFixed(2)} / ${d.load['15min'].toFixed(2)} (5m/15m)`;

    // Temperatures
    if (d.temps && d.temps.cpu !== undefined) {
        const cpuTemp = d.temps.cpu;
        document.getElementById('temp-value').innerHTML = `${cpuTemp.toFixed(0)}<span class="unit">&deg;C</span>`;
        let sub = `CPU ${cpuTemp.toFixed(1)}&deg;C`;
        if (d.temps.nvme !== undefined) sub += ` &middot; NVMe ${d.temps.nvme.toFixed(0)}&deg;C`;
        document.getElementById('temp-sub').innerHTML = sub;
    }

    // GPUs tab
    renderGpus(d.gpus || []);

    // Processes tab
    renderProcesses(d.processes || []);

    // Network tab
    renderNetInterfaces(d.network.interfaces || []);

    // Storage tab
    renderStorage(d.disks || []);
}

// ---- GPUs ----

function renderGpus(gpus) {
    const el = document.getElementById('gpu-cards');
    if (gpus.length === 0) {
        el.innerHTML = '<div class="empty-state">No GPUs detected</div>';
        return;
    }
    el.innerHTML = gpus.map(gpu => {
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
    }).join('');
}

// ---- Processes ----

function renderProcesses(procs) {
    const el = document.getElementById('process-table');
    if (procs.length === 0) {
        el.innerHTML = '<div class="empty-state">No process data</div>';
        return;
    }
    el.innerHTML = `
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
    `;
}

// ---- Network interfaces ----

function renderNetInterfaces(interfaces) {
    const el = document.getElementById('net-cards');
    if (interfaces.length === 0) {
        el.innerHTML = '<div class="empty-state">No active network interfaces</div>';
        return;
    }
    el.innerHTML = interfaces.map(iface => `
        <div class="metric-card">
            <div class="metric-label">${esc(iface.name)}</div>
            <div class="metric-value" style="font-size:18px">${iface.ip || 'No IP'}</div>
            <div class="metric-sub">${iface.speed_mbps ? iface.speed_mbps + ' Mbps' : ''}</div>
            <div class="net-iface-stats">
                <span>&darr; ${formatBytes(iface.bytes_recv)}</span>
                <span>&uarr; ${formatBytes(iface.bytes_sent)}</span>
            </div>
        </div>
    `).join('');
}

// ---- Storage ----

function renderStorage(disks) {
    const el = document.getElementById('storage-cards');
    if (disks.length === 0) {
        el.innerHTML = '<div class="empty-state">No disks found</div>';
        return;
    }
    el.innerHTML = disks.map(dk => `
        <div class="metric-card">
            <div class="metric-label">${esc(dk.mount)} <span style="color:var(--text-muted);font-size:10px">${esc(dk.device)}</span></div>
            <div class="metric-value">${dk.percent}<span class="unit">%</span></div>
            <div class="metric-sub">${formatBytes(dk.used)} / ${formatBytes(dk.total)} (${dk.fstype})</div>
            <div class="progress-bar"><div class="progress-fill ${progressColor(dk.percent)}" style="width:${dk.percent}%"></div></div>
        </div>
    `).join('');
}

// ---- Tunnel ----

async function refreshTunnel() {
    try {
        const data = await api('GET', nodePath('/api/tunnel'));
        renderTunnel(data);
    } catch (e) {
        renderTunnel({ connected: false, detail: 'unreachable' });
    }
}

function renderTunnel(d) {
    const dot = document.getElementById('tunnel-dot');
    const text = document.getElementById('tunnel-text');
    dot.className = 'status-dot ' + (d.connected ? 'green' : 'red');
    text.textContent = d.connected ? `Tunnel: ${d.detail}` : 'Tunnel: disconnected';
}

// ---- Services ----

async function refreshServices() {
    try {
        const data = await api('GET', nodePath('/api/services'));
        renderServices(data);
    } catch (e) {
        console.error('Failed to fetch services:', e);
    }
}

function renderServices(services) {
    const el = document.getElementById('services-list');
    if (services.length === 0) {
        el.innerHTML = '<div class="empty-state">No services registered yet. Add one to get started.</div>';
        return;
    }

    el.innerHTML = services.map(svc => {
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
    }).join('');
}

function esc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
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

// ---- Settings modal ----

function openSettings() {
    document.getElementById('settings-modal').classList.add('active');
    document.getElementById('settings-error').textContent = '';
    loadSettingsView();
}

function closeSettings() {
    document.getElementById('settings-modal').classList.remove('active');
}

async function loadSettingsView() {
    // Hide all settings panels
    document.getElementById('settings-unconfigured').style.display = 'none';
    document.getElementById('settings-standalone').style.display = 'none';
    document.getElementById('settings-init-master').style.display = 'none';
    document.getElementById('settings-init-worker').style.display = 'none';
    document.getElementById('settings-worker-key').style.display = 'none';
    document.getElementById('settings-master').style.display = 'none';

    try {
        const config = await api('GET', '/api/config');

        if (config.role === 'master') {
            document.getElementById('settings-master').style.display = 'block';
            document.getElementById('master-info-name').textContent = config.node_name;
            renderMasterWorkers(config.workers || {});
            renderEnrollmentTokens(config.enrollment_tokens || []);
            renderCfSetup('master-cf-setup', config.cf_configured);
            renderServiceTokens('master-service-tokens', config.service_tokens || []);
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
            document.getElementById('settings-worker-key').style.display = 'block';
            document.getElementById('worker-key-value').textContent = config.api_key;
            document.getElementById('worker-info-name').textContent = config.node_name;
            document.getElementById('worker-info-master').textContent = config.master_url;
        } else if (config.role === 'standalone') {
            document.getElementById('settings-standalone').style.display = 'block';
            document.getElementById('standalone-info-name').textContent = config.node_name;
            renderCfSetup('standalone-cf-setup', config.cf_configured);
            renderServiceTokens('standalone-service-tokens', config.service_tokens || []);
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
        <div class="form-group">
            <label>API Token</label>
            <input type="password" id="cf-wiz-token" placeholder="Paste your Cloudflare API token" autocomplete="off">
            <div class="label-hint" style="margin-top:4px">Needs permissions: Tunnel Edit, Access Edit, DNS Edit</div>
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

function renderMasterWorkers(workers) {
    const el = document.getElementById('master-workers-list');
    const entries = Object.entries(workers);
    if (entries.length === 0) {
        el.innerHTML = '<div class="settings-empty">No workers configured yet.</div>';
        return;
    }
    el.innerHTML = entries.map(([nodeId, w]) => {
        // Find online status from nodes list
        const nodeInfo = nodes.find(n => n.node_id === nodeId);
        const status = nodeInfo ? nodeInfo.status : 'offline';
        const statusClass = status === 'online' ? 'green' : 'red';
        const cfBadge = w.tunnel_id
            ? '<span class="worker-cf-badge">CF</span>'
            : `<button class="btn" onclick="setupWorkerTunnel('${esc(nodeId)}', '${esc(w.name)}')">Setup Tunnel</button>`;
        return `
        <div class="master-worker-row">
            <span class="status-dot ${statusClass}"></span>
            <span class="master-worker-name">${esc(w.name)}</span>
            <span class="master-worker-address">${esc(w.address)}</span>
            ${cfBadge}
            <button class="btn danger" onclick="removeWorker('${esc(nodeId)}', '${esc(w.name)}')">Remove</button>
        </div>`;
    }).join('');
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
    navigator.clipboard.writeText(text).then(() => {
        const orig = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(() => btn.textContent = orig, 1500);
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

function renderServiceTokens(containerId, tokens) {
    const el = document.getElementById(containerId);
    if (!el) return;

    const tokenRows = tokens.map(t => {
        const createdDate = t.created_at ? new Date(t.created_at * 1000).toLocaleDateString() : '--';
        return `
        <div class="master-worker-row">
            <span>${esc(t.service)}</span>
            <span class="master-worker-address">${createdDate}</span>
            <button class="btn danger" onclick="revokeServiceToken('${containerId}', '${esc(t.token_id || '')}', '${esc(t.service || '')}')">Revoke</button>
        </div>
    `;
    }).join('');

    el.innerHTML = `
        <div class="settings-subsection-header">Service Tokens</div>
        ${tokens.length > 0 ? tokenRows : '<p class="settings-desc">No service tokens. Generate one to let AI agents manage a service.</p>'}
        <div id="${containerId}-new-token" style="display:none">
            <div class="master-worker-row" style="background:var(--bg-input);padding:12px;border-radius:var(--radius-sm);margin:8px 0">
                <code id="${containerId}-token-value" style="word-break:break-all"></code>
                <button class="btn" onclick="copyText(document.getElementById('${containerId}-token-value').textContent, this)">Copy</button>
            </div>
            <p class="settings-desc">Copy this token now — it won't be shown again.</p>
        </div>
        <div class="form-group" style="margin-top:8px">
            <input type="text" id="${containerId}-service-name" placeholder="Service name" autocomplete="off">
        </div>
        <div class="form-actions">
            <button class="btn primary" onclick="generateServiceToken('${containerId}')">Generate Token</button>
        </div>`;
}

async function generateServiceToken(containerId) {
    const nameEl = document.getElementById(`${containerId}-service-name`);
    const service = nameEl.value.trim();
    if (!service) { alert('Service name is required.'); return; }
    try {
        const result = await api('POST', '/api/config/service-tokens', { service });
        // Show the token once
        const tokenDisplay = document.getElementById(`${containerId}-new-token`);
        document.getElementById(`${containerId}-token-value`).textContent = result.token;
        tokenDisplay.style.display = '';
        nameEl.value = '';
    } catch (e) {
        alert('Failed to generate token: ' + e.message);
    }
}

async function revokeServiceToken(containerId, tokenId, serviceName) {
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
    document.getElementById('init-worker-name').value = '';
    document.getElementById('init-worker-master').value = '';
    document.getElementById('init-worker-token').value = '';
    document.getElementById('settings-error').textContent = '';
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
    const master_url = document.getElementById('init-worker-master').value.trim().replace(/\/+$/, '');
    const token = document.getElementById('init-worker-token').value.trim();
    const errEl = document.getElementById('settings-error');
    errEl.textContent = '';
    if (!name || !master_url || !token) { errEl.textContent = 'All fields are required.'; return; }

    try {
        const address = `http://${window.location.hostname}:9000`;

        // Enroll with master
        const resp = await fetch(`${master_url}/api/nodes/enroll`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token, node_name: name, address }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
            throw new Error(err.detail || 'Enrollment failed');
        }
        const result = await resp.json();

        // Configure locally
        await api('POST', '/api/config/init-worker', {
            name,
            master_url,
            api_key: result.api_key,
            update_public_key: result.signing_public_key || null,
        });
        location.reload();
    } catch (e) {
        errEl.textContent = e.message;
    }
}

function copyWorkerKey(btn) {
    const key = document.getElementById('worker-key-value').textContent;
    navigator.clipboard.writeText(key).then(() => {
        const orig = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(() => btn.textContent = orig, 1500);
    });
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

async function refreshCfSection() {
    if (!isMaster) return;
    try {
        // Resolve tunnel_id from the nodes list (includes tunnel_id for all nodes)
        const node = nodes.find(n => n.node_id === selectedNodeId);
        currentTunnelId = node ? (node.tunnel_id || null) : null;

        // Show the section header
        document.getElementById('tunnel-section-header').style.display = '';
        // Show the active tab content
        const activeTab = document.querySelector('.tunnel-tab-content.active');
        if (activeTab) activeTab.style.display = '';

        let tunnelData;
        try {
            tunnelData = await api('GET', nodePath('/api/tunnel'));
        } catch (e) {
            tunnelData = { connected: false, connections: 0, detail: 'unreachable' };
        }
        renderCfStatus(tunnelData);
        await refreshCfServiceControl();

        // If this node has no tunnel configured, show empty state for routes
        if (!currentTunnelId) {
            renderCfRoutes([]);
            renderCfAccessApps(await api('GET', '/api/cf/access/apps'));
            return;
        }

        const [routes, accessApps] = await Promise.all([
            api('GET', `/api/cf/routes?tunnel_id=${currentTunnelId}`),
            api('GET', '/api/cf/access/apps'),
        ]);
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

function cfServiceEndpoint(kind, lines = null) {
    let base = `/api/cf/service/${kind}`;
    if (isMaster && selectedNodeId && selectedNodeId !== selfNodeId) {
        base = `/api/nodes/${selectedNodeId}/cf/service/${kind}`;
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

async function refreshCfServiceControl() {
    const errEl = document.getElementById('cf-service-error');
    const logsEl = document.getElementById('cf-service-logs');
    if (!errEl || !logsEl) return;

    errEl.textContent = '';
    try {
        const status = await api('GET', cfServiceEndpoint('status'));
        renderCfServiceStatus(status);
    } catch (e) {
        errEl.textContent = `Status unavailable: ${e.message}`;
    }

    try {
        const data = await api('GET', cfServiceEndpoint('logs', 80));
        logsEl.textContent = data.logs || 'No recent logs.';
    } catch (e) {
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

function renderCfAccessApps(apps) {
    const el = document.getElementById('cf-access-table');
    if (!apps || apps.length === 0) {
        el.innerHTML = '<div class="empty-state">No Access applications</div>';
        return;
    }
    el.innerHTML = `
        <div class="cf-table-header cf-table-4col">
            <span>Name</span>
            <span>Domain</span>
            <span>Policy</span>
            <span></span>
        </div>
        ${apps.map(a => {
            const policyName = (a.policies && a.policies[0]) ? a.policies[0].name : 'None';
            return `
            <div class="cf-table-row cf-table-4col">
                <span>${esc(a.name)}</span>
                <span><a href="https://${esc(a.domain)}" target="_blank">${esc(a.domain)}</a></span>
                <span>${esc(policyName)}</span>
                <span><button class="btn danger" onclick="removeCfAccessApp('${esc(a.domain)}')">Remove</button></span>
            </div>`;
        }).join('')}
    `;
}

async function loadCfPolicies() {
    try {
        cfPolicies = await api('GET', '/api/cf/access/policies');
        const select = document.getElementById('cf-access-policy');
        if (cfPolicies.length === 0) {
            select.innerHTML = '<option value="">No policies found</option>';
        } else {
            select.innerHTML = cfPolicies.map(p =>
                `<option value="${esc(p.id)}">${esc(p.name)}</option>`
            ).join('');
        }
    } catch (e) {
        console.error('Failed to load policies:', e);
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

async function removeCfAccessApp(hostname) {
    if (!confirm(`Delete Access app for ${hostname}?`)) return;
    try {
        await api('DELETE', `/api/cf/access/apps/${hostname}`);
        await refreshCfSection();
    } catch (e) {
        alert('Failed to delete Access app: ' + e.message);
    }
}

async function setupWorkerTunnel(nodeId, nodeName) {
    if (!confirm(`Create a Cloudflare tunnel for "${nodeName}" and push the token?`)) return;
    try {
        const result = await api('POST', `/api/nodes/${nodeId}/cf/setup`, {});
        alert(`Tunnel created for ${nodeName}.\nTunnel ID: ${result.tunnel_id}`);
        await loadSettingsView();
    } catch (e) {
        alert('Tunnel setup failed: ' + e.message);
    }
}

// ---- Deploy / update ----

async function loadDeployInfo() {
    try {
        const ver = await api('GET', '/api/node/version');
        document.getElementById('deploy-version').textContent = ver.summary || 'unknown';
    } catch (e) {
        document.getElementById('deploy-version').textContent = 'unknown';
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
            const msg = ok ? 'Updated, restarting' : (r.detail || 'Failed');
            return `<div class="master-worker-row"><span class="status-dot ${ok ? 'green' : 'red'}"></span><span class="master-worker-name">${esc(r.name)}</span><span class="master-worker-address" style="color:${color}">${msg}</span></div>`;
        }).join('');
    } catch (e) {
        resultsEl.innerHTML = `<div class="settings-desc" style="color:var(--red)">${esc(e.message)}</div>`;
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

async function refreshAll() {
    await Promise.all([refreshSystem(), refreshTunnel(), refreshServices()]);
    if (isMaster && !cfSectionLoaded) {
        cfSectionLoaded = true;
        await Promise.all([refreshCfSection(), loadCfPolicies()]);
    }
}

async function loadVersionTag() {
    try {
        const ver = await api('GET', '/api/node/version');
        const el = document.getElementById('version-tag');
        el.textContent = ver.summary || '';
        el.title = ver.branch ? `${ver.branch} @ ${ver.commit}` : '';
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
        refreshAll();
        refreshInterval = setInterval(refreshAll, 5000);
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
            refreshAll();
            refreshInterval = setInterval(refreshAll, 5000);
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
            refreshAll();
            refreshInterval = setInterval(refreshAll, 5000);
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
