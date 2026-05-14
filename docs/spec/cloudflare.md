# Cloudflare Integration Specification

**Status:** Complete

## Overview

inframatik integrates with the Cloudflare API to provide secure public access to services via Cloudflare Tunnels, DNS management, and Zero Trust Access applications. Configuration is done through a guided setup wizard in the settings UI. CF credentials are stored in the node's `node.json` config file. The integration supports per-node tunnels, dashboard protection, and automatic tunnel+DNS+Access provisioning when services are registered with a hostname.

---

## Related Specs

| Spec | Relationship |
|------|-------------|
| [Stack](stack.md) | httpx for CF API calls, cloudflared binary, 10s timeout |
| [UI](ui.md) | Tunnel section with tabs, CF setup wizard in settings, route/access add forms |
| [Service Management](service-management.md) | Auto CF setup on service registration with hostname |
| [Clustering](clustering.md) | Master creates tunnels for workers, pushes token to worker |

---

## Requirements

1. **Optional integration** -- CF is not required. All CF features gracefully degrade when unconfigured.
2. **Guided setup** -- Wizard validates token, discovers accounts/zones/policies, saves config.
3. **Credentials in node.json** -- No separate config file. CF token, account ID, zone ID, and default policy ID stored alongside node config.
4. **Tunnel-per-node** -- Each node (master or worker) gets its own tunnel. Tunnel ID stored in node config.
5. **Automatic service exposure** -- When a service is registered with a hostname, create tunnel route + DNS CNAME + Access app.
6. **Dashboard protection** -- Dashboard itself can be placed behind CF Access via a dedicated endpoint.
7. **Worker tunnel setup** -- Master can create a tunnel for a worker and push the connector token remotely.
8. **cloudflared managed in userland** -- User-level service at `~/.config/systemd/user/cloudflared.service`, started and managed without sudo.

---

## Setup Wizard

The CF setup wizard is a multi-step flow triggered from the settings modal:

### Step 1: Validate Token

- User enters a Cloudflare API token
- `POST /api/cf/setup/validate-token {token}`
- Calls `GET https://api.cloudflare.com/client/v4/accounts` with Bearer token
- Returns list of accessible accounts
- Error if token is invalid or no accounts accessible

### Step 2: Select Account and List Zones

- User selects account from dropdown
- `POST /api/cf/setup/zones {token, account_id}`
- Calls `GET https://api.cloudflare.com/client/v4/zones` filtered by account and active status
- Returns list of zones (domains)

### Step 3: Select Zone and List Policies

- User selects zone from dropdown
- `POST /api/cf/setup/policies {token, account_id}`
- Calls `GET https://api.cloudflare.com/client/v4/accounts/{id}/access/policies`
- Returns existing reusable Access policies
- Option to create a new policy: `POST /api/cf/setup/create-policy {token, account_id, name, email_domain}`

### Step 4: Save Configuration

- `POST /api/cf/setup/save {token, account_id, zone_id, default_policy_id}`
- Stores all values in node.json

### Clear Configuration

- `DELETE /api/cf/setup`
- Removes `cf_token`, `cf_account_id`, `cf_zone_id`, `cf_default_policy_id` from node.json

---

## Credentials Storage

Stored in `node.json` alongside other node configuration:

| Key | Value | Description |
|-----|-------|-------------|
| `cf_token` | string | Cloudflare API bearer token |
| `cf_account_id` | string | Selected Cloudflare account ID |
| `cf_zone_id` | string | Selected DNS zone ID |
| `cf_default_policy_id` | string or null | Default Access policy for new apps |
| `tunnel_id` | string or null | This node's tunnel UUID |
| `dashboard_hostname` | string or null | Hostname for dashboard CF Access |

For workers managed by a master, the worker's `tunnel_id` is also stored in the master's config under `workers.{node_id}.tunnel_id`.

---

## Tunnel Operations

All CF API calls use `httpx.AsyncClient` with 10-second timeout and Bearer token auth.

### Create Tunnel

`create_tunnel(name) -> {id, name}`

- `POST /accounts/{account_id}/cfd_tunnel`
- Payload: `{name, tunnel_secret}` (secret = base64-encoded 32 random bytes)
- Returns tunnel ID and name

### Initialize Tunnel Config

`init_tunnel_config(tunnel_id) -> bool`

