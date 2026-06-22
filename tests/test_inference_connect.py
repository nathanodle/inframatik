import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference_connect
import inference_profiles
import node_config
import tunnel
from test_inference_profiles import _Patch, _profile, _setup_launcher, _temp_inference, _write_model


def _run(coro):
    return asyncio.run(coro)


def _configure_cf():
    config = node_config.get_node_config()
    config.update({
        "cf_token": "cf-token",
        "cf_account_id": "acc-1",
        "cf_zone_id": "zone-1",
        "tunnel_id": "tun-1",
    })
    node_config.save_node_config(config)


def test_engine_api_key_is_one_time_and_units_get_raw_key(tmp_path: Path):
    with _temp_inference(tmp_path) as ctx:
        _write_model(ctx["store"])
        _setup_launcher(tmp_path)
        inference_profiles.create_profile(_profile())

        result = inference_connect.rotate_engine_api_key("qwen", {"render_bundle": True})
        raw_key = result["engine_api_key"]
        unit = (ctx["unit_dir"] / "infra-llm-qwen.service").read_text()
        stored = json.loads(ctx["profiles_file"].read_text())["profiles"]["qwen"]
        secrets = json.loads((ctx["config_dir"] / "inference_secrets.json").read_text())

        assert raw_key.startswith("llm_")
        assert result["one_time_secret"] is True
        assert "--api-key" in unit
        assert raw_key in unit
        assert stored["secrets"]["engine_api_key_id"] == "engine-api-key-qwen"
        assert secrets["secrets"]["engine-api-key-qwen"]["value"] == raw_key
        assert raw_key not in json.dumps(result["profile"])
        assert result["client_bundle"]["headers"]["Authorization"] == f"Bearer {raw_key}"

        disabled = inference_connect.disable_engine_api_key("qwen")
        assert disabled["status"] == "disabled"
        assert not json.loads((ctx["config_dir"] / "inference_secrets.json").read_text())["secrets"]
        assert "--api-key" not in (ctx["unit_dir"] / "infra-llm-qwen.service").read_text()


def test_client_bundles_require_explicit_instance_for_replicas(tmp_path: Path):
    with _temp_inference(tmp_path) as ctx:
        _write_model(ctx["store"])
        _setup_launcher(tmp_path)
        inference_profiles.create_profile(_profile(replicas=2))

        try:
            inference_connect.render_client_bundle("qwen", {})
            raise AssertionError("replicated profile bundle should require explicit instance")
        except inference_connect.InferenceConnectConflict:
            pass

        bundle = inference_connect.render_client_bundle("qwen", {"target_type": "instance", "instance_index": 1})
        assert bundle["base_url"] == "http://127.0.0.1:10001/v1"
        assert bundle["target"] == {"type": "instance", "instance_index": 1}

        saved = inference_connect.save_client_bundle(
            "qwen",
            {"id": "ci", "name": "CI", "target_type": "instance", "instance_index": 1},
        )
        assert saved["bundle"]["id"] == "ci"
        listed = inference_connect.list_client_bundles("qwen")
        assert listed["bundles"][0]["id"] == "ci"
        assert listed["default"]["requires_instance"] is True
        assert [item["base_url"] for item in listed["instance_bundles"]] == [
            "http://127.0.0.1:10000/v1",
            "http://127.0.0.1:10001/v1",
        ]
        assert listed["instance_bundles"][1]["target"] == {"type": "instance", "instance_index": 1}


