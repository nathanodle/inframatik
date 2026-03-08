import secrets
import time
import logging
import os
from typing import Optional

import bcrypt
import httpx
import jwt

from node_config import get_node_config

# In-memory session store: token -> {expires_at}
_sessions: dict[str, dict] = {}
_login_failures: dict[str, dict] = {}

# CF public key cache
_cf_keys_cache: dict[str, dict] = {}
CF_KEYS_TTL = 3600  # 1 hour
CF_KEYS_MAX_STALE = 86400  # 24 hours

SESSION_COOKIE_NAME = "inframatik_session"
SESSION_COOKIE_SECURE = os.getenv("INFRAMATIK_SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes")

logger = logging.getLogger("inframatik.auth")


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
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_ATTEMPTS = 6
LOGIN_BASE_BACKOFF_SECONDS = 2
LOGIN_MAX_BACKOFF_SECONDS = 300
MAX_LOGIN_TRACKED_CLIENTS = 2000


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


def clear_all_sessions():
    _sessions.clear()
    _login_failures.clear()


def _cleanup_sessions():
    now = time.time()
    expired = [t for t, s in _sessions.items() if now > s["expires_at"]]
    for t in expired:
        del _sessions[t]


def _cleanup_login_failures(now: float):
    stale = []
    for client_id, state in _login_failures.items():
        attempts = [t for t in state.get("attempts", []) if (now - t) <= LOGIN_WINDOW_SECONDS]
        state["attempts"] = attempts
        blocked_until = float(state.get("blocked_until", 0))
        if attempts:
            continue
        if blocked_until > now:
            continue
        stale.append(client_id)
    for client_id in stale:
        _login_failures.pop(client_id, None)


def _ensure_login_state(client_id: str, now: float) -> dict:
    state = _login_failures.setdefault(client_id, {"attempts": [], "blocked_until": 0.0, "last_seen": now})
    state["last_seen"] = now
    return state


def _evict_login_clients():
    if len(_login_failures) <= MAX_LOGIN_TRACKED_CLIENTS:
        return
    oldest = sorted(
        _login_failures.items(),
        key=lambda item: item[1].get("last_seen", 0),
    )
    to_remove = len(_login_failures) - MAX_LOGIN_TRACKED_CLIENTS
    for i in range(to_remove):
        _login_failures.pop(oldest[i][0], None)


def login_is_allowed(client_id: str) -> tuple[bool, int]:
    """Return (allowed, retry_after_seconds) for a login attempt from client_id."""
    now = time.time()
    _cleanup_login_failures(now)
    state = _login_failures.get(client_id)
    if not state:
        return True, 0

    blocked_until = float(state.get("blocked_until", 0))
    if blocked_until > now:
        return False, max(1, int(blocked_until - now))

    attempts = state.get("attempts", [])
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        overflow = len(attempts) - LOGIN_MAX_ATTEMPTS
        delay = min(LOGIN_BASE_BACKOFF_SECONDS * (2 ** overflow), LOGIN_MAX_BACKOFF_SECONDS)
        state["blocked_until"] = now + delay
        logger.warning(
            "Login throttle triggered for %s after %d failures in %ds",
            client_id,
            len(attempts),
            LOGIN_WINDOW_SECONDS,
        )
        return False, int(delay)
    return True, 0


def record_failed_login(client_id: str) -> int:
    """Record a failed login attempt and return any imposed backoff in seconds."""
    now = time.time()
    _cleanup_login_failures(now)
    state = _ensure_login_state(client_id, now)

    attempts = state.get("attempts", [])
    attempts.append(now)
    state["attempts"] = [t for t in attempts if (now - t) <= LOGIN_WINDOW_SECONDS]

    failures = len(state["attempts"])
    if failures >= LOGIN_MAX_ATTEMPTS:
        overflow = failures - LOGIN_MAX_ATTEMPTS
        delay = min(LOGIN_BASE_BACKOFF_SECONDS * (2 ** overflow), LOGIN_MAX_BACKOFF_SECONDS)
        state["blocked_until"] = max(float(state.get("blocked_until", 0)), now + delay)
        logger.warning(
            "Repeated login failures from %s: failures=%d window=%ds backoff=%ds",
            client_id,
            failures,
            LOGIN_WINDOW_SECONDS,
            delay,
        )
    _evict_login_clients()

    blocked_until = float(state.get("blocked_until", 0))
    if blocked_until > now:
        return max(1, int(blocked_until - now))
    return 0


