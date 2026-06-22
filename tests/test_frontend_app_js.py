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
                confirm: () => true,
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

                const activeJobHtml = vm.runInContext(`
                    renderModelJobs([{
                        id: 'mdl-active',
                        kind: 'download',
                        artifact_id: 'active-model',
                        snapshot: 'v1',
                        source: { type: 'url', url: 'https://example.invalid/model.gguf' },
                        state: 'running',
                        progress: 50,
                        downloaded_bytes: 5 * 1024 * 1024,
                        total_bytes: 10 * 1024 * 1024,
                        started_at: (Date.now() / 1000) - 10,
                        current_file: 'model.gguf',
                    }]);
                    document.getElementById('model-jobs-list').innerHTML;
                `, context);
                assert(
                    activeJobHtml.includes('model-job-metrics') &&
                    activeJobHtml.includes('elapsed') &&
                    activeJobHtml.includes('/s') &&
                    activeJobHtml.includes('ETA'),
                    'active model jobs should show transfer rate, elapsed time, and ETA'
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
                    startupPanelHtml.includes('42s / 10m') &&
                    startupPanelHtml.includes('loadOperationLogs(&quot;qwen&quot;, 0,') &&
                    startupPanelHtml.includes('profile-operation-log-output-panel-op-start'),
                    'active inference operation panel should show live startup readiness facts and inline targeted logs'
                );

                const startupOperationListHtml = vm.runInContext(`
                    renderInferenceOperations([{
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
                    }]);
                    document.getElementById('inference-operations-list').innerHTML;
                `, context);
                assert(
                    startupOperationListHtml.includes('profile-operation-panel') &&
                    startupOperationListHtml.includes('infra-llm-qwen.service') &&
                    startupOperationListHtml.includes('TCP') &&
                    startupOperationListHtml.includes('waiting') &&
                    startupOperationListHtml.includes('42s / 10m'),
                    'operations tab should reuse the rich operation panel with live readiness facts'
                );

                const queuedOperationHtml = vm.runInContext(`
                    renderProfileOperationPanel({
                        id: 'op-cancel',
                        kind: 'profile_start',
                        state: 'queued',
                        profile_id: 'qwen',
                        current_step: 'queued',
                        progress: 0,
                        steps: [{ name: 'validate', state: 'pending' }],
                    });
                `, context);
                assert(
                    queuedOperationHtml.includes('cancelInferenceOperation') &&
                    queuedOperationHtml.includes('Cancel') &&
                    queuedOperationHtml.includes('loadOperationLogs(&quot;qwen&quot;, null,') &&
                    queuedOperationHtml.includes('profile-operation-log-output-panel-op-cancel'),
                    'queued profile operations should render cancel and inline profile log actions'
                );

                const queuedOperationsListHtml = vm.runInContext(`
                    renderInferenceOperations([{
                        id: 'op-cancel',
                        kind: 'profile_start',
                        state: 'queued',
                        profile_id: 'qwen',
                        current_step: 'queued',
                        progress: 0,
                    }]);
                    document.getElementById('inference-operations-list').innerHTML;
                `, context);
                assert(
                    queuedOperationsListHtml.includes('cancelInferenceOperation') &&
                    queuedOperationsListHtml.includes('Cancel'),
                    'queued operations in the Jobs tab should render a cancel action'
                );

                const operationLogResult = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        selectedNodeId = 'self-node';
                        isMaster = false;
                        const targetId = 'profile-operation-log-output-panel-op-start';
                        const target = document.getElementById(targetId);
                        api = async function(method, path) {
                            calls.push([method, path]);
                            if (method === 'GET' && path === '/api/inference/profiles/qwen/instances/0/logs?lines=180') {
                                return { logs: 'server ready on :10000' };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        await loadOperationLogs('qwen', 0, targetId);
                        const reRendered = renderProfileOperationPanel({
                            id: 'op-start',
                            kind: 'profile_start',
                            state: 'running',
                            profile_id: 'qwen',
                            current_step: 'waiting_ready',
                            progress: 75,
                            runtime_status: { instance_index: 0 },
                        });
                        return { calls, html: target.innerHTML, reRendered };
                    })()
                `, context);
                assert(
                    operationLogResult.calls.some(call => call[0] === 'GET' && call[1] === '/api/inference/profiles/qwen/instances/0/logs?lines=180') &&
                    operationLogResult.html.includes('instance 0 logs') &&
                    operationLogResult.html.includes('server ready on :10000') &&
                    operationLogResult.reRendered.includes('server ready on :10000'),
                    'operation log action should load and preserve instance logs in the clicked operation panel'
                );

                const cancelCalls = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        selectedNodeId = 'self-node';
                        isMaster = false;
                        currentAppView = 'inference';
                        inferenceProfilesData = [];
                        refreshInferenceProfiles = async function() { calls.push(['refreshProfiles']); };
                        api = async function(method, path) {
                            calls.push([method, path]);
                            if (method === 'POST' && path === '/api/inference/operations/op-cancel/cancel') {
                                return { id: 'op-cancel', kind: 'profile_start', state: 'canceled', profile_id: 'qwen', progress: 100 };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        await cancelInferenceOperation('op-cancel');
                        return { calls, state: inferenceOperationsData[0] && inferenceOperationsData[0].state };
                    })()
                `, context);
                assert(
                    cancelCalls.calls.some(call => call[0] === 'POST' && call[1] === '/api/inference/operations/op-cancel/cancel') &&
                    cancelCalls.calls.some(call => call[0] === 'refreshProfiles') &&
                    cancelCalls.state === 'canceled',
                    'cancel operation should call the cancel endpoint, merge the canceled operation, and refresh profiles'
                );

                const conflictResult = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        selectedNodeId = 'self-node';
                        isMaster = false;
                        wsConnected = true;
                        currentAppView = 'inference';
                        activeInferenceTab = 'profiles';
                        inferenceProfilesData = [{ id: 'qwen', display_name: 'Qwen', state: 'running', instances: [] }];
                        renderInferenceProfiles = function() { calls.push(['renderProfiles']); };
                        renderInferenceOperations = function(operations) { calls.push(['renderOperations', operations[0] && operations[0].id]); };
                        setInferenceError = function(message) { calls.push(['error', message]); };
                        api = async function(method, path) {
                            calls.push([method, path]);
                            if (method === 'POST' && path === '/api/inference/profiles/qwen/start') {
                                const err = new Error('active operation');
                                err.status = 409;
                                err.detail = {
                                    message: 'An inference operation is already active for this profile',
                                    active_operation_id: 'op-live',
                                    kind: 'profile_restart',
                                };
                                throw err;
                            }
                            if (method === 'GET' && path === '/api/inference/operations/op-live') {
                                return { id: 'op-live', kind: 'profile_restart', state: 'running', profile_id: 'qwen', current_step: 'waiting_ready', progress: 70 };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        await runProfileAction('qwen', 'start');
                        return { calls, operation: inferenceOperationsData.find(item => item.id === 'op-live') };
                    })()
                `, context);
                assert(
                    conflictResult.calls.some(call => call[0] === 'GET' && call[1] === '/api/inference/operations/op-live') &&
                    conflictResult.calls.some(call => call[0] === 'error' && String(call[1]).includes('already active')) &&
                    conflictResult.operation && conflictResult.operation.current_step === 'waiting_ready',
                    'profile action conflicts should fetch and render the active operation'
                );

                const richPreviewHtml = vm.runInContext(`
                    renderProfilePreview({
                        valid_for_save: true,
                        resolved_instances: [
                            { index: 0, host: '127.0.0.1', port: 10000, gpu_ids: [0], unit: 'infra-llm-qwen@0.service' },
                        ],
                        port_plan: {
                            mode: 'auto',
                            range: 'inference',
                            range_start: 10000,
                            range_end: 10999,
                            allocated: [10000],
                            persisted: false,
                        },
                        gpu_plan: {
                            mode: 'one_per_instance',
                            claim_mode: 'exclusive',
                            assignments: [{ index: 0, gpu_ids: [0] }],
                        },
                        command_preview: [{
                            index: 0,
                            argv: ['/home/aiml/vllm/bin/python', '-m', 'vllm.entrypoints.openai.api_server', '--port', '10000'],
                            env: { CUDA_VISIBLE_DEVICES: '0', HF_TOKEN: '<redacted>' },
                        }],
                        systemd_preview: {
                            units: [{ index: 0, name: 'infra-llm-qwen@0.service', content: '[Service]\\nExecStart=/home/aiml/vllm/bin/python' }],
                        },
                        cloudflare_plan: {
                            mode: 'cloudflare',
                            would_provision: true,
                            resources: [
                                { kind: 'dns_record', hostname: 'qwen.example.com' },
                                { kind: 'access_service_token', secret: 'generated_on_save' },
                            ],
                        },
                        restart_required: { required: true, fields: ['common.context_length'] },
                    });
                    document.getElementById('profile-preview-panel').innerHTML;
                `, context);
                assert(
                    richPreviewHtml.includes('restart required') &&
                    richPreviewHtml.includes('common.context_length') &&
                    richPreviewHtml.includes('Ports') &&
                    richPreviewHtml.includes('10000') &&
                    richPreviewHtml.includes('GPU Plan') &&
                    richPreviewHtml.includes('#0: 0') &&
                    richPreviewHtml.includes('Command Preview') &&
                    richPreviewHtml.includes('CUDA_VISIBLE_DEVICES') &&
                    richPreviewHtml.includes('&lt;redacted&gt;') &&
                    richPreviewHtml.includes('Systemd Unit Preview') &&
                    richPreviewHtml.includes('infra-llm-qwen@0.service') &&
                    richPreviewHtml.includes('Cloudflare Plan') &&
                    richPreviewHtml.includes('dns_record') &&
                    richPreviewHtml.includes('generated_on_save'),
                    'profile preview should render port/GPU, command env, systemd, Cloudflare, and restart facts'
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
                    replicatedConnectHtml.includes('Security Posture') &&
                    replicatedConnectHtml.includes('Local only') &&
                    replicatedConnectHtml.includes('Instance 0') &&
                    replicatedConnectHtml.includes('Instance 1') &&
                    replicatedConnectHtml.includes('data-copy="http://127.0.0.1:10000/v1"') &&
                    replicatedConnectHtml.includes('data-copy="http://127.0.0.1:10001/v1"'),
                    'replicated Connect view should render copyable per-instance endpoint options'
                );

                const cleanupHtml = vm.runInContext(`
                    inferenceProfilesData = [{
                        id: 'qwen-cleanup',
                        display_name: 'Qwen Cleanup',
                        exposure: { mode: 'cloudflare' },
                        cloudflare: {
                            hostname: 'qwen.example.com',
                            access_app_id: 'app-1',
                            access_policy_id: 'pol-1',
                            service_tokens: [{
                                id: 'tok-owned',
                                name: 'owned client',
                                client_id: 'owned.access',
                                state: 'active',
                                owned_by_inframatik: true,
                            }, {
                                id: 'tok-ext',
                                name: 'external client',
                                client_id: 'external.access',
                                state: 'active',
                                owned_by_inframatik: false,
                            }, {
                                id: 'tok-old',
                                name: 'retired client',
                                client_id: 'old.access',
                                state: 'retired',
                                owned_by_inframatik: true,
                            }],
                        },
                    }];
                    renderProfileConnect('qwen-cleanup', {
                        default: {
                            id: 'default',
                            name: 'Default',
                            target: { type: 'profile' },
                            exposure_mode: 'cloudflare',
                            base_url: 'https://qwen.example.com/v1',
                            model: 'qwen',
                            headers: {},
                            secret_state: {},
                            examples: {},
                        },
                        cleanup_records: [{
                            id: 'qwen-dns-record',
                            kind: 'dns_record',
                            payload: { hostname: 'qwen.example.com' },
                            attempts: 2,
                            error: 'route still exists',
                        }],
                    });
                    document.getElementById('profile-detail-qwen-cleanup').innerHTML;
                `, context);
                assert(
                    cleanupHtml.includes('Cloudflare Cleanup Pending') &&
                    cleanupHtml.includes('Security Posture') &&
                    cleanupHtml.includes('Cloudflare Access') &&
                    cleanupHtml.includes('Service Auth ready') &&
                    cleanupHtml.includes('Cloudflare clients') &&
                    cleanupHtml.includes('Generate Client') &&
                    cleanupHtml.includes('profile-cf-delete-owned-qwen-cleanup') &&
                    cleanupHtml.includes('Delete inframatik-owned clients if unreferenced') &&
                    cleanupHtml.includes('dns_record') &&
                    cleanupHtml.includes('qwen.example.com') &&
                    cleanupHtml.includes('retryInferenceCleanup') &&
                    cleanupHtml.includes('forgetInferenceCleanup') &&
                    cleanupHtml.includes('owned client') &&
                    cleanupHtml.includes('rotateProfileCfToken(&quot;qwen-cleanup&quot;,&quot;tok-owned&quot;)') &&
                    cleanupHtml.includes('external client') &&
                    cleanupHtml.includes('Detach') &&
                    !cleanupHtml.includes('rotateProfileCfToken(&quot;qwen-cleanup&quot;,&quot;tok-ext&quot;)') &&
                    cleanupHtml.includes('retired client') &&
                    cleanupHtml.includes('retired</span>'),
                    'Connect view should show cleanup records and ownership-aware Cloudflare token actions'
                );

                const removeCloudflareCalls = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        isMaster = false;
                        selectedNodeId = 'self-node';
                        document.getElementById('profile-cf-delete-owned-qwen-cleanup').checked = true;
                        confirm = function(message) {
                            calls.push(['confirm', message]);
                            return true;
                        };
                        api = async function(method, path) {
                            calls.push([method, path]);
                            if (method === 'DELETE' && path === '/api/inference/profiles/qwen-cleanup/cloudflare/exposure?delete_owned_tokens=true') {
                                return { warnings: [] };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        refreshInferenceProfiles = async function() { calls.push(['refreshProfiles']); };
                        loadProfileConnect = async function(profileId) { calls.push(['loadConnect', profileId]); };
                        await removeProfileCloudflare('qwen-cleanup');
                        return calls;
                    })()
                `, context);
                assert(
                    removeCloudflareCalls.filter(call => call[0] === 'confirm').length === 1 &&
                    removeCloudflareCalls.some(call => call[0] === 'DELETE' && call[1].endsWith('delete_owned_tokens=true')) &&
                    removeCloudflareCalls.some(call => call[0] === 'refreshProfiles') &&
                    removeCloudflareCalls.some(call => call[0] === 'loadConnect' && call[1] === 'qwen-cleanup'),
                    'Cloudflare endpoint removal should read the delete-owned checkbox and use one confirmation'
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


def test_app_js_main_refresh_loop_is_view_scoped():
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
                createElement() { return makeElement('created'); },
                getElementById(id) {
                    if (!elements.has(id)) elements.set(id, makeElement(id));
                    return elements.get(id);
                },
                querySelectorAll() { return []; },
                querySelector() { return makeElement('query-result'); },
            };

            const calls = [];
            let nextIntervalId = 100;
            const context = {
                console,
                document,
                window: { location: { hostname: 'localhost' } },
                location: { protocol: 'http:', host: 'localhost' },
                WebSocket: function WebSocket() { return {}; },
                fetch: async () => { throw new Error('fetch should not run'); },
                setTimeout,
                clearTimeout,
                setInterval: (fn, ms) => {
                    const id = nextIntervalId++;
                    calls.push(['setInterval', id, ms]);
                    return id;
                },
                clearInterval: (id) => {
                    calls.push(['clearInterval', id]);
                },
                calls,
            };
            context.globalThis = context;

            vm.createContext(context);
            vm.runInContext(fs.readFileSync('static/app.js', 'utf8'), context, {
                filename: 'static/app.js',
            });

            vm.runInContext(`
                (async () => {
                    function assert(condition, message) {
                        if (!condition) throw new Error(message);
                    }

                    nodeRole = 'master';
                    isMaster = true;
                    selectedNodeId = 'node-a';
                    currentAppView = 'main';
                    refreshInterval = 77;
                    sidebarInterval = 88;
                    loadInferenceView = async function() { calls.push(['loadInferenceView']); };
                    loadSettingsView = async function() { calls.push(['loadSettingsView']); };
                    refreshAll = async function() { calls.push(['refreshAll', currentAppView]); };
                    refreshSidebar = async function() { calls.push(['refreshSidebar', currentAppView]); };

                    await showAppView('inference');
                    assert(currentAppView === 'inference', 'inference view should be active');
                    assert(refreshInterval === null, 'main refresh interval should be cleared in inference view');
                    assert(sidebarInterval === null, 'sidebar refresh interval should be cleared in inference view');
                    assert(calls.some(call => call[0] === 'clearInterval' && call[1] === 77), 'inference view should clear the previous main interval');
                    assert(calls.some(call => call[0] === 'clearInterval' && call[1] === 88), 'inference view should clear the previous sidebar interval');
                    assert(calls.some(call => call[0] === 'loadInferenceView'), 'inference view should load');
                    assert(!calls.some(call => call[0] === 'setInterval'), 'inference view should not start the main refresh interval');

                    calls.length = 0;
                    await showAppView('settings');
                    assert(currentAppView === 'settings', 'settings view should be active');
                    assert(refreshInterval === null, 'settings view should leave main refresh stopped');
                    assert(sidebarInterval === null, 'settings view should leave sidebar refresh stopped');
                    assert(calls.some(call => call[0] === 'loadSettingsView'), 'settings view should load');
                    assert(!calls.some(call => call[0] === 'setInterval'), 'settings view should not start background refresh intervals');

                    calls.length = 0;
                    await showAppView('main');
                    assert(currentAppView === 'main', 'main view should be active');
                    assert(calls.some(call => call[0] === 'refreshSidebar' && call[1] === 'main'), 'main view should refresh the sidebar immediately');
                    assert(calls.some(call => call[0] === 'refreshAll' && call[1] === 'main'), 'main view should refresh immediately');
                    assert(calls.some(call => call[0] === 'setInterval' && call[2] === 15000), 'main view should start the sidebar refresh interval');
                    assert(calls.some(call => call[0] === 'setInterval' && call[2] === 5000), 'main view should start the dashboard refresh interval');
                    const intervalId = refreshInterval;
                    const sidebarId = sidebarInterval;
                    assert(intervalId !== null, 'main refresh interval id should be stored');
                    assert(sidebarId !== null, 'sidebar refresh interval id should be stored');

                    calls.length = 0;
                    await showAppView('inference');
                    assert(calls.some(call => call[0] === 'clearInterval' && call[1] === intervalId), 'leaving main should clear the new interval');
                    assert(calls.some(call => call[0] === 'clearInterval' && call[1] === sidebarId), 'leaving main should clear the new sidebar interval');
                })().catch((error) => {
                    console.error(error.stack || error.message);
                    process.exit(1);
                });
            `, context);
            """
        )
    )


def test_app_js_inference_ws_state_transitions_manage_activity_polling():
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
                querySelector() { return makeElement('query-result'); },
            };

            const calls = [];
            const timerCallbacks = new Map();
            let nextTimerId = 10;
            const context = {
                console,
                document,
                window: { location: { hostname: 'localhost' } },
                location: { protocol: 'http:', host: 'localhost' },
                WebSocket: function WebSocket() { return {}; },
                fetch: async () => { throw new Error('fetch should not run'); },
                setTimeout,
                clearTimeout,
                setInterval: (fn, ms) => {
                    const id = nextTimerId++;
                    calls.push(['setInterval', id, ms]);
                    timerCallbacks.set(id, fn);
                    return id;
                },
                clearInterval: (id) => {
                    calls.push(['clearInterval', id]);
                    timerCallbacks.delete(id);
                },
                calls,
                timerCallbacks,
            };
            context.globalThis = context;

            vm.createContext(context);
            vm.runInContext(fs.readFileSync('static/app.js', 'utf8'), context, {
                filename: 'static/app.js',
            });

            vm.runInContext(`
                (async () => {
                    function assert(condition, message) {
                        if (!condition) throw new Error(message);
                    }

                    isMaster = false;
                    nodeRole = 'worker';
                    selfNodeId = 'node-a';
                    selectedNodeId = 'node-a';
                    currentAppView = 'inference';
                    activeInferenceTab = 'launchers';
                    inferenceModelData = { artifacts: [], jobs: [] };
                    inferenceOperationsData = [{ id: 'op-1', state: 'running', profile_id: 'qwen' }];
                    renderInferenceOperations = function(operations) {
                        calls.push(['renderOperations', operations[0] && operations[0].state]);
                    };
                    hydrateVisibleInferenceFailures = function(nodeId) {
                        calls.push(['hydrateFailures', nodeId]);
                    };
                    setInferenceError = function(message) {
                        if (message) calls.push(['error', message]);
                    };
                    api = async function(method, path) {
                        calls.push(['api', method, path]);
                        if (path === '/api/inference/operations') {
                            return { operations: [{ id: 'op-1', state: 'succeeded', profile_id: 'qwen' }] };
                        }
                        throw new Error('unexpected API call: ' + method + ' ' + path);
                    };

                    wsConnected = true;
                    handleWsDisconnected();
                    const activeTimerId = inferenceJobsTimer;
                    assert(wsConnected === false, 'websocket close should mark the socket disconnected');
                    assert(activeTimerId !== null, 'websocket close should start fallback polling for active operations');
                    assert(
                        calls.some(call => call[0] === 'setInterval' && call[1] === activeTimerId && call[2] === 2500),
                        'fallback polling should run on the inference activity cadence'
                    );

                    await timerCallbacks.get(activeTimerId)();
                    assert(
                        calls.some(call => call[0] === 'api' && call[2] === '/api/inference/operations'),
                        'activity polling should refresh operations even when the Launchers tab is active'
                    );
                    assert(
                        inferenceOperationsData[0].state === 'succeeded',
                        'activity polling should merge the refreshed terminal operation'
                    );
                    assert(inferenceJobsTimer === null, 'terminal operation should stop fallback polling');
                    assert(
                        calls.some(call => call[0] === 'clearInterval' && call[1] === activeTimerId),
                        'terminal operation should clear the fallback timer'
                    );

                    calls.length = 0;
                    inferenceOperationsData = [{ id: 'op-2', state: 'running', profile_id: 'qwen' }];
                    api = async function(method, path) {
                        calls.push(['api', method, path]);
                        if (path === '/api/inference/operations') {
                            return { operations: [{ id: 'op-2', state: 'succeeded', profile_id: 'qwen' }] };
                        }
                        throw new Error('unexpected API call: ' + method + ' ' + path);
                    };
                    handleWsDisconnected();
                    const reconnectTimerId = inferenceJobsTimer;
                    assert(reconnectTimerId !== null, 'second disconnect should start fallback polling');

                    await handleWsConnected();
                    assert(wsConnected === true, 'websocket reconnect should mark the socket connected');
                    assert(
                        calls.some(call => call[0] === 'clearInterval' && call[1] === reconnectTimerId),
                        'websocket reconnect should stop fallback polling'
                    );
                    assert(
                        calls.some(call => call[0] === 'api' && call[2] === '/api/inference/operations'),
                        'websocket reconnect should do a one-shot activity resync'
                    );
                    assert(inferenceOperationsData[0].state === 'succeeded', 'reconnect resync should refresh stale active operation state');
                    assert(inferenceJobsTimer === null, 'websocket reconnect should leave fallback polling stopped');
                })().catch((error) => {
                    console.error(error.stack || error.message);
                    process.exit(1);
                });
            `, context);
            """
        )
    )