- `PUT /accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations`
- Sets initial config with catch-all 404 rule: `{config: {ingress: [{service: "http_status:404"}]}}`

### Get Tunnel Token

`get_tunnel_token(tunnel_id) -> str`

- `GET /accounts/{account_id}/cfd_tunnel/{tunnel_id}/token`
- Returns the connector token used by cloudflared to authenticate

### List Tunnels

`list_tunnels() -> [{id, name, status}]`

- `GET /accounts/{account_id}/cfd_tunnel?is_deleted=false`
- Filters to non-deleted tunnels

### Get Tunnel Routes (Ingress)

`get_tunnel_routes(tunnel_id?) -> [{hostname, service}]`

- `GET /accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations`
- Extracts ingress rules, filters to those with hostnames (excludes catch-all)

### Add Tunnel Route

`add_tunnel_route(hostname, service, tunnel_id?) -> bool`

- GET current config, append new rule before catch-all, PUT updated config
- `service` can be a port number (converted to `http://localhost:{port}`) or a full URL
- Raises `ValueError` if route for hostname already exists

### Remove Tunnel Route

`remove_tunnel_route(hostname, tunnel_id?) -> bool`

- GET current config, filter out matching hostname, PUT updated config
- Raises `ValueError` if no route found for hostname

---

## DNS Operations

### Create CNAME Record

`create_dns_record(hostname, tunnel_id?) -> record_id`

- `POST /zones/{zone_id}/dns_records`
- Payload: `{type: "CNAME", name: subdomain, content: "{tunnel_id}.cfargotunnel.com", proxied: true}`
- Subdomain extracted as first part of hostname (before first dot)

### Delete DNS Record

`delete_dns_record(hostname) -> bool`

- `GET /zones/{zone_id}/dns_records?type=CNAME&name={hostname}` to find record
- `DELETE /zones/{zone_id}/dns_records/{id}` to remove it
- Returns `false` if no matching record found

### List DNS Records

`list_dns_records() -> [{id, type, name, content, proxied}]`

- `GET /zones/{zone_id}/dns_records` with pagination (100 per page)
- Returns all record types in the zone

---

## Access Operations

### Create Access Application

`create_access_app(name, hostname, policy_id) -> app_id`

- `POST /accounts/{account_id}/access/apps`
- Payload:
  ```json
  {
    "name": "app-name",
    "domain": "app.example.com",
    "type": "self_hosted",
    "session_duration": "24h",
    "app_launcher_visible": true,
    "policies": [{"id": "policy-uuid", "precedence": 1}]
  }
  ```

### Delete Access Application

`delete_access_app(hostname) -> bool`

- `GET /accounts/{account_id}/access/apps` to list all apps
- Find app matching domain == hostname
- `DELETE /accounts/{account_id}/access/apps/{id}`

### List Access Applications

`list_access_apps() -> [{id, name, domain, type, session_duration, policies}]`

- `GET /accounts/{account_id}/access/apps`

### List Access Policies (Discovery)

`list_access_policies() -> [{id, name, decision}]`

- Discovers reusable policies by iterating all Access apps and extracting unique policies
- Used by the setup wizard and Access app creation form

### Create Reusable Policy

`POST /api/cf/setup/create-policy {token, account_id, name, email_domain}`

- Creates a policy with `decision: "allow"` and `include: [{email_domain: {domain: "..."}}]`
- `POST /accounts/{account_id}/access/policies`

---

## Dashboard Access

Dedicated flow to place the inframatik dashboard (port 9000) behind CF Access:

### Enable (`POST /api/config/dashboard-access`)

1. If node has no tunnel, create one + init config + start cloudflared
2. Add ingress route for hostname pointing to `http://localhost:9000`
3. Create DNS CNAME record
4. Create Access app with default policy (if configured)
5. Store hostname in `dashboard_hostname` config field

### Disable (`DELETE /api/config/dashboard-access`)

1. Remove tunnel ingress route (best-effort)
2. Delete DNS record (best-effort)
3. Delete Access app (best-effort)
4. Clear `dashboard_hostname` from config

---

## Worker Tunnel Setup

Enrollment flow for workers:

