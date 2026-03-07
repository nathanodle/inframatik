# inframatik Specification

**inframatik** — System dashboard, service manager, and AI deployment platform for Linux machines.

A FastAPI + vanilla JS application that monitors system metrics, manages services via systemd, clusters multiple nodes with master/worker architecture, integrates with Cloudflare for public access and Zero Trust, and provides an MCP server for AI agent deployments.

---

## Spec Components

### Planning
| Document | Status | Description |
|----------|--------|-------------|
| [Build Order](build-order.md) | ✅ Complete | Implementation order (retrospective) |

### Shared Specs
| Document | Status | Description |
|----------|--------|-------------|
| [Stack](stack.md) | ✅ Complete | Tech stack, infrastructure, data flow |
| [Backend](backend.md) | ✅ Complete | Architecture, project structure, API conventions, auth |
| [UI](ui.md) | ✅ Complete | Dashboard layout, design system, components |

### Feature Specs
| Document | Status | Description |
|----------|--------|-------------|
| [System Monitoring](system-monitoring.md) | ✅ Complete | CPU, memory, disk, network, GPU, temps, processes |
| [Service Management](service-management.md) | ✅ Complete | Register, start/stop, logs, port assignment, systemd units |
| [Clustering](clustering.md) | ✅ Complete | Master/worker, enrollment, heartbeats, proxy, deploy |
| [Cloudflare Integration](cloudflare.md) | ✅ Complete | Tunnels, DNS, Access apps, dashboard protection, setup wizard |
| [Authentication](authentication.md) | ✅ Complete | Password, sessions, CF JWT, API keys, service tokens |
| [AI Agent Integration](ai-agents.md) | ✅ Complete | MCP server, CLI tool, .inframatik, harness detection |

---

## Project Scope

### Phase 1: Core (Complete)
- System monitoring dashboard
- Service management with auto port assignment
- Multi-node clustering with enrollment tokens
- Authentication (password + sessions)

### Phase 2: Cloudflare + Security (Complete)
- CF setup wizard (auto-discover account/zones/policies)
- Tunnel, DNS, Access app management
- Dashboard CF Access protection
- CF JWT bypass authentication
- Security hardening (systemd injection prevention, session limits, etc.)

### Phase 3: AI Agent Platform (Complete)
- Scoped service tokens
- Built-in MCP server (streamable HTTP)
- CLI tool (`inframatik init`)
- Claude Code + Codex integration
- .inframatik config file with inline instructions

### Future
- HTTP MCP endpoint improvements (streaming responses, resource exposure)
- Persistent session storage (survive restarts)
- Multi-user support (user accounts, RBAC)
- Service health checks and auto-restart policies
- Log aggregation and search
- Metrics history and graphs

---

## Target Users

- **Developers** managing services on Linux servers (homelab, GPU rigs, small clusters)
- **AI coding agents** (Claude Code, Codex) deploying and managing apps
- **Teams** with multiple machines that need centralized monitoring and service management

---

## External References

- [FastAPI](https://fastapi.tiangolo.com/)
- [Cloudflare Tunnel API](https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/)
- [Cloudflare Access API](https://developers.cloudflare.com/api/resources/zero_trust/subresources/access/)
- [MCP Specification](https://modelcontextprotocol.io/)
- [systemd User Services](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)