def record_successful_login(client_id: str):
    _login_failures.pop(client_id, None)


# ---------------------------------------------------------------------------
# CF Access JWT validation
# ---------------------------------------------------------------------------

def _normalize_cf_team_domain(team_domain: str) -> str:
    team_domain = team_domain.strip().lower()
    if team_domain.startswith("https://"):
        team_domain = team_domain[len("https://"):]
    elif team_domain.startswith("http://"):
        team_domain = team_domain[len("http://"):]
    team_domain = team_domain.split("/", 1)[0]
    if team_domain.endswith(".cloudflareaccess.com"):
        team_domain = team_domain[: -len(".cloudflareaccess.com")]
    return team_domain


async def _fetch_cf_keys(team_domain: str) -> list:
    """Fetch CF Access public keys (domain-scoped cache with bounded stale fallback)."""
    team_domain = _normalize_cf_team_domain(team_domain)

    if not team_domain:
        return []

    now = time.time()
    entry = _cf_keys_cache.get(team_domain, {"keys": [], "fetched_at": 0, "last_success_at": 0})
    if entry["keys"] and (now - entry["fetched_at"]) < CF_KEYS_TTL:
        return entry["keys"]

    try:
        url = f"https://{team_domain}.cloudflareaccess.com/cdn-cgi/access/certs"
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
            data = resp.json()
        keys = data.get("public_certs", []) or data.get("keys", [])
        if not keys:
            raise ValueError("No CF Access certs returned")
        _cf_keys_cache[team_domain] = {
            "keys": keys,
            "fetched_at": now,
            "last_success_at": now,
        }
        return keys
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        if entry["keys"] and (now - entry.get("last_success_at", 0)) <= CF_KEYS_MAX_STALE:
            entry["fetched_at"] = now
            _cf_keys_cache[team_domain] = entry
            return entry["keys"]
        return []


def _validate_cf_jwt_sync(token: str, keys: list, audience: str, issuer: Optional[str] = None) -> bool:
    """Validate a CF Access JWT against public keys."""
    from cryptography.x509 import load_pem_x509_certificate

    for key_data in keys:
        try:
            cert_pem = key_data.get("cert", "")
            if not cert_pem:
                continue
            # CF returns X.509 certificates — extract the public key
            cert = load_pem_x509_certificate(cert_pem.encode())
            public_key = cert.public_key()

            decode_kwargs = {
                "algorithms": ["RS256"],
                "audience": audience,
            }
            if issuer:
                decode_kwargs["issuer"] = issuer
            jwt.decode(
                token,
                public_key,
                **decode_kwargs,
            )
            return True
        except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
            continue
    return False


