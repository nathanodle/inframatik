"""Frontend regression tests for static/app.js.

These tests intentionally avoid npm dependencies. They execute app.js in a
Node VM with a small DOM/API stub so role-gated browser logic can be tested
without a browser.
"""

import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

EMPTY_SERVICES_COPY = (
    "No services registered yet. Add one to get started or use 'inframatik init' "
    "in the root directory of your repo."
)


def _run_node(script: str):
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            "Node harness failed\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def test_app_js_cloudflare_section_gating_by_role():
    _run_node(
        textwrap.dedent(
            r"""
            const fs = require('fs');
            const vm = require('vm');

            function makeElement(id) {
                return {
                    id,
                    style: {},
                    dataset: {},
                    value: '',
                    textContent: '',
                    innerHTML: '',
                    disabled: false,
                    selectedIndex: 0,
                    options: [],
                    classList: {
                        add() {},
                        remove() {},
                        contains() { return false; },
                    },
                    addEventListener() {},
                    querySelectorAll() { return []; },
                    querySelector() { return null; },
                };
            }

            const elements = new Map();
            const document = {
                cookie: '',
                addEventListener() {},
                createElement() {
                    const el = makeElement('created');
                    Object.defineProperty(el, 'textContent', {
                        get() { return this._textContent || ''; },
                        set(value) {
                            this._textContent = String(value ?? '');
                            this.innerHTML = this._textContent
                                .replace(/&/g, '&amp;')
                                .replace(/</g, '&lt;')
                                .replace(/>/g, '&gt;')
                                .replace(/"/g, '&quot;');
                        },
                    });
                    return el;
                },
                getElementById(id) {
                    if (!elements.has(id)) elements.set(id, makeElement(id));
                    return elements.get(id);
                },
                querySelectorAll() { return []; },
                querySelector() { return makeElement('query-result'); },
            };

            const context = {
                console,
                document,
                window: { location: { hostname: 'localhost' } },
                location: { protocol: 'http:', host: 'localhost' },
                WebSocket: function WebSocket() { return {}; },
                fetch: async () => { throw new Error('fetch should be stubbed through api'); },
                setTimeout,
                clearTimeout,
                setInterval: () => 1,
                clearInterval: () => {},
                scenario: null,
                calls: null,
            };
            context.globalThis = context;

            vm.createContext(context);
            vm.runInContext(fs.readFileSync('static/app.js', 'utf8'), context, {
                filename: 'static/app.js',
            });

            function assert(condition, message) {
                if (!condition) throw new Error(message);
            }

            async function runRole(role, config) {
                return await vm.runInContext(`
                    (async () => {
                        isMaster = false;
                        nodeRole = null;
                        selfNodeId = null;
                        selectedNodeId = null;
                        nodes = [];
                        currentTunnelId = null;
                        cfSectionLoaded = false;
                        scenario = {
                            role: ${JSON.stringify(role)},
                            config: ${JSON.stringify(config || {})},
                        };
                        calls = {
                            api: [],
                            renderCfStatus: 0,
                            refreshCfServiceControl: 0,
                            renderCfRoutes: 0,
                            loadCfPolicies: 0,
                            renderCfAccessApps: 0,
                        };

                        api = async function(method, path) {
                            calls.api.push([method, path]);
                            if (path === '/api/node/info') {
                                return {
                                    role: scenario.role,
                                    node_id: 'self-node',
                                    node_name: 'Self Node',
                                };
                            }
                            if (path === '/api/config') return scenario.config;
                            if (path === '/api/nodes') {
                                return [{
                                    node_id: 'self-node',
                                    node_name: 'Self Node',
                                    status: 'online',
                                    is_self: true,
                                    tunnel_id: 'tid-master',
                                }];
                            }
                            if (path === '/api/tunnel') {
                                return { connected: true, connections: 1, routes: [] };
                            }
                            if (path.startsWith('/api/cf/routes?')) return [];
                            if (path === '/api/cf/access/apps') return [];
                            if (path === '/api/cf/access/policies') return [];
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };

                        refreshSystem = async function() {};
                        refreshTunnel = async function() {};
                        refreshServices = async function() {};
                        refreshCfServiceControl = async function() {
                            calls.refreshCfServiceControl += 1;
                        };
                        renderCfRoutes = function() {
                            calls.renderCfRoutes += 1;
                        };
                        renderCfStatus = function() { calls.renderCfStatus += 1; };
                        loadCfPolicies = function() { calls.loadCfPolicies += 1; };
                        renderCfAccessApps = function() { calls.renderCfAccessApps += 1; };
                        renderSidebar = function() {};
                        connectWs = function() {};

                        const configured = await initCluster();
                        await refreshAll();
                        return {
                            configured,
                            isMaster,
                            nodeRole,
                            selfNodeId,
                            selectedNodeId,
                            currentTunnelId,
                            cfSectionLoaded,
                            shouldShowLocalCfSection: shouldShowLocalCfSection(),
                            calls,
                        };
                    })()
                `, context);
            }

            (async () => {
                const standalone = await runRole('standalone', { tunnel_id: 'tid-self' });
                assert(standalone.configured === true, 'standalone should initialize');
                assert(standalone.shouldShowLocalCfSection === true, 'standalone should show local CF section');
                assert(standalone.selfNodeId === 'self-node', 'standalone self node id should be set');
                assert(standalone.selectedNodeId === 'self-node', 'standalone selected node id should be set');
                assert(standalone.currentTunnelId === 'tid-self', 'standalone tunnel id should load from config');
                assert(standalone.cfSectionLoaded === true, 'standalone refresh loop should load CF section');
                assert(
                    standalone.calls.api.some((call) => call[1] === '/api/tunnel'),
                    'standalone should load local tunnel status'
                );
                assert(
                    standalone.calls.api.some((call) => call[1] === '/api/cf/access/apps'),
                    'standalone should load access apps'
                );
                assert(standalone.calls.renderCfRoutes === 1, 'standalone should render tunnel routes');

                const worker = await runRole('worker', { tunnel_id: 'tid-worker' });
                assert(worker.configured === true, 'worker should initialize');
                assert(worker.shouldShowLocalCfSection === true, 'worker should show local CF section');
                assert(worker.currentTunnelId === 'tid-worker', 'worker tunnel id should load from config');
                assert(worker.cfSectionLoaded === true, 'worker refresh loop should load CF section');
                assert(
                    worker.calls.api.some((call) => call[1] === '/api/cf/access/apps'),
                    'worker should load local Cloudflare APIs'
                );

                const servicesHtml = vm.runInContext(`
                    renderServices([]);
                    document.getElementById('services-list').innerHTML;
                `, context);
                assert(
                    servicesHtml.includes("inframatik init"),
                    'empty services state should mention inframatik init'
                );

                const cfSetupHtml = vm.runInContext(`
                    renderCfSetup('cf-setup-container', false);
                    document.getElementById('cf-setup-container').innerHTML;
                `, context);
                assert(
                    cfSetupHtml.includes('Manage Account') &&
                    cfSetupHtml.includes('Account API Tokens') &&
                    cfSetupHtml.includes('Access: Organizations, Identity Providers, and Groups') &&
                    cfSetupHtml.includes('Argo Tunnel (Legacy)') &&
                    cfSetupHtml.includes('DNS (Read/Edit)') &&
                    cfSetupHtml.includes('Zones (Read)'),
                    'Cloudflare wizard should describe required dashboard path and token policies'
                );
                assert(
                    !cfSetupHtml.includes('https://dash.cloudflare.com'),
                    'Cloudflare wizard should not include dashboard token link'
                );

                const workersHtml = vm.runInContext(`
                    renderMasterWorkers({
                        'cfg-kitt': {
                            name: 'kitt',
                            address: 'http://192.168.166.150:9000',
                            tunnel_id: 'tid-kitt',
                        },
                    }, [{
                        node_id: 'real-kitt',
                        config_node_id: 'cfg-kitt',
                        node_name: 'kitt',
                        address: 'http://192.168.166.150:9000',
                        status: 'online',
                    }]);
                    document.getElementById('master-workers-list').innerHTML;
                `, context);
                assert(
                    workersHtml.includes('worker-status-label green') &&
                    workersHtml.includes('Online'),
                    'settings workers should use live node status by config_node_id'
                );

                const serviceTokenHtml = vm.runInContext(`
                    renderServiceTokens('service-token-panel', [{
                        service: 'uderp',
                        token_id: 'tok-1',
                        capability: 'deploy',
                        created_at: 1710000000,
                        expires_at: 1711000000,
                    }], [
                        { name: 'disco', status: 'active' },
                        { name: 'uderp', status: 'inactive' },
                    ]);
                    document.getElementById('service-token-panel').innerHTML;
                `, context);
                assert(
                    serviceTokenHtml.includes('disco') &&
                    serviceTokenHtml.includes('No token generated') &&
                    !serviceTokenHtml.includes('Generate Missing Tokens') &&
                    serviceTokenHtml.includes('uderp') &&
                    serviceTokenHtml.includes('deploy') &&
                    serviceTokenHtml.includes('inactive'),
                    'service token settings should show every service, not only existing tokens'
                );

                const cleanedJobHtml = vm.runInContext(`
                    renderModelJobs([{
                        id: 'mdl-cleaned',
                        kind: 'download',
                        artifact_id: 'cleaned-model',
                        snapshot: 'v1',
                        source: { type: 'url', url: 'https://example.invalid/model.gguf' },
                        state: 'failed',
                        progress: 40,
                        staging_path: '/tmp/inframatik/staging/mdl-cleaned',
                        cleanup: {
                            staging_removed: true,
                            staging_removed_at: 1710000000,
                            staging_removed_reason: 'manual',
                        },
                    }]);
                    document.getElementById('model-jobs-list').innerHTML;
                `, context);
                assert(
                    cleanedJobHtml.includes('Staging cleaned') &&
                    !cleanedJobHtml.includes('Clean staging'),
                    'cleaned model jobs should show cleanup state without a stale cleanup action'
                );

                const startupPanelHtml = vm.runInContext(`
                    renderProfileOperationPanel({
                        id: 'op-start',
                        kind: 'profile_start',
                        state: 'running',
                        profile_id: 'qwen',
                        current_step: 'waiting_ready',
                        progress: 72,
                        steps: [{ name: 'waiting_ready', state: 'running' }],
                        runtime_status: {
                            phase: 'waiting_ready',
                            instance_index: 0,
                            unit: 'infra-llm-qwen.service',
                            host: '127.0.0.1',
                            port: 10000,
                            systemd_state: 'active',
                            tcp_reachable: false,
                            restart_count: 1,
                            elapsed_seconds: 42,
                            timeout_seconds: 600,
                            wait_position: 1,
                            wait_total: 1,
                        },
                    });
                `, context);
                assert(
                    startupPanelHtml.includes('Systemd') &&
                    startupPanelHtml.includes('active') &&
                    startupPanelHtml.includes('TCP') &&
                    startupPanelHtml.includes('waiting') &&
                    startupPanelHtml.includes('Restarts') &&
                    startupPanelHtml.includes('42s / 10m'),
                    'active inference operation panel should show live startup readiness facts'
                );

                const bundleHtml = vm.runInContext(`
                    renderClientBundle({
                        id: 'default',
                        name: 'Default connection',
                        target: { type: 'profile' },
                        exposure_mode: 'cloudflare',
                        base_url: 'https://llm.example.com/v1',
                        model: 'qwen',
                        headers: {
                            Authorization: 'Bearer <engine_api_key>',
                            'CF-Access-Client-Id': 'client.access',
                            'CF-Access-Client-Secret': '<shown_once>',
                        },
                        secret_state: { missing_secret_actions: [] },
                        examples: {
                            curl: 'curl https://llm.example.com/v1/models',
                            python_openai: 'from openai import OpenAI',
                            litellm: 'model_list:',
                        },
                    }, {
                        engine_api_key: 'llm_secret',
                        client_secret: 'cf_secret',
                    });
                `, context);
                assert(
                    bundleHtml.includes('data-copy="https://llm.example.com/v1"') &&
                    bundleHtml.includes('data-copy="Bearer &lt;engine_api_key&gt;"') &&
                    bundleHtml.includes('data-copy="client.access"') &&
                    bundleHtml.includes('data-copy="llm_secret"') &&
                    bundleHtml.includes('data-copy="cf_secret"') &&
                    bundleHtml.includes('copyText(this.dataset.copy, this)') &&
                    bundleHtml.includes('client-example-card'),
                    'client bundles should provide copy controls for endpoints, headers, one-time secrets, and examples'
                );

                const replicatedConnectHtml = vm.runInContext(`
                    inferenceProfilesData = [{
                        id: 'qwen-repl',
                        display_name: 'Qwen Replicas',
                        exposure: { mode: 'local' },
                        cloudflare: {},
                    }];
                    renderProfileConnect('qwen-repl', {
                        default: {
                            id: 'default',
                            requires_instance: true,
                            message: 'Replicated profile requires explicit instance target',
                        },
                        instance_bundles: [{
                            id: 'default',
                            target: { type: 'instance', instance_index: 0 },
                            exposure_mode: 'local',
                            base_url: 'http://127.0.0.1:10000/v1',
                            instance: { index: 0, gpu_ids: [0], unit: 'infra-llm-qwen@0.service', state: 'running' },
                        }, {
                            id: 'default',
                            target: { type: 'instance', instance_index: 1 },
                            exposure_mode: 'local',
                            base_url: 'http://127.0.0.1:10001/v1',
                            instance: { index: 1, gpu_ids: [1], unit: 'infra-llm-qwen@1.service', state: 'running' },
                        }],
                    });
                    document.getElementById('profile-detail-qwen-repl').innerHTML;
                `, context);
                assert(
                    replicatedConnectHtml.includes('Instance Endpoints') &&
                    replicatedConnectHtml.includes('Instance 0') &&
                    replicatedConnectHtml.includes('Instance 1') &&
                    replicatedConnectHtml.includes('data-copy="http://127.0.0.1:10000/v1"') &&
                    replicatedConnectHtml.includes('data-copy="http://127.0.0.1:10001/v1"'),
                    'replicated Connect view should render copyable per-instance endpoint options'
                );

                const restartButtonDisplay = vm.runInContext(`
                    fillProfileForm({
                        id: 'qwen',
                        display_name: 'Qwen',
                        engine: 'vllm',
                        engine_launcher_id: 'vllm-main',
                        model: { artifact_id: 'qwen', snapshot: 'v1' },
                        common: {},
                        deployment: {},
                        advanced: {},
                        engine_config: {},
                        exposure: {},
                        instances: [{ port: 10000 }],
                        state: 'running',
                    });
                    document.getElementById('profile-save-restart-btn').style.display;
                `, context);
                assert(
                    restartButtonDisplay === '',
                    'profile editor should show Save & Restart while editing a running profile'
                );

                const saveRestartCalls = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        document.getElementById('profile-edit-id').value = 'qwen';
                        buildProfileDraft = function() {
                            return { engine_launcher_id: 'vllm-main', model: { artifact_id: 'qwen' } };
                        };
                        api = async function(method, path, body) {
                            calls.push([method, path, body && body.engine_launcher_id]);
                            return { plan: { valid_for_save: true }, profile: { id: 'qwen' } };
                        };
                        resetProfileForm = function() {
                            calls.push(['reset']);
                            document.getElementById('profile-edit-id').value = '';
                        };
                        refreshInferenceProfiles = async function() {
                            calls.push(['refresh']);
                        };
                        renderProfilePreview = function(plan) {
                            calls.push(['preview', Boolean(plan)]);
                        };
                        runProfileAction = async function(profileId, action) {
                            calls.push(['action', profileId, action]);
                        };
                        await saveInferenceProfile({ restart: true });
                        return calls;
                    })()
                `, context);
                assert(
                    saveRestartCalls.some(call => call[0] === 'PUT' && call[1] === '/api/inference/profiles/qwen') &&
                    saveRestartCalls.some(call => call[0] === 'action' && call[1] === 'qwen' && call[2] === 'restart'),
                    'Save & Restart should save the edited profile and then queue a restart operation'
                );
            })().catch((error) => {
                console.error(error.stack || error.message);
                process.exit(1);
            });
            """
        )
    )


