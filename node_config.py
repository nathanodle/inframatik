import ipaddress
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

CONFIG_FILE = Path.home() / ".config" / "inframatik" / "node.json"

DEFAULT_ENROLLMENT_TOKEN_TTL_SECONDS = 3600
DEFAULT_SERVICE_TOKEN_TTL_SECONDS = 30 * 24 * 3600
DEFAULT_SERVICE_TOKEN_CAPABILITY = "deploy"
SERVICE_TOKEN_CAPABILITIES = ("read", "operate", "deploy")
_SERVICE_TOKEN_CAPABILITY_RANK = {
    "read": 0,
    "operate": 1,
    "deploy": 2,
}
_HOST_ENTRY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

_config_cache: Optional[dict] = None


def get_node_config() -> Optional[dict]:
    """Load node.json and return config dict. Returns None if absent (standalone mode)."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if not CONFIG_FILE.exists():
        return None
    _config_cache = json.loads(CONFIG_FILE.read_text())
    return _config_cache


def _secure_write_json(path: Path, payload: str, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, mode)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def save_node_config(config: dict):
    """Write config to node.json and update cache."""
    global _config_cache
    _secure_write_json(CONFIG_FILE, json.dumps(config, indent=2))
    _config_cache = config


def invalidate_cache():
    global _config_cache
    _config_cache = None


def _get_enrollment_token_ttl() -> int:
    raw = os.getenv("INFRAMATIK_ENROLLMENT_TOKEN_TTL_SECONDS", str(DEFAULT_ENROLLMENT_TOKEN_TTL_SECONDS))
    try:
        ttl = int(raw)
    except ValueError:
        ttl = DEFAULT_ENROLLMENT_TOKEN_TTL_SECONDS
    return max(60, ttl)


def _get_service_token_ttl() -> int:
    raw = os.getenv("INFRAMATIK_SERVICE_TOKEN_TTL_SECONDS", str(DEFAULT_SERVICE_TOKEN_TTL_SECONDS))
    try:
        ttl = int(raw)
    except ValueError:
        ttl = DEFAULT_SERVICE_TOKEN_TTL_SECONDS
    return max(3600, ttl)


def _purge_expired_enrollment_tokens(config: dict) -> bool:
    now = int(time.time())
    tokens = config.get("enrollment_tokens", {})
    changed = False
    for token, meta in list(tokens.items()):
        expires_at = meta.get("expires_at")
        if expires_at is not None and now >= expires_at:
            del tokens[token]
            changed = True
    return changed


def _purge_expired_service_tokens(config: dict) -> bool:
    now = int(time.time())
    tokens = config.get("service_tokens", {})
    changed = False
    for token, meta in list(tokens.items()):
        expires_at = meta.get("expires_at")
        if expires_at is not None and now >= expires_at:
            del tokens[token]
            changed = True
    return changed


def cleanup_expired_tokens() -> bool:
    """Purge expired enrollment/service tokens from config. Returns True if changed."""
    config = get_node_config()
    if not config:
        return False
    changed = False
    changed = _purge_expired_enrollment_tokens(config) or changed
    changed = _purge_expired_service_tokens(config) or changed
    if changed:
        save_node_config(config)
    return changed


def has_admin_password() -> bool:
    config = get_node_config()
    return bool(config and config.get("admin_password_hash"))


def set_admin_password(password: str):
    from auth import hash_password
    config = get_node_config()
    if not config:
        # Bootstrap: create minimal config just for the password
        config = {}
    config["admin_password_hash"] = hash_password(password)
    save_node_config(config)


def verify_admin_password(password: str) -> bool:
    from auth import verify_password
    config = get_node_config()
    if not config:
        return False
    hashed = config.get("admin_password_hash")
    if not hashed:
        return False
    return verify_password(password, hashed)


def generate_service_token() -> str:
    return "svc_" + secrets.token_hex(32)


def service_token_id(token: str) -> str:
    digest = hashlib.sha256(token.encode()).hexdigest()[:16]
    return f"st_{digest}"


def normalize_service_token_capability(capability: Optional[str]) -> str:
    cap = (capability or DEFAULT_SERVICE_TOKEN_CAPABILITY).strip().lower()
    if cap not in SERVICE_TOKEN_CAPABILITIES:
        raise ValueError(
            "Invalid service token capability. Allowed values: "
            + ", ".join(SERVICE_TOKEN_CAPABILITIES)
        )
    return cap


def service_token_capability_allows(capability: str, required: str) -> bool:
    try:
        cap = normalize_service_token_capability(capability)
        req = normalize_service_token_capability(required)
    except ValueError:
        return False
    return _SERVICE_TOKEN_CAPABILITY_RANK[cap] >= _SERVICE_TOKEN_CAPABILITY_RANK[req]


def create_service_token(service_name: str, capability: Optional[str] = None) -> str:
    """Generate a scoped service token. Returns the token string."""
    config = get_node_config()
    if not config:
        raise ValueError("Node not configured")
    token_capability = normalize_service_token_capability(capability)
    _purge_expired_service_tokens(config)
    now = int(time.time())
    ttl = _get_service_token_ttl()
    token = generate_service_token()
    config.setdefault("service_tokens", {})[token] = {
        "service": service_name,
        "capability": token_capability,
        "created_at": now,
        "expires_at": now + ttl,
    }
    save_node_config(config)
    return token


def rotate_service_token(token: str) -> tuple[str, str, str]:
    """Replace an existing service token and return (new_token, service_name, capability)."""
    config = get_node_config()
    if not config:
        raise ValueError("Node not configured")
    _purge_expired_service_tokens(config)
    tokens = config.get("service_tokens", {})
    entry = tokens.get(token)
    if not entry:
        raise ValueError("Service token not found")

    service = entry.get("service")
    capability = normalize_service_token_capability(entry.get("capability"))
    del tokens[token]

    now = int(time.time())
    ttl = _get_service_token_ttl()
    new_token = generate_service_token()
    tokens[new_token] = {
        "service": service,
        "capability": capability,
        "created_at": now,
        "expires_at": now + ttl,
    }
    save_node_config(config)
    return new_token, service, capability


def revoke_service_token(token: str):
    """Remove a service token."""
    config = get_node_config()
    if not config:
        return
    tokens = config.get("service_tokens", {})
    changed = _purge_expired_service_tokens(config)
    if token in tokens:
        del tokens[token]
        changed = True
    if changed:
        save_node_config(config)


def revoke_service_token_by_id(token_id: str) -> bool:
    """Remove a service token by opaque token_id. Returns True if a token was revoked."""
    config = get_node_config()
    if not config:
        return False
    tokens = config.get("service_tokens", {})
    changed = _purge_expired_service_tokens(config)
    found = False
    for token in list(tokens.keys()):
        if service_token_id(token) == token_id:
            del tokens[token]
            found = True
            changed = True
    if changed:
        save_node_config(config)
    return found


def get_service_token_auth(token: str) -> Optional[dict]:
    """Return service token auth metadata, or None if invalid."""
    config = get_node_config()
    if not config:
        return None
    changed = _purge_expired_service_tokens(config)
    entry = config.get("service_tokens", {}).get(token)
    if not entry:
        if changed:
            save_node_config(config)
        return None
    service = entry.get("service")
    try:
        capability = normalize_service_token_capability(entry.get("capability"))
    except ValueError:
        if changed:
            save_node_config(config)
        return None
    if not entry.get("capability"):
        entry["capability"] = capability
        changed = True
    if changed:
        save_node_config(config)
    return {
        "service": service,
        "capability": capability,
        "created_at": entry.get("created_at"),
        "expires_at": entry.get("expires_at"),
    }


def get_service_token_scope(token: str) -> Optional[str]:
    """Return the service name a token is scoped to, or None if invalid."""
    meta = get_service_token_auth(token)
    if not meta:
        return None
    return meta.get("service")


def generate_api_key() -> str:
    return "sdk_" + secrets.token_hex(32)


def generate_node_id() -> str:
    return secrets.token_hex(4)


def generate_enrollment_token() -> str:
    return "enroll-" + secrets.token_hex(16)


def create_enrollment_token() -> str:
    """Generate and store an enrollment token. Returns the token string."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise ValueError("Only a master node can create enrollment tokens")
    _purge_expired_enrollment_tokens(config)
    now = int(time.time())
    ttl = _get_enrollment_token_ttl()
    token = generate_enrollment_token()
    config.setdefault("enrollment_tokens", {})[token] = {
        "created_at": now,
        "expires_at": now + ttl,
    }
    save_node_config(config)
    return token