async def validate_cf_access(token: str, config: dict) -> bool:
    """Validate CF Access JWT. Auto-discovers team domain from JWT issuer if not configured."""
    aud = config.get("cf_access_aud")
    team_domain = _normalize_cf_team_domain(config.get("cf_team_domain", ""))

    # Auto-discover team domain from JWT issuer claim if not configured
    if not team_domain:
        try:
            # Decode WITHOUT verification just to peek at issuer
            unverified = jwt.decode(token, options={"verify_signature": False, "verify_aud": False})
            iss = unverified.get("iss", "")
            logger.warning("CF JWT auto-discovery: iss=%s, aud_from_jwt=%s", iss, unverified.get("aud"))
            # iss must be https://<team>.cloudflareaccess.com — reject anything else
            if iss.endswith(".cloudflareaccess.com") or ".cloudflareaccess.com/" in iss:
                team_domain = iss.split("//")[-1].split(".cloudflareaccess.com")[0]
                if not team_domain or "/" in team_domain or "." in team_domain:
                    logger.warning("CF JWT: rejected suspicious team_domain=%s", team_domain)
                    team_domain = ""  # reject suspicious values
                else:
                    logger.warning("CF JWT: discovered team_domain=%s", team_domain)
                # Also auto-discover AUD if not set
                if not aud:
                    aud = unverified.get("aud", [None])[0] if isinstance(unverified.get("aud"), list) else unverified.get("aud")
            else:
                logger.warning("CF JWT: issuer '%s' is not cloudflareaccess.com", iss)
        except Exception as e:
            logger.warning("CF JWT: failed to peek at unverified claims: %s", e)
            pass

    if not team_domain or not aud:
        logger.warning("CF JWT: missing team_domain=%s or aud=%s", team_domain, bool(aud))
        return False

    issuer = config.get("cf_access_issuer")
    if not issuer:
        issuer = f"https://{team_domain}.cloudflareaccess.com"
    logger.warning("CF JWT: fetching keys for team_domain=%s, aud=%s..., issuer=%s", team_domain, str(aud)[:20], issuer)
    keys = await _fetch_cf_keys(team_domain)
    if not keys:
        logger.warning("CF JWT: no keys returned from CF")
        return False
    logger.warning("CF JWT: got %d keys, validating...", len(keys))
    valid = _validate_cf_jwt_sync(token, keys, aud, issuer=issuer)
    logger.warning("CF JWT: validation result=%s", valid)

    # Auto-store discovered values for future use
    if valid and not config.get("cf_team_domain"):
        try:
            from node_config import save_node_config
            config["cf_team_domain"] = team_domain
            if not config.get("cf_access_aud"):
                config["cf_access_aud"] = aud
            save_node_config(config)
            logger.info("Auto-discovered CF team domain: %s", team_domain)
        except Exception:
            pass

    return valid


# ---------------------------------------------------------------------------
# Auth check (used by middleware)
# ---------------------------------------------------------------------------

async def check_auth(request) -> bool:
    """Check if request is authenticated via any method. Returns True if auth passes.
    Sets request.state.service_scope/service_capability if using a scoped service token."""
    config = get_node_config()

    # Path 1: X-Api-Key (worker-to-master)
    api_key = request.headers.get("X-Api-Key")
    if api_key and config and api_key == config.get("api_key"):
        return True

    # Path 2: CF Access JWT (header from CF proxy, or cookie from browser)
    cf_jwt_header = request.headers.get("Cf-Access-Jwt-Assertion", "")
    cf_jwt_cookie = request.cookies.get("CF_Authorization", "")
    cf_jwt = cf_jwt_header or cf_jwt_cookie
    if cf_jwt and config:
        logger.warning("CF JWT found (header=%d, cookie=%d), jwt=%s...", len(cf_jwt_header), len(cf_jwt_cookie), cf_jwt[:80])
        try:
            result = await validate_cf_access(cf_jwt, config)
            if result:
                logger.debug("CF JWT validation succeeded")
                return True
            else:
                logger.debug("CF JWT validation returned False")
        except Exception as e:
            logger.warning("CF JWT validation error: %s", e)

    # Path 3: Session cookie
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME, "")
    if session_cookie and validate_session(session_cookie):
        return True

    # Path 4: Session token or service token via Authorization header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        # Check session tokens first
        if validate_session(token):
            return True
        # Check scoped service tokens
        if token.startswith("svc_"):
            from node_config import get_service_token_auth
            token_auth = get_service_token_auth(token)
            if token_auth and token_auth.get("service"):
                request.state.service_scope = token_auth["service"]
                request.state.service_capability = token_auth["capability"]
                return True

    return False
