# UI Specification

**Status:** Complete

## Overview

inframatik uses a dark-themed web dashboard built with vanilla JavaScript, HTML, and CSS. There is no build step, no framework, and no npm dependency. The entire frontend consists of three static files (`index.html`, `style.css`, `app.js`) served by FastAPI's `StaticFiles` mount. The UI is responsive across desktop, tablet, and mobile breakpoints.

---

## Related Specs

| Spec | Relationship |
|------|-------------|
| [Stack](stack.md) | Frontend technology choices, CSS variables, fonts |
| [System Monitoring](system-monitoring.md) | Overview tab, GPU tab, processes tab, network tab, storage tab |
| [Service Management](service-management.md) | Service cards, new service modal, logs modal |
| [Clustering](clustering.md) | Sidebar node list, setup modal, settings panels |
| [Cloudflare Integration](cloudflare.md) | Tunnel section, CF setup wizard, route/access forms |

---

## Requirements

1. **No build step** -- All frontend files are served as-is. No transpilation, bundling, or npm.
2. **Dark theme only** -- All colors defined via CSS custom properties for consistency.
3. **Responsive** -- Functional on desktop, tablet (768px), and mobile (480px).
4. **XSS prevention** -- All user-provided strings rendered via the `esc()` helper (creates a text node and reads `textContent`).
5. **Session-based auth** -- Token stored in `sessionStorage`; all API calls include `Authorization: Bearer <token>` via the `api()` helper.
6. **Polling for live data** -- System metrics refresh every 5 seconds via `setInterval`.
7. **Cluster-aware** -- Sidebar appears on master nodes; node selection changes the API target to proxied endpoints.

---

## Design System

### CSS Variables

Defined on `:root` in `style.css`:

| Variable | Value | Purpose |
|----------|-------|---------|
| `--bg-primary` | `#0a0e17` | Page background |
| `--bg-secondary` | `#111827` | Topbar, sidebar, table headers |
| `--bg-card` | `#1a2234` | Card backgrounds |
| `--bg-card-hover` | `#1f2a40` | Card hover state |
| `--bg-input` | `#0f1623` | Form input backgrounds |
| `--border` | `#2a3550` | Default borders |
| `--border-light` | `#374463` | Hover borders |
| `--text-primary` | `#e2e8f0` | Primary text |
| `--text-secondary` | `#8892a8` | Secondary text, descriptions |
| `--text-muted` | `#5a6478` | Labels, hints, empty states |
| `--accent` | `#3b82f6` | Primary action color (blue) |
| `--accent-glow` | `rgba(59, 130, 246, 0.15)` | Accent background glow |
| `--green` | `#10b981` | Success, online, active |
| `--green-glow` | `rgba(16, 185, 129, 0.15)` | Green background glow |
| `--red` | `#ef4444` | Error, failed, danger |
| `--red-glow` | `rgba(239, 68, 68, 0.15)` | Red background glow |
| `--yellow` | `#f59e0b` | Warning, LAN badge |
| `--yellow-glow` | `rgba(245, 158, 11, 0.15)` | Yellow background glow |
| `--radius` | `12px` | Card border radius |
| `--radius-sm` | `8px` | Button/input border radius |
| `--shadow` | `0 4px 24px rgba(0, 0, 0, 0.3)` | Modal/card drop shadow |
| `--transition` | `0.2s ease` | Default transition timing |
| `--sidebar-width` | `220px` | Sidebar width (desktop) |

### Typography

- **UI text:** Inter (400, 500, 600 weights) -- loaded from Google Fonts
- **Code/data:** JetBrains Mono (400, 500 weights) -- used for port numbers, log output, API keys, version tags, process tables
- **Fallback stack:** `-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif`

### Color Semantics for Progress

The `progressColor(pct)` helper maps values to colors:
- `>= 90%` -- red (critical)
- `>= 70%` -- yellow (warning)
- `< 70%` -- green (normal)

CPU core bars use `coreColor(pct)` with finer granularity:
- `>= 90%` -- `var(--red)`
- `>= 60%` -- `var(--yellow)`
- `>= 30%` -- `var(--accent)` (blue)
- `< 30%` -- `var(--green)`

Temperature colors use `tempColor(c)`:
- `>= 85` -- `var(--red)`
- `>= 70` -- `var(--yellow)`
- `< 70` -- `var(--green)`

---

## Layout Structure

```
+------------------------------------------------------------------+
| Topbar (sticky top, z-index 100)                                 |
|  [Logo/Title]              [Tunnel status] [Uptime] [Version] [Settings gear] |
+--------+---------------------------------------------------------+
| Sidebar| Main Content (.container, max-width 1400px)             |
| (master|  +-- Host Info Bar                                      |
|  only) |  +-- System Section (tabs: Overview|GPUs|Processes|Network|Storage) |
|        |  +-- Tunnel Section (master only, tabs: Status|Routes|Access) |
|        |  +-- Services Section                                   |
+--------+---------------------------------------------------------+
```