def consume_enrollment_token(token: str) -> bool:
    """Validate and consume a one-time enrollment token. Returns True if valid."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        return False
    tokens = config.get("enrollment_tokens", {})
    changed = _purge_expired_enrollment_tokens(config)
    if token not in tokens:
        if changed:
            save_node_config(config)
        return False
    del tokens[token]
    save_node_config(config)
    return True


def delete_enrollment_token(token: str):
    """Cancel an unused enrollment token."""
    config = get_node_config()
    if not config:
        return
    tokens = config.get("enrollment_tokens", {})
    changed = _purge_expired_enrollment_tokens(config)
    if token in tokens:
        del tokens[token]
        changed = True
    if changed:
        save_node_config(config)


def _existing_admin_password_hash() -> Optional[str]:
    existing = get_node_config()
    if not existing:
        return None
    value = existing.get("admin_password_hash")
    if isinstance(value, str) and value:
        return value
    return None


def init_as_standalone(name: str) -> dict:
    """Create a standalone node config (single machine, no clustering)."""
    admin_hash = _existing_admin_password_hash()
    config = {
        "node_id": generate_node_id(),
        "node_name": name,
        "role": "standalone",
    }
    if admin_hash:
        config["admin_password_hash"] = admin_hash
    save_node_config(config)
    return config


def init_as_master(name: str) -> dict:
    """Create a master node config. Returns the new config."""
    admin_hash = _existing_admin_password_hash()
    config = {
        "node_id": generate_node_id(),
        "node_name": name,
        "role": "master",
        "api_key": generate_api_key(),
        "tunnel_id": None,
        "workers": {},
        "worker_target_allowlist": [],
    }
    if admin_hash:
        config["admin_password_hash"] = admin_hash
    save_node_config(config)
    return config


def init_as_worker(
    name: str,
    master_url: str,
    api_key: Optional[str] = None,
    node_id: Optional[str] = None,
    update_public_key: Optional[str] = None,
) -> dict:
    """Create a worker node config. If api_key/node_id provided (from enrollment), uses those."""
    admin_hash = _existing_admin_password_hash()
    config = {
        "node_id": node_id or generate_node_id(),
        "node_name": name,
        "role": "worker",
        "api_key": api_key or generate_api_key(),
        "tunnel_id": None,
        "master_url": master_url.rstrip("/"),
    }
    if admin_hash:
        config["admin_password_hash"] = admin_hash
    if update_public_key:
        config["update_public_key"] = update_public_key
    save_node_config(config)
    return config


def is_worker_allowlist_required() -> bool:
    return os.getenv("INFRAMATIK_REQUIRE_WORKER_ALLOWLIST", "").lower() in ("1", "true", "yes")


def normalize_worker_allowlist_entry(entry: str) -> str:
    value = entry.strip().lower()
    if not value:
        raise ValueError("Allowlist entries cannot be empty")

    if value.startswith("*."):
        suffix = value[2:]
        if not suffix or not _HOST_ENTRY_RE.match(suffix):
            raise ValueError(f"Invalid wildcard allowlist entry: {entry}")
        return f"*.{suffix}"

    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError:
        network = None
    if network is not None:
        return str(network)

    raw = value
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        ip = None
    if ip is not None:
        return str(ip)

    if not _HOST_ENTRY_RE.match(value):
        raise ValueError(f"Invalid allowlist host entry: {entry}")
    return value


def normalize_worker_allowlist_entries(entries: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        item = normalize_worker_allowlist_entry(entry)
        if item in seen:
            continue
        normalized.append(item)
        seen.add(item)
    return normalized


def get_worker_target_allowlist(config: Optional[dict] = None) -> list[str]:
    cfg = config if config is not None else get_node_config()
    if not cfg:
        return []
    entries = cfg.get("worker_target_allowlist", [])
    if not isinstance(entries, list):
        raise ValueError("worker_target_allowlist must be a list")
    return normalize_worker_allowlist_entries(entries)


def set_worker_target_allowlist(entries: list[str]) -> list[str]:
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise ValueError("Only a master node can set worker target allowlist")
    normalized = normalize_worker_allowlist_entries(entries)
    config["worker_target_allowlist"] = normalized
    save_node_config(config)
    return normalized


def _worker_host_matches_allowlist(host: str, allowlist: list[str]) -> bool:
    host = host.lower()
    try:
        host_ip = ipaddress.ip_address(host)
    except ValueError:
        host_ip = None

    for rule in allowlist:
        if rule.startswith("*."):
            suffix = rule[2:]
            if host.endswith("." + suffix):
                return True
            continue

        if "/" in rule:
            if host_ip is None:
                continue
            try:
                network = ipaddress.ip_network(rule, strict=False)
            except ValueError:
                continue
            if host_ip in network:
                return True
            continue

        try:
            rule_ip = ipaddress.ip_address(rule)
        except ValueError:
            rule_ip = None

        if rule_ip is not None:
            if host_ip is not None and host_ip == rule_ip:
                return True
            continue

        if host == rule:
            return True

    return False


def is_worker_address_allowed(address: str, config: Optional[dict] = None) -> bool:
    normalized_address = normalize_worker_address(address)
    parsed = urlparse(normalized_address)
    host = (parsed.hostname or "").lower()
    if not host:
        return False

    allowlist = get_worker_target_allowlist(config=config)
    if not allowlist:
        return not is_worker_allowlist_required()
    return _worker_host_matches_allowlist(host, allowlist)


def assert_worker_address_allowed(address: str, config: Optional[dict] = None) -> str:
    normalized_address = normalize_worker_address(address)
    if is_worker_address_allowed(normalized_address, config=config):
        return normalized_address

    allowlist = get_worker_target_allowlist(config=config)
    if not allowlist and is_worker_allowlist_required():
        raise ValueError("Worker target allowlist is required and cannot be empty")
    raise ValueError("Worker address is not in the configured worker target allowlist")


def normalize_worker_address(address: str) -> str:
    """Validate and normalize worker base URL."""
    parsed = urlparse(address.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Worker address must use http or https")
    if not parsed.hostname:
        raise ValueError("Worker address must include a host")
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("Worker address must be a base URL without path/query/fragment")

    host = parsed.hostname.lower()
    if host == "localhost":
        raise ValueError("Worker address cannot use localhost")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise ValueError("Worker address uses a disallowed IP range")

    port = parsed.port
    if port is None:
        raise ValueError("Worker address must include an explicit port")

    if ":" in host and not host.startswith("["):
        host_part = f"[{host}]"
    else:
        host_part = host
    return f"{parsed.scheme}://{host_part}:{port}"


def add_worker(name: str, address: str, api_key: str) -> str:
    """Add a worker to the master's config. Returns the assigned node_id."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise ValueError("Only a master node can add workers")
    normalized_address = assert_worker_address_allowed(address, config=config)
    node_id = generate_node_id()
    config.setdefault("workers", {})[node_id] = {
        "name": name,
        "address": normalized_address,
        "api_key": api_key,
    }
    save_node_config(config)
    return node_id