def test_app_js_launcher_venv_builder_sets_python_module_command():
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
                    disabled: false,
                    selectedIndex: 0,
                    options: [],
                    className: '',
                    children: [],
                    classList: {
                        add() {},
                        remove() {},
                        contains() { return false; },
                    },
                    addEventListener() {},
                    appendChild(child) {
                        this.children.push(child);
                        this.innerHTML += child.innerHTML || '';
                        return child;
                    },
                    querySelectorAll() { return []; },
                    querySelector() { return null; },
                };
                let html = '';
                Object.defineProperty(el, 'innerHTML', {
                    get() { return html; },
                    set(value) { html = String(value ?? ''); },
                });
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
            }

            const elements = new Map();
            const document = {
                cookie: '',
                addEventListener() {},
                createElement() {
                    return makeElement('created');
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
                function assert(condition, message) {
                    if (!condition) throw new Error(message);
                }

                document.getElementById('launcher-venv-path').value = '/home/aiml/venvs/vllm/';
                applyLauncherVenvPreset('vllm');
                assert(
                    document.getElementById('launcher-executable').value === '/home/aiml/venvs/vllm/bin/python',
                    'vLLM venv preset should derive the Python executable'
                );
                assert(
                    document.getElementById('launcher-engine').value === 'vllm',
                    'vLLM venv preset should select the vLLM engine'
                );
                const vllmArgs = document.getElementById('launcher-arg-rows').innerHTML;
                assert(vllmArgs.includes('-m'), 'vLLM venv preset should include module flag');
                assert(
                    vllmArgs.includes('vllm.entrypoints.openai.api_server'),
                    'vLLM venv preset should use the OpenAI-compatible API server module'
                );

                document.getElementById('launcher-venv-path').value = '~/sglang-env';
                applyLauncherVenvPreset('sglang');
                assert(
                    document.getElementById('launcher-executable').value === '~/sglang-env/bin/python',
                    'SGLang venv preset should preserve expandable home paths'
                );
                assert(
                    document.getElementById('launcher-engine').value === 'sglang',
                    'SGLang venv preset should select the SGLang engine'
                );
                const sglangArgs = document.getElementById('launcher-arg-rows').innerHTML;
                assert(sglangArgs.includes('sglang.launch_server'), 'SGLang venv preset should use the SGLang module');

                document.getElementById('launcher-executable').value = '/opt/runtime/bin/python3.11';
                syncLauncherVenvFromExecutable();
                assert(
                    document.getElementById('launcher-venv-path').value === '/opt/runtime',
                    'launcher form should infer a venv root from a Python executable'
                );

                resetLauncherForm();
                assert(document.getElementById('launcher-venv-path').value === '', 'reset should clear the venv helper');
            `, context);
            """
        )
    )


def test_app_js_profile_editor_sections_toggle():
    _run_node(
        textwrap.dedent(
            r"""
            const fs = require('fs');
            const vm = require('vm');

            function makeElement(id, dataset = {}) {
                const classes = new Set();
                const element = {
                    id,
                    dataset,
                    style: {},
                    value: '',
                    textContent: '',
                    innerHTML: '',
                    attributes: {},
                    children: [],
                    parentNode: null,
                    className: '',
                    classList: {
                        add(name) { classes.add(name); },
                        remove(name) { classes.delete(name); },
                        toggle(name, force) {
                            if (force) classes.add(name);
                            else classes.delete(name);
                        },
                        contains(name) { return classes.has(name); },
                    },
                    setAttribute(name, value) { this.attributes[name] = String(value); },
                    getAttribute(name) { return this.attributes[name]; },
                    addEventListener() {},
                    appendChild(child) {
                        child.parentNode = this;
                        this.children.push(child);
                        return child;
                    },
                    removeChild(child) {
                        this.children = this.children.filter(item => item !== child);
                        child.parentNode = null;
                        return child;
                    },
                    querySelectorAll() { return []; },
                    querySelector(selector) {
                        if (selector === '.profile-editor-issue-badge') {
                            return this.children.find(child => String(child.className || '').includes('profile-editor-issue-badge')) || null;
                        }
                        return null;
                    },
                };
                return element;
            }

            const sections = ['basics', 'runtime', 'placement', 'exposure', 'engine', 'advanced'];
            const tabs = sections.map(name => makeElement('tab-' + name, { profileEditorSection: name }));
            const panels = sections.map(name => makeElement('panel-' + name, { profileEditorPanel: name }));
            const elements = new Map([...tabs, ...panels].map(el => [el.id, el]));

            const document = {
                cookie: '',
                addEventListener() {},
                createElement() { return makeElement('created'); },
                getElementById(id) {
                    if (!elements.has(id)) elements.set(id, makeElement(id));
                    return elements.get(id);
                },
                querySelectorAll(selector) {
                    if (selector === '[data-profile-editor-section]') return tabs;
                    if (selector === '[data-profile-editor-panel]') return panels;
                    return [];
                },
                querySelector() { return makeElement('query-result'); },
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
                function assert(condition, message) {
                    if (!condition) throw new Error(message);
                }

                setProfileEditorSection('runtime');
                assert(document.getElementById('tab-runtime').classList.contains('active'), 'runtime tab should be active');
                assert(document.getElementById('tab-runtime').getAttribute('aria-selected') === 'true', 'runtime tab should be selected');
                assert(!document.getElementById('tab-basics').classList.contains('active'), 'basics tab should be inactive');
                assert(document.getElementById('tab-basics').getAttribute('aria-selected') === 'false', 'basics tab should be unselected');
                assert(document.getElementById('panel-runtime').classList.contains('active'), 'runtime panel should be active');
                assert(!document.getElementById('panel-basics').classList.contains('active'), 'basics panel should be hidden');

                setProfileEditorSection('nope');
                assert(document.getElementById('tab-basics').classList.contains('active'), 'invalid section should fall back to basics tab');
                assert(document.getElementById('panel-basics').classList.contains('active'), 'invalid section should fall back to basics panel');

                updateProfileEditorIssueBadges({
                    blockers: [
                        { field: 'engine_launcher_id', message: 'Launcher missing' },
                        { field: 'deployment.gpu_policy', message: 'GPU layout invalid' },
                    ],
                    warnings: [
                        { field: 'common.api_key', message: 'API key recommended' },
                        { field: 'advanced.env.CUDA_VISIBLE_DEVICES', message: 'GPU placement overridden' },
                    ],
                });
                assert(document.getElementById('tab-basics').classList.contains('has-blockers'), 'basics tab should show blockers');
                assert(document.getElementById('tab-placement').classList.contains('has-blockers'), 'placement tab should show blockers');
                assert(document.getElementById('tab-exposure').classList.contains('has-warnings'), 'exposure tab should show warnings');
                assert(document.getElementById('tab-advanced').classList.contains('has-warnings'), 'advanced tab should show warnings');
                assert(document.getElementById('tab-placement').querySelector('.profile-editor-issue-badge').textContent === '1', 'placement badge should show blocker count');

                clearProfileEditorIssueBadges();
                assert(!document.getElementById('tab-basics').classList.contains('has-blockers'), 'clear should remove blocker class');
                assert(!document.getElementById('tab-exposure').classList.contains('has-warnings'), 'clear should remove warning class');
                assert(document.getElementById('tab-advanced').querySelector('.profile-editor-issue-badge') === null, 'clear should remove badges');
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
    assert 'class="profile-editor-nav"' in index_html
    assert 'data-profile-editor-section="basics"' in index_html
    assert 'data-profile-editor-section="runtime"' in index_html
    assert 'data-profile-editor-section="placement"' in index_html
    assert 'data-profile-editor-section="exposure"' in index_html
    assert 'data-profile-editor-section="engine"' in index_html
    assert 'data-profile-editor-section="advanced"' in index_html
    assert 'data-profile-editor-panel="engine"' in index_html
    assert 'class="profile-engine-details" open' in index_html
    assert "setProfileEditorSection" in app_js
    assert "profileIssueSection" in app_js
    assert "updateProfileEditorIssueBadges" in app_js
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
    assert 'id="launcher-venv-path"' in index_html
    assert 'id="launcher-arg-rows"' in index_html
    assert 'id="launcher-env-rows"' in index_html
    assert "applyLauncherPreset('vllm-module')" in index_html
    assert "applyLauncherPreset('sglang-module')" in index_html
    assert "applyLauncherVenvPreset('vllm')" in index_html
    assert "applyLauncherVenvPreset('sglang')" in index_html
    assert "syncLauncherVenvFromExecutable" in index_html
    assert "vllm.entrypoints.openai.api_server" in app_js
    assert "sglang.launch_server" in app_js
    assert "setLauncherBaseArgs" in app_js
    assert "pythonExecutableFromVenv" in app_js
    assert "deriveVenvPathFromPythonExecutable" in app_js
    assert "launcherModuleName" in app_js
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
    assert "profile-token-guidance" in app_js
    assert "profile-cf-delete-owned-" in app_js
    assert "renderConnectionPosture" in app_js
    assert "renderInstanceBundleOptions" in app_js
    assert "profile-cf-hostname-" in app_js
    assert "prompt('Cloudflare hostname')" not in app_js
    assert "setProfileDetail(profileId, html, 'connect')" in app_js
    assert "activeInferenceTab = 'profiles'" in app_js
    assert "refreshInferenceProfiles" in app_js
    assert "stopSidebarLoop" in app_js
    assert "startSidebarLoop" in app_js
    assert "inference_operation" in app_js
    assert "handleInferenceOperationEvent" in app_js
    assert "handleInferenceOperationEvent(msg.operation, msg)" in app_js
    assert "websocketEventMatchesSelectedNode" in app_js
    assert "selectedInferenceNodeIds" in app_js
    assert "renderProfileOperationPanel" in app_js
    assert "operationLogButton" in app_js
    assert "operationLogOutputCache" in app_js
    assert "loadOperationLogs" in app_js
    assert "profileLogRequest" in app_js
    assert "hydrateInferenceFailureDiagnostics" in app_js
    assert "hydrateVisibleInferenceFailures" in app_js
    assert "operationWithHydratedLogs" in app_js
    assert "operationRuntimeStatus" in app_js
    assert "renderSystemdPreview" in app_js
    assert "renderCloudflarePreview" in app_js
    assert "renderCommandEnv" in app_js
    assert "cancelInferenceOperation" in app_js
    assert "surfaceActiveInferenceOperation" in app_js
    assert "watchInferenceOperation" not in app_js
    assert "inferenceOperationWatchers" not in app_js
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
    assert "renderCloudflareCleanupRecords" in app_js
    assert "retryInferenceCleanup" in app_js
    assert "forgetInferenceCleanup" in app_js
    assert "/api/inference/cleanup/" in app_js
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
    assert ".model-job-metrics" in style_css
    assert ".launcher-card" in style_css
    assert ".launcher-preset-row" in style_css
    assert ".launcher-validation-panel" in style_css
    assert ".launcher-validation-output" in style_css
    assert ".profile-card" in style_css
    assert ".profile-editor-nav" in style_css
    assert ".profile-editor-tab.active" in style_css
    assert ".profile-editor-tab.has-blockers" in style_css
    assert ".profile-editor-issue-badge" in style_css
    assert ".profile-editor-section.active" in style_css
    assert ".profile-detail-panel" in style_css
    assert ".profile-operation-panel" in style_css
    assert ".profile-operation-actions" in style_css
    assert ".profile-operation-steps" in style_css
    assert ".profile-operation-facts" in style_css
    assert ".profile-operation-log-output" in style_css
    assert ".inference-operation-context" in style_css
    assert "renderProfileOperationPanel(op, '', { context: 'operations' })" in app_js
    assert ".profile-instance-row" in style_css
    assert ".profile-test-panel" in style_css
    assert ".profile-config-chips" in style_css
    assert ".profile-gpu-chip" in style_css
    assert ".profile-engine-details" in style_css
    assert ".form-check-grid" in style_css
    assert ".profile-preview-panel" in style_css
    assert ".profile-preview-facts" in style_css
    assert ".profile-preview-resource-grid" in style_css
    assert ".profile-command-env" in style_css
    assert ".profile-connect-panel" in style_css
    assert ".connect-posture-panel" in style_css
    assert ".connect-posture-grid" in style_css
    assert ".connect-posture-item" in style_css
    assert ".connect-action-grid" in style_css
    assert ".connect-inline-form" in style_css
    assert ".connect-mini-facts" in style_css
    assert ".connect-copy-row" in style_css
    assert ".copy-btn" in style_css
    assert ".client-example-card" in style_css
    assert ".instance-bundle-row" in style_css
    assert ".cleanup-record-row" in style_css
    assert ".profile-one-time-secret" in style_css
    assert ".profile-token-row" in style_css
    assert ".profile-token-guidance" in style_css
    assert ".profile-cf-removal-option" in style_css


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
    test_app_js_main_refresh_loop_is_view_scoped()
    test_app_js_inference_ws_state_transitions_manage_activity_polling()
    test_app_js_launcher_venv_builder_sets_python_module_command()
    test_app_js_profile_editor_sections_toggle()
    test_static_index_contains_setup_guidance_and_empty_state_copy()
    test_static_inference_model_ui_assets_present()
    test_worker_enrollment_ui_uses_same_origin_backend_endpoint()
    print("ok")