def test_app_js_system_render_limits_hidden_tab_dom_writes():
    _run_node(
        textwrap.dedent(
            r"""
            const fs = require('fs');
            const vm = require('vm');

            function makeElement(id) {
                const el = {
                    id,
                    style: {},
                    dataset: {},
                    value: '',
                    textContent: '',
                    disabled: false,
                    selectedIndex: 0,
                    options: [],
                    className: '',
                    classList: {
                        add() {},
                        remove() {},
                        contains() { return false; },
                    },
                    addEventListener() {},
                    querySelectorAll() { return []; },
                    querySelector() { return null; },
                    htmlWrites: 0,
                };
                let html = '';
                Object.defineProperty(el, 'innerHTML', {
                    get() { return html; },
                    set(value) {
                        html = String(value ?? '');
                        el.htmlWrites += 1;
                    },
                });
                return el;
            }

            const elements = new Map();
            const state = { activeTab: 'overview' };
            const document = {
                cookie: '',
                addEventListener() {},
                createElement() {
                    const el = makeElement('created');
                    Object.defineProperty(el, 'textContent', {
                        get() { return this._textContent || ''; },
                        set(value) {
                            this._textContent = String(value ?? '');
                            this.innerHTML = this._textContent
                                .replace(/&/g, '&amp;')
                                .replace(/</g, '&lt;')
                                .replace(/>/g, '&gt;')
                                .replace(/"/g, '&quot;');
                        },
                    });
                    return el;
                },
                getElementById(id) {
                    if (!elements.has(id)) elements.set(id, makeElement(id));
                    return elements.get(id);
                },
                querySelectorAll() { return []; },
                querySelector(selector) {
                    if (selector === '.tab.active') {
                        return { dataset: { tab: state.activeTab } };
                    }
                    return makeElement('query-result');
                },
            };

            const context = {
                console,
                document,
                window: { location: { hostname: 'localhost' } },
                location: { protocol: 'http:', host: 'localhost' },
                WebSocket: function WebSocket() { return {}; },
                fetch: async () => { throw new Error('fetch should not run'); },
                setTimeout,
                clearTimeout,
                setInterval: () => 1,
                clearInterval: () => {},
                calls: null,
            };
            context.globalThis = context;

            vm.createContext(context);
            vm.runInContext(fs.readFileSync('static/app.js', 'utf8'), context, {
                filename: 'static/app.js',
            });

            function assert(condition, message) {
                if (!condition) throw new Error(message);
            }

            context.state = state;
            context.assert = assert;
            context.sample = {
                uptime: '1h',
                host: { distro: 'Test Linux', cpu_model: 'Test CPU' },
                cpu: { percent: 10, count: 2, freq_mhz: 2400, per_cpu: [5, 15] },
                memory: { total: 1000, used: 500, percent: 50 },
                disks: [{ mount: '/', device: '/dev/sda1', fstype: 'ext4', used: 400, total: 1000, percent: 40 }],
                network: { bytes_sent: 100, bytes_recv: 200, interfaces: [{ name: 'eth0', ip: '10.0.0.2', speed_mbps: 1000, bytes_sent: 100, bytes_recv: 200 }] },
                load: { '1min': 0.1, '5min': 0.2, '15min': 0.3 },
                temps: { cpu: 42 },
                gpus: [{ index: 0, name: 'GPU', util_percent: 20, mem_used_mb: 100, mem_total_mb: 1000, temp_c: 50, power_w: 75 }],
                processes: [{ pid: 1, name: 'proc', cpu: 1.2, mem: 3.4 }],
            };

            vm.runInContext(`
                calls = { gpus: 0, processes: 0, network: 0, storage: 0 };
                renderGpus = function() { calls.gpus += 1; };
                renderProcesses = function() { calls.processes += 1; };
                renderNetInterfaces = function() { calls.network += 1; };
                renderStorage = function() { calls.storage += 1; };

                state.activeTab = 'overview';
                renderSystem(sample);
                assert(calls.gpus === 0, 'overview render should not repaint GPU tab');
                assert(calls.processes === 0, 'overview render should not repaint process tab');
                assert(calls.network === 0, 'overview render should not repaint network tab');
                assert(calls.storage === 0, 'overview render should not repaint storage tab');

                state.activeTab = 'processes';
                renderSystem(sample);
                assert(calls.processes === 1, 'active process tab should repaint');
                assert(calls.gpus === 0 && calls.network === 0 && calls.storage === 0, 'inactive system tabs should stay untouched');

                renderServices([]);
                renderServices([]);
                assert(
                    document.getElementById('services-list').htmlWrites === 1,
                    'unchanged service markup should not rewrite the service list'
                );
            `, context);
            """
        )
    )