def remove_worker(node_id: str):
    """Remove a worker from the master's config."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise ValueError("Only a master node can remove workers")
    if node_id not in config.get("workers", {}):
        raise ValueError(f"Worker '{node_id}' not found")
    del config["workers"][node_id]
    save_node_config(config)


def reset_config():
    """Delete node.json and return to standalone mode."""
    global _config_cache
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
    _config_cache = None


def get_worker_by_api_key(api_key: str) -> Optional[tuple[str, dict]]:
    """Find a worker entry by its API key. Returns (node_id, worker_config) or None."""
    config = get_node_config()
    if not config:
        return None
    for nid, worker in config.get("workers", {}).items():
        if worker["api_key"] == api_key:
            return (nid, worker)
    return None


def get_worker_by_node_id(node_id: str) -> Optional[dict]:
    """Find a worker's config by node_id. Checks both config keys and registered mappings."""
    config = get_node_config()
    if not config:
        return None
    worker = config.get("workers", {}).get(node_id)
    if worker:
        return worker
    return None


def get_tunnel_id() -> Optional[str]:
    """Return the tunnel_id for this node, or None if not set."""
    config = get_node_config()
    if not config:
        return None
    return config.get("tunnel_id")


def set_tunnel_id(tunnel_id: str):
    """Set/update the tunnel_id for this node."""
    config = get_node_config()
    if not config:
        raise ValueError("Node not configured")
    config["tunnel_id"] = tunnel_id
    save_node_config(config)


