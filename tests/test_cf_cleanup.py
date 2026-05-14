"""Tests for the Cloudflare uninstall cleanup helper."""

import contextlib
import io
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import cf_cleanup


class _UrlopenResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def _run_main(config: dict, services: dict | None = None, *, answer: str = "y"):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        node_path = tmpdir_path / "node.json"
        node_path.write_text(json.dumps(config))
        if services is not None:
            (tmpdir_path / "services.json").write_text(json.dumps(services))

        calls = []

        def fake_cf_request(method, url, token, body=None):
            calls.append((method, url, token, body))
            base = "https://api.cloudflare.com/client/v4"

            if method == "GET" and url == f"{base}/accounts/acct/cfd_tunnel/tun-1/configurations":
                return {
                    "success": True,
                    "result": {
                        "config": {
                            "ingress": [
                                {"hostname": "route.example.com"},
                                {"service": "http_status:404"},
                            ]
                        }
                    },
                }
            if method == "GET" and url == f"{base}/zones/zone/dns_records?type=CNAME&per_page=100":
                return {
                    "success": True,
                    "result": [
                        {"id": "dns-app", "name": "app.example.com"},
                        {"id": "dns-route", "name": "route.example.com"},
                        {"id": "dns-other", "name": "other.example.com"},
                    ],
                }
            if method == "GET" and url == f"{base}/accounts/acct/access/apps?per_page=100":
                return {
                    "success": True,
                    "result": [
                        {"id": "app-1", "name": "App", "domain": "app.example.com"},
                        {"id": "app-2", "name": "Keep", "domain": "keep.example.com", "policies": [{"id": "pol-shared"}]},
                        {"id": "app-3", "name": "Dashboard", "domain": "dash.example.com"},
                    ],
                }
            if method == "GET" and url == f"{base}/accounts/acct/access/apps/app-1":
                return {
                    "success": True,
                    "result": {
                        "policies": [
                            {"id": "pol-app", "name": "App policy"},
                            {"id": "pol-shared", "name": "Shared policy"},
                        ]
                    },
                }
            if method == "GET" and url == f"{base}/accounts/acct/access/apps/app-3":
                return {
                    "success": True,
                    "result": {"policies": [{"id": "pol-dash", "name": "Dashboard policy"}]},
                }
            if method == "DELETE":
                return {"success": True}
            raise AssertionError(f"Unexpected CF request: {method} {url}")

        output = io.StringIO()
        original_argv = sys.argv
        original_cf_request = cf_cleanup._cf_request
        original_input = cf_cleanup.input if hasattr(cf_cleanup, "input") else None
        original_sleep = None
        sys.argv = ["cf_cleanup.py", str(node_path)]
        cf_cleanup._cf_request = fake_cf_request
        cf_cleanup.input = lambda _prompt="": answer
        try:
            import time

            original_sleep = time.sleep
            time.sleep = lambda _seconds: None
            with contextlib.redirect_stdout(output):
                cf_cleanup.main()
        finally:
            if original_sleep is not None:
                import time

                time.sleep = original_sleep
            if original_input is None:
                delattr(cf_cleanup, "input")
            else:
                cf_cleanup.input = original_input
            cf_cleanup._cf_request = original_cf_request
            sys.argv = original_argv

        return output.getvalue(), calls


def test_cf_request_encodes_json_body_and_parses_success_response():
    seen = {}

    def fake_urlopen(req):
        seen["method"] = req.get_method()
        seen["url"] = req.full_url
        seen["auth"] = req.headers["Authorization"]
        seen["content_type"] = req.headers["Content-type"]
        seen["body"] = json.loads(req.data.decode())
        return _UrlopenResponse({"success": True, "result": {"id": "ok"}})

    original_urlopen = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        result = cf_cleanup._cf_request(
            "POST",
            "https://api.cloudflare.com/client/v4/example",
            "tok",
            {"name": "demo"},
        )
    finally:
        urllib.request.urlopen = original_urlopen

    assert result == {"success": True, "result": {"id": "ok"}}
    assert seen == {
        "method": "POST",
        "url": "https://api.cloudflare.com/client/v4/example",
        "auth": "Bearer tok",
        "content_type": "application/json",
        "body": {"name": "demo"},
    }


