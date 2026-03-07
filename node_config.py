import json
import secrets
from pathlib import Path
from typing import Optional

CONFIG_FILE = Path.home() / ".config" / "inframatik" / "node.json"

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


def save_node_config(config: dict):
    """Write config to node.json and update cache."""
    global _config_cache
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    _config_cache = config


def invalidate_cache():
    global _config_cache
    _config_cache = None


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


def create_service_token(service_name: str) -> str:
    """Generate a scoped service token. Returns the token string."""
    config = get_node_config()
    if not config:
        raise ValueError("Node not configured")
    token = generate_service_token()
    config.setdefault("service_tokens", {})[token] = {
        "service": service_name,
        "created_at": int(__import__("time").time()),
    }
    save_node_config(config)
    return token


def revoke_service_token(token: str):
    """Remove a service token."""
    config = get_node_config()
    if not config:
        return
    tokens = config.get("service_tokens", {})
    if token in tokens:
        del tokens[token]
        save_node_config(config)


def get_service_token_scope(token: str) -> Optional[str]:
    """Return the service name a token is scoped to, or None if invalid."""
    config = get_node_config()
    if not config:
        return None
    entry = config.get("service_tokens", {}).get(token)
    if not entry:
        return None
    return entry.get("service")


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
    token = generate_enrollment_token()
    config.setdefault("enrollment_tokens", {})[token] = {
        "created_at": int(__import__("time").time()),
    }
    save_node_config(config)
    return token


def consume_enrollment_token(token: str) -> bool:
    """Validate and consume a one-time enrollment token. Returns True if valid."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        return False
    tokens = config.get("enrollment_tokens", {})
    if token not in tokens:
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
    if token in tokens:
        del tokens[token]
        save_node_config(config)


def init_as_standalone(name: str) -> dict:
    """Create a standalone node config (single machine, no clustering)."""
    config = {
        "node_id": generate_node_id(),
        "node_name": name,
        "role": "standalone",
    }
    save_node_config(config)
    return config


def init_as_master(name: str) -> dict:
    """Create a master node config. Returns the new config."""
    config = {
        "node_id": generate_node_id(),
        "node_name": name,
        "role": "master",
        "api_key": generate_api_key(),
        "tunnel_id": None,
        "workers": {},
    }
    save_node_config(config)
    return config


def init_as_worker(name: str, master_url: str, api_key: Optional[str] = None, node_id: Optional[str] = None) -> dict:
    """Create a worker node config. If api_key/node_id provided (from enrollment), uses those."""
    config = {
        "node_id": node_id or generate_node_id(),
        "node_name": name,
        "role": "worker",
        "api_key": api_key or generate_api_key(),
        "tunnel_id": None,
        "master_url": master_url.rstrip("/"),
    }
    save_node_config(config)
    return config


def add_worker(name: str, address: str, api_key: str) -> str:
    """Add a worker to the master's config. Returns the assigned node_id."""
    config = get_node_config()
    if not config or config.get("role") != "master":
        raise ValueError("Only a master node can add workers")
    node_id = generate_node_id()
    config.setdefault("workers", {})[node_id] = {
        "name": name,
        "address": address.rstrip("/"),
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
    # Direct lookup in config
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


def set_dashboard_hostname(hostname: Optional[str]):
    """Set/clear the dashboard_hostname for this node."""
    config = get_node_config()
    if not config:
        raise ValueError("Node not configured")
    config["dashboard_hostname"] = hostname
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