def test_app_js_node_selection_starts_priority_refresh_immediately():
    _run_node(
        textwrap.dedent(
            r"""
            const fs = require('fs');
            const vm = require('vm');

            function makeElement(id) {
                return {
                    id,
                    style: {},
                    dataset: {},
                    value: '',
                    textContent: '',
                    innerHTML: '',
                    disabled: false,
                    selectedIndex: 0,
                    options: [],
                    className: '',
                    classList: {
                        add() {},
                        remove() {},
                        contains() { return false; },
                    },
                    addEventListener() {},
                    querySelectorAll() { return []; },
                    querySelector() { return null; },
                };
            }

            const elements = new Map();
            const document = {
                cookie: '',
                addEventListener() {},
                createElement() {
                    const el = makeElement('created');
                    Object.defineProperty(el, 'textContent', {
                        get() { return this._textContent || ''; },
                        set(value) {
                            this._textContent = String(value ?? '');
                            this.innerHTML = this._textContent
                                .replace(/&/g, '&amp;')
                                .replace(/</g, '&lt;')
                                .replace(/>/g, '&gt;')
                                .replace(/"/g, '&quot;');
                        },
                    });
                    return el;
                },
                getElementById(id) {
                    if (!elements.has(id)) elements.set(id, makeElement(id));
                    return elements.get(id);
                },
                querySelectorAll() { return []; },
                querySelector() { return { dataset: { tab: 'overview' } }; },
            };

            const context = {
                console,
                document,
                window: { location: { hostname: 'localhost' } },
                location: { protocol: 'http:', host: 'localhost' },
                WebSocket: function WebSocket() { return {}; },
                fetch: async () => { throw new Error('fetch should not run'); },
                setTimeout,
                clearTimeout,
                setInterval: () => 1,
                clearInterval: () => {},
            };
            context.globalThis = context;

            vm.createContext(context);
            vm.runInContext(fs.readFileSync('static/app.js', 'utf8'), context, {
                filename: 'static/app.js',
            });

            vm.runInContext(`
                (async () => {
                    const calls = [];
                    isMaster = true;
                    nodeRole = 'master';
                    selfNodeId = 'master';
                    selectedNodeId = 'node-a';
                    currentAppView = 'main';
                    nodes = [
                        { node_id: 'node-a', node_name: 'Node A', status: 'online' },
                        { node_id: 'node-b', node_name: 'Node B', status: 'online' },
                    ];
                    renderSystem = function() {};
                    renderTunnel = function() {};
                    renderServices = function() {};
                    refreshCfSection = async function(context) {
                        calls.push('cf:' + context.nodeId);
                    };
                    api = async function(_method, path) {
                        calls.push(path);
                        if (path.includes('/node-a/')) {
                            return new Promise(() => {});
                        }
                        if (path === '/api/nodes/node-b/snapshot') {
                            return { system: {}, tunnel: {}, services: [] };
                        }
                        return {};
                    };

                    refreshAll();
                    await new Promise(resolve => setTimeout(resolve, 0));
                    selectNode('node-b');
                    await new Promise(resolve => setTimeout(resolve, 0));

                    if (!calls.includes('/api/nodes/node-b/snapshot')) {
                        throw new Error('node click should start snapshot refresh immediately for selected node');
                    }
                    if (selectedNodeId !== 'node-b') {
                        throw new Error('selected node should update synchronously');
                    }
                })().catch((error) => {
                    console.error(error.stack || error.message);
                    process.exit(1);
                });
            `, context);
            """
        )
    )


