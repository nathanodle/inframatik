# Jank Cleanup Plan

## Goals
- Remove confusing or misleading behavior.
- Reduce maintenance risk from duplicated or oversized logic.
- Tighten error handling so failures are explicit and debuggable.

## P0: Correctness / UX Mismatch

### [x] 1) Enrollment token API/UI shape mismatch
- Files:
  - `cluster_routes.py`
  - `static/app.js`
- Problem:
  - Backend returns enrollment token objects (`token`, `created_at`, `expires_at`) but UI treats entries as plain strings.
- Fix:
  - Update `renderEnrollmentTokens` to handle object shape.
  - Use `t.token` for copy/cancel actions.
  - Render created/expiry metadata in UI.
  - Add regression test for `/api/config` enrollment token response shape and UI behavior expectations (where practical).
- Acceptance:
  - Copy/cancel works for generated tokens.
  - No `[object Object]` rendering in settings.

### [x] 2) Bootstrap partial-config edge case
- Files:
  - `node_config.py`
  - `cluster_routes.py`
- Problem:
  - `set_admin_password` can write password-only config before role fields exist; other routes assume `role/node_id/node_name` are present.
- Fix:
  - Add a config normalizer/guard so routes safely treat missing role metadata as `unconfigured`.
  - Ensure password setup path does not leave a shape that breaks `/api/node/info` and `/api/config`.
  - Add tests for first-run sequence: set password -> read node info/config.
- Acceptance:
  - First-run auth/setup flow never throws due to missing role fields.

### [x] 3) Installer reports password-set success without checking response
- File:
  - `install.sh`
- Problem:
  - Script prints success even if `/api/auth/set-password` failed.
- Fix:
  - Check HTTP status or parse response for `"status":"password_set"` and fail loudly otherwise.
  - Print server detail on failure.
- Acceptance:
  - Installer exits non-zero when password setup fails.

## P1: Reliability / Maintainability

### [x] 4) Worker deploy fanout is serial
- File:
  - `cluster_routes.py`
- Problem:
  - Deploy loop builds task list but awaits each coroutine sequentially.
- Fix:
  - Use `asyncio.gather` (with error capture) for concurrent worker push.
  - Preserve per-worker result structure.
- Acceptance:
  - Deploy duration scales with slowest worker, not sum of all workers.

### [x] 5) `proxy._handle_local` is a monolithic route switchboard
- Files:
  - `proxy.py`
  - (coordination with `main.py` route behavior)
- Problem:
  - Duplicate route behavior and manual query parsing increase drift risk.
- Fix:
  - Break into smaller handlers (`services`, `tunnel`, `cf-service`).
  - Use shared parsing helpers for query params and service-name extraction.
  - Keep response shape parity with main endpoints.
- Acceptance:
  - Reduced function size/branching.
  - Existing behavior preserved (validated by smoke tests).

### [x] 6) Silent registry corruption fallback
- File:
  - `services.py`
- Problem:
  - Corrupt `services.json` becomes empty dict silently.
- Fix:
  - Log warning/error with path and parse exception.
  - Consider backup-and-recover behavior (`services.json.bak`) before fallback.
- Acceptance:
  - Corruption is visible in logs and does not silently erase operator context.

## P2: Cleanup / Consistency

### [x] 7) UI has nonfunctional service-token revoke action
- File:
  - `static/app.js`
- Problem:
  - Button exists but endpoint requires raw token value not retained by UI.
- Fix options:
  - Preferred: add token IDs/server-side opaque handles for revoke.
  - Interim: remove/disable button until revoke UX is implemented.
- Acceptance:
  - No dead-end action in UI.

### [x] 8) CLI help text mismatch (`mcp` “coming soon”)
- File:
  - `inframatik-cli.py`
- Fix:
  - Update help text to reflect current behavior.

### [x] 9) Broad/silent exceptions and small dead code
- Files:
  - `nodes.py`, `cf_routes.py`, `updater.py`, `mcp_routes.py`, `updater.py`
- Fix:
  - Replace silent `except`/`pass` with scoped catches + logs.
  - Remove unused imports/constants.
- Progress:
  - Removed dead code (`mcp_routes` unused import, `updater` unused include constant).
  - Replaced heartbeat-loop silent swallow with debug logging in `nodes.py`.
  - Narrowed CF setup wizard broad catches in `cf_routes.py` to explicit network/parse failures.

## Execution Order
1. P0.1 enrollment token mismatch
2. P0.2 bootstrap partial-config guard
3. P0.3 installer password success check
4. P1.4 deploy fanout concurrency
5. P1.5 proxy decomposition
6. P1.6 services registry error visibility
7. P2 cleanup items

## Tracking
- Mark each item as:
  - `[ ] not started`
  - `[~] in progress`
  - `[x] done`
