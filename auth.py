import secrets
import time
from typing import Optional

import bcrypt
import httpx
import jwt

from node_config import get_node_config

# In-memory session store: token -> {expires_at}
_sessions: dict[str, dict] = {}

# CF public key cache
_cf_keys_cache: dict = {"keys": [], "fetched_at": 0}
CF_KEYS_TTL = 3600  # 1 hour


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

MAX_SESSIONS = 100


def create_session(duration_hours: int = 24) -> tuple[str, int]:
    token = secrets.token_hex(32)
    expires_at = int(time.time()) + duration_hours * 3600
    _sessions[token] = {"expires_at": expires_at}
    _cleanup_sessions()
    # Evict oldest sessions if over limit
    while len(_sessions) > MAX_SESSIONS:
        oldest = min(_sessions, key=lambda t: _sessions[t]["expires_at"])
        del _sessions[oldest]
    return token, expires_at


def validate_session(token: str) -> bool:
    session = _sessions.get(token)
    if not session:
        return False
    if time.time() > session["expires_at"]:
        del _sessions[token]
        return False
    return True


def invalidate_session(token: str):
    _sessions.pop(token, None)


def _cleanup_sessions():
    now = time.time()
    expired = [t for t, s in _sessions.items() if now > s["expires_at"]]
    for t in expired:
        del _sessions[t]


# ---------------------------------------------------------------------------
# CF Access JWT validation
# ---------------------------------------------------------------------------

async def _fetch_cf_keys(team_domain: str) -> list:
    """Fetch CF Access public keys (cached for 1 hour)."""
    now = time.time()
    if _cf_keys_cache["keys"] and (now - _cf_keys_cache["fetched_at"]) < CF_KEYS_TTL:
        return _cf_keys_cache["keys"]
    try:
        url = f"https://{team_domain}.cloudflareaccess.com/cdn-cgi/access/certs"
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
            data = resp.json()
        keys = data.get("public_certs", []) or data.get("keys", [])
        _cf_keys_cache["keys"] = keys
        _cf_keys_cache["fetched_at"] = now
        return keys
    except Exception:
        return _cf_keys_cache["keys"]  # Return stale cache on failure


def _validate_cf_jwt_sync(token: str, keys: list, audience: str) -> bool:
    """Validate a CF Access JWT against public keys."""
    for key_data in keys:
        try:
            cert = key_data.get("cert", "")
            if not cert:
                continue
            public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data) if "n" in key_data else cert
            jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                audience=audience,
            )
            return True
        except (jwt.InvalidTokenError, Exception):
            continue
    return False


async def validate_cf_access(token: str, config: dict) -> bool:
    """Validate CF Access JWT. Returns True if valid."""
    team_domain = config.get("cf_team_domain")
    aud = config.get("cf_access_aud")
    if not team_domain or not aud:
        return False
    keys = await _fetch_cf_keys(team_domain)
    if not keys:
        return False
    return _validate_cf_jwt_sync(token, keys, aud)


# ---------------------------------------------------------------------------
# Auth check (used by middleware)
# ---------------------------------------------------------------------------

async def check_auth(request) -> bool:
    """Check if request is authenticated via any method. Returns True if auth passes.
    Sets request.state.service_scope if using a scoped service token."""
    config = get_node_config()

    # Path 1: X-Api-Key (worker-to-master)
    api_key = request.headers.get("X-Api-Key")
    if api_key and config and api_key == config.get("api_key"):
        return True

    # Path 2: CF Access JWT
    cf_jwt = request.headers.get("Cf-Access-Jwt-Assertion")
    if cf_jwt and config:
        if await validate_cf_access(cf_jwt, config):
            return True

    # Path 3: Session token or service token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        # Check session tokens first
        if validate_session(token):
            return True
        # Check scoped service tokens
        if token.startswith("svc_"):
            from node_config import get_service_token_scope
            scope = get_service_token_scope(token)
            if scope:
                request.state.service_scope = scope
                return True

    return False