def test_static_index_contains_setup_guidance_and_empty_state_copy():
    index_html = (ROOT / "static" / "index.html").read_text()

    assert EMPTY_SERVICES_COPY in index_html
    assert "Manage Account" in index_html
    assert "Account API Tokens" in index_html
    assert "Access: Organizations, Identity Providers, and Groups" in index_html
    assert "Argo Tunnel (Legacy)" in index_html
    assert "DNS (Read/Edit)" in index_html
    assert "Zones (Read)" in index_html
    assert "dash.cloudflare.com/profile/api-tokens" not in index_html
    assert 'id="settings-view"' in index_html
    assert "Update Master from Git" in index_html
    assert "settings-btn" not in index_html
    assert "Back to Main" not in index_html
    assert "worker-key-value" not in index_html


def test_static_inference_model_ui_assets_present():
    app_js = (ROOT / "static" / "app.js").read_text()
    index_html = (ROOT / "static" / "index.html").read_text()
    style_css = (ROOT / "static" / "style.css").read_text()

    assert 'id="inference-view"' in index_html
    assert 'id="inference-view-tab"' in index_html
    assert 'id="model-import-path"' in index_html
    assert 'id="model-url-url"' in index_html
    assert 'id="model-hf-repo"' in index_html
    assert 'data-inference-tab="profiles"' in index_html
    assert 'id="profile-id"' in index_html
    assert 'id="profile-launcher"' in index_html
    assert 'id="profile-model"' in index_html
    assert 'id="profile-common-json"' in index_html
    assert 'id="profile-engine-json"' in index_html
    assert 'id="profile-save-restart-btn"' in index_html
    assert "Save & Restart" in index_html
    assert 'id="profile-gpu-hints"' in index_html
    assert 'id="profile-kv-cache-dtype"' in index_html
    assert 'id="profile-gpu-memory-utilization"' in index_html
    assert 'id="profile-expert-parallel"' in index_html
    assert 'id="profile-max-concurrent"' in index_html
    assert 'id="profile-reasoning-parser"' in index_html
    assert 'id="profile-vllm-all2all-backend"' in index_html
    assert 'id="profile-sglang-moe-a2a-backend"' in index_html
    assert 'id="profile-llama-tensor-split"' in index_html
    assert 'id="inference-profiles-list"' in index_html
    assert 'id="inference-operations-list"' in index_html
    assert 'data-inference-tab="launchers"' in index_html
    assert 'id="launcher-executable"' in index_html
    assert 'id="launcher-arg-rows"' in index_html
    assert 'id="launcher-env-rows"' in index_html
    assert 'id="model-jobs-list"' in index_html
    assert 'id="model-store-root-input"' in index_html
    assert "/api/models" in app_js
    assert "/api/inference/profiles" in app_js
    assert "/api/inference/launchers" in app_js
    assert "/api/inference/operations" in app_js
    assert "client-bundles" in app_js
    assert "cloudflare/service-tokens" in app_js
    assert "cloudflare/exposure" in app_js
    assert "loadProfileDetails" in app_js
    assert "runInstanceAction" in app_js
    assert "runProfileTest" in app_js
    assert "exportInferenceProfile" in app_js
    assert "profileCanSaveRestart" in app_js
    assert "saveInferenceProfile({restart:true})" in index_html
    assert "/api/inference/profiles/${encodeURIComponent(profileId)}/export" in app_js
    assert "rotateProfileApiKey" in app_js
    assert "loadProfileConnect" in app_js
    assert "copyButton" in app_js
    assert "data-copy" in app_js
    assert "renderInstanceBundleOptions" in app_js
    assert "profile-cf-hostname-" in app_js
    assert "prompt('Cloudflare hostname')" not in app_js
    assert "setProfileDetail(profileId, html, 'connect')" in app_js
    assert "activeInferenceTab = 'profiles'" in app_js
    assert "refreshInferenceProfiles" in app_js
    assert "inference_operation" in app_js
    assert "handleInferenceOperationEvent" in app_js
    assert "handleInferenceOperationEvent(msg.operation, msg)" in app_js
    assert "websocketEventMatchesSelectedNode" in app_js
    assert "selectedInferenceNodeIds" in app_js
    assert "renderProfileOperationPanel" in app_js
    assert "hydrateInferenceFailureDiagnostics" in app_js
    assert "hydrateVisibleInferenceFailures" in app_js
    assert "operationWithHydratedLogs" in app_js
    assert "operationRuntimeStatus" in app_js
    assert "runtime_status" in app_js
    assert "model_job" in app_js
    assert "handleModelJobEvent" in app_js
    assert "handleModelJobEvent(msg.job, msg)" in app_js
    assert "profileJsonValue" in app_js
    assert "renderInferenceGpuHints" in app_js
    assert "profileConfigChips" in app_js
    assert "structuredCommonConfig" in app_js
    assert "structuredEngineConfig" in app_js
    assert "draft = buildProfileDraft()" in app_js
    assert "setInferenceError(e.message)" in app_js
    assert "profile-speculative-model" in index_html
    assert "profile-log-level" in index_html
    assert "profile-vllm-distributed-executor" in index_html
    assert "profile-vllm-kv-offloading-size" in index_html
    assert "profile-vllm-compilation-config" in index_html
    assert "profile-vllm-ep-weight-filter" in index_html
    assert "profile-sglang-sampling-defaults" in index_html
    assert "profile-sglang-cuda-graph-config" in index_html
    assert "profile-sglang-hicache" in index_html
    assert "distributed_executor_backend" in app_js
    assert "kv_offloading_size" in app_js
    assert "sampling_defaults" in app_js
    assert "mergeEngineConfig" in app_js
    assert "renderProfileEngineFields" in app_js
    assert "removeProfileCloudflare" in app_js
    assert "submitModelImport" in app_js
    assert "submitModelUrlDownload" in app_js
    assert "submitModelHfDownload" in app_js
    assert "submitLauncherForm" in app_js
    assert "validateLauncher" in app_js
    assert "validate?runtime=true" in app_js
    assert "renderLauncherValidation" in app_js
    assert "cleanModelJobStaging" in app_js
    assert "modelJobStagingCleaned" in app_js
    assert ".model-job-badges" in style_css
    assert "force_stopped_references=true" in app_js
    assert "path.startsWith('/api/models')" in app_js
    assert "path.startsWith('/api/inference')" in app_js
    assert ".model-table-row" in style_css
    assert ".model-job-row" in style_css
    assert ".launcher-card" in style_css
    assert ".launcher-validation-panel" in style_css
    assert ".launcher-validation-output" in style_css
    assert ".profile-card" in style_css
    assert ".profile-detail-panel" in style_css
    assert ".profile-operation-panel" in style_css
    assert ".profile-operation-steps" in style_css
    assert ".profile-operation-facts" in style_css
    assert ".profile-instance-row" in style_css
    assert ".profile-test-panel" in style_css
    assert ".profile-config-chips" in style_css
    assert ".profile-gpu-chip" in style_css
    assert ".profile-engine-details" in style_css
    assert ".form-check-grid" in style_css
    assert ".profile-preview-panel" in style_css
    assert ".profile-connect-panel" in style_css
    assert ".connect-action-grid" in style_css
    assert ".connect-inline-form" in style_css
    assert ".connect-mini-facts" in style_css
    assert ".connect-copy-row" in style_css
    assert ".copy-btn" in style_css
    assert ".client-example-card" in style_css
    assert ".instance-bundle-row" in style_css
    assert ".profile-one-time-secret" in style_css
    assert ".profile-token-row" in style_css