def test_cf_request_returns_cloudflare_json_error_response():
    def fake_urlopen(_req):
        raise urllib.error.HTTPError(
            "https://api.cloudflare.com/client/v4/example",
            403,
            "Forbidden",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"success": False, "errors": [{"message": "denied"}]}).encode()),
        )

    original_urlopen = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        result = cf_cleanup._cf_request(
            "DELETE",
            "https://api.cloudflare.com/client/v4/example",
            "tok",
        )
    finally:
        urllib.request.urlopen = original_urlopen

    assert result == {"success": False, "errors": [{"message": "denied"}]}


def test_cf_cleanup_exits_when_config_has_no_cloudflare_credentials():
    output, calls = _run_main({"node_id": "n1"}, services={})

    assert "No Cloudflare credentials in config" in output
    assert calls == []


def test_cf_cleanup_deletes_only_owned_resources_and_unused_policies():
    output, calls = _run_main(
        {
            "cf_token": "tok",
            "cf_account_id": "acct",
            "cf_zone_id": "zone",
            "tunnel_id": "tun-1",
            "dashboard_hostname": "dash.example.com",
        },
        services={"svc": {"hostname": "app.example.com"}},
        answer="yes",
    )

    delete_urls = [url for method, url, _token, _body in calls if method == "DELETE"]

    assert "Tunnel:  tun-1" in output
    assert "app.example.com" in output
    assert "route.example.com" in output
    assert "Dashboard (dash.example.com)" in output
    assert "App policy" in output
    assert "Dashboard policy" in output
    assert "Shared policy" not in output

    assert delete_urls == [
        "https://api.cloudflare.com/client/v4/accounts/acct/access/apps/app-1",
        "https://api.cloudflare.com/client/v4/accounts/acct/access/apps/app-3",
        "https://api.cloudflare.com/client/v4/zones/zone/dns_records/dns-app",
        "https://api.cloudflare.com/client/v4/zones/zone/dns_records/dns-route",
        "https://api.cloudflare.com/client/v4/accounts/acct/access/policies/pol-app",
        "https://api.cloudflare.com/client/v4/accounts/acct/access/policies/pol-dash",
        "https://api.cloudflare.com/client/v4/accounts/acct/cfd_tunnel/tun-1/connections",
        "https://api.cloudflare.com/client/v4/accounts/acct/cfd_tunnel/tun-1",
    ]
    assert {token for _method, _url, token, _body in calls} == {"tok"}


def test_cf_cleanup_deletes_tunnel_without_dns_or_hostnames():
    output, calls = _run_main(
        {
            "cf_token": "tok",
            "cf_account_id": "acct",
            "tunnel_id": "tun-1",
        },
        services={},
        answer="yes",
    )

    delete_urls = [url for method, url, _token, _body in calls if method == "DELETE"]
    assert "Tunnel:  tun-1" in output
    assert delete_urls == [
        "https://api.cloudflare.com/client/v4/accounts/acct/cfd_tunnel/tun-1/connections",
        "https://api.cloudflare.com/client/v4/accounts/acct/cfd_tunnel/tun-1",
    ]


def test_cf_cleanup_confirmation_decline_skips_deletions():
    output, calls = _run_main(
        {
            "cf_token": "tok",
            "cf_account_id": "acct",
            "cf_zone_id": "zone",
            "tunnel_id": "tun-1",
        },
        services={"svc": {"hostname": "app.example.com"}},
        answer="no",
    )

    assert "Skipped." in output
    assert not any(method == "DELETE" for method, _url, _token, _body in calls)


if __name__ == "__main__":
    print("Running Cloudflare cleanup tests...\n")
    tests = [
        test_cf_request_encodes_json_body_and_parses_success_response,
        test_cf_request_returns_cloudflare_json_error_response,
        test_cf_cleanup_exits_when_config_has_no_cloudflare_credentials,
        test_cf_cleanup_deletes_only_owned_resources_and_unused_policies,
        test_cf_cleanup_deletes_tunnel_without_dns_or_hostnames,
        test_cf_cleanup_confirmation_decline_skips_deletions,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {test.__name__}: {e}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)