1. Master includes its saved `cf_token`, `cf_account_id`, selected zone, and Access metadata in the enrollment response when Cloudflare is configured.
2. Worker saves that config locally.
3. Worker creates its own Cloudflare tunnel using its local config.
4. Worker initializes tunnel ingress config, gets the connector token, starts its local cloudflared user service, and stores `tunnel_id` in its own node.json.
5. Worker reports `tunnel_id` to the master via `POST /api/nodes/tunnel`, and heartbeats continue to include `tunnel_id`.

Manual fallback for already-enrolled or older workers:

### `POST /api/nodes/{node_id}/cf/setup`

1. Create tunnel (named after worker)
2. Get connector token from CF API
3. Initialize tunnel ingress config (catch-all 404)
4. Push token to worker: proxy `POST /api/cf/token {tunnel_id, token}` to worker
5. Store `tunnel_id` in master's worker config after the worker accepts the token

### Worker Token Receiver (`POST /api/cf/token`)

1. Validate `X-Api-Key` header
2. Store `tunnel_id` in worker's node.json
3. Call local setup helper to:
   - Write token to `~/.config/inframatik/cf-tunnel-token` (mode 600)
   - Create/update `~/.config/systemd/user/cloudflared.service`
   - Run `systemctl --user daemon-reload` and `systemctl --user enable --now cloudflared.service`

---

## Tunnel Status

Parsed from cloudflared's local Prometheus metrics endpoint.

### `GET /api/tunnel`

Fetches `http://127.0.0.1:20241/metrics` and parses:

| Metric | Prometheus Line | Parsing |
|--------|----------------|---------|
| Connection count | `cloudflared_tunnel_ha_connections {value}` | Extract float, convert to int |
| Edge locations | `cloudflared_tunnel_server_locations{edge_location="{loc}"} 1` | Extract location string |

**Response:**

```json
{
  "connected": true,
  "connections": 4,
  "locations": ["DFW", "IAH", "ORD", "LAX"],
  "detail": "4 connection(s) -- DFW, IAH, ORD, LAX",
  "routes": [
    {"hostname": "app.example.com", "service": "http://localhost:8000"},
    {"hostname": "dash.example.com", "service": "http://localhost:9000"}
  ]
}
```

If cloudflared is unreachable (connect error or timeout):

```json
{
  "connected": false,
  "connections": 0,
  "detail": "cloudflared unreachable",
  "routes": []
}
```

---

## API Endpoints

### Setup Wizard

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/cf/setup/validate-token` | POST | Validate CF token, return accounts |
| `/api/cf/setup/zones` | POST | List zones for account |
| `/api/cf/setup/policies` | POST | List reusable Access policies |
| `/api/cf/setup/create-policy` | POST | Create new email-domain policy |
| `/api/cf/setup/save` | POST | Store CF credentials in node config |
| `/api/cf/setup` | DELETE | Remove CF credentials |

### Tunnel Management

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/cf/tunnels` | GET | List all tunnels |
| `/api/cf/tunnels` | POST | Create new tunnel |
| `/api/cf/tunnels/{id}/token` | GET | Get tunnel connector token |

### Ingress Routes

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/cf/routes` | GET | List ingress routes (optional `tunnel_id` query param) |
| `/api/cf/routes` | POST | Add ingress route `{hostname, service, tunnel_id?}` |
| `/api/cf/routes/{hostname}` | DELETE | Remove ingress route |

### DNS Records

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/cf/dns` | GET | List all DNS records in zone |
| `/api/cf/dns` | POST | Create CNAME record `{hostname, tunnel_id?}` |
| `/api/cf/dns/{hostname}` | DELETE | Delete DNS record |

### Access Applications

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/cf/access/apps` | GET | List all Access apps |
| `/api/cf/access/apps` | POST | Create Access app `{name, hostname, policy_id}` |
| `/api/cf/access/apps/{hostname}` | DELETE | Delete Access app by hostname |
| `/api/cf/access/policies` | GET | Discover reusable policies |

### Dashboard Access

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/config/dashboard-access` | POST | Enable dashboard CF Access `{hostname}` |
| `/api/config/dashboard-access` | DELETE | Disable dashboard CF Access |

### Worker Tunnel

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/nodes/tunnel` | POST | Worker reports its local tunnel ID to master (X-Api-Key auth) |
| `/api/nodes/{id}/cf/setup` | POST | Manual fallback: create tunnel for worker and push connector token |
| `/api/cf/token` | POST | Worker receives tunnel token (X-Api-Key auth) |

### Tunnel Status (combined with routes)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/tunnel` | GET | Local tunnel status + ingress routes |
| `/api/nodes/{id}/tunnel` | GET | Proxied tunnel status for a specific node |

