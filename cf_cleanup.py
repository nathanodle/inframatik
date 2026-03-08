#!/usr/bin/env python3
"""Cloudflare resource cleanup for uninstall.

Reads node.json, identifies CF resources belonging to this machine,
shows the user what will be deleted, asks for confirmation, then cleans up.

Only removes resources associated with services registered on this node.
Policies are only removed if no other apps reference them.
"""

import json
import sys
import urllib.error
import urllib.request


def _cf_request(method, url, token, body=None):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"success": False, "errors": [{"message": str(e)}]}
    except Exception as e:
        return {"success": False, "errors": [{"message": str(e)}]}


def main():
    if len(sys.argv) < 2:
        print("Usage: cf_cleanup.py <path-to-node.json>")
        sys.exit(1)

    try:
        config = json.loads(open(sys.argv[1]).read())
    except Exception as e:
        print(f"  Error reading config: {e}")
        sys.exit(1)

    token = config.get("cf_token")
    account_id = config.get("cf_account_id")
    zone_id = config.get("cf_zone_id")
    tunnel_id = config.get("tunnel_id")
    dashboard_hostname = config.get("dashboard_hostname")

    if not token or not account_id:
        print("  No Cloudflare credentials in config.")
        return

    base = "https://api.cloudflare.com/client/v4"

    # --- Collect hostnames from registered services on this node ---
    hostnames = set()
    services = config.get("services", {})
    # Also check services.json
    try:
        svc_path = sys.argv[1].replace("node.json", "services.json")
        svc_data = json.loads(open(svc_path).read())
        for name, svc in svc_data.items():
            if svc.get("hostname"):
                hostnames.add(svc["hostname"])
    except Exception:
        pass

    if dashboard_hostname:
        hostnames.add(dashboard_hostname)

    # --- Collect tunnel ingress routes ---
    route_hostnames = set()
    if tunnel_id:
        data = _cf_request("GET", f"{base}/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations", token)
        if data.get("success"):
            ingress = data.get("result", {}).get("config", {}).get("ingress", [])
            for r in ingress:
                h = r.get("hostname")
                if h:
                    route_hostnames.add(h)

    # Merge: all hostnames this node owns
    all_hostnames = hostnames | route_hostnames

    if not all_hostnames and not tunnel_id:
        print("  No Cloudflare resources found for this node.")
        return

    # --- Find DNS records to delete ---
    dns_to_delete = []
    if zone_id and all_hostnames:
        data = _cf_request("GET", f"{base}/zones/{zone_id}/dns_records?type=CNAME&per_page=100", token)
        if data.get("success"):
            for r in data.get("result", []):
                if r["name"] in all_hostnames:
                    dns_to_delete.append({"id": r["id"], "name": r["name"]})

    # --- Find Access apps to delete ---
    apps_to_delete = []
    policy_ids_in_use = set()  # policies used by apps we're NOT deleting
    apps_data = _cf_request("GET", f"{base}/accounts/{account_id}/access/apps?per_page=100", token)
    if apps_data.get("success"):
        for app in apps_data.get("result", []):
            domain = app.get("domain", "")
            if domain in all_hostnames:
                apps_to_delete.append({"id": app["id"], "name": app.get("name", ""), "domain": domain})
            else:
                # Track policies used by apps we're keeping
                for p in app.get("policies", []):
                    pid = p.get("id")
                    if pid:
                        policy_ids_in_use.add(pid)

    # --- Check policies from our apps: safe to delete if not used elsewhere ---
    policies_to_delete = []
    for app in apps_to_delete:
        # Fetch full app to see its policies
        app_detail = _cf_request("GET", f"{base}/accounts/{account_id}/access/apps/{app['id']}", token)
        if app_detail.get("success"):
            for p in app_detail.get("result", {}).get("policies", []):
                pid = p.get("id")
                pname = p.get("name", "")
                if pid and pid not in policy_ids_in_use:
                    policies_to_delete.append({"id": pid, "name": pname})

    # Deduplicate policies
    seen_policies = set()
    unique_policies = []
    for p in policies_to_delete:
        if p["id"] not in seen_policies:
            seen_policies.add(p["id"])
            unique_policies.append(p)
    policies_to_delete = unique_policies

    # --- Show plan ---
    print("  Resources to remove:\n")

    if tunnel_id:
        print(f"  Tunnel:  {tunnel_id}")

    if dns_to_delete:
        print(f"  DNS records ({len(dns_to_delete)}):")
        for r in dns_to_delete:
            print(f"    - {r['name']}")

    if apps_to_delete:
        print(f"  Access apps ({len(apps_to_delete)}):")
        for a in apps_to_delete:
            print(f"    - {a['name']} ({a['domain']})")

    if policies_to_delete:
        print(f"  Access policies ({len(policies_to_delete)}) (not used by other apps):")
        for p in policies_to_delete:
            print(f"    - {p['name']}")
    elif apps_to_delete:
        print("  Access policies: none (all in use by other apps)")

    if not tunnel_id and not dns_to_delete and not apps_to_delete:
        print("  Nothing to remove.")
        return

    print("")
    resp = input("  Proceed with deletion? [y/N] ").strip().lower()
    if resp not in ("y", "yes"):
        print("  Skipped.")
        return

    print("")

    # --- Execute: Access apps first, then DNS, then policies, then tunnel ---
    for a in apps_to_delete:
        result = _cf_request("DELETE", f"{base}/accounts/{account_id}/access/apps/{a['id']}", token)
        ok = "✓" if result.get("success") else "✗"
        print(f"  {ok} Deleted Access app: {a['name']}")

    for r in dns_to_delete:
        result = _cf_request("DELETE", f"{base}/zones/{zone_id}/dns_records/{r['id']}", token)
        ok = "✓" if result.get("success") else "✗"
        print(f"  {ok} Deleted DNS: {r['name']}")

    for p in policies_to_delete:
        result = _cf_request("DELETE", f"{base}/accounts/{account_id}/access/policies/{p['id']}", token)
        ok = "✓" if result.get("success") else "✗"
        print(f"  {ok} Deleted policy: {p['name']}")

    if tunnel_id:
        # Clean connections first
        _cf_request("DELETE", f"{base}/accounts/{account_id}/cfd_tunnel/{tunnel_id}/connections", token)
        import time
        time.sleep(3)
        result = _cf_request("DELETE", f"{base}/accounts/{account_id}/cfd_tunnel/{tunnel_id}", token)
        ok = "✓" if result.get("success") else "✗"
        detail = ""
        if not result.get("success"):
            errors = result.get("errors", [])
            if errors:
                detail = f" ({errors[0].get('message', '')})"
        print(f"  {ok} Deleted tunnel{detail}")

    print("")
    print("  Cloudflare cleanup complete.")


if __name__ == "__main__":
    main()
