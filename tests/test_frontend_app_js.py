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
                assert(worker.shouldShowLocalCfSection === false, 'worker should not show local CF section');
                assert(worker.cfSectionLoaded === false, 'worker refresh loop should not load CF section');
                assert(
                    !worker.calls.api.some((call) => call[1].startsWith('/api/cf/')),
                    'worker should not load local Cloudflare APIs'
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
            })().catch((error) => {
                console.error(error.stack || error.message);
                process.exit(1);
            });
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


def test_worker_enrollment_ui_uses_same_origin_backend_endpoint():
    app_js = (ROOT / "static" / "app.js").read_text()
    index_html = (ROOT / "static" / "index.html").read_text()

    assert "/api/config/enroll-worker" in app_js
    assert "/api/nodes/enroll" not in app_js
    assert "worker-enroll" in app_js
    assert "setup-worker-progress" in index_html
    assert "init-worker-progress" in index_html


if __name__ == "__main__":
    print("Running frontend app.js tests...\n")
    test_app_js_cloudflare_section_gating_by_role()
    test_static_index_contains_setup_guidance_and_empty_state_copy()
    test_worker_enrollment_ui_uses_same_origin_backend_endpoint()
    print("ok")