---

## Data Model

### CF Fields in node.json

```json
{
  "cf_token": "cf-api-token-...",
  "cf_account_id": "account-uuid",
  "cf_zone_id": "zone-uuid",
  "cf_default_policy_id": "policy-uuid",
  "tunnel_id": "tunnel-uuid",
  "dashboard_hostname": "dash.example.com"
}
```

### Worker CF Fields in Master's Config

```json
{
  "workers": {
    "node-id": {
      "name": "gpu-server",
      "address": "http://192.168.1.20:9000",
      "api_key": "sdk_...",
      "tunnel_id": "worker-tunnel-uuid"
    }
  }
}
```

### cloudflared Userland Files

| File | Purpose |
|------|---------|
| `~/.config/inframatik/cf-tunnel-token` | Tunnel connector token (mode 600) |
| `~/.config/systemd/user/cloudflared.service` | User-level cloudflared service unit |
| `~/.local/bin/cloudflared` | cloudflared binary installed by installer (optional step) |

---

## UI Components

### Tunnel Section (Dashboard)

Visible when tunnel is configured. Three tabs:

**Status Tab:**
- Tunnel Status card: "Connected"/"Disconnected" with colored text
- Connections card: number of active HA connections
- Location card: edge locations joined by commas
- Tunnel ID card: displayed in monospace

**Routes Tab:**
- Table with hostname and service columns + delete button
- Add Route form: hostname input, service input (e.g., `http://localhost:8001`), Add button

**Access Apps Tab:**
- Table with name, domain, policy, and delete button columns (4-column grid)
- Add Access App form: name input, hostname input, policy dropdown, Add button

### CF Setup Wizard (Settings)

Multi-step form within the settings modal subsection:
1. Token input with "Validate" button
2. Account dropdown (auto-populated)
3. Zone dropdown (auto-populated)
4. Policy dropdown with "Create Policy" option
5. "Save" and "Clear" buttons

### Worker Tunnel Badge

In the sidebar and settings worker list, workers with a `tunnel_id` show a blue "CF" badge.

---

## Error Handling

| Scenario | Handling |
|----------|---------|
| CF not configured | Functions return empty results or raise `ValueError`. Routes return `[]`. |
| Invalid CF token | 401 from validate endpoint |
| No accounts accessible | 400: "No accounts accessible with this token" |
| CF API timeout (10s) | `httpx.TimeoutException` caught, raises `ValueError` or returns empty |
| CF API error response | Error message extracted from CF `errors` array |
| Route already exists | 400: "Route for {hostname} already exists" |
| Route not found | 400: "No route found for {hostname}" |
| cloudflared unreachable | Tunnel status returns `{connected: false}` |
| cloudflared setup fails (user service) | 500 with setup/runtime error detail |
| Dashboard access without CF config | 400: "Cloudflare not configured. Set up in Settings." |
| CF cleanup on service delete | Best-effort, errors silently caught |
| CF setup on service register | Best-effort, logged at debug level, `cf_route_added: false` |

---

## Decisions

| Decision | Alternatives Considered | Why This Choice |
|----------|------------------------|-----------------|
| Credentials in node.json | Separate cf.env, vault, env vars | Single config file. Consistent with all other config. File permissions for security. |
| Guided setup wizard | Manual config file editing, CLI | Users shouldn't need to know CF API internals. Wizard discovers and validates everything. |
| One tunnel per node | Shared tunnel, tunnel per service | Isolates nodes. Clean ingress management. Worker tunnels independent. |
| User-level cloudflared | System-level, Docker | Removes runtime sudo and sudoers. Keeps token + service under user-owned paths with least privilege. |
| Direct Python setup + systemctl --user | Helper script + sudoers | Fewer moving parts and no privileged helper surface. |
| Prometheus metrics for status | CF API status endpoint, health check | cloudflared exposes metrics locally. Fast, no API call. Real-time connection info. |
| Policy discovery from apps | Dedicated policy list API | Reusable policies found by scanning existing apps. Works with the CF API structure. Also uses direct `/access/policies` endpoint in wizard. |
| 10s timeout for CF API calls | 5s, 30s | CF API is usually fast. 10s handles slow responses without blocking too long. |