def test_worker_enrollment_ui_uses_same_origin_backend_endpoint():
    app_js = (ROOT / "static" / "app.js").read_text()
    index_html = (ROOT / "static" / "index.html").read_text()

    assert "/api/config/enroll-worker" in app_js
    assert "/api/nodes/enroll" not in app_js
    assert "worker-enroll" in app_js
    assert "/api/cf/workers/setup" in app_js
    assert "Sync Cloudflare to Workers" in app_js
    assert "skip_cf" in app_js
    assert "Use Cloudflare" in index_html
    assert "Local Only" in index_html
    assert "setup-worker-master-host" in index_html
    assert "setup-worker-master-port" in index_html
    assert "init-worker-master-host" in index_html
    assert "init-worker-master-port" in index_html
    assert 'value="9000"' in index_html
    assert 'id="setup-worker-master"' not in index_html
    assert 'id="init-worker-master"' not in index_html
    assert "http://192.168.1.10:9000" not in index_html
    assert "installSourceMasterUrl" in app_js
    assert "install_source_master_url" in app_js
    assert "splitMasterUrlForFields" in app_js
    assert "getWorkerMasterUrl" in app_js
    assert "defaultWorkerNodeName" in app_js
    assert "setup-worker-name').value = defaultWorkerNodeName()" in app_js
    assert "init-worker-name').value = defaultWorkerNodeName()" in app_js
    assert "setup-worker-progress" in index_html
    assert "init-worker-progress" in index_html
    assert "worker-cf-sync-progress" in index_html


if __name__ == "__main__":
    print("Running frontend app.js tests...\n")
    test_app_js_cloudflare_section_gating_by_role()
    test_app_js_system_render_limits_hidden_tab_dom_writes()
    test_app_js_node_selection_starts_priority_refresh_immediately()
    test_static_index_contains_setup_guidance_and_empty_state_copy()
    test_static_inference_model_ui_assets_present()
    test_worker_enrollment_ui_uses_same_origin_backend_endpoint()
    print("ok")