### Topbar

- Sticky at top, `z-index: 100`, `backdrop-filter: blur(12px)`
- Left: Application title with node name appended (e.g., "inframatik my-server")
- Right: Tunnel status dot + text, uptime counter, version tag (git short hash in `JetBrains Mono`), settings gear button
- On mobile (480px): right section hidden entirely

### Sidebar

- Width: `var(--sidebar-width)` (220px), hidden by default (`display: none`)
- Shown only when node role is `master` (class `visible` toggled by JS)
- Contains "Nodes" heading and a list of `.sidebar-node` items
- Each node shows: status dot (green/red/yellow), node name, role tag ("MASTER"/"WORKER")
- Active node highlighted with `--bg-card` background and border
- On mobile (768px): becomes a fixed overlay sliding in from left, `z-index: 90`

### Main Content

- `.container` with `max-width: 1400px`, centered with auto margins
- Padding: 24px 32px (desktop), 16px (tablet)

---

## Shared Components

### Metric Cards

`.metric-card` inside `.metrics-grid` (CSS Grid, `auto-fit`, `minmax(200px, 1fr)`):
- Background: `--bg-card`, 1px `--border`, `--radius` border-radius
- Structure: `.metric-label` (uppercase 12px muted), `.metric-value` (28px bold), `.metric-sub` (12px secondary), optional `.progress-bar`
- Hover: border lightens to `--border-light`

### Progress Bars

`.progress-bar` (6px height, `--bg-primary` background):
- `.progress-fill` child with color classes: `.green`, `.yellow`, `.red`
- Width animated via `transition: width 0.6s ease`

### CPU Core Bars

`.cpu-cores` container (flexbox, 32px height):
- Individual `.cpu-core-bar` elements, `flex: 1`, height proportional to core usage
- Color set inline via `coreColor()` function

### Tabs

`.tabs` container with `.tab` buttons:
- Inactive: transparent background, muted text
- Active: `--bg-card` background, `--border` border, primary text
- Tab content panels: `.tab-content` (hidden by default), `.tab-content.active` (visible)
- System tabs: Overview, GPUs, Processes, Network, Storage
- Tunnel tabs: Status, Routes, Access Apps

### Modals

`.modal-overlay` with `.modal` child:
- Overlay: fixed, full viewport, `rgba(0,0,0,0.6)` background, `backdrop-filter: blur(4px)`, `z-index: 200`
- Modal: `--bg-card`, `--border`, `--radius`, 28px padding, 480px width (max 90vw)
- Active state: `.modal-overlay.active` sets `display: flex`
- Header: `.modal-header` with title and close button

### Forms

`.form-group` with label + input:
- Label: 12px uppercase, `--text-secondary`, 0.04em letter-spacing
- Input: `--bg-input`, `--border`, `--radius-sm`, 14px font, focus border changes to `--accent`
- Error display: `.form-error` (red text, 13px)
- Actions: `.form-actions` (flex, right-aligned, 8px gap)

### Buttons

`.btn` base with variants:
- Default: `--bg-secondary`, `--border`, `--text-secondary`
- `.btn.primary`: `--accent` background, white text
- `.btn.danger`: red text, red glow on hover
- `.btn.success`: green text, green glow on hover

### Service Cards

`.service-card` in `.services-list` (flex column, 8px gap):
- Layout: status dot + service info + port badge + action buttons
- `.service-status` dot: `.active` (green), `.inactive` (muted), `.failed` (red)
- `.service-name`: 15px bold
- `.service-meta`: 12px secondary with port, hostname link, LAN/CF badges
- `.service-port`: monospace, `--bg-primary` pill
- `.service-badge.lan`: yellow glow, "LAN" text
- `.service-badge.cf`: blue glow, "CF" text
- Actions: Start/Stop/Restart/Logs/Delete buttons

### Settings Panels

`.settings-modal-content` (540px width, scrollable):
- Subsections: `.settings-subsection` with `.settings-subsection-header`
- Info display: `.settings-info` with label/value pairs
- Option cards: `.settings-option` (clickable, hover accent border)
- Key display: `.key-display` with monospace `<code>` and Copy button

### CF Tables

`.cf-table-header` + `.cf-table-row` (CSS Grid):
- 3-column layout for routes: hostname, service, actions
- 4-column layout for access apps: name, domain, policy, actions
- Header: `--bg-secondary`, uppercase labels
- Rows: `--bg-card`, hover to `--bg-card-hover`

### Process Table

`.process-table` with `.proc-header` + `.proc-row`:
- Grid: `80px 1fr 80px 80px` (PID, Name, CPU%, Mem%)
- Header: `--bg-secondary`, uppercase Inter labels
- Rows: JetBrains Mono, `--text-secondary`, right-aligned CPU/Mem columns

---

## Login Screen

Shown when no valid session token exists (`#login-screen`):

