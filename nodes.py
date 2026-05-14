import asyncio
import logging
import socket
import time
from typing import Optional

import httpx

from node_config import (
    get_node_config,
    get_worker_by_api_key,
    normalize_worker_address,
    assert_worker_address_allowed,
)

# Cache for active health check results: address -> (status, timestamp)
_health_cache: dict[str, tuple[str, float]] = {}
HEALTH_CACHE_TTL = 15  # seconds

logger = logging.getLogger("inframatik.nodes")

# In-memory node registry: node_id -> node info
_nodes: dict[str, dict] = {}

# Maps a worker's real node_id to the config-key node_id on the master
_id_map: dict[str, str] = {}

STALE_THRESHOLD = 45  # seconds


def unregister_node(config_node_id: str):
    """Remove a node from in-memory registries. Called when a worker is deleted from config."""
    real_id = _id_map.pop(config_node_id, None)
    if real_id and real_id in _nodes:
        _nodes.pop(real_id, None)
        _id_map.pop(real_id, None)


def register_node(node_id: str, node_name: str, address: str, api_key: str) -> bool:
    """Called when a worker POSTs to /api/nodes/register. Returns True if accepted."""
    match = get_worker_by_api_key(api_key)
    if not match:
        return False

    config_node_id, worker_config = match
    try:
        reported_address = normalize_worker_address(address)
    except ValueError:
        return False
    try:
        expected_address = assert_worker_address_allowed(worker_config["address"])
    except ValueError:
        logger.warning(
            "Rejected register for %s: configured address for worker %s is invalid",
            node_name,
            config_node_id,
        )
        return False
    if reported_address != expected_address:
        logger.warning(
            "Rejected register for %s: reported address %s does not match configured address %s",
            node_name,
            reported_address,
            expected_address,
        )
        return False

    _nodes[node_id] = {
        "node_name": node_name,
        "address": expected_address,
        "status": "online",
        "last_seen": time.time(),
        "registered_at": time.time(),
        "config_node_id": config_node_id,
    }
    # Map both the real node_id and the config-key node_id to this entry
    _id_map[node_id] = node_id
    _id_map[config_node_id] = node_id

    return True


def heartbeat_node(node_id: str) -> bool:
    """Update last_seen for a node. Returns False if not registered."""
    real_id = _id_map.get(node_id, node_id)
    if real_id in _nodes:
        _nodes[real_id]["last_seen"] = time.time()
        _nodes[real_id]["status"] = "online"
        return True
    return False


def validate_heartbeat_key(node_id: str, api_key: str) -> bool:
    """Validate worker API key and enforce key->node binding when node is registered."""
    match = get_worker_by_api_key(api_key)
    if not match:
        return False

    config_node_id, _ = match
    real_id = _id_map.get(node_id, node_id)

    # If node is registered, heartbeat key must match that node's configured worker identity.
    if real_id in _nodes:
        return _nodes[real_id].get("config_node_id") == config_node_id

    # If node is not registered yet, allow heartbeat auth to pass and let caller return 404
    # so workers can trigger their re-registration flow.
    return True


async def _check_health(address: str) -> str:
    """Ping a worker's /api/system endpoint. Returns 'online' or 'offline'. Cached."""
    try:
        address = assert_worker_address_allowed(address)
    except ValueError:
        logger.warning("Skipped health check for invalid worker address: %s", address)
        return "offline"

    now = time.time()
    cached = _health_cache.get(address)
    if cached and (now - cached[1]) < HEALTH_CACHE_TTL:
        return cached[0]
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{address}/api/node/health")
            if resp.status_code == 200:
                status = "online"
            else:
                # Node is reachable but doesn't have cluster code — still online
                resp2 = await client.get(f"{address}/api/system")
                status = "online" if resp2.status_code == 200 else "offline"
    except (httpx.HTTPError, ValueError, OSError):
        status = "offline"
    _health_cache[address] = (status, now)
    return status


async def get_all_nodes() -> list[dict]:
    """Return all nodes including self (master). Used by GET /api/nodes."""
    config = get_node_config()
    result = []

    # Self (always first, always online)
    if config:
        result.append({
            "node_id": config["node_id"],
            "node_name": config["node_name"],
            "status": "online",
            "is_self": True,
            "tunnel_id": config.get("tunnel_id"),
        })

    # Registered remote nodes
    seen_config_ids = set()
    workers_cfg = config.get("workers", {}) if config else {}
    for node_id, info in _nodes.items():
        cfg_id = info.get("config_node_id")
        seen_config_ids.add(cfg_id)
        worker_cfg = workers_cfg.get(cfg_id, {})
        result.append({
            "node_id": node_id,
            "node_name": info["node_name"],
            "address": info["address"],
            "status": info["status"],
            "is_self": False,
            "last_seen": info["last_seen"],
            "tunnel_id": worker_cfg.get("tunnel_id"),
        })

    # Configured workers that haven't registered (heartbeat) yet — actively check
    if config and config.get("workers"):
        for cfg_id, worker in config["workers"].items():
            if cfg_id not in seen_config_ids:
                status = await _check_health(worker["address"])
                result.append({
                    "node_id": cfg_id,
                    "node_name": worker["name"],
                    "address": worker["address"],
                    "status": status,
                    "is_self": False,
                    "last_seen": None,
                    "tunnel_id": worker.get("tunnel_id"),
                })

    return result