def save_cf_config(token: str, account_id: str, zone_id: str, default_policy_id: Optional[str] = None):
    """Store Cloudflare credentials in node config."""
    config = get_node_config()
    if not config:
        raise ValueError("Node not configured")
    config["cf_token"] = token
    config["cf_account_id"] = account_id
    config["cf_zone_id"] = zone_id
    config["cf_default_policy_id"] = default_policy_id
    save_node_config(config)


def clear_cf_config():
    """Remove all Cloudflare credentials from node config."""
    config = get_node_config()
    if not config:
        return
    for key in ("cf_token", "cf_account_id", "cf_zone_id", "cf_default_policy_id"):
        config.pop(key, None)
    save_node_config(config)


def get_dashboard_hostname() -> Optional[str]:
    """Return the dashboard_hostname for this node, or None if not set."""
    config = get_node_config()
    if not config:
        return None
    return config.get("dashboard_hostname")


def set_dashboard_hostname(
    hostname: Optional[str],
    zone_id: Optional[str] = None,
    zone_name: Optional[str] = None,
):
    """Set/clear dashboard Cloudflare hostname metadata for this node."""
    config = get_node_config()
    if not config:
        raise ValueError("Node not configured")
    if hostname:
        config["dashboard_hostname"] = hostname
        if zone_id:
            config["dashboard_zone_id"] = zone_id
        else:
            config.pop("dashboard_zone_id", None)
        if zone_name:
            config["dashboard_zone_name"] = zone_name
        else:
            config.pop("dashboard_zone_name", None)
    else:
        config.pop("dashboard_hostname", None)
        config.pop("dashboard_zone_id", None)
        config.pop("dashboard_zone_name", None)
    save_node_config(config)


def set_worker_tunnel_id(node_id: str, tunnel_id: str):
    """Set tunnel_id for a remote worker (master only)."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise ValueError("Only master can set worker tunnel_id")
    worker = config.get("workers", {}).get(node_id)
    if not worker:
        raise ValueError(f"Worker '{node_id}' not found")
    worker["tunnel_id"] = tunnel_id
    save_node_config(config)