### Password Login Form (`#login-form`)
- Centered card (380px max-width, 120px top margin)
- Title: "inframatik" (24px, centered)
- Password input with Enter key handler calling `submitLogin()`
- Error display for invalid credentials
- Calls `POST /api/auth/login` with `{password}`
- On success: stores token in `sessionStorage`, loads dashboard

### Set Password Form (`#set-password-form`)
- Shown on first run when `GET /api/auth/status` returns `has_password: false`
- Two fields: password + confirm (minimum 8 characters)
- Calls `POST /api/auth/set-password`
- On success: automatically logs in

---

## Setup Modal

First-run wizard shown when node config returns `role: "unconfigured"` and no password is set:

### Step 1: Role Selection (`#setup-choose`)
- Three option cards in a 3-column grid (`.setup-grid`):
  - **Standalone**: Single machine dashboard
  - **Master**: Central node for multi-machine clusters
  - **Worker**: Connect to an existing master

### Step 2a: Name Form (`#setup-name`)
- For standalone/master roles
- Node name input
- Calls `POST /api/config/init-standalone` or `POST /api/config/init-master`

### Step 2b: Worker Form (`#setup-worker`)
- Node name, master address, enrollment token inputs
- Calls `POST /api/nodes/enroll` on the master, then `POST /api/config/init-worker` locally

---

## Responsive Breakpoints

### Tablet (max-width: 768px)
- Topbar padding: 12px 16px
- Container padding: 16px
- Metrics grid: 2 columns
- Metric card padding: 14px, value font: 22px
- Service cards: wrap layout, actions span full width
- Sidebar: fixed overlay, slide-in from left, 240px width
- Settings grid: single column

### Mobile (max-width: 480px)
- Metrics grid: single column
- Topbar right section: hidden
- CF add form row: stacked vertically
- GPU cards: no minimum width

---

## State Patterns

### Loading States
- Initial dashboard load calls `initCluster()` then starts polling
- "Loading routes..." / "Loading access apps..." placeholder text in CF tables
- Tunnel status shows "Tunnel: checking..." until first response

### Empty States
- `.empty-state`: centered text, dashed border, `--text-muted`
- Services: "No services registered yet. Add one to get started."
- Settings workers list: "No workers enrolled yet."

### Error Display
- API errors shown in `.form-error` elements (red text)
- 401 responses trigger automatic redirect to login screen (token cleared from `sessionStorage`)
- Network errors logged to console

### Polling and Data Refresh
- `refreshInterval` set to 5000ms, calls `refreshData()` which fetches `/api/system` (or `/api/nodes/{id}/system` for remote nodes)
- Sidebar node list refreshed every 10 seconds via `sidebarInterval`
- Tunnel status refreshed alongside system metrics
- CF section loaded once on first view, then on node switch

---

## Data Flow

```
Browser boots
  -> GET /api/auth/status (check if password set)
  -> If no password: show set-password form
  -> If password set: show login form
  -> POST /api/auth/login -> receive session token
  -> Store token in sessionStorage
  -> GET /api/node/info (determine role)
  -> If unconfigured: show setup modal
  -> If configured: load dashboard
     -> GET /api/system (every 5s)
     -> GET /api/services
     -> GET /api/tunnel
     -> If master: GET /api/nodes (sidebar), GET /api/config
```

---

## Key JavaScript Functions

| Function | Purpose |
|----------|---------|
| `api(method, path, body, extraHeaders)` | Central API caller; adds Bearer token, handles 401 redirect |
| `esc(s)` | XSS-safe string escaping via DOM text node |
| `refreshData()` | Fetches system metrics, updates all metric cards |
| `initCluster()` | Determines node role, configures sidebar, starts polling |
| `renderServices(services)` | Renders service card list with status dots and action buttons |
| `loadSettingsView()` | Populates settings modal based on current role |
| `openNewService()` / `submitNewService()` | New service modal workflow |
| `showLogs(name)` / `closeLogs()` | Service log viewer modal |
| `openSettings()` / `closeSettings()` | Settings modal open/close |
| `showSetupForm(role)` / `submitSetup()` | First-run setup wizard |

---

## Decisions

| Decision | Alternatives Considered | Why This Choice |
|----------|------------------------|-----------------|
| Vanilla JS, no framework | React, Vue, Svelte | Zero build step, no npm. Dashboard is read-heavy with simple forms. Total JS is ~1500 lines. |
| `innerHTML` with `esc()` | DOM API, template literals | Faster to write, `esc()` prevents XSS. No complex component trees needed. |
| `sessionStorage` for token | `localStorage`, cookies | Token cleared on tab close for security. No CSRF concerns. |
| CSS Grid for metrics | Flexbox | `auto-fit` with `minmax` provides ideal responsive behavior without JS. |
| Polling (5s interval) | WebSocket, SSE | Simpler implementation. Acceptable latency for monitoring dashboard. |
| Single app.js file | ES modules, multiple files | No build step means no bundler. Single file keeps it simple for the scale. |
| Google Fonts CDN | Self-hosted, system fonts | Inter and JetBrains Mono are modern and readable. CDN avoids bundling font files. |
