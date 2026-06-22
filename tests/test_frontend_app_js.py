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
                    replaceWith(next) {
                        if (next && next.id) elements.set(next.id, next);
                    },
                };
            }

            const elements = new Map();
            const document = {
                cookie: '',
                addEventListener() {},
                createElement(tagName = '') {
                    if (String(tagName).toLowerCase() === 'template') {
                        return {
                            _innerHTML: '',
                            content: { firstElementChild: null },
                            set innerHTML(value) {
                                this._innerHTML = String(value ?? '');
                                const match = this._innerHTML.match(/id="([^"]+)"/);
                                const child = makeElement(match ? match[1] : 'template-child');
                                child.innerHTML = this._innerHTML;
                                this.content.firstElementChild = child;
                            },
                            get innerHTML() { return this._innerHTML; },
                        };
                    }
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

                const overviewLoadResult = await vm.runInContext(`
                    (async () => {
                        const localCalls = [];
                        isMaster = true;
                        selfNodeId = 'master-node';
                        selectedNodeId = 'worker-a';
                        currentAppView = 'inference';
                        activeInferenceTab = 'profiles';
                        wsConnected = true;
                        inferenceJobsTimer = null;
                        nodes = [{ node_id: 'worker-a', node_name: 'Worker A', status: 'online' }];
                        api = async function(method, path) {
                            localCalls.push([method, path]);
                            if (method === 'GET' && path === '/api/nodes/worker-a/inference/overview') {
                                return {
                                    profiles: {
                                        profiles: [{
                                            id: 'qwen',
                                            display_name: 'Qwen',
                                            engine: 'vllm',
                                            engine_launcher_id: 'vllm-main',
                                            model: { artifact_id: 'qwen', snapshot: 'v1' },
                                            common: { context_length: 4096 },
                                            deployment: {},
                                            exposure: { mode: 'local' },
                                            instances: [{ index: 0, host: '127.0.0.1', port: 10000, gpu_ids: [0] }],
                                            state: 'stopped',
                                        }],
                                    },
                                    models: { artifacts: [{ id: 'qwen', display_name: 'Qwen', active_snapshot: 'v1' }], jobs: [] },
                                    launchers: { launchers: [{ id: 'vllm-main', display_name: 'vLLM', engine: 'vllm' }] },
                                    operations: { operations: [] },
                                    system: { gpus: [{ index: 0, name: 'GPU 0', mem_total_mb: 49152, mem_used_mb: 1024 }] },
                                    partial_errors: {},
                                };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        await refreshInferenceProfiles();
                        return {
                            calls: localCalls,
                            profiles: inferenceProfilesData.map(profile => profile.id),
                            launchers: inferenceLaunchersData.map(launcher => launcher.id),
                            profileHtml: document.getElementById('inference-profiles-list').innerHTML,
                            gpuHtml: document.getElementById('profile-gpu-hints').innerHTML,
                        };
                    })()
                `, context);
                assert(
                    overviewLoadResult.calls.length === 1 &&
                    overviewLoadResult.calls[0][1] === '/api/nodes/worker-a/inference/overview' &&
                    overviewLoadResult.profiles.includes('qwen') &&
                    overviewLoadResult.launchers.includes('vllm-main') &&
                    overviewLoadResult.profileHtml.includes('Qwen') &&
                    overviewLoadResult.gpuHtml.includes('GPU 0'),
                    'Profiles tab should load selected worker inference state through one overview request'
                );

                const readyModelJobResult = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        selectedNodeId = 'self-node';
                        selfNodeId = 'self-node';
                        isMaster = false;
                        currentAppView = 'inference';
                        activeInferenceTab = 'models';
                        inferenceModelData = { artifacts: [], jobs: [] };
                        inferenceStorageData = {};
                        refreshActiveInferenceTab = async function() { calls.push(['refreshActiveInferenceTab']); };
                        handleModelJobEvent({
                            id: 'mdl-ready',
                            kind: 'import',
                            artifact_id: 'qwen-ready',
                            snapshot: 'v1',
                            source: { type: 'local', path: '/models/qwen.gguf' },
                            state: 'ready',
                            progress: 100,
                            artifact: {
                                id: 'qwen-ready',
                                display_name: 'Qwen Ready',
                                manifest_display_name: 'Qwen Ready',
                                kind: 'gguf',
                                format: 'gguf',
                                active_snapshot: 'v1',
                                active_snapshot_state: 'ready',
                                size_bytes: 4096,
                                files_count: 1,
                                source: { type: 'local', path: '/models/qwen.gguf' },
                                snapshots: { v1: { state: 'ready' } },
                                current_root: true,
                                path_exists: true,
                            },
                        });
                        return {
                            calls,
                            artifacts: inferenceModelData.artifacts.map(item => item.id),
                            jobs: inferenceModelData.jobs.map(item => item.id),
                            inventoryHtml: document.getElementById('models-list').innerHTML,
                            summaryHtml: document.getElementById('model-storage-summary').innerHTML,
                            modelOptions: document.getElementById('profile-model').innerHTML,
                        };
                    })()
                `, context);
                assert(
                    !readyModelJobResult.calls.some(call => call[0] === 'refreshActiveInferenceTab') &&
                    readyModelJobResult.artifacts.includes('qwen-ready') &&
                    readyModelJobResult.jobs.includes('mdl-ready') &&
                    readyModelJobResult.inventoryHtml.includes('Qwen Ready') &&
                    readyModelJobResult.summaryHtml.includes('Models') &&
                    readyModelJobResult.modelOptions.includes('qwen-ready@v1'),
                    'ready model job events should patch inventory locally without refreshing the active tab'
                );

                const readyModelJobFallbackResult = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        selectedNodeId = 'self-node';
                        selfNodeId = 'self-node';
                        isMaster = false;
                        currentAppView = 'inference';
                        activeInferenceTab = 'models';
                        inferenceModelData = { artifacts: [], jobs: [] };
                        inferenceStorageData = {};
                        refreshActiveInferenceTab = async function() { calls.push(['refreshActiveInferenceTab']); };
                        api = async function(method, path) {
                            calls.push([method, path]);
                            if (method === 'GET' && path === '/api/models') {
                                return {
                                    artifacts: [{
                                        id: 'qwen-fallback',
                                        display_name: 'Qwen Fallback',
                                        active_snapshot: 'v1',
                                        active_snapshot_state: 'ready',
                                        snapshots: { v1: { state: 'ready' } },
                                    }],
                                    jobs: [{ id: 'mdl-ready-fallback', state: 'ready', artifact_id: 'qwen-fallback', snapshot: 'v1' }],
                                };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        handleModelJobEvent({
                            id: 'mdl-ready-fallback',
                            kind: 'import',
                            artifact_id: 'qwen-fallback',
                            snapshot: 'v1',
                            state: 'ready',
                            progress: 100,
                        });
                        await Promise.resolve();
                        await Promise.resolve();
                        return {
                            calls,
                            artifacts: inferenceModelData.artifacts.map(item => item.id),
                            inventoryHtml: document.getElementById('models-list').innerHTML,
                            modelOptions: document.getElementById('profile-model').innerHTML,
                        };
                    })()
                `, context);
                assert(
                    !readyModelJobFallbackResult.calls.some(call => call[0] === 'refreshActiveInferenceTab') &&
                    readyModelJobFallbackResult.calls.some(call => call[0] === 'GET' && call[1] === '/api/models') &&
                    readyModelJobFallbackResult.artifacts.includes('qwen-fallback') &&
                    readyModelJobFallbackResult.inventoryHtml.includes('Qwen Fallback') &&
                    readyModelJobFallbackResult.modelOptions.includes('qwen-fallback@v1'),
                    'ready model job events without artifact payload should refresh only the model snapshot, not the active tab'
                );

                const modelActionPatchResult = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        isMaster = false;
                        selfNodeId = 'self-node';
                        selectedNodeId = 'self-node';
                        currentAppView = 'inference';
                        activeInferenceTab = 'models';
                        inferenceStorageData = {};
                        inferenceModelData = {
                            artifacts: [{
                                id: 'qwen-local',
                                display_name: 'Qwen Local',
                                manifest_display_name: 'Qwen Local',
                                format: 'gguf',
                                active_snapshot: 'v1',
                                active_snapshot_state: 'ready',
                                snapshots: { v1: { state: 'ready' } },
                                files_count: 1,
                                size_bytes: 1024,
                                source: { type: 'local', path: '/models/qwen.gguf' },
                                current_root: true,
                                path_exists: true,
                            }],
                            jobs: [{
                                id: 'mdl-failed',
                                kind: 'download',
                                artifact_id: 'qwen-local',
                                snapshot: 'v1',
                                source: { type: 'url', url: 'https://example.invalid/model.gguf' },
                                state: 'failed',
                                progress: 50,
                                staging_path: '/tmp/inframatik/staging/mdl-failed',
                            }, {
                                id: 'mdl-running',
                                kind: 'download',
                                artifact_id: 'qwen-local',
                                snapshot: 'v2',
                                source: { type: 'url', url: 'https://example.invalid/model2.gguf' },
                                state: 'running',
                                progress: 10,
                            }],
                        };
                        refreshInferenceModels = async function() { calls.push(['refreshInferenceModels']); };
                        confirm = function(message) { calls.push(['confirm', message]); return true; };
                        api = async function(method, path) {
                            calls.push([method, path]);
                            if (method === 'POST' && path === '/api/models/qwen-local/verify?snapshot=v1') {
                                return { artifact_id: 'qwen-local', snapshot: 'v1', valid: false, checked: [], missing: ['model.gguf'], changed: [], extra: [] };
                            }
                            if (method === 'POST' && path === '/api/models/jobs/mdl-running/cancel') {
                                return { id: 'mdl-running', kind: 'download', artifact_id: 'qwen-local', snapshot: 'v2', state: 'canceled', progress: 10, source: { type: 'url', url: 'https://example.invalid/model2.gguf' } };
                            }
                            if (method === 'DELETE' && path === '/api/models/jobs/mdl-failed/staging') {
                                return { job_id: 'mdl-failed', removed: true, staging_path: '/tmp/inframatik/staging/mdl-failed', job: { id: 'mdl-failed', kind: 'download', artifact_id: 'qwen-local', snapshot: 'v1', state: 'failed', progress: 50, staging_path: '/tmp/inframatik/staging/mdl-failed', cleanup: { staging_removed: true, staging_removed_reason: 'manual' }, source: { type: 'url', url: 'https://example.invalid/model.gguf' } } };
                            }
                            if (method === 'DELETE' && path === '/api/models/qwen-local') {
                                return { deleted: 'qwen-local', snapshot: null, paths: [], references: { running: [], stopped: [] } };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        await verifyModelArtifact('qwen-local', 'v1');
                        const afterVerifyHtml = document.getElementById('models-list').innerHTML;
                        await cancelModelJob('mdl-running');
                        await cleanModelJobStaging('mdl-failed', '/tmp/inframatik/staging/mdl-failed');
                        const afterJobHtml = document.getElementById('model-jobs-list').innerHTML;
                        await deleteModelArtifact('qwen-local', 'Qwen Local');
                        return {
                            calls,
                            artifacts: inferenceModelData.artifacts.map(item => item.id),
                            jobs: inferenceModelData.jobs.map(item => [item.id, item.state, item.cleanup && item.cleanup.staging_removed]),
                            afterVerifyHtml,
                            afterJobHtml,
                            inventoryHtml: document.getElementById('models-list').innerHTML,
                            modelOptions: document.getElementById('profile-model').innerHTML,
                        };
                    })()
                `, context);
                assert(
                    !modelActionPatchResult.calls.some(call => call[0] === 'refreshInferenceModels') &&
                    modelActionPatchResult.afterVerifyHtml.includes('degraded') &&
                    modelActionPatchResult.jobs.some(item => item[0] === 'mdl-running' && item[1] === 'canceled') &&
                    modelActionPatchResult.jobs.some(item => item[0] === 'mdl-failed' && item[2] === true) &&
                    modelActionPatchResult.afterJobHtml.includes('Staging cleaned') &&
                    modelActionPatchResult.artifacts.length === 0 &&
                    modelActionPatchResult.inventoryHtml.includes('No managed models yet.') &&
                    !modelActionPatchResult.modelOptions.includes('qwen-local@v1'),
                    'model verify/delete/cancel/clean actions should patch local state without refreshing models'
                );

                const profileDetailResult = await vm.runInContext(`
                    (async () => {
                        const localCalls = [];
                        isMaster = true;
                        selfNodeId = 'master-node';
                        selectedNodeId = 'worker-a';
                        currentAppView = 'inference';
                        inferenceProfilesData = [{
                            id: 'qwen',
                            display_name: 'Qwen',
                            engine: 'vllm',
                            engine_launcher_id: 'vllm-main',
                            model: { artifact_id: 'qwen', snapshot: 'v1' },
                            common: { context_length: 4096 },
                            deployment: {},
                            exposure: { mode: 'local' },
                            instances: [{ index: 0, host: '127.0.0.1', port: 10000, gpu_ids: [0], unit: 'infra-llm-qwen.service' }],
                            state: 'stopped',
                        }];
                        inferenceOperationsData = [];
                        api = async function(method, path) {
                            localCalls.push([method, path]);
                            if (method === 'GET' && path === '/api/nodes/worker-a/inference/profiles/qwen/detail') {
                                return {
                                    profile: inferenceProfilesData[0],
                                    instances: {
                                        profile_id: 'qwen',
                                        health: 'degraded',
                                        instances: [{
                                            index: 0,
                                            host: '127.0.0.1',
                                            port: 10000,
                                            gpu_ids: [0],
                                            unit: 'infra-llm-qwen.service',
                                            systemd_state: 'active',
                                            tcp_reachable: false,
                                            health: 'degraded',
                                        }],
                                    },
                                    plan: {
                                        blockers: [],
                                        warnings: [{ message: 'restart required before live traffic' }],
                                        command_preview: [{ argv: ['vllm', 'serve', '/models/qwen'], env: {} }],
                                        systemd_preview: { units: [] },
                                    },
                                    partial_errors: { plan: 'preview timed out' },
                                };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        await loadProfileDetails('qwen');
                        return {
                            calls: localCalls,
                            html: document.getElementById('profile-detail-qwen').innerHTML,
                        };
                    })()
                `, context);
                assert(
                    profileDetailResult.calls.length === 1 &&
                    profileDetailResult.calls[0][1] === '/api/nodes/worker-a/inference/profiles/qwen/detail' &&
                    profileDetailResult.html.includes('Some live detail could not be loaded') &&
                    profileDetailResult.html.includes('infra-llm-qwen.service') &&
                    profileDetailResult.html.includes('TCP no') &&
                    profileDetailResult.html.includes('vllm serve /models/qwen'),
                    'Profile Details should load through one detail request and preserve partial diagnostics'
                );

                const submitUrlJobResult = await vm.runInContext(`
                    (async () => {
                        const localCalls = [];
                        isMaster = false;
                        selectedNodeId = 'self-node';
                        currentAppView = 'inference';
                        activeInferenceTab = 'models';
                        inferenceModelData = { artifacts: [], jobs: [] };
                        inferenceOperationsData = [];
                        document.getElementById('model-url-url').value = 'https://example.invalid/model.gguf';
                        document.getElementById('model-url-artifact').value = 'qwen-url';
                        document.getElementById('model-url-snapshot').value = 'v1';
                        document.getElementById('model-url-extract').checked = false;
                        refreshInferenceModels = async function() {
                            localCalls.push(['refreshInferenceModels']);
                        };
                        api = async function(method, path, body) {
                            localCalls.push([method, path, body && body.source && body.source.url]);
                            if (method === 'POST' && path === '/api/models/download') {
                                return {
                                    id: 'mdl-new',
                                    kind: 'download',
                                    artifact_id: 'qwen-url',
                                    snapshot: 'v1',
                                    source: { type: 'url', url: 'https://example.invalid/model.gguf' },
                                    state: 'queued',
                                    progress: 0,
                                    downloaded_bytes: 0,
                                    total_bytes: 0,
                                };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        await submitModelUrlDownload();
                        return {
                            calls: localCalls,
                            activeInferenceTab,
                            jobsHtml: document.getElementById('model-jobs-list').innerHTML,
                            operationsHtml: document.getElementById('inference-operations-list').innerHTML,
                            jobs: inferenceModelData.jobs.map(job => job.id),
                        };
                    })()
                `, context);
                assert(
                    submitUrlJobResult.calls.some(call => call[0] === 'POST' && call[1] === '/api/models/download') &&
                    !submitUrlJobResult.calls.some(call => call[0] === 'refreshInferenceModels') &&
                    submitUrlJobResult.activeInferenceTab === 'jobs' &&
                    submitUrlJobResult.jobs.includes('mdl-new') &&
                    submitUrlJobResult.jobsHtml.includes('qwen-url') &&
                    submitUrlJobResult.operationsHtml.includes('No inference operations yet.'),
                    'model download start should render the returned job without a broad model refresh'
                );

                const startupPanelHtml = vm.runInContext(`
                    renderProfileOperationPanel({
                        id: 'op-start',
                        kind: 'profile_start',
                        state: 'running',
                        profile_id: 'qwen',
                        current_step: 'waiting_ready',
                        progress: 72,
                        steps: [
                            { name: 'validate', state: 'succeeded' },
                            { name: 'start_units', state: 'succeeded' },
                            { name: 'waiting_ready', state: 'running' },
                            { name: 'complete', state: 'pending' },
                        ],
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
                            log_tail: 'Loading model shard 1/8\\nCUDA graph capture starting',
                            log_tail_lines: 2,
                        },
                    });
                `, context);
                assert(
                    startupPanelHtml.includes('Systemd') &&
                    startupPanelHtml.includes('active') &&
                    startupPanelHtml.includes('TCP') &&
                    startupPanelHtml.includes('waiting') &&
                    startupPanelHtml.includes('is active; waiting for the inference API port') &&
                    startupPanelHtml.includes('Restarts') &&
                    startupPanelHtml.includes('42s / 10m') &&
                    startupPanelHtml.includes('profile-operation-readiness') &&
                    startupPanelHtml.includes('Readiness wait') &&
                    startupPanelHtml.includes('Waiting for TCP') &&
                    startupPanelHtml.includes('Operation timeline') &&
                    startupPanelHtml.includes('validate') &&
                    startupPanelHtml.includes('succeeded') &&
                    startupPanelHtml.includes('waiting ready') &&
                    startupPanelHtml.includes('running · current') &&
                    startupPanelHtml.includes('profile-operation-step-dot') &&
                    startupPanelHtml.includes('42s elapsed') &&
                    startupPanelHtml.includes('9m 18s remaining') &&
                    startupPanelHtml.includes('Startup log tail') &&
                    startupPanelHtml.includes('2 lines') &&
                    startupPanelHtml.includes('Loading model shard 1/8') &&
                    startupPanelHtml.includes('CUDA graph capture starting') &&
                    startupPanelHtml.includes('loadOperationLogs(&quot;qwen&quot;, 0,') &&
                    startupPanelHtml.includes('profile-operation-log-output-panel-op-start'),
                    'active inference operation panel should show live startup readiness facts, log tail, and inline targeted logs'
                );

                const failedStartupHtml = vm.runInContext(`
                    inferenceProfilesData = [{
                        id: 'qwen',
                        display_name: 'Qwen',
                        engine_launcher_id: 'vllm-main',
                        state: 'failed',
                        instances: [],
                    }];
                    renderProfileOperationPanel({
                        id: 'op-failed-start',
                        kind: 'profile_start',
                        state: 'failed',
                        profile_id: 'qwen',
                        current_step: 'failed',
                        progress: 100,
                        result: {
                            message: 'Start failed; started instances were stopped',
                            cause: {
                                message: 'Unit infra-llm-qwen.service restarted 3 times before TCP readiness',
                                unit: 'infra-llm-qwen.service',
                                host: '127.0.0.1',
                                port: 10000,
                                systemd_state: 'active',
                                tcp_reachable: false,
                                restart_count: 3,
                                elapsed_seconds: 90,
                                timeout_seconds: 600,
                                wait_position: 1,
                                wait_total: 1,
                                logs: 'ImportError: libcudart.so.12: cannot open shared object file',
                            },
                            rollback: [{ index: 0, unit: 'infra-llm-qwen.service', ok: true }],
                        },
                    });
                `, context);
                assert(
                    failedStartupHtml.includes('Start failed; started instances were stopped') &&
                    failedStartupHtml.includes('Unit infra-llm-qwen.service restarted 3 times before TCP readiness') &&
                    failedStartupHtml.includes('Likely cause') &&
                    failedStartupHtml.includes('cannot load a required shared library') &&
                    failedStartupHtml.includes('Next action') &&
                    failedStartupHtml.includes('Validate the engine launcher') &&
                    failedStartupHtml.includes('Suggested Env') &&
                    failedStartupHtml.includes('openLauncherValidation(&quot;vllm-main&quot;, &quot;qwen&quot;)') &&
                    failedStartupHtml.includes('Validate launcher') &&
                    failedStartupHtml.includes('profile-operation-readiness') &&
                    failedStartupHtml.includes('Waiting for TCP') &&
                    failedStartupHtml.includes('1m 30s / 10m') &&
                    failedStartupHtml.includes('Systemd') &&
                    failedStartupHtml.includes('active') &&
                    failedStartupHtml.includes('TCP') &&
                    failedStartupHtml.includes('waiting') &&
                    failedStartupHtml.includes('Rollback') &&
                    failedStartupHtml.includes('Rollback stopped 1 started instance') &&
                    failedStartupHtml.includes('ImportError: libcudart.so.12'),
                    'failed startup operation panel should explain cause, next action, rollback, and captured logs'
                );

                const failedValidationHtml = vm.runInContext(`
                    inferenceProfilesData = [{
                        id: 'qwen',
                        display_name: 'Qwen',
                        engine_launcher_id: 'vllm-main',
                        state: 'failed',
                        instances: [],
                    }];
                    renderProfileOperationPanel({
                        id: 'op-failed-validation',
                        kind: 'profile_start',
                        state: 'failed',
                        profile_id: 'qwen',
                        current_step: 'failed',
                        progress: 100,
                        result: {
                            message: 'Launcher runtime validation failed',
                            launcher_id: 'vllm-main',
                            validation: {
                                valid: false,
                                runtime: {
                                    checked: true,
                                    valid: false,
                                    code: 7,
                                    output: 'ImportError: libcudart.so.12: cannot open shared object file',
                                    suggested_env: {
                                        LD_LIBRARY_PATH: '/home/aiml/vllm/venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib',
                                    },
                                },
                            },
                            suggested_env: {
                                LD_LIBRARY_PATH: '/home/aiml/vllm/venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib',
                            },
                        },
                    });
                `, context);
                assert(
                    failedValidationHtml.includes('Launcher runtime validation failed') &&
                    failedValidationHtml.includes('failed runtime validation before inframatik started the service') &&
                    failedValidationHtml.includes('Suggested Env') &&
                    failedValidationHtml.includes('applyLauncherSuggestedEnv') &&
                    failedValidationHtml.includes('LD_LIBRARY_PATH') &&
                    failedValidationHtml.includes('nvidia/cuda_runtime/lib') &&
                    failedValidationHtml.includes('ImportError: libcudart.so.12') &&
                    failedValidationHtml.includes('openLauncherValidation(&quot;vllm-main&quot;, &quot;qwen&quot;)'),
                    'runtime validation failures should show suggested env and probe output before any systemd rollback'
                );

                const launcherFixActionResult = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        currentAppView = 'inference';
                        activeInferenceTab = 'profiles';
                        selectedNodeId = 'self-node';
                        isMaster = false;
                        inferenceLaunchersData = [];
                        inferenceProfilesData = [{ id: 'qwen', display_name: 'Qwen', state: 'failed', engine_launcher_id: 'vllm-main' }];
                        launcherValidationProfileContext = new Map();
                        const originalRenderLaunchers = renderLaunchers;
                        const originalValidateLauncher = validateLauncher;
                        const originalApi = api;
                        renderLaunchers = function(launchers) { calls.push(['renderLaunchers', launchers.map(item => item.id).join(',')]); };
                        validateLauncher = async function(launcherId) { calls.push(['validateLauncher', launcherId]); };
                        setInferenceStatus = function(message) { calls.push(['status', message]); };
                        setInferenceError = function(message) { if (message) calls.push(['error', message]); };
                        api = async function(method, path) {
                            calls.push([method, path]);
                            if (method === 'GET' && path === '/api/inference/launchers') {
                                return { launchers: [{ id: 'vllm-main', display_name: 'vLLM Main', engine: 'vllm', executable: '/opt/vllm/bin/python' }] };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        try {
                            await openLauncherValidation('vllm-main', 'qwen');
                            return { calls, activeInferenceTab, contextProfile: launcherValidationProfileContext.get('vllm-main') };
                        } finally {
                            renderLaunchers = originalRenderLaunchers;
                            validateLauncher = originalValidateLauncher;
                            api = originalApi;
                        }
                    })()
                `, context);
                assert(
                    launcherFixActionResult.activeInferenceTab === 'launchers' &&
                    launcherFixActionResult.calls.some(call => call[0] === 'GET' && call[1] === '/api/inference/launchers') &&
                    launcherFixActionResult.calls.some(call => call[0] === 'renderLaunchers' && call[1] === 'vllm-main') &&
                    launcherFixActionResult.calls.some(call => call[0] === 'validateLauncher' && call[1] === 'vllm-main') &&
                    launcherFixActionResult.contextProfile === 'qwen' &&
                    launcherFixActionResult.calls.some(call => call[0] === 'status' && String(call[1]).includes('Validating launcher')),
                    'failed startup launcher fix action should load Launchers and validate the profile launcher'
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
                            log_tail: 'Loading model shard 1/8\\nCUDA graph capture starting',
                            log_tail_lines: 2,
                        },
                    }]);
                    document.getElementById('inference-operations-list').innerHTML;
                `, context);
                assert(
                    startupOperationListHtml.includes('profile-operation-panel') &&
                    startupOperationListHtml.includes('infra-llm-qwen.service') &&
                    startupOperationListHtml.includes('TCP') &&
                    startupOperationListHtml.includes('waiting') &&
                    startupOperationListHtml.includes('42s / 10m') &&
                    startupOperationListHtml.includes('Readiness wait') &&
                    startupOperationListHtml.includes('9m 18s remaining') &&
                    startupOperationListHtml.includes('Startup log tail') &&
                    startupOperationListHtml.includes('CUDA graph capture starting'),
                    'operations tab should reuse the rich operation panel with live readiness facts and log tail'
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
                    !cancelCalls.calls.some(call => call[0] === 'refreshProfiles') &&
                    cancelCalls.state === 'canceled',
                    'cancel operation should call the cancel endpoint and merge the canceled operation without a broad profile refresh'
                );

                const terminalPatchResult = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        selectedNodeId = 'self-node';
                        selfNodeId = 'self-node';
                        isMaster = false;
                        wsConnected = true;
                        currentAppView = 'inference';
                        activeInferenceTab = 'profiles';
                        inferenceProfilesData = [{
                            id: 'qwen',
                            display_name: 'Qwen',
                            state: 'starting',
                            instances: [
                                { index: 0, state: 'starting' },
                                { index: 1, state: 'starting' },
                            ],
                        }];
                        inferenceOperationsData = [];
                        profileDetailModes.clear();
                        profileDetailModes.set('qwen', 'details');
                        refreshInferenceProfiles = async function() { calls.push(['refreshProfiles']); };
                        loadProfileDetails = async function(profileId) { calls.push(['loadDetails', profileId]); };
                        const originalUpdateCard = updateInferenceProfileCard;
                        renderInferenceProfiles = function(profiles) {
                            calls.push(['renderProfiles', profiles[0].state, profiles[0].instances.map(item => item.state).join(',')]);
                        };
                        updateInferenceProfileCard = function(profileId) {
                            calls.push(['updateCard', profileId]);
                        };
                        renderInferenceOperations = function(operations) {
                            calls.push(['renderOperations', operations[0] && operations[0].state]);
                        };
                        handleInferenceOperationEvent({
                            id: 'op-done',
                            kind: 'profile_start',
                            state: 'succeeded',
                            profile_id: 'qwen',
                            progress: 100,
                            result: {
                                state: 'running',
                                instances: [{ index: 0 }, { index: 1 }],
                            },
                        });
                        await Promise.resolve();
                        updateInferenceProfileCard = originalUpdateCard;
                        return { calls, profile: inferenceProfilesData[0] };
                    })()
                `, context);
                assert(
                    !terminalPatchResult.calls.some(call => call[0] === 'refreshProfiles') &&
                    !terminalPatchResult.calls.some(call => call[0] === 'renderProfiles') &&
                    terminalPatchResult.calls.some(call => call[0] === 'updateCard' && call[1] === 'qwen') &&
                    terminalPatchResult.calls.some(call => call[0] === 'loadDetails' && call[1] === 'qwen') &&
                    terminalPatchResult.profile.state === 'running' &&
                    terminalPatchResult.profile.instances.every(item => item.state === 'running'),
                    'terminal inference operation events should patch profile state locally and refresh only the touched card/open detail'
                );

                const singleCardUpdateResult = vm.runInContext(`
                    (() => {
                        currentAppView = 'inference';
                        activeInferenceTab = 'profiles';
                        inferenceProfilesData = [
                            { id: 'qwen', display_name: 'Qwen', engine: 'vllm', engine_launcher_id: 'vllm-main', state: 'starting', model: { artifact_id: 'qwen', snapshot: 'main' }, instances: [] },
                            { id: 'other', display_name: 'Other', engine: 'vllm', engine_launcher_id: 'vllm-main', state: 'stopped', model: { artifact_id: 'other', snapshot: 'main' }, instances: [] },
                        ];
                        inferenceOperationsData = [{
                            id: 'op-live',
                            kind: 'profile_start',
                            state: 'running',
                            profile_id: 'qwen',
                            current_step: 'waiting_ready',
                            progress: 72,
                            runtime_status: { unit: 'infra-llm-qwen@0.service', host: '127.0.0.1', port: 10000, systemd_state: 'active', tcp_reachable: false },
                        }];
                        const list = document.getElementById('inference-profiles-list');
                        list._inframatikHtml = 'full-list-cache';
                        const qwenCard = document.getElementById('profile-card-qwen');
                        qwenCard.innerHTML = 'old qwen card';
                        qwenCard._inframatikHtml = 'old qwen card';
                        const otherCard = document.getElementById('profile-card-other');
                        otherCard.innerHTML = 'untouched other card';
                        otherCard._inframatikHtml = 'untouched other card';
                        profileDetailCache.set('qwen', '<div class="detail-marker">open detail</div>');
                        document.getElementById('profile-detail-qwen').innerHTML = '';
                        updateInferenceProfileCard('qwen');
                        return {
                            qwenHtml: document.getElementById('profile-card-qwen').innerHTML,
                            otherHtml: document.getElementById('profile-card-other').innerHTML,
                            detailHtml: document.getElementById('profile-detail-qwen').innerHTML,
                            listCache: document.getElementById('inference-profiles-list')._inframatikHtml,
                        };
                    })()
                `, context);
                assert(
                    singleCardUpdateResult.qwenHtml.includes('waiting ready') &&
                    singleCardUpdateResult.qwenHtml.includes('infra-llm-qwen@0.service') &&
                    singleCardUpdateResult.otherHtml === 'untouched other card' &&
                    singleCardUpdateResult.detailHtml.includes('open detail') &&
                    singleCardUpdateResult.listCache === null,
                    'active operation updates should replace only the touched profile card and preserve open detail'
                );

                const profileCardActionHtml = vm.runInContext(`
                    renderProfileCard({
                        id: 'qwen-actions',
                        display_name: 'Qwen Actions',
                        engine: 'vllm',
                        engine_launcher_id: 'vllm-main',
                        state: 'stopped',
                        model: { artifact_id: 'qwen', snapshot: 'main' },
                        instances: [{ index: 0, host: '127.0.0.1', port: 10000, gpu_ids: [0] }],
                    });
                `, context);
                assert(
                    profileCardActionHtml.includes('profile-action-bar') &&
                    profileCardActionHtml.includes('profile-action-group operate') &&
                    profileCardActionHtml.includes('profile-action-group inspect') &&
                    profileCardActionHtml.includes('profile-action-group manage') &&
                    profileCardActionHtml.indexOf('Operate') < profileCardActionHtml.indexOf('Inspect') &&
                    profileCardActionHtml.indexOf('Inspect') < profileCardActionHtml.indexOf('Manage') &&
                    profileCardActionHtml.includes('data-profile-action="start"') &&
                    profileCardActionHtml.includes('loadProfileConnect') &&
                    profileCardActionHtml.includes('deleteInferenceProfile'),
                    'profile cards should group operate, inspect, and manage actions'
                );

                const deleteProfileResult = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        selectedNodeId = 'self-node';
                        isMaster = false;
                        currentAppView = 'inference';
                        activeInferenceTab = 'profiles';
                        inferenceProfilesData = [
                            { id: 'qwen-delete', display_name: 'Qwen Delete', state: 'stopped', instances: [] },
                            { id: 'keep', display_name: 'Keep', state: 'stopped', instances: [] },
                        ];
                        profileDetailCache.set('qwen-delete', '<div>stale detail</div>');
                        profileDetailModes.set('qwen-delete', 'details');
                        profileOutputCache.set('qwen-delete', '<div>stale output</div>');
                        pendingInferenceProfileActions.set('qwen-delete', 'start');
                        pendingInferenceInstanceActions.set('qwen-delete:0', 'restart');
                        document.getElementById('profile-detail-qwen-delete').innerHTML = '<div>stale detail</div>';
                        confirm = function(message) {
                            calls.push(['confirm', message]);
                            return true;
                        };
                        api = async function(method, path) {
                            calls.push([method, path]);
                            if (method === 'DELETE' && path === '/api/inference/profiles/qwen-delete') {
                                return { deleted: 'qwen-delete', removed_units: ['infra-llm-qwen-delete.service'] };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        refreshInferenceProfiles = async function() { calls.push(['refreshProfiles']); };
                        renderInferenceProfiles = function(profiles) {
                            calls.push(['renderProfiles', profiles.map(item => item.id).join(',')]);
                        };
                        await deleteInferenceProfile('qwen-delete', 'Qwen Delete');
                        return {
                            calls,
                            profileIds: inferenceProfilesData.map(item => item.id),
                            hasDetail: profileDetailCache.has('qwen-delete'),
                            hasPendingProfile: pendingInferenceProfileActions.has('qwen-delete'),
                            hasPendingInstance: pendingInferenceInstanceActions.has('qwen-delete:0'),
                            detailHtml: document.getElementById('profile-detail-qwen-delete').innerHTML,
                        };
                    })()
                `, context);
                assert(
                    deleteProfileResult.calls.some(call => call[0] === 'DELETE' && call[1] === '/api/inference/profiles/qwen-delete') &&
                    !deleteProfileResult.calls.some(call => call[0] === 'refreshProfiles') &&
                    deleteProfileResult.calls.some(call => call[0] === 'renderProfiles' && call[1] === 'keep') &&
                    deleteProfileResult.profileIds.length === 1 &&
                    deleteProfileResult.profileIds[0] === 'keep' &&
                    deleteProfileResult.hasDetail === false &&
                    deleteProfileResult.hasPendingProfile === false &&
                    deleteProfileResult.hasPendingInstance === false &&
                    deleteProfileResult.detailHtml === '',
                    'deleting a profile should remove local state, cached detail, and pending actions without a broad refresh'
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

                const thinActionResult = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        selectedNodeId = 'self-node';
                        isMaster = false;
                        currentAppView = 'inference';
                        activeInferenceTab = 'profiles';
                        inferenceProfilesData = [{ id: 'qwen', display_name: 'Qwen', state: 'stopped', instances: [] }];
                        inferenceOperationsData = [];
                        pendingInferenceProfileActions.clear();
                        const originalUpdateCard = updateInferenceProfileCard;
                        const originalRenderOperations = renderInferenceOperations;
                        updateInferenceProfileCard = function(profileId) { calls.push(['updateCard', profileId]); };
                        renderInferenceOperations = function(operations) { calls.push(['renderOperations', operations[0] && operations[0].profile_id, operations[0] && operations[0].kind]); };
                        setInferenceStatus = function(message) { calls.push(['status', message]); };
                        setInferenceError = function(message) { calls.push(['error', message]); };
                        api = async function(method, path) {
                            calls.push([method, path]);
                            if (method === 'POST' && path === '/api/inference/profiles/qwen/start') {
                                return { id: 'op-thin' };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        try {
                            await runProfileAction('qwen', 'start');
                            return { calls, operation: inferenceOperationsData[0], pendingAction: pendingInferenceProfileActions.get('qwen') };
                        } finally {
                            updateInferenceProfileCard = originalUpdateCard;
                            renderInferenceOperations = originalRenderOperations;
                        }
                    })()
                `, context);
                assert(
                    thinActionResult.operation &&
                    thinActionResult.operation.id === 'op-thin' &&
                    thinActionResult.operation.profile_id === 'qwen' &&
                    thinActionResult.operation.kind === 'profile_start' &&
                    thinActionResult.operation.state === 'queued' &&
                    thinActionResult.pendingAction === 'start' &&
                    thinActionResult.calls.some(call => call[0] === 'updateCard' && call[1] === 'qwen') &&
                    !thinActionResult.calls.some(call => call[0] === 'renderOperations') &&
                    thinActionResult.calls.some(call => call[0] === 'status' && String(call[1]).includes('operation queued')),
                    'thin accepted profile action responses should be normalized and update the visible profile card without rebuilding hidden jobs'
                );

                const hiddenOperationRenderResult = vm.runInContext(`
                    (() => {
                        const calls = [];
                        const originalRenderOperations = renderInferenceOperations;
                        const originalHydrateFailure = hydrateInferenceFailureDiagnostics;
                        selectedNodeId = 'self-node';
                        isMaster = false;
                        currentAppView = 'inference';
                        inferenceProfilesData = [{ id: 'qwen', display_name: 'Qwen', state: 'failed', instances: [] }];
                        inferenceOperationsData = [];
                        renderInferenceOperations = function(operations) {
                            calls.push(['renderOperations', activeInferenceTab, operations[0] && operations[0].id]);
                        };
                        hydrateInferenceFailureDiagnostics = function(operation) {
                            calls.push(['hydrate', activeInferenceTab, operation && operation.id]);
                        };
                        setInferenceError = function(message) {
                            if (message) calls.push(['error', message]);
                        };
                        try {
                            activeInferenceTab = 'models';
                            mergeInferenceOperation({
                                id: 'op-hidden',
                                kind: 'profile_start',
                                state: 'failed',
                                profile_id: 'qwen',
                                result: { message: 'failed before logs' },
                            });
                            activeInferenceTab = 'jobs';
                            mergeInferenceOperation({
                                id: 'op-visible',
                                kind: 'profile_start',
                                state: 'failed',
                                profile_id: 'qwen',
                                result: { message: 'failed before logs' },
                            });
                            return calls;
                        } finally {
                            renderInferenceOperations = originalRenderOperations;
                            hydrateInferenceFailureDiagnostics = originalHydrateFailure;
                        }
                    })()
                `, context);
                assert(
                    !hiddenOperationRenderResult.some(call => call[0] === 'renderOperations' && call[1] === 'models') &&
                    !hiddenOperationRenderResult.some(call => call[0] === 'hydrate' && call[1] === 'models') &&
                    hiddenOperationRenderResult.some(call => call[0] === 'renderOperations' && call[1] === 'jobs' && call[2] === 'op-visible') &&
                    hiddenOperationRenderResult.some(call => call[0] === 'hydrate' && call[1] === 'jobs' && call[2] === 'op-visible'),
                    'operation events should avoid hidden tab DOM/log work while keeping the visible Jobs tab live'
                );

                const acceptedReconcileResult = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        const timers = [];
                        const originalSetTimeout = setTimeout;
                        const originalUpdateCard = updateInferenceProfileCard;
                        const originalRenderOperations = renderInferenceOperations;
                        setTimeout = function(fn, ms) {
                            calls.push(['setTimeout', ms]);
                            timers.push(fn);
                            return 501;
                        };
                        selectedNodeId = 'self-node';
                        isMaster = false;
                        currentAppView = 'inference';
                        activeInferenceTab = 'profiles';
                        wsConnected = true;
                        inferenceProfilesData = [{
                            id: 'qwen-fast',
                            display_name: 'Qwen Fast',
                            state: 'stopped',
                            instances: [{ index: 0, state: 'stopped' }],
                        }];
                        inferenceOperationsData = [];
                        pendingInferenceProfileActions.clear();
                        profileDetailModes.clear();
                        updateInferenceProfileCard = function(profileId) {
                            const profile = profileById(profileId);
                            calls.push(['updateCard', profileId, profile && profile.state]);
                        };
                        renderInferenceOperations = function(operations) {
                            calls.push(['renderOperations', operations[0] && operations[0].state]);
                        };
                        setInferenceStatus = function(message) { calls.push(['status', message]); };
                        setInferenceError = function(message) { if (message) calls.push(['error', message]); };
                        api = async function(method, path) {
                            calls.push([method, path]);
                            if (method === 'POST' && path === '/api/inference/profiles/qwen-fast/start') {
                                return { id: 'op-fast' };
                            }
                            if (method === 'GET' && path === '/api/inference/operations/op-fast') {
                                return {
                                    id: 'op-fast',
                                    kind: 'profile_start',
                                    state: 'failed',
                                    profile_id: 'qwen-fast',
                                    current_step: 'failed',
                                    progress: 100,
                                    result: {
                                        message: 'Launcher runtime validation failed',
                                        launcher_id: 'vllm-main',
                                        validation: {
                                            runtime: {
                                                output: 'ImportError: libcudart.so.12: cannot open shared object file',
                                                suggested_env: { LD_LIBRARY_PATH: '/venv/cuda/lib' },
                                            },
                                        },
                                    },
                                };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        try {
                            await runProfileAction('qwen-fast', 'start');
                            const getBeforeTimer = calls.some(call => call[0] === 'GET');
                            const pendingBeforeTimer = pendingInferenceProfileActions.get('qwen-fast');
                            await timers[0]();
                            return {
                                calls,
                                getBeforeTimer,
                                pendingBeforeTimer,
                                operation: inferenceOperationsData.find(item => item.id === 'op-fast'),
                                profile: inferenceProfilesData[0],
                                pendingAfterTimer: pendingInferenceProfileActions.get('qwen-fast') || null,
                            };
                        } finally {
                            setTimeout = originalSetTimeout;
                            updateInferenceProfileCard = originalUpdateCard;
                            renderInferenceOperations = originalRenderOperations;
                        }
                    })()
                `, context);
                assert(
                    acceptedReconcileResult.calls.some(call => call[0] === 'setTimeout' && call[1] === 1200) &&
                    acceptedReconcileResult.getBeforeTimer === false &&
                    acceptedReconcileResult.pendingBeforeTimer === 'start' &&
                    acceptedReconcileResult.calls.some(call => call[0] === 'GET' && call[1] === '/api/inference/operations/op-fast') &&
                    acceptedReconcileResult.operation &&
                    acceptedReconcileResult.operation.state === 'failed' &&
                    acceptedReconcileResult.profile.state === 'failed' &&
                    acceptedReconcileResult.profile.instances[0].state === 'failed' &&
                    acceptedReconcileResult.pendingAfterTimer === null &&
                    acceptedReconcileResult.calls.some(call => call[0] === 'updateCard' && call[1] === 'qwen-fast' && call[2] === 'failed') &&
                    acceptedReconcileResult.calls.some(call => call[0] === 'error' && String(call[1]).includes('Launcher runtime validation failed')),
                    'accepted profile actions should run one targeted reconcile so fast validation failures cannot stay queued if a websocket event is missed'
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

                const previewIssueHtml = vm.runInContext(`
                    renderProfilePreview({
                        valid_for_save: false,
                        blockers: [
                            { field: 'model.artifact_id', message: 'Model is required.' },
                            { field: 'deployment.gpu_policy.gpu_ids', message: 'GPU 8 does not exist.' },
                        ],
                        warnings: [
                            { field: 'exposure.hostname', message: 'Cloudflare hostname will be provisioned on save.' },
                            { field: 'advanced.env', message: 'Raw env values are used as-is.' },
                        ],
                        resolved_instances: [],
                        port_plan: {},
                        gpu_plan: {},
                        command_preview: [],
                        systemd_preview: { units: [] },
                        cloudflare_plan: {},
                    });
                    document.getElementById('profile-preview-panel').innerHTML;
                `, context);
                assert(
                    previewIssueHtml.includes('profile-preview-issues') &&
                    previewIssueHtml.includes('Basics') &&
                    previewIssueHtml.includes('Placement') &&
                    previewIssueHtml.includes('Exposure') &&
                    previewIssueHtml.includes('Advanced') &&
                    previewIssueHtml.includes('Model is required.') &&
                    previewIssueHtml.includes('GPU 8 does not exist.') &&
                    previewIssueHtml.includes('Cloudflare hostname will be provisioned on save.') &&
                    previewIssueHtml.includes('Raw env values are used as-is.') &&
                    previewIssueHtml.includes("setProfileEditorSection('placement')"),
                    'profile preview should group blockers and warnings by editor section with jump actions'
                );

                const stalePreviewResult = vm.runInContext(`
                    renderProfilePreview({
                        valid_for_save: true,
                        blockers: [],
                        warnings: [],
                        resolved_instances: [],
                        port_plan: {},
                        gpu_plan: {},
                        command_preview: [],
                        systemd_preview: { units: [] },
                        cloudflare_plan: {},
                    });
                    const fresh = document.getElementById('profile-preview-panel').innerHTML;
                    markProfilePreviewStale();
                    const stale = document.getElementById('profile-preview-panel').innerHTML;
                    renderProfilePreview({
                        valid_for_save: true,
                        blockers: [],
                        warnings: [],
                        resolved_instances: [],
                        port_plan: { allocated: [10001], mode: 'auto' },
                        gpu_plan: {},
                        command_preview: [],
                        systemd_preview: { units: [] },
                        cloudflare_plan: {},
                    });
                    const freshAgain = document.getElementById('profile-preview-panel').innerHTML;
                    resetProfilePreviewPanel();
                    const reset = document.getElementById('profile-preview-panel').innerHTML;
                    ({ fresh, stale, freshAgain, reset });
                `, context);
                assert(
                    !stalePreviewResult.fresh.includes('Preview is stale') &&
                    stalePreviewResult.stale.includes('Preview is stale') &&
                    stalePreviewResult.stale.includes('Run Preview again') &&
                    !stalePreviewResult.freshAgain.includes('Preview is stale') &&
                    stalePreviewResult.freshAgain.includes('10001') &&
                    stalePreviewResult.reset.includes('No preview yet.'),
                    'profile preview should warn when stale and clear the warning on preview or reset'
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

                const missingSecretBundleHtml = vm.runInContext(`
                    renderClientBundle({
                        id: 'default',
                        name: 'Default connection',
                        profile_id: 'qwen-cf',
                        target: { type: 'profile' },
                        exposure_mode: 'cloudflare',
                        base_url: 'https://llm.example.com/v1',
                        model: 'qwen',
                        service_token_id: 'tok-1',
                        headers: {
                            Authorization: 'Bearer <rotate_inference_api_key_to_show_once>',
                            'CF-Access-Client-Id': 'client.access',
                            'CF-Access-Client-Secret': '<rotate_or_generate_cloudflare_service_token_to_show_once>',
                        },
                        secret_state: {
                            missing_secret_actions: ['rotate_inference_api_key', 'rotate_cloudflare_service_token'],
                        },
                        examples: {
                            curl: 'curl https://llm.example.com/v1/models',
                            python_openai: 'from openai import OpenAI',
                            litellm: 'model_list:',
                        },
                    }, {}, 'qwen-cf');
                `, context);
                assert(
                    missingSecretBundleHtml.includes('One-Time Secrets Needed') &&
                    missingSecretBundleHtml.includes('Stored metadata is available, but raw client secrets are not persisted') &&
                    missingSecretBundleHtml.includes('Engine API Key') &&
                    missingSecretBundleHtml.includes('Show API Key') &&
                    missingSecretBundleHtml.includes('rotateProfileApiKey(&quot;qwen-cf&quot;)') &&
                    missingSecretBundleHtml.includes('Cloudflare Client Secret') &&
                    missingSecretBundleHtml.includes('Generate New Client') &&
                    missingSecretBundleHtml.includes('generateProfileCfToken(&quot;qwen-cf&quot;)') &&
                    missingSecretBundleHtml.includes('Rotate This Client') &&
                    missingSecretBundleHtml.includes('rotateProfileCfToken(&quot;qwen-cf&quot;,&quot;tok-1&quot;)') &&
                    !missingSecretBundleHtml.includes('Missing one-time values') &&
                    !missingSecretBundleHtml.includes('rotate_cloudflare_service_token'),
                    'client bundles with unavailable one-time secrets should render readable recovery actions'
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
                    replicatedConnectHtml.includes('Credential Chain') &&
                    replicatedConnectHtml.includes('Local only') &&
                    replicatedConnectHtml.includes('Cloudflare Endpoint') &&
                    replicatedConnectHtml.includes('Optional public API endpoint') &&
                    replicatedConnectHtml.includes('Use Cloudflare') &&
                    replicatedConnectHtml.includes('focusProfileCloudflareHostname(&quot;qwen-repl&quot;)') &&
                    replicatedConnectHtml.includes('profile-cf-hostname-qwen-repl') &&
                    replicatedConnectHtml.includes('Cloudflare Access') &&
                    replicatedConnectHtml.includes('Not used') &&
                    replicatedConnectHtml.includes('2 instance endpoints') &&
                    replicatedConnectHtml.includes('Per-instance URLs below') &&
                    !replicatedConnectHtml.includes('Cloudflare Service Tokens') &&
                    replicatedConnectHtml.includes('Instance 0') &&
                    replicatedConnectHtml.includes('Instance 1') &&
                    replicatedConnectHtml.includes('data-copy="http://127.0.0.1:10000/v1"') &&
                    replicatedConnectHtml.includes('data-copy="http://127.0.0.1:10001/v1"'),
                    'replicated Connect view should render copyable per-instance endpoint options and a direct Cloudflare upgrade path'
                );

                const localCloudflareProvisionResult = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        isMaster = false;
                        selectedNodeId = 'self-node';
                        currentAppView = 'inference';
                        activeInferenceTab = 'profiles';
                        inferenceProfilesData = [{
                            id: 'qwen-local',
                            display_name: 'Qwen Local',
                            exposure: { mode: 'local' },
                            cloudflare: {},
                            secrets: {},
                        }];
                        renderProfileConnect('qwen-local', {
                            default: {
                                id: 'default',
                                target: { type: 'profile' },
                                exposure_mode: 'local',
                                base_url: 'http://127.0.0.1:10000/v1',
                                model: 'qwen-local',
                                headers: {},
                                secret_state: {},
                                examples: {},
                            },
                        });
                        document.getElementById('profile-cf-hostname-qwen-local').value = 'qwen.example.com';
                        refreshInferenceProfiles = async function() { calls.push(['refreshProfiles']); };
                        setInferenceStatus = function(message) { calls.push(['status', message]); };
                        setInferenceError = function(message) { calls.push(['error', message]); };
                        api = async function(method, path, body) {
                            calls.push([method, path, body && body.hostname, body && body.render_bundle]);
                            if (method === 'POST' && path === '/api/inference/profiles/qwen-local/cloudflare/exposure') {
                                return {
                                    status: 'provisioned',
                                    client_secret: 'cf_once',
                                    profile: {
                                        id: 'qwen-local',
                                        display_name: 'Qwen Local',
                                        exposure: { mode: 'cloudflare', hostname: 'qwen.example.com' },
                                        cloudflare: {
                                            hostname: 'qwen.example.com',
                                            access_app_id: 'app-1',
                                            access_policy_id: 'pol-1',
                                            service_tokens: [{
                                                id: 'tok-1',
                                                name: 'default client',
                                                client_id: 'client.access',
                                                state: 'active',
                                                owned_by_inframatik: true,
                                            }],
                                        },
                                    },
                                    client_bundle: {
                                        id: 'default',
                                        target: { type: 'profile' },
                                        exposure_mode: 'cloudflare',
                                        base_url: 'https://qwen.example.com/v1',
                                        model: 'qwen-local',
                                        headers: {
                                            'CF-Access-Client-Id': 'client.access',
                                            'CF-Access-Client-Secret': '<shown_once>',
                                        },
                                        secret_state: {},
                                        examples: { curl: 'curl https://qwen.example.com/v1/models' },
                                    },
                                };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        await provisionProfileCloudflare('qwen-local');
                        return {
                            calls,
                            profile: inferenceProfilesData.find(item => item.id === 'qwen-local'),
                            html: document.getElementById('profile-detail-qwen-local').innerHTML,
                        };
                    })()
                `, context);
                assert(
                    localCloudflareProvisionResult.calls.some(call =>
                        call[0] === 'POST' &&
                        call[1] === '/api/inference/profiles/qwen-local/cloudflare/exposure' &&
                        call[2] === 'qwen.example.com' &&
                        call[3] === true
                    ) &&
                    !localCloudflareProvisionResult.calls.some(call => call[0] === 'refreshProfiles') &&
                    localCloudflareProvisionResult.calls.some(call => call[0] === 'status' && String(call[1]).includes('Cloudflare endpoint ready')) &&
                    localCloudflareProvisionResult.profile.exposure.mode === 'cloudflare' &&
                    localCloudflareProvisionResult.html.includes('Cloudflare Service Tokens') &&
                    localCloudflareProvisionResult.html.includes('client.access') &&
                    localCloudflareProvisionResult.html.includes('data-copy="cf_once"'),
                    'local profiles should provision Cloudflare directly from Connect and patch the local profile state'
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
                    cleanupHtml.includes('Credential Chain') &&
                    cleanupHtml.includes('Cloudflare Access') &&
                    cleanupHtml.includes('Service Auth ready') &&
                    cleanupHtml.includes('Service Auth') &&
                    cleanupHtml.includes('2 active clients') &&
                    cleanupHtml.includes('Cloudflare clients') &&
                    cleanupHtml.includes('Generate Client') &&
                    cleanupHtml.includes('profile-cf-delete-owned-qwen-cleanup') &&
                    cleanupHtml.includes('Delete inframatik-owned clients if unreferenced') &&
                    cleanupHtml.includes('Removes the route, DNS record, Access app, and policy from Cloudflare') &&
                    cleanupHtml.includes('Cleanup failures stay visible here for retry') &&
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
                        currentAppView = 'inference';
                        activeInferenceTab = 'jobs';
                        document.getElementById('profile-cf-delete-owned-qwen-cleanup').checked = true;
                        confirm = function(message) {
                            calls.push(['confirm', message]);
                            return true;
                        };
                        api = async function(method, path) {
                            calls.push([method, path]);
                            if (method === 'DELETE' && path === '/api/inference/profiles/qwen-cleanup/cloudflare/exposure?delete_owned_tokens=true') {
                                return {
                                    warnings: [],
                                    profile: {
                                        id: 'qwen-cleanup',
                                        display_name: 'Qwen Cleanup',
                                        exposure: { mode: 'local' },
                                        cloudflare: { service_tokens: [] },
                                    },
                                };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        refreshInferenceProfiles = async function() { calls.push(['refreshProfiles']); };
                        loadProfileConnect = async function(profileId) { calls.push(['loadConnect', profileId]); };
                        await removeProfileCloudflare('qwen-cleanup');
                        return { calls, profile: inferenceProfilesData.find(item => item.id === 'qwen-cleanup') };
                    })()
                `, context);
                assert(
                    removeCloudflareCalls.calls.filter(call => call[0] === 'confirm').length === 1 &&
                    removeCloudflareCalls.calls.some(call => call[0] === 'confirm' && String(call[1]).includes('tunnel route, DNS record, Access app, and Access policy')) &&
                    removeCloudflareCalls.calls.some(call => call[0] === 'confirm' && String(call[1]).includes('service-token clients will also be deleted')) &&
                    removeCloudflareCalls.calls.some(call => call[0] === 'confirm' && String(call[1]).includes('retry records in the Connect view')) &&
                    removeCloudflareCalls.calls.some(call => call[0] === 'DELETE' && call[1].endsWith('delete_owned_tokens=true')) &&
                    !removeCloudflareCalls.calls.some(call => call[0] === 'refreshProfiles') &&
                    removeCloudflareCalls.calls.some(call => call[0] === 'loadConnect' && call[1] === 'qwen-cleanup') &&
                    removeCloudflareCalls.profile.exposure.mode === 'local',
                    'Cloudflare endpoint removal should patch local profile state and reload only the Connect panel'
                );

                const apiKeyPatchResult = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        isMaster = false;
                        selectedNodeId = 'self-node';
                        currentAppView = 'inference';
                        activeInferenceTab = 'jobs';
                        inferenceProfilesData = [{
                            id: 'qwen-key',
                            display_name: 'Qwen Key',
                            exposure: { mode: 'lan' },
                            cloudflare: {},
                            secrets: {},
                        }];
                        api = async function(method, path, body) {
                            calls.push([method, path, body && body.render_bundle]);
                            if (method === 'POST' && path === '/api/inference/profiles/qwen-key/api-key') {
                                return {
                                    status: 'rotated',
                                    engine_api_key: 'llm_raw_secret',
                                    profile: {
                                        id: 'qwen-key',
                                        display_name: 'Qwen Key',
                                        exposure: { mode: 'lan' },
                                        cloudflare: {},
                                        secrets: { engine_api_key_id: 'sec-1' },
                                    },
                                    client_bundle: {
                                        id: 'default',
                                        target: { type: 'profile' },
                                        exposure_mode: 'lan',
                                        base_url: 'http://node.local:10000/v1',
                                        model: 'qwen-key',
                                        headers: { Authorization: 'Bearer llm_raw_secret' },
                                        secret_state: { engine_api_key_configured: true },
                                        examples: { curl: 'curl http://node.local:10000/v1/models' },
                                    },
                                };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        refreshInferenceProfiles = async function() { calls.push(['refreshProfiles']); };
                        await rotateProfileApiKey('qwen-key');
                        return {
                            calls,
                            profile: inferenceProfilesData.find(item => item.id === 'qwen-key'),
                            html: document.getElementById('profile-detail-qwen-key').innerHTML,
                        };
                    })()
                `, context);
                assert(
                    apiKeyPatchResult.calls.some(call => call[0] === 'POST' && call[1] === '/api/inference/profiles/qwen-key/api-key' && call[2] === true) &&
                    !apiKeyPatchResult.calls.some(call => call[0] === 'refreshProfiles') &&
                    apiKeyPatchResult.profile.secrets.engine_api_key_id === 'sec-1' &&
                    apiKeyPatchResult.html.includes('engine key ready') &&
                    apiKeyPatchResult.html.includes('data-copy="Bearer llm_raw_secret"'),
                    'Engine API key rotation should patch local profile state and render the one-time secret without a broad refresh'
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

                const portPolicyRoundTrip = vm.runInContext(`
                    fillProfileForm({
                        id: 'qwen-replicas',
                        display_name: 'Qwen Replicas',
                        engine: 'vllm',
                        engine_launcher_id: 'vllm-main',
                        model: { artifact_id: 'qwen', snapshot: 'v1' },
                        common: {},
                        deployment: {
                            mode: 'replicated',
                            replicas: 2,
                            port_policy: { mode: 'contiguous' },
                            gpu_policy: { mode: 'one_per_instance', gpu_ids: [0, 1] },
                        },
                        advanced: {},
                        engine_config: {},
                        exposure: {},
                        instances: [{ port: 10000 }, { port: 10001 }],
                        state: 'stopped',
                    });
                    const draft = buildProfileDraft();
                    ({
                        policyValue: document.getElementById('profile-port-policy').value,
                        portsValue: document.getElementById('profile-ports').value,
                        portsDisabled: document.getElementById('profile-ports').disabled,
                        portPolicy: draft.deployment.port_policy,
                    });
                `, context);
                assert(
                    portPolicyRoundTrip.policyValue === 'contiguous' &&
                    portPolicyRoundTrip.portsValue === '' &&
                    portPolicyRoundTrip.portsDisabled === true &&
                    portPolicyRoundTrip.portPolicy.mode === 'contiguous' &&
                    !('ports' in portPolicyRoundTrip.portPolicy),
                    'editing a replicated contiguous profile should not rewrite allocated ports as a one-port explicit policy'
                );

                const manualPortDraft = vm.runInContext(`
                    resetProfileForm();
                    document.getElementById('profile-id').value = 'qwen-manual';
                    document.getElementById('profile-engine').value = 'vllm';
                    document.getElementById('profile-launcher').value = 'vllm-main';
                    document.getElementById('profile-model').value = 'qwen@v1';
                    document.getElementById('profile-deployment-mode').value = 'replicated';
                    document.getElementById('profile-replicas').value = '2';
                    document.getElementById('profile-port-policy').value = 'explicit';
                    syncProfilePortPolicyFields();
                    document.getElementById('profile-ports').value = '10020, 10021';
                    buildProfileDraft().deployment.port_policy;
                `, context);
                assert(
                    manualPortDraft.mode === 'explicit' &&
                    manualPortDraft.ports.length === 2 &&
                    manualPortDraft.ports[0] === 10020 &&
                    manualPortDraft.ports[1] === 10021,
                    'manual port policy should preserve explicit multi-instance ports'
                );

                const metricsDraftResult = vm.runInContext(`
                    resetProfileForm();
                    document.getElementById('profile-id').value = 'qwen-metrics';
                    document.getElementById('profile-engine').value = 'vllm';
                    document.getElementById('profile-launcher').value = 'vllm-main';
                    document.getElementById('profile-model').value = 'qwen@v1';
                    document.getElementById('profile-metrics').checked = true;
                    buildProfileDraft().common.enable_metrics;
                `, context);
                assert(
                    metricsDraftResult === true,
                    'profile editor should include explicitly enabled metrics in structured common config'
                );

                const loraDraftResult = vm.runInContext(`
                    resetProfileForm();
                    document.getElementById('profile-id').value = 'qwen-lora';
                    document.getElementById('profile-engine').value = 'vllm';
                    document.getElementById('profile-launcher').value = 'vllm-main';
                    document.getElementById('profile-model').value = 'qwen@v1';
                    document.getElementById('profile-lora-enabled').checked = true;
                    document.getElementById('profile-lora-paths').value = '[{"name":"style","path":"/models/style-lora"}]';
                    buildProfileDraft().common.lora;
                `, context);
                assert(
                    loraDraftResult.enabled === true &&
                    Array.isArray(loraDraftResult.paths) &&
                    loraDraftResult.paths[0].name === 'style' &&
                    loraDraftResult.paths[0].path === '/models/style-lora',
                    'profile editor should include LoRA adapters in structured common config'
                );

                const loraRoundTripResult = vm.runInContext(`
                    fillProfileForm({
                        id: 'qwen-lora-edit',
                        display_name: 'Qwen LoRA Edit',
                        engine: 'sglang',
                        engine_launcher_id: 'sglang-main',
                        model: { artifact_id: 'qwen', snapshot: 'v1' },
                        common: {
                            lora: {
                                enabled: true,
                                paths: [{ name: 'tool', path: '/models/tool-lora' }],
                            },
                        },
                        deployment: {},
                        advanced: {},
                        engine_config: {},
                        exposure: {},
                        instances: [],
                        state: 'stopped',
                    });
                    ({
                        checked: document.getElementById('profile-lora-enabled').checked,
                        text: document.getElementById('profile-lora-paths').value,
                        commonJson: document.getElementById('profile-common-json').value,
                    });
                `, context);
                assert(
                    loraRoundTripResult.checked === true &&
                    loraRoundTripResult.text.includes('/models/tool-lora') &&
                    !loraRoundTripResult.commonJson.includes('lora'),
                    'profile editor should round-trip LoRA fields without duplicating them into raw common JSON'
                );

                const vllmContextParallelDraftResult = vm.runInContext(`
                    resetProfileForm();
                    document.getElementById('profile-id').value = 'qwen-vllm-cp';
                    document.getElementById('profile-engine').value = 'vllm';
                    document.getElementById('profile-launcher').value = 'vllm-main';
                    document.getElementById('profile-model').value = 'qwen@v1';
                    document.getElementById('profile-vllm-context-backend').value = 'nccl';
                    document.getElementById('profile-vllm-decode-cp-size').value = '2';
                    document.getElementById('profile-vllm-prefill-cp-size').value = '4';
                    buildProfileDraft().engine_config.vllm;
                `, context);
                assert(
                    vllmContextParallelDraftResult.context_parallel_backend === 'nccl' &&
                    vllmContextParallelDraftResult.decode_context_parallel_size === 2 &&
                    vllmContextParallelDraftResult.prefill_context_parallel_size === 4,
                    'profile editor should include explicit vLLM context-parallel engine fields'
                );

                const vllmContextParallelRoundTripResult = vm.runInContext(`
                    fillProfileForm({
                        id: 'qwen-vllm-cp-edit',
                        display_name: 'Qwen vLLM CP Edit',
                        engine: 'vllm',
                        engine_launcher_id: 'vllm-main',
                        model: { artifact_id: 'qwen', snapshot: 'v1' },
                        common: {},
                        deployment: {},
                        advanced: {},
                        engine_config: { vllm: {
                            context_parallel_backend: 'nccl',
                            decode_context_parallel_size: 2,
                            prefill_context_parallel_size: 4,
                        } },
                        exposure: {},
                        instances: [],
                        state: 'stopped',
                    });
                    ({
                        backend: document.getElementById('profile-vllm-context-backend').value,
                        decode: document.getElementById('profile-vllm-decode-cp-size').value,
                        prefill: document.getElementById('profile-vllm-prefill-cp-size').value,
                        engineJson: document.getElementById('profile-engine-json').value,
                    });
                `, context);
                assert(
                    vllmContextParallelRoundTripResult.backend === 'nccl' &&
                    vllmContextParallelRoundTripResult.decode === '2' &&
                    vllmContextParallelRoundTripResult.prefill === '4' &&
                    !vllmContextParallelRoundTripResult.engineJson.includes('decode_context_parallel_size') &&
                    !vllmContextParallelRoundTripResult.engineJson.includes('prefill_context_parallel_size'),
                    'profile editor should round-trip vLLM context-parallel fields without duplicating them into raw engine JSON'
                );

                const engineGuideResult = vm.runInContext(`
                    (() => {
                        const originalQuerySelectorAll = document.querySelectorAll;
                        const sections = {
                            vllm: { style: {}, classList: { contains(cls) { return cls === 'engine-field-vllm'; } } },
                            sglang: { style: {}, classList: { contains(cls) { return cls === 'engine-field-sglang'; } } },
                            llama: { style: {}, classList: { contains(cls) { return cls === 'engine-field-llama'; } } },
                        };
                        document.querySelectorAll = function(selector) {
                            if (selector === '.engine-field') return [sections.vllm, sections.sglang, sections.llama];
                            return originalQuerySelectorAll.call(document, selector);
                        };
                        try {
                            document.getElementById('profile-engine').value = 'vllm';
                            renderProfileEngineFields();
                            const vllmHtml = document.getElementById('profile-engine-guide').innerHTML;
                            const vllmDisplays = {
                                vllm: sections.vllm.style.display || '',
                                sglang: sections.sglang.style.display,
                                llama: sections.llama.style.display,
                            };
                            document.getElementById('profile-engine').value = 'sglang';
                            renderProfileEngineFields();
                            const sglangHtml = document.getElementById('profile-engine-guide').innerHTML;
                            const sglangDisplays = {
                                vllm: sections.vllm.style.display,
                                sglang: sections.sglang.style.display || '',
                                llama: sections.llama.style.display,
                            };
                            document.getElementById('profile-engine').value = 'llama.cpp';
                            renderProfileEngineFields();
                            const llamaHtml = document.getElementById('profile-engine-guide').innerHTML;
                            const llamaDisplays = {
                                vllm: sections.vllm.style.display,
                                sglang: sections.sglang.style.display,
                                llama: sections.llama.style.display || '',
                            };
                            return { vllmHtml, sglangHtml, llamaHtml, vllmDisplays, sglangDisplays, llamaDisplays };
                        } finally {
                            document.querySelectorAll = originalQuerySelectorAll;
                        }
                    })()
                `, context);
                assert(
                    engineGuideResult.vllmHtml.includes('Throughput server') &&
                    engineGuideResult.vllmHtml.includes('Expert parallel') &&
                    engineGuideResult.sglangHtml.includes('Structured generation') &&
                    engineGuideResult.sglangHtml.includes('DSA prefill CP') &&
                    engineGuideResult.llamaHtml.includes('GGUF local server') &&
                    engineGuideResult.llamaHtml.includes('Tensor split') &&
                    engineGuideResult.vllmDisplays.vllm === '' &&
                    engineGuideResult.vllmDisplays.sglang === 'none' &&
                    engineGuideResult.sglangDisplays.sglang === '' &&
                    engineGuideResult.sglangDisplays.vllm === 'none' &&
                    engineGuideResult.llamaDisplays.llama === '' &&
                    engineGuideResult.llamaDisplays.vllm === 'none',
                    'engine tab should show a selected-engine guide and hide non-selected engine fields'
                );

                const saveRestartCalls = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        currentAppView = 'inference';
                        activeInferenceTab = 'jobs';
                        inferenceProfilesData = [];
                        document.getElementById('profile-edit-id').value = 'qwen';
                        buildProfileDraft = function() {
                            return { engine_launcher_id: 'vllm-main', model: { artifact_id: 'qwen' } };
                        };
                        api = async function(method, path, body) {
                            calls.push([method, path, body && body.engine_launcher_id]);
                            return {
                                plan: { valid_for_save: true },
                                profile: { id: 'qwen', display_name: 'Qwen Saved', state: 'running' },
                            };
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
                        return {
                            calls,
                            profile: inferenceProfilesData.find(item => item.id === 'qwen'),
                        };
                    })()
                `, context);
                assert(
                    saveRestartCalls.calls.some(call => call[0] === 'PUT' && call[1] === '/api/inference/profiles/qwen') &&
                    !saveRestartCalls.calls.some(call => call[0] === 'refresh') &&
                    saveRestartCalls.calls.some(call => call[0] === 'action' && call[1] === 'qwen' && call[2] === 'restart') &&
                    saveRestartCalls.profile.display_name === 'Qwen Saved',
                    'Save & Restart should patch the saved profile locally and then queue a restart operation'
                );

                const launcherPatchResult = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        isMaster = false;
                        selectedNodeId = 'self-node';
                        currentAppView = 'inference';
                        activeInferenceTab = 'launchers';
                        inferenceLaunchersData = [];
                        document.getElementById('profile-engine').value = 'vllm';
                        refreshInferenceLaunchers = async function() { calls.push(['refreshLaunchers']); };
                        confirm = function(message) { calls.push(['confirm', message]); return true; };
                        api = async function(method, path, body) {
                            calls.push([method, path, body && body.display_name]);
                            if (method === 'POST' && path === '/api/inference/launchers') {
                                return {
                                    id: 'vllm-main',
                                    display_name: 'vLLM Main',
                                    engine: 'vllm',
                                    executable: '/opt/vllm/bin/python',
                                    base_args: ['-m', 'vllm.entrypoints.openai.api_server'],
                                    redacted_env_keys: [],
                                    env_count: 0,
                                };
                            }
                            if (method === 'PUT' && path === '/api/inference/launchers/vllm-main') {
                                return {
                                    id: 'vllm-main',
                                    display_name: 'vLLM Prod',
                                    engine: 'vllm',
                                    executable: '/opt/vllm/bin/python',
                                    base_args: ['-m', 'vllm.entrypoints.openai.api_server', '--disable-log-requests'],
                                    redacted_env_keys: [],
                                    env_count: 0,
                                };
                            }
                            if (method === 'DELETE' && path === '/api/inference/launchers/vllm-main') {
                                return { deleted: 'vllm-main', references: { running: [], stopped: [] } };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };

                        document.getElementById('launcher-edit-id').value = '';
                        document.getElementById('launcher-id').value = 'vllm-main';
                        document.getElementById('launcher-display-name').value = 'vLLM Main';
                        document.getElementById('launcher-engine').value = 'vllm';
                        document.getElementById('launcher-executable').value = '/opt/vllm/bin/python';
                        document.getElementById('launcher-working-dir').value = '';
                        await submitLauncherForm();
                        const afterCreateHtml = document.getElementById('launchers-list').innerHTML;
                        const afterCreateOptions = document.getElementById('profile-launcher').innerHTML;

                        document.getElementById('launcher-edit-id').value = 'vllm-main';
                        document.getElementById('launcher-id').value = 'vllm-main';
                        document.getElementById('launcher-display-name').value = 'vLLM Prod';
                        document.getElementById('launcher-engine').value = 'vllm';
                        document.getElementById('launcher-executable').value = '/opt/vllm/bin/python';
                        document.getElementById('launcher-working-dir').value = '';
                        await submitLauncherForm();
                        const afterUpdateHtml = document.getElementById('launchers-list').innerHTML;

                        await deleteLauncher('vllm-main', 'vLLM Prod');
                        return {
                            calls,
                            launchers: inferenceLaunchersData.map(item => item.id),
                            afterCreateHtml,
                            afterCreateOptions,
                            afterUpdateHtml,
                            afterDeleteHtml: document.getElementById('launchers-list').innerHTML,
                            afterDeleteOptions: document.getElementById('profile-launcher').innerHTML,
                        };
                    })()
                `, context);
                assert(
                    launcherPatchResult.calls.some(call => call[0] === 'POST' && call[1] === '/api/inference/launchers') &&
                    launcherPatchResult.calls.some(call => call[0] === 'PUT' && call[1] === '/api/inference/launchers/vllm-main') &&
                    launcherPatchResult.calls.some(call => call[0] === 'DELETE' && call[1] === '/api/inference/launchers/vllm-main') &&
                    !launcherPatchResult.calls.some(call => call[0] === 'refreshLaunchers') &&
                    launcherPatchResult.afterCreateHtml.includes('vLLM Main') &&
                    launcherPatchResult.afterCreateOptions.includes('vllm-main') &&
                    launcherPatchResult.afterUpdateHtml.includes('vLLM Prod') &&
                    launcherPatchResult.launchers.length === 0 &&
                    launcherPatchResult.afterDeleteHtml.includes('No engine launchers configured yet.') &&
                    !launcherPatchResult.afterDeleteOptions.includes('vllm-main'),
                    'launcher save/update/delete should patch local launcher state without reloading the launcher registry'
                );

                const launcherEnvMergeResult = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        const originalQuerySelectorAll = document.querySelectorAll;
                        function envRow(key, value) {
                            return {
                                querySelector(selector) {
                                    if (selector === '.launcher-env-key') return { value: key };
                                    if (selector === '.launcher-env-value') return { value };
                                    return null;
                                },
                            };
                        }
                        document.querySelectorAll = function(selector) {
                            if (selector === '.launcher-env-row') {
                                return [
                                    envRow('TOKEN', ''),
                                    envRow('VLLM_USE_V1', ''),
                                    envRow('LD_LIBRARY_PATH', '/opt/cuda/lib'),
                                ];
                            }
                            return originalQuerySelectorAll.call(document, selector);
                        };
                        const collected = collectLauncherEnv(true);
                        currentAppView = 'inference';
                        activeInferenceTab = 'launchers';
                        selectedNodeId = 'self-node';
                        isMaster = false;
                        inferenceLaunchersData = [{ id: 'vllm-main', display_name: 'vLLM Main', engine: 'vllm', executable: '/opt/vllm/bin/python', redacted_env_keys: ['TOKEN', 'VLLM_USE_V1'] }];
                        document.getElementById('launcher-edit-id').value = 'vllm-main';
                        document.getElementById('launcher-id').value = 'vllm-main';
                        document.getElementById('launcher-display-name').value = 'vLLM Main';
                        document.getElementById('launcher-engine').value = 'vllm';
                        document.getElementById('launcher-executable').value = '/opt/vllm/bin/python';
                        document.getElementById('launcher-working-dir').value = '';
                        api = async function(method, path, body) {
                            calls.push([method, path, body]);
                            if (method === 'PUT' && path === '/api/inference/launchers/vllm-main') {
                                return { id: 'vllm-main', display_name: 'vLLM Main', engine: 'vllm', executable: '/opt/vllm/bin/python', redacted_env_keys: ['TOKEN', 'VLLM_USE_V1'] };
                            }
                            if (method === 'POST' && path === '/api/inference/launchers/vllm-main/env') {
                                return { id: 'vllm-main', display_name: 'vLLM Main', engine: 'vllm', executable: '/opt/vllm/bin/python', redacted_env_keys: ['TOKEN', 'VLLM_USE_V1', 'LD_LIBRARY_PATH'] };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        try {
                            await submitLauncherForm();
                            return { calls, collected, launchers: inferenceLaunchersData };
                        } finally {
                            document.querySelectorAll = originalQuerySelectorAll;
                        }
                    })()
                `, context);
                assert(
                    launcherEnvMergeResult.collected.mode === 'merge' &&
                    launcherEnvMergeResult.collected.env.LD_LIBRARY_PATH === '/opt/cuda/lib' &&
                    !('TOKEN' in launcherEnvMergeResult.collected.env) &&
                    launcherEnvMergeResult.calls.some(call => call[0] === 'PUT' && call[1] === '/api/inference/launchers/vllm-main' && !('env' in call[2])) &&
                    launcherEnvMergeResult.calls.some(call => call[0] === 'POST' && call[1] === '/api/inference/launchers/vllm-main/env' && call[2].env.LD_LIBRARY_PATH === '/opt/cuda/lib') &&
                    launcherEnvMergeResult.launchers[0].redacted_env_keys.includes('LD_LIBRARY_PATH'),
                    'editing a launcher should merge entered env values without replacing hidden redacted env'
                );

                const launcherValidationSuggestionHtml = vm.runInContext(`
                    renderLauncherValidation({
                        launcher_id: 'vllm-main',
                        valid: false,
                        errors: ['Runtime dependency was found inside the venv; add the suggested launcher env and validate again.'],
                        executable: { path: '/home/aiml/vllm/venv/bin/vllm', exists: true, is_file: true, executable: true },
                        working_dir: null,
                        runtime: {
                            checked: true,
                            valid: false,
                            code: 7,
                            elapsed_ms: 123,
                            command_preview: ['/home/aiml/vllm/venv/bin/vllm', '--help'],
                            suggested_env: { LD_LIBRARY_PATH: '/home/aiml/vllm/venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib' },
                            output: 'ImportError: libcudart.so.12',
                        },
                    });
                `, context);
                assert(
                    launcherValidationSuggestionHtml.includes('Suggested Env') &&
                    launcherValidationSuggestionHtml.includes('Apply') &&
                    launcherValidationSuggestionHtml.includes('applyLauncherSuggestedEnv') &&
                    launcherValidationSuggestionHtml.includes('LD_LIBRARY_PATH') &&
                    launcherValidationSuggestionHtml.includes('nvidia/cuda_runtime/lib') &&
                    launcherValidationSuggestionHtml.includes('data-copy=') &&
                    launcherValidationSuggestionHtml.includes('copyText(this.dataset.copy, this)'),
                    'launcher validation should render suggested env values with copy controls'
                );

                const launcherValidationRecoveryHtml = vm.runInContext(`
                    inferenceProfilesData = [{ id: 'qwen', display_name: 'Qwen', state: 'failed', engine_launcher_id: 'vllm-main' }];
                    launcherValidationProfileContext = new Map([['vllm-main', 'qwen']]);
                    renderLauncherValidation({
                        launcher_id: 'vllm-main',
                        valid: true,
                        errors: [],
                        executable: { path: '/home/aiml/vllm/venv/bin/vllm', exists: true, is_file: true, executable: true },
                        working_dir: null,
                        runtime: {
                            checked: true,
                            valid: true,
                            code: 0,
                            elapsed_ms: 44,
                            command_preview: ['/home/aiml/vllm/venv/bin/vllm', '--help'],
                            output: '',
                        },
                    }, 'vllm-main');
                `, context);
                assert(
                    launcherValidationRecoveryHtml.includes('launcher-validation-recovery') &&
                    launcherValidationRecoveryHtml.includes('Recover Qwen') &&
                    launcherValidationRecoveryHtml.includes('runProfileAction(&quot;qwen&quot;') &&
                    launcherValidationRecoveryHtml.includes('Start profile') &&
                    launcherValidationRecoveryHtml.includes('loadProfileDetails(&quot;qwen&quot;)') &&
                    launcherValidationRecoveryHtml.includes('Back to profile'),
                    'launcher validation should offer direct recovery actions for the failed profile context'
                );

                const launcherApplyEnvResult = await vm.runInContext(`
                    (async () => {
                        const calls = [];
                        selectedNodeId = 'self-node';
                        isMaster = false;
                        currentAppView = 'inference';
                        activeInferenceTab = 'launchers';
                        const originalPatch = patchInferenceLauncher;
                        const originalValidate = validateLauncher;
                        patchInferenceLauncher = function(launcher) {
                            calls.push(['patch', launcher.id, launcher.redacted_env_keys && launcher.redacted_env_keys.join(',')]);
                            return true;
                        };
                        validateLauncher = async function(launcherId) { calls.push(['validate', launcherId]); };
                        setInferenceStatus = function(message) { calls.push(['status', message]); };
                        setInferenceError = function(message) { calls.push(['error', message]); };
                        api = async function(method, path, body) {
                            calls.push([method, path, body && body.env && body.env.LD_LIBRARY_PATH]);
                            if (method === 'POST' && path === '/api/inference/launchers/vllm-main/env') {
                                return { id: 'vllm-main', redacted_env_keys: ['LD_LIBRARY_PATH'] };
                            }
                            throw new Error('unexpected API call: ' + method + ' ' + path);
                        };
                        const button = {
                            dataset: { env: JSON.stringify({ LD_LIBRARY_PATH: '/opt/cuda/lib' }) },
                            disabled: false,
                            textContent: 'Apply',
                        };
                        try {
                            await applyLauncherSuggestedEnv('vllm-main', button);
                            return { calls, button };
                        } finally {
                            patchInferenceLauncher = originalPatch;
                            validateLauncher = originalValidate;
                        }
                    })()
                `, context);
                assert(
                    launcherApplyEnvResult.calls.some(call => call[0] === 'POST' && call[1] === '/api/inference/launchers/vllm-main/env' && call[2] === '/opt/cuda/lib') &&
                    launcherApplyEnvResult.calls.some(call => call[0] === 'patch' && call[1] === 'vllm-main') &&
                    launcherApplyEnvResult.calls.some(call => call[0] === 'validate' && call[1] === 'vllm-main') &&
                    launcherApplyEnvResult.calls.some(call => call[0] === 'status' && String(call[1]).includes('Applied suggested env')) &&
                    launcherApplyEnvResult.button.disabled === true,
                    'applying launcher suggested env should merge env, patch local launcher state, and revalidate'
                );
            })().catch((error) => {
                console.error(error.stack || error.message);
                process.exit(1);
            });
            """
        )
    )


def test_app_js_inference_node_selection_renders_cached_snapshot_immediately():
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
                    replaceWith(next) {
                        if (next && next.id) elements.set(next.id, next);
                    },
                };
            }

            const elements = new Map();
            const document = {
                cookie: '',
                activeElement: null,
                addEventListener() {},
                createElement(tagName = '') {
                    if (String(tagName).toLowerCase() === 'template') {
                        return {
                            _innerHTML: '',
                            content: { firstElementChild: null },
                            set innerHTML(value) {
                                this._innerHTML = String(value ?? '');
                                const match = this._innerHTML.match(/id="([^"]+)"/);
                                const child = makeElement(match ? match[1] : 'template-child');
                                child.innerHTML = this._innerHTML;
                                this.content.firstElementChild = child;
                            },
                            get innerHTML() { return this._innerHTML; },
                        };
                    }
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
                AbortController,
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
                    let workerAOverviewLoads = 0;
                    const overviewA = {
                        profiles: {
                            profiles: [{
                                id: 'qwen-a',
                                display_name: 'Qwen A',
                                engine: 'vllm',
                                engine_launcher_id: 'vllm-main',
                                model: { artifact_id: 'qwen-a', snapshot: 'v1' },
                                common: { context_length: 4096 },
                                deployment: {},
                                exposure: { mode: 'local' },
                                instances: [],
                                state: 'stopped',
                            }],
                        },
                        models: { artifacts: [{ id: 'qwen-a', display_name: 'Qwen A', active_snapshot: 'v1' }], jobs: [] },
                        launchers: { launchers: [{ id: 'vllm-main', display_name: 'vLLM', engine: 'vllm' }] },
                        operations: { operations: [] },
                        system: { gpus: [] },
                        partial_errors: {},
                    };

                    isMaster = true;
                    nodeRole = 'master';
                    selfNodeId = 'master';
                    selectedNodeId = 'worker-a';
                    currentAppView = 'inference';
                    activeInferenceTab = 'profiles';
                    nodes = [
                        { node_id: 'worker-a', node_name: 'Worker A', status: 'online' },
                        { node_id: 'worker-b', node_name: 'Worker B', status: 'online' },
                    ];
                    wsConnected = true;
                    api = async function(method, path) {
                        calls.push([method, path]);
                        if (method === 'GET' && path === '/api/nodes/worker-a/inference/overview') {
                            workerAOverviewLoads += 1;
                            if (workerAOverviewLoads === 1) return overviewA;
                            return new Promise(() => {});
                        }
                        if (method === 'GET' && path === '/api/nodes/worker-b/inference/overview') {
                            return new Promise(() => {});
                        }
                        throw new Error('unexpected API call: ' + method + ' ' + path);
                    };

                    await refreshInferenceProfiles();
                    if (!document.getElementById('inference-profiles-list').innerHTML.includes('Qwen A')) {
                        throw new Error('initial overview should render worker A profile');
                    }

                    selectNode('worker-b');
                    if (!document.getElementById('inference-profiles-list').innerHTML.includes('Loading profiles')) {
                        throw new Error('uncached worker should show a pending state while loading');
                    }

                    selectNode('worker-a');
                    const profileHtml = document.getElementById('inference-profiles-list').innerHTML;
                    const status = document.getElementById('inference-status').textContent;
                    if (!profileHtml.includes('Qwen A')) {
                        throw new Error('cached worker profile should render immediately before fresh overview resolves');
                    }
                    if (!status.includes('Showing cached state')) {
                        throw new Error('cached render should disclose that a fresh refresh is in progress');
                    }
                    const workerACalls = calls.filter(call => call[1] === '/api/nodes/worker-a/inference/overview').length;
                    if (workerACalls !== 2) {
                        throw new Error('switching back should still start a fresh background overview request');
                    }

                    abortActiveInferenceRefresh();
                    calls.length = 0;
                    let abortedStaleRefresh = false;
                    activeInferenceTab = 'profiles';
                    selectedNodeId = 'worker-a';
                    api = async function(method, path, body, extraHeaders, options = {}) {
                        calls.push([method, path]);
                        if (method === 'GET' && path === '/api/nodes/worker-a/inference/overview') {
                            return new Promise((_resolve, reject) => {
                                if (!options.signal) throw new Error('overview refresh should receive an abort signal');
                                options.signal.addEventListener('abort', () => {
                                    abortedStaleRefresh = true;
                                    const error = new Error('aborted');
                                    error.name = 'AbortError';
                                    reject(error);
                                });
                            });
                        }
                        if (method === 'GET' && path === '/api/nodes/worker-a/models') {
                            return { artifacts: [], jobs: [] };
                        }
                        if (method === 'GET' && path === '/api/nodes/worker-a/models/storage') {
                            return { root: '/models', disk: null };
                        }
                        throw new Error('unexpected API call during abort check: ' + method + ' ' + path);
                    };

                    const staleRefresh = refreshInferenceProfiles();
                    await Promise.resolve();
                    activeInferenceTab = 'models';
                    await refreshInferenceModels();
                    await staleRefresh;
                    if (!abortedStaleRefresh) {
                        throw new Error('starting a newer active-tab refresh should abort the stale one');
                    }
                    if (document.getElementById('inference-error').textContent.includes('aborted')) {
                        throw new Error('aborted stale refreshes should not be shown as UI errors');
                    }
                })().catch((error) => {
                    console.error(error.stack || error.message);
                    process.exit(1);
                });
            `, context);
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
                    replaceWith(next) {
                        if (next && next.id) elements.set(next.id, next);
                    },
                };
            }

            const elements = new Map();
            const document = {
                cookie: '',
                addEventListener() {},
                createElement(tagName = '') {
                    if (String(tagName).toLowerCase() === 'template') {
                        return {
                            _innerHTML: '',
                            content: { firstElementChild: null },
                            set innerHTML(value) {
                                this._innerHTML = String(value ?? '');
                                const match = this._innerHTML.match(/id="([^"]+)"/);
                                const child = makeElement(match ? match[1] : 'template-child');
                                child.innerHTML = this._innerHTML;
                                this.content.firstElementChild = child;
                            },
                            get innerHTML() { return this._innerHTML; },
                        };
                    }
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
                    const liveStatusEl = document.getElementById('inference-live-status');
                    assert(wsConnected === false, 'websocket close should mark the socket disconnected');
                    assert(activeTimerId !== null, 'websocket close should start fallback polling for active operations');
                    assert(
                        calls.some(call => call[0] === 'setInterval' && call[1] === activeTimerId && call[2] === 2500),
                        'fallback polling should run on the inference activity cadence'
                    );
                    assert(
                        liveStatusEl.className.includes('yellow') &&
                        liveStatusEl.innerHTML.includes('Fallback sync') &&
                        liveStatusEl.innerHTML.includes('Event stream reconnecting'),
                        'websocket close with active work should show fallback sync status'
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
                    assert(
                        liveStatusEl.innerHTML.includes('Event stream') &&
                        liveStatusEl.innerHTML.includes('Idle'),
                        'terminal fallback refresh should return the live status to idle when no work remains'
                    );

                    calls.length = 0;
                    activeInferenceTab = 'profiles';
                    inferenceProfilesData = [{
                        id: 'qwen',
                        display_name: 'Qwen',
                        state: 'starting',
                        instances: [{ index: 0, state: 'starting' }],
                    }];
                    inferenceOperationsData = [{ id: 'op-profile', kind: 'profile_start', state: 'running', profile_id: 'qwen' }];
                    refreshActiveInferenceTab = async function() {
                        calls.push(['refreshActiveInferenceTab']);
                    };
                    renderInferenceProfiles = function(profiles) {
                        calls.push(['renderProfiles', profiles[0] && profiles[0].state]);
                    };
                    updateInferenceProfileCard = function(profileId) {
                        const profile = profileById(profileId);
                        calls.push(['updateCard', profileId, profile && profile.state]);
                    };
                    renderInferenceOperations = function(operations) {
                        calls.push(['renderOperations', operations[0] && operations[0].state]);
                    };
                    api = async function(method, path) {
                        calls.push(['api', method, path]);
                        if (path === '/api/inference/operations') {
                            return {
                                operations: [{
                                    id: 'op-profile',
                                    kind: 'profile_start',
                                    state: 'succeeded',
                                    profile_id: 'qwen',
                                    result: { state: 'running', instances: [{ index: 0 }] },
                                }],
                            };
                        }
                        throw new Error('unexpected API call: ' + method + ' ' + path);
                    };
                    await refreshInferenceActivity();
                    assert(
                        !calls.some(call => call[0] === 'refreshActiveInferenceTab'),
                        'fallback polling on Profiles should not run the broad active-tab refresh'
                    );
                    assert(
                        inferenceProfilesData[0].state === 'running' &&
                        inferenceProfilesData[0].instances[0].state === 'running',
                        'fallback polling on Profiles should patch terminal operation state locally'
                    );
                    assert(
                        !calls.some(call => call[0] === 'renderProfiles') &&
                        calls.some(call => call[0] === 'updateCard' && call[1] === 'qwen' && call[2] === 'running'),
                        'fallback polling on Profiles should update only the patched profile card'
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
                        liveStatusEl.className.includes('green') &&
                        liveStatusEl.innerHTML.includes('Live events'),
                        'websocket reconnect should show live event status'
                    );
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
    assert 'id="profile-port-policy"' in index_html
    assert 'id="profile-ports"' in index_html
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
    assert 'id="profile-engine-guide"' in index_html
    assert 'class="profile-engine-details" open' in index_html
    assert "PROFILE_ENGINE_GUIDES" in app_js
    assert "setProfileEditorSection" in app_js
    assert "profileIssueSection" in app_js
    assert "updateProfileEditorIssueBadges" in app_js
    assert "renderProfilePreviewIssues" in app_js
    assert "groupProfileIssues" in app_js
    assert "PROFILE_EDITOR_SECTION_LABELS" in app_js
    assert "markProfilePreviewStale" in app_js
    assert "resetProfilePreviewPanel" in app_js
    assert 'id="profile-gpu-hints"' in index_html
    assert 'id="profile-kv-cache-dtype"' in index_html
    assert 'id="profile-gpu-memory-utilization"' in index_html
    assert 'id="profile-expert-parallel"' in index_html
    assert 'id="profile-max-concurrent"' in index_html
    assert 'id="profile-reasoning-parser"' in index_html
    assert 'id="profile-vllm-all2all-backend"' in index_html
    assert 'id="profile-sglang-moe-a2a-backend"' in index_html
    assert 'id="profile-llama-tensor-split"' in index_html
    assert 'id="profile-metrics"' in index_html
    assert 'id="profile-lora-enabled"' in index_html
    assert 'id="profile-lora-paths"' in index_html
    assert "lora" in app_js
    assert 'id="inference-profiles-list"' in index_html
    assert 'id="inference-operations-list"' in index_html
    assert 'id="inference-live-status"' in index_html
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
    assert "/api/inference/overview" in app_js
    assert "/api/inference/profiles" in app_js
    assert "/api/inference/launchers" in app_js
    assert "/api/inference/operations" in app_js
    assert "client-bundles" in app_js
    assert "cloudflare/service-tokens" in app_js
    assert "cloudflare/exposure" in app_js
    assert "/api/inference/profiles/${encodeURIComponent(profileId)}/detail" in app_js
    assert "loadProfileDetails" in app_js
    assert "runInstanceAction" in app_js
    assert "runProfileTest" in app_js
    assert "exportInferenceProfile" in app_js
    assert "profileCanSaveRestart" in app_js
    assert "saveInferenceProfile({restart:true})" in index_html
    assert "/api/inference/profiles/${encodeURIComponent(profileId)}/export" in app_js
    assert "patchInferenceProfile" in app_js
    assert "removeInferenceProfile" in app_js
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
    assert "renderInferenceLiveStatus" in app_js
    assert "markInferenceLiveEvent" in app_js
    assert "markInferenceFallbackSync" in app_js
    assert "normalizeInferenceOperationResponse" in app_js
    assert "openLauncherValidation" in app_js
    assert "launcherValidationProfileContext" in app_js
    assert "renderLauncherValidationRecovery" in app_js
    assert "websocketEventMatchesSelectedNode" in app_js
    assert "mergeInferenceOperationSnapshot" in app_js
    assert "selectedInferenceNodeIds" in app_js
    assert "renderProfileOperationPanel" in app_js
    assert "operationLogButton" in app_js
    assert "operationLogOutputCache" in app_js
    assert "loadOperationLogs" in app_js
    assert "renderOperationReadiness" in app_js
    assert "renderOperationLiveLog" in app_js
    assert "profileLogRequest" in app_js
    assert "hydrateInferenceFailureDiagnostics" in app_js
    assert "hydrateVisibleInferenceFailures" in app_js
    assert "operationWithHydratedLogs" in app_js
    assert "operationRuntimeStatus" in app_js
    assert "patchProfileFromOperation" in app_js
    assert "renderProfileCard" in app_js
    assert "updateInferenceProfileCard" in app_js
    assert "replaceWith(next)" in app_js
    assert "profile-action-group operate" in app_js
    assert "profile-action-group inspect" in app_js
    assert "profile-action-group manage" in app_js
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
    assert "mergeModelArtifactFromJob" in app_js
    assert "refreshInferenceModelSnapshot" in app_js
    assert "renderPolledModelState" in app_js
    assert "patchInferenceLauncher" in app_js
    assert "removeInferenceLauncher" in app_js
    assert "removeModelArtifactLocal" in app_js
    assert "patchModelVerification" in app_js
    assert "profileJsonValue" in app_js
    assert "parseProfilePorts" in app_js
    assert "buildProfilePortPolicy" in app_js
    assert "syncProfilePortPolicyFields" in app_js
    assert "renderInferenceGpuHints" in app_js
    assert "profileConfigChips" in app_js
    assert "structuredCommonConfig" in app_js
    assert "enable_metrics" in app_js
    assert "structuredEngineConfig" in app_js
    assert "draft = buildProfileDraft()" in app_js
    assert "setInferenceError(e.message)" in app_js
    assert "profile-speculative-model" in index_html
    assert "profile-log-level" in index_html
    assert "profile-vllm-distributed-executor" in index_html
    assert "profile-vllm-dp-backend" in index_html
    assert "profile-vllm-dp-rank" in index_html
    assert "profile-vllm-dp-lb-mode" in index_html
    assert "profile-vllm-headless" in index_html
    assert "profile-vllm-decode-cp-size" in index_html
    assert "profile-vllm-prefill-cp-size" in index_html
    assert "profile-vllm-kv-offloading-size" in index_html
    assert "profile-vllm-compilation-config" in index_html
    assert "profile-vllm-ep-weight-filter" in index_html
    assert "profile-sglang-sampling-defaults" in index_html
    assert "profile-sglang-hf-chat-template-name" in index_html
    assert "profile-sglang-dist-init-addr" in index_html
    assert "profile-sglang-nnodes" in index_html
    assert "profile-sglang-node-rank" in index_html
    assert "profile-sglang-cuda-graph-config" in index_html
    assert "profile-sglang-hicache" in index_html
    assert "distributed_executor_backend" in app_js
    assert "data_parallel_backend" in app_js
    assert "data_parallel_rank" in app_js
    assert "data_parallel_lb_mode" in app_js
    assert "decode_context_parallel_size" in app_js
    assert "prefill_context_parallel_size" in app_js
    assert "max_queued_requests" in app_js
    assert "hf_chat_template_name" in app_js
    assert "dist_init_addr" in app_js
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
    assert "renderLauncherValidationSuggestions" in app_js
    assert "applyLauncherSuggestedEnv" in app_js
    assert "/api/inference/launchers/${encodeURIComponent(launcherId)}/env" in app_js
    assert "cleanModelJobStaging" in app_js
    assert "modelJobStagingCleaned" in app_js
    assert "showStartedModelJob" in app_js
    assert ".model-job-badges" in style_css
    assert ".profile-missing-secrets" in style_css
    assert ".missing-secret-row" in style_css
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
    assert ".launcher-validation-suggestions" in style_css
    assert ".launcher-validation-suggestions-head" in style_css
    assert ".launcher-validation-recovery" in style_css
    assert ".profile-card" in style_css
    assert ".inference-live-status" in style_css
    assert ".inference-header-controls" in style_css
    assert ".profile-action-bar" in style_css
    assert ".profile-action-group" in style_css
    assert ".profile-action-buttons" in style_css
    assert ".profile-editor-nav" in style_css
    assert ".profile-editor-tab.active" in style_css
    assert ".profile-editor-tab.has-blockers" in style_css
    assert ".profile-editor-issue-badge" in style_css
    assert ".profile-editor-section.active" in style_css
    assert ".profile-detail-panel" in style_css
    assert ".profile-operation-panel" in style_css
    assert ".profile-operation-actions" in style_css
    assert ".profile-operation-steps" in style_css
    assert ".profile-operation-steps-title" in style_css
    assert ".profile-operation-step-dot" in style_css
    assert ".profile-operation-step.current" in style_css
    assert ".profile-operation-facts" in style_css
    assert ".profile-operation-narrative" in style_css
    assert ".profile-operation-diagnosis" in style_css
    assert ".profile-operation-fix-action" in style_css
    assert ".profile-operation-readiness" in style_css
    assert ".profile-live-log" in style_css
    assert ".profile-operation-log-output" in style_css
    assert ".inference-operation-context" in style_css
    assert "renderProfileOperationPanel(op, '', { context: 'operations' })" in app_js
    assert ".profile-instance-row" in style_css
    assert ".profile-test-panel" in style_css
    assert ".profile-config-chips" in style_css
    assert ".profile-gpu-chip" in style_css
    assert ".profile-engine-guide" in style_css
    assert ".profile-engine-guide-chips" in style_css
    assert ".profile-engine-details" in style_css
    assert ".form-check-grid" in style_css
    assert ".profile-preview-panel" in style_css
    assert ".profile-preview-stale" in style_css
    assert ".profile-preview-issues" in style_css
    assert ".profile-preview-issue-group" in style_css
    assert ".profile-preview-issue-head" in style_css
    assert ".profile-preview-facts" in style_css
    assert ".profile-preview-resource-grid" in style_css
    assert ".profile-command-env" in style_css
    assert ".profile-connect-panel" in style_css
    assert ".connect-posture-panel" in style_css
    assert ".connect-posture-grid" in style_css
    assert ".connect-posture-item" in style_css
    assert ".credential-chain-panel" in style_css
    assert ".credential-chain-grid" in style_css
    assert ".credential-chain-item" in style_css
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
    assert ".profile-cf-removal-note" in style_css


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