def resolve_node(node_id: str) -> Optional[dict]:
    """Resolve a node_id to its connection info. Returns dict with address and api_key,
    or None for self-node. Raises ValueError if unknown."""
    config = get_node_config()
    if not config:
        raise ValueError(f"Unknown node: {node_id}")

    # Self-node
    if node_id == config["node_id"]:
        return None

    # Check registered nodes (real node_id)
    real_id = _id_map.get(node_id, node_id)
    if real_id in _nodes:
        info = _nodes[real_id]
        cfg_id = info.get("config_node_id", node_id)
        worker_cfg = config.get("workers", {}).get(cfg_id, {})
        address = worker_cfg.get("address")
        if not address:
            raise ValueError(f"Worker '{cfg_id}' has no configured address")
        try:
            normalized_address = assert_worker_address_allowed(address, config=config)
        except ValueError as e:
            raise ValueError(f"Invalid worker address for '{cfg_id}': {e}")
        return {
            "address": normalized_address,
            "api_key": worker_cfg.get("api_key", ""),
        }

    # Check config (not yet registered)
    worker = config.get("workers", {}).get(node_id)
    if worker:
        try:
            normalized_address = assert_worker_address_allowed(worker["address"], config=config)
        except ValueError as e:
            raise ValueError(f"Invalid worker address for '{node_id}': {e}")
        return {
            "address": normalized_address,
            "api_key": worker["api_key"],
        }

    raise ValueError(f"Unknown node: {node_id}")


def check_stale_nodes():
    """Mark nodes as offline if they haven't heartbeated recently."""
    now = time.time()
    for info in _nodes.values():
        if now - info["last_seen"] > STALE_THRESHOLD:
            info["status"] = "offline"


async def stale_checker_loop():
    """Background task for the master: checks for stale nodes every 10s."""
    while True:
        check_stale_nodes()
        await asyncio.sleep(10)


async def heartbeat_sender_loop():
    """Background task for workers: registers then heartbeats to master every 15s."""
    config = get_node_config()
    if not config or config.get("role") != "worker":
        return

    master_url = config["master_url"]
    headers = {"X-Api-Key": config["api_key"], "Content-Type": "application/json"}

    # Determine this worker's reachable address
    listen_port = config.get("listen_port", 9000)
    try:
        # Get the IP used to reach the master (most reliable routable address)
        master_host = config["master_url"].split("://")[-1].split(":")[0].split("/")[0]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((master_host, 1))
        local_ip = s.getsockname()[0]
        s.close()
    except (OSError, ValueError):
        local_ip = socket.gethostname()
    worker_address = f"http://{local_ip}:{listen_port}"

    def current_tunnel_id():
        current = get_node_config() or config
        return current.get("tunnel_id")

    register_payload = {
        "node_id": config["node_id"],
        "node_name": config["node_name"],
        "address": worker_address,
    }
    tunnel_id = current_tunnel_id()
    if tunnel_id:
        register_payload["tunnel_id"] = tunnel_id

    # Initial registration (retry with backoff)
    backoff = 5
    failures = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(
                    f"{master_url}/api/nodes/register",
                    headers=headers,
                    json=register_payload,
                )
                if resp.status_code == 200:
                    logger.info("Registered with master at %s", master_url)
                    break
                else:
                    logger.warning("Registration rejected: %s", resp.text)
        except (httpx.HTTPError, ValueError, OSError) as e:
            logger.debug("Registration failed, retrying: %s", e)
        failures += 1
        if failures == 10:
            logger.warning("Registration failing repeatedly — check master URL and API key")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)

    # Heartbeat loop
    while True:
        await asyncio.sleep(15)
        try:
            heartbeat_payload = {"node_id": config["node_id"]}
            tunnel_id = current_tunnel_id()
            if tunnel_id:
                heartbeat_payload["tunnel_id"] = tunnel_id
                register_payload["tunnel_id"] = tunnel_id
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(
                    f"{master_url}/api/nodes/heartbeat",
                    headers=headers,
                    json=heartbeat_payload,
                )
                # If master doesn't know us, re-register
                if resp.status_code == 404:
                    await client.post(
                        f"{master_url}/api/nodes/register",
                        headers=headers,
                        json=register_payload,
                    )
        except (httpx.HTTPError, ValueError, OSError) as e:
            logger.debug("Heartbeat failed, will retry: %s", e)
