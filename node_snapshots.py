import asyncio
import logging
import time
from copy import deepcopy

from node_config import get_node_config
from nodes import get_all_nodes
from proxy import proxy_to_node

logger = logging.getLogger("inframatik.node_snapshots")

SNAPSHOT_POLL_INTERVAL = 5
SNAPSHOT_COMPONENT_TIMEOUT = 5

_snapshot_cache: dict[str, dict] = {}
_snapshot_lock = asyncio.Lock()


async def _with_timeout(label: str, coro):
    try:
        return await asyncio.wait_for(coro, timeout=SNAPSHOT_COMPONENT_TIMEOUT), None
    except Exception as e:
        return None, f"{label}: {e}"


async def collect_node_snapshot(node_id: str) -> dict:
    """Collect the fast dashboard data for a node and update the in-memory cache."""
    system_task = _with_timeout("system", proxy_to_node(node_id, "GET", "/api/system"))
    tunnel_task = _with_timeout("tunnel", proxy_to_node(node_id, "GET", "/api/tunnel"))
    services_task = _with_timeout("services", proxy_to_node(node_id, "GET", "/api/services"))
    (system, system_error), (tunnel, tunnel_error), (services, services_error) = await asyncio.gather(
        system_task,
        tunnel_task,
        services_task,
    )

    errors = {}
    if system_error:
        errors["system"] = system_error
    if tunnel_error:
        errors["tunnel"] = tunnel_error
    if services_error:
        errors["services"] = services_error

    snapshot = {
        "node_id": node_id,
        "updated_at": time.time(),
        "system": system,
        "tunnel": tunnel,
        "services": services,
        "errors": errors,
    }

    async with _snapshot_lock:
        _snapshot_cache[node_id] = snapshot
    return deepcopy(snapshot)


async def get_cached_node_snapshot(node_id: str) -> dict | None:
    async with _snapshot_lock:
        snapshot = _snapshot_cache.get(node_id)
        return deepcopy(snapshot) if snapshot else None


async def get_node_snapshot(node_id: str) -> dict:
    """Return cached snapshot data, collecting synchronously only on cold cache."""
    cached = await get_cached_node_snapshot(node_id)
    if cached:
        return cached
    return await collect_node_snapshot(node_id)


async def refresh_all_node_snapshots():
    config = get_node_config()
    if not config or config.get("role") != "master":
        return

    try:
        nodes = await get_all_nodes()
    except Exception as e:
        logger.debug("Failed to list nodes for snapshot refresh: %s", e)
        nodes = [{"node_id": config.get("node_id")}]

    node_ids = []
    seen = set()
    for node in nodes:
        node_id = node.get("node_id")
        if node_id and node_id not in seen:
            seen.add(node_id)
            node_ids.append(node_id)

    if not node_ids:
        return

    await asyncio.gather(
        *(collect_node_snapshot(node_id) for node_id in node_ids),
        return_exceptions=True,
    )


async def snapshot_collector_loop():
    """Master background task: keep dashboard node snapshots warm."""
    while True:
        try:
            await refresh_all_node_snapshots()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Node snapshot refresh failed: %s", e)
        await asyncio.sleep(SNAPSHOT_POLL_INTERVAL)


def _reset_snapshots_for_tests():
    _snapshot_cache.clear()