def test_cloudflare_exposure_and_service_token_lifecycle(tmp_path: Path):
    with _temp_inference(tmp_path) as ctx:
        _write_model(ctx["store"])
        _setup_launcher(tmp_path)
        _configure_cf()
        inference_profiles.create_profile({**_profile(), "exposure": {"mode": "cloudflare", "hostname": "qwen.example.com"}})
        calls = []

        async def add_route(hostname, service, tunnel_id=None):
            calls.append(("add_route", hostname, service, tunnel_id))
            return True

        async def create_dns(hostname, tunnel_id=None, zone_id=None, zone_name=None):
            calls.append(("create_dns", hostname, tunnel_id, zone_id))
            return "dns-1"

        async def create_token(name, duration="8760h"):
            calls.append(("create_token", name, duration))
            idx = len([c for c in calls if c[0] == "create_token"])
            return {"id": f"tok-{idx}", "name": name, "client_id": f"cid-{idx}", "client_secret": f"secret-{idx}", "duration": duration}

        async def create_policy(name, token_ids):
            calls.append(("create_policy", name, list(token_ids)))
            return {"id": "pol-1", "name": name, "decision": "non_identity", "include": []}

        async def create_app(name, hostname, policy_id):
            calls.append(("create_app", name, hostname, policy_id))
            return {"id": "app-1", "aud": "aud-1"}

        async def update_policy(policy_id, token_ids):
            calls.append(("update_policy", policy_id, list(token_ids)))
            return {"id": policy_id, "include": token_ids}

        async def rotate_token(token_id):
            calls.append(("rotate_token", token_id))
            return {"id": token_id, "name": token_id, "client_id": "cid-rotated", "client_secret": "secret-rotated"}

        async def delete_token(token_id):
            calls.append(("delete_token", token_id))
            return True

        with _Patch([
            (tunnel, "add_tunnel_route", add_route),
            (tunnel, "create_dns_record", create_dns),
            (tunnel, "create_access_service_token", create_token),
            (tunnel, "create_service_auth_policy", create_policy),
            (tunnel, "create_access_app", create_app),
            (tunnel, "update_service_auth_policy_tokens", update_policy),
            (tunnel, "rotate_access_service_token", rotate_token),
            (tunnel, "delete_access_service_token", delete_token),
        ]):
            provisioned = _run(inference_connect.provision_cloudflare_exposure("qwen", {"render_bundle": True}))
            assert provisioned["client_secret"] == "secret-1"
            assert provisioned["client_bundle"]["headers"]["CF-Access-Client-Secret"] == "secret-1"
            stored = json.loads(ctx["profiles_file"].read_text())["profiles"]["qwen"]
            assert stored["cloudflare"]["hostname"] == "qwen.example.com"
            assert "secret-1" not in json.dumps(stored)

            generated = _run(inference_connect.generate_cloudflare_service_token("qwen", {"name": "second"}))
            assert generated["client_secret"] == "secret-2"
            assert ("update_policy", "pol-1", ["tok-1", "tok-2"]) in calls

            rotated = _run(inference_connect.rotate_cloudflare_service_token("qwen", "tok-2", {"render_bundle": True}))
            assert rotated["client_secret"] == "secret-rotated"
            assert rotated["client_bundle"]["headers"]["CF-Access-Client-Secret"] == "secret-rotated"

            retired = _run(inference_connect.retire_cloudflare_service_token("qwen", "tok-2", delete_if_owned=True))
            assert retired["service_token"]["state"] == "retired"
            assert ("delete_token", "tok-2") in calls
            try:
                _run(inference_connect.retire_cloudflare_service_token("qwen", "tok-1"))
                raise AssertionError("retiring the last active Cloudflare token should be blocked")
            except inference_connect.InferenceConnectConflict:
                pass


def test_cloudflare_cleanup_records_can_be_retried_and_forgotten(tmp_path: Path):
    with _temp_inference(tmp_path) as ctx:
        _write_model(ctx["store"])
        _setup_launcher(tmp_path)
        inference_profiles.create_profile(_profile())
        _configure_cf()

        def _add_cf(profile):
            profile["cloudflare"] = {
                "hostname": "qwen.example.com",
                "tunnel_id": "tun-1",
                "access_app_id": "app-1",
                "access_policy_id": "pol-1",
                "service_tokens": [],
            }
            profile["exposure"] = {"mode": "cloudflare", "hostname": "qwen.example.com"}
            return inference_profiles._public_profile(profile)

        inference_profiles.mutate_profile("qwen", _add_cf)
        fail_route = {"enabled": True}

        async def remove_route(hostname, tunnel_id=None):
            if fail_route["enabled"]:
                raise ValueError("route still exists")
            return True

        async def ok_delete(*_args, **_kwargs):
            return True

        with _Patch([
            (tunnel, "remove_tunnel_route", remove_route),
            (tunnel, "delete_dns_record", ok_delete),
            (tunnel, "delete_access_app", ok_delete),
            (tunnel, "delete_access_policy", ok_delete),
        ]):
            removed = _run(inference_connect.remove_cloudflare_exposure("qwen"))
            assert removed["warnings"]
            records = inference_connect.list_cleanup_records()["cleanup"]
            assert records[0]["kind"] == "tunnel_route"
            record_id = records[0]["id"]

            fail_route["enabled"] = False
            retry = _run(inference_connect.retry_cleanup_record(record_id))
            assert retry["status"] == "cleaned"
            assert inference_connect.list_cleanup_records()["cleanup"] == []

        inference_connect._record_cleanup("qwen", "dns_record", "delete", {"hostname": "qwen.example.com"}, "manual")
        record_id = inference_connect.list_cleanup_records()["cleanup"][0]["id"]
        forgotten = inference_connect.forget_cleanup_record(record_id)
        assert forgotten["status"] == "forgotten"


def main():
    tests = [
        test_engine_api_key_is_one_time_and_units_get_raw_key,
        test_client_bundles_require_explicit_instance_for_replicas,
        test_cloudflare_exposure_and_service_token_lifecycle,
        test_cloudflare_cleanup_records_can_be_retried_and_forgotten,
    ]
    failed = 0
    print("Running inference connect tests...\n")
    import tempfile

    for test in tests:
        with tempfile.TemporaryDirectory() as td:
            try:
                test(Path(td))
                print(f"  OK {test.__name__}")
            except Exception as exc:
                failed += 1
                print(f"  FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
