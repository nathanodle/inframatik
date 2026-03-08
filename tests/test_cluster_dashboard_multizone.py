"""Tests for multi-zone dashboard Cloudflare access flow."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import HTTPException

import cluster_routes
import tunnel


class _Patch:
    def __init__(self, patches):
        self._patches = patches
        self._originals = []

    def __enter__(self):
        for obj, attr, value in self._patches:
            self._originals.append((obj, attr, getattr(obj, attr)))
            setattr(obj, attr, value)
        return self

    def __exit__(self, exc_type, exc, tb):
        for obj, attr, old in reversed(self._originals):
            setattr(obj, attr, old)
        return False


def _run(coro):
    return asyncio.run(coro)


def _assert_raises_async(exc_type, coro_fn, *args, **kwargs):
    try:
        _run(coro_fn(*args, **kwargs))
    except exc_type as e:
        return e
    raise AssertionError(f"Expected {exc_type.__name__} from {coro_fn.__name__}")


def test_enable_dashboard_access_with_subdomain_and_selected_zone():
    seen = {}

    async def fake_add(hostname, service, tunnel_id=None):
        seen["route"] = (hostname, service, tunnel_id)

    async def fake_dns(hostname, tunnel_id=None, zone_id=None, zone_name=None):
        seen["dns"] = (hostname, tunnel_id, zone_id, zone_name)
        return "dns-1"

    async def fake_access(name, hostname, policy_id):
        seen["access"] = (name, hostname, policy_id)
        return "app-1"

    async def fake_zones():
        return [
            {"id": "z1", "name": "example.com"},
            {"id": "z2", "name": "example.net"},
        ]

    with _Patch(
        [
            (cluster_routes, "get_node_config", lambda: {"node_name": "node-1", "tunnel_id": "tid-1"}),
            (cluster_routes, "set_dashboard_hostname", lambda h, zone_id=None, zone_name=None: seen.setdefault("saved", (h, zone_id, zone_name))),
            (tunnel, "_load_cf_config", lambda: {"token": "tok", "account_id": "acct", "zone_id": "z1", "default_policy_id": "pol-1"}),
            (tunnel, "list_available_zones", fake_zones),
            (tunnel, "add_tunnel_route", fake_add),
            (tunnel, "create_dns_record", fake_dns),
            (tunnel, "create_access_app", fake_access),
        ]
    ):
        body = cluster_routes.DashboardAccessBody(subdomain="dash", zone_id="z2")
        result = _run(cluster_routes.config_enable_dashboard_access(body))

    assert result["status"] == "enabled"
    assert result["hostname"] == "dash.example.net"
    assert result["zone_id"] == "z2"
    assert result["zone_name"] == "example.net"
    assert seen["route"] == ("dash.example.net", "http://localhost:9000", "tid-1")
    assert seen["dns"] == ("dash.example.net", "tid-1", "z2", "example.net")
    assert seen["access"] == ("inframatik dashboard", "dash.example.net", "pol-1")
    assert seen["saved"] == ("dash.example.net", "z2", "example.net")


def test_enable_dashboard_access_rejects_unknown_zone():
    async def fake_zones():
        return [{"id": "z1", "name": "example.com"}]

    with _Patch(
        [
            (cluster_routes, "get_node_config", lambda: {"node_name": "node-1", "tunnel_id": "tid-1"}),
            (tunnel, "_load_cf_config", lambda: {"token": "tok", "account_id": "acct", "zone_id": "z1", "default_policy_id": None}),
            (tunnel, "list_available_zones", fake_zones),
        ]
    ):
        body = cluster_routes.DashboardAccessBody(subdomain="dash", zone_id="z9")
        exc = _assert_raises_async(HTTPException, cluster_routes.config_enable_dashboard_access, body)

    assert exc.status_code == 400
    assert "Selected Cloudflare domain is not available" in str(exc.detail)


def test_enable_dashboard_access_rejects_hostname_outside_selected_zone():
    async def fake_zones():
        return [{"id": "z1", "name": "example.com"}]

    with _Patch(
        [
            (cluster_routes, "get_node_config", lambda: {"node_name": "node-1", "tunnel_id": "tid-1"}),
            (tunnel, "_load_cf_config", lambda: {"token": "tok", "account_id": "acct", "zone_id": "z1", "default_policy_id": None}),
            (tunnel, "list_available_zones", fake_zones),
        ]
    ):
        body = cluster_routes.DashboardAccessBody(hostname="dash.other.net", zone_id="z1")
        exc = _assert_raises_async(HTTPException, cluster_routes.config_enable_dashboard_access, body)

    assert exc.status_code == 400
    assert "Hostname must be under selected domain" in str(exc.detail)


def test_enable_dashboard_access_uses_default_cf_zone_for_legacy_hostname():
    seen = {}

    async def fake_zones():
        return [
            {"id": "z1", "name": "example.com"},
            {"id": "z2", "name": "example.net"},
        ]

    async def fake_add(hostname, service, tunnel_id=None):
        seen["route"] = (hostname, service, tunnel_id)

    async def fake_dns(hostname, tunnel_id=None, zone_id=None, zone_name=None):
        seen["dns"] = (hostname, tunnel_id, zone_id, zone_name)
        return "dns-1"

    with _Patch(
        [
            (cluster_routes, "get_node_config", lambda: {"node_name": "node-1", "tunnel_id": "tid-1"}),
            (cluster_routes, "set_dashboard_hostname", lambda h, zone_id=None, zone_name=None: seen.setdefault("saved", (h, zone_id, zone_name))),
            (tunnel, "_load_cf_config", lambda: {"token": "tok", "account_id": "acct", "zone_id": "z1", "default_policy_id": None}),
            (tunnel, "list_available_zones", fake_zones),
            (tunnel, "add_tunnel_route", fake_add),
            (tunnel, "create_dns_record", fake_dns),
            (tunnel, "create_access_app", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not create access app"))),
        ]
    ):
        body = cluster_routes.DashboardAccessBody(hostname="dash.example.com")
        result = _run(cluster_routes.config_enable_dashboard_access(body))

    assert result["hostname"] == "dash.example.com"
    assert result["zone_id"] == "z1"
    assert seen["dns"] == ("dash.example.com", "tid-1", "z1", "example.com")
    assert seen["saved"] == ("dash.example.com", "z1", "example.com")


def test_disable_dashboard_access_uses_saved_zone_id_for_dns_cleanup():
    seen = {}

    async def fake_remove_route(hostname):
        seen["route"] = hostname

    async def fake_delete_dns(hostname, zone_id=None):
        seen["dns"] = (hostname, zone_id)
        return True

    async def fake_delete_access(hostname):
        seen["access"] = hostname
        return True

    with _Patch(
        [
            (
                cluster_routes,
                "get_node_config",
                lambda: {
                    "dashboard_hostname": "dash.example.net",
                    "dashboard_zone_id": "z2",
                },
            ),
            (cluster_routes, "set_dashboard_hostname", lambda h, zone_id=None, zone_name=None: seen.setdefault("saved", (h, zone_id, zone_name))),
            (tunnel, "remove_tunnel_route", fake_remove_route),
            (tunnel, "delete_dns_record", fake_delete_dns),
            (tunnel, "delete_access_app", fake_delete_access),
        ]
    ):
        result = _run(cluster_routes.config_disable_dashboard_access())

    assert result == {"status": "disabled"}
    assert seen["route"] == "dash.example.net"
    assert seen["dns"] == ("dash.example.net", "z2")
    assert seen["access"] == "dash.example.net"
    assert seen["saved"] == (None, None, None)


def run_tests():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    print("Running cluster dashboard multi-zone tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
