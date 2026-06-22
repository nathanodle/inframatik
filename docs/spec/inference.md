# Inference Specification

**Status:** Draft

## Overview

inframatik will manage local LLM inference servers as first-class **profiles**. A profile combines a model artifact, a user-configured engine launcher, runtime configuration, endpoint exposure, and generated systemd service lifecycle.

This is different from normal service management. Normal services are arbitrary commands. Inference profiles are structured configurations that inframatik understands, validates, renders into engine-specific commands, and monitors on the node that owns them.

Supported engines for the initial design:

1. `llama.cpp`
2. `vLLM`
3. `SGLang`

---

## Related Specs

| Spec | Relationship |
|------|-------------|
| [Model Storage](model-storage.md) | Profiles reference model artifacts and snapshots |
| [Service Management](service-management.md) | Inference instances are implemented as generated systemd user services |
| [Backend](backend.md) | API conventions, JSON config, auth, proxying |
| [UI](ui.md) | Inference tab, profile editor, model inventory, logs |
| [Clustering](clustering.md) | Master can remotely manage worker-local inference state through existing proxy patterns |
| [Cloudflare Integration](cloudflare.md) | Optional public endpoints and Access protection |
| [Authentication](authentication.md) | Admin/session auth, scoped MCP tokens, and engine API key secret handling |
| [AI Agent Integration](ai-agents.md) | MCP resources/tools for AI-assisted profile configuration |
| [Build Order](build-order.md) | Planned implementation sequence, acceptance criteria, and test strategy |

---

## Cross-Spec Ownership

| Area | Owning spec | Notes |
|------|-------------|-------|
| Model files, manifests, imports, downloads, model jobs | [Model Storage](model-storage.md) | Inference profiles reference artifacts by ID/snapshot and do not duplicate manifests |
| Profiles, launchers, resolved instances, operations, client bundles | This spec | Node-local source of truth for inference lifecycle and rendered commands |
| Cloudflare tunnels, DNS, Access apps, policies, service-token API calls | [Cloudflare Integration](cloudflare.md) | Inference stores profile-facing metadata and cleanup records; Cloudflare spec owns API behavior |
| Browser/session auth, `svc_` service tokens, draft `mcp_` tokens | [Authentication](authentication.md) | Existing `svc_` tokens remain service-scoped; inference mutations use scoped `mcp_` tokens |
| MCP resources and tool schemas | [AI Agent Integration](ai-agents.md) | MCP tools call the same services as REST/UI; they are not a separate source of truth |
| Implementation order, acceptance criteria, test strategy | [Build Order](build-order.md) | Local-first build sequence for the inference MVP |

---

## Goals

1. **Profile-first management** - Users create model-serving profiles, not one-off shell commands.
2. **Engine abstraction without hiding power** - Common options are normalized, advanced engine-specific args remain available.
3. **Local-first serving** - Each node runs inference locally from local model storage.
4. **Operational control** - Start, stop, restart, logs, health, endpoint, and metrics are visible from the dashboard.
5. **Explicit profile setup** - Profile creation starts from a known model artifact and a user-provided engine path without compatibility guessing.
6. **Node-local with remote control** - Each node owns its inference state; master can manage a selected worker remotely.
7. **Safe escape hatches** - Raw args and env vars exist, but are validated and visibly separated from common settings.
8. **AI-assisted configuration** - Expose enough MCP context and safe operations for an assistant to help configure profiles from local hardware, model manifests, logs, and configured launchers.

---

## Non-Goals

1. **Do not route worker inference traffic through master** - Clients should hit the target node endpoint, Cloudflare route, or future router directly.
2. **Do not fully automate every model quirk** - Profiles expose structured fields and raw overrides for model-specific settings.
3. **Do not build a hosted inference platform** - This is local server management for user-controlled machines.
4. **Do not replace existing normal services** - Inference profiles are a specialized layer beside service management.
5. **Do not require Cloudflare** - Public exposure is optional.

---

## Terminology

| Term | Definition |
|------|------------|
| Engine | Inference runtime family: `llama.cpp`, `vLLM`, or `SGLang` |
| Engine launcher | User-configured command path, optional base args, working directory, and env for an engine on one node |
| Model artifact | Managed local model package from [Model Storage](model-storage.md) |
| Profile | User-defined serving configuration for a model and engine launcher |
| Instance | Running systemd service generated from a profile |
| Adapter | Backend module that maps profile config to an engine command, health checks, and metrics |
| Endpoint | Local or public URL clients use to call the inference server |

---

## Product Model

The primary user objects are:

```text
Engine launcher + Model artifact -> Profile -> Instance
```

Example:

```json
{
  "id": "qwen-coder-vllm",
  "display_name": "Qwen Coder",
  "engine": "vllm",
  "engine_launcher": {
    "id": "vllm-main",
    "executable": "/home/aiml/vllm/bin/vllm",
    "base_args": [],
    "working_dir": null,
    "env": {}
  },
  "model_ref": {
    "artifact_id": "qwen3-coder-30b-a3b",
    "snapshot": "2026-06-21-main"
  },
  "common": {
    "served_model_name": "qwen-coder",
    "context_length": 65536,
    "dtype": "auto",
    "gpu_memory_utilization": 0.9,
    "tensor_parallel": 2,
    "host": "127.0.0.1",
    "port": 10001
  },
  "deployment": {
    "mode": "single",
    "replicas": 1,
    "port_policy": {
      "mode": "auto",
      "range": "inference"
    },
    "gpu_policy": {
      "mode": "profile",
      "claim_mode": "exclusive"
    }
  },
  "engine_config": {
    "trust_remote_code": false,
    "enable_prefix_caching": true
  },
  "advanced": {
    "args": ["--max-num-seqs", "64"],
    "env": {}
  },
  "exposure": {
    "mode": "cloudflare",
    "hostname": "qwen.example.com",
    "cloudflare": {
      "access_mode": "service_token",
      "access_app_id": "uuid-or-null",
      "service_tokens": [
        {
          "id": "uuid-or-null",
          "name": "qwen-coder-client",
          "client_id": "abc123.access",
          "created_by_inframatik": true,
          "active": true
        }
      ],
      "aud": "access-aud-tag-or-null"
    }
  }
}
```

---

## Profile Registry

Profiles should be stored in a dedicated node-local registry, not in `node.json` and not in `services.json`.

Default file:

```text
~/.config/inframatik/inference_profiles.json
```

Related state files:

| File | Purpose |
|------|---------|
| `node.json` | Node identity, auth, Cloudflare credentials, and node-level settings such as inference port range, model store root, and import allowlist roots |
| `inference_profiles.json` | Desired inference profiles plus resolved instances, ports, GPU placement, units, and exposure metadata |
| `inference_engine_launchers.json` | User-provided launcher paths, base argv, working directory, and launcher env |
| `inference_secrets.json` | Local secret material such as generated engine API keys; restrictive permissions; never exposed through normal APIs or MCP |
| `inference_operations.json` | Recent long-running inference operations and progress, excluding model download/import jobs |
| `inference_cleanup.json` | Retryable external cleanup records, mainly failed Cloudflare cleanup after local profile deletion |

Model inventory and model jobs are owned by the model storage spec (`models.json` and persisted download/import job records). Inference profiles reference model artifacts by ID and snapshot; they do not duplicate model manifests.

Registry rules:

1. Each node owns its local inference registry files.
2. A master edits worker inference state by proxying to the worker; the worker performs local validation, locking, writes, unit rendering, and lifecycle operations.
3. Generated systemd units are derived artifacts. The profile registry is the source of truth.
4. Profile registries are JSON files with a top-level `schema_version` so later migrations have an explicit hook.
5. Writes should be atomic: write a temporary file, fsync where practical, and rename over the target.
6. Mutations should use an in-process per-file lock in MVP. Advisory lock files can be added later if inframatik ever supports multiple local app processes.
7. The profile registry stores desired config and resolved deployment facts, but not raw secrets, live health, logs, or model file manifests.

---

## Lifecycle and State Model

The registry stores desired configuration and resolved deployment facts. Live process state is derived from systemd, health checks, engine endpoints, and current node metrics.

Persisted profile fields:

| Field | Why persisted |
|-------|---------------|
| Profile config | Source of truth for command rendering |
| Engine launcher reference | Stable path/env/base-argv used to start the selected runtime on this node |
| Resolved instances | Stable ports, GPU placement, and unit names |
| Reserved ports | Prevent endpoint churn across restarts/edits |
| Resolved GPU assignments | Show planned placement before start and avoid accidental reshuffle |
| Unit names | Stable lifecycle/log lookup |
| Cloudflare metadata | Cleanup and UI display for route, DNS, Access app, policy, and token metadata |
| Secret references | Point to engine API key or token metadata without storing raw secrets |
| `created_at`, `updated_at` | UI and audit context |

Derived state:

| State | Source |
|-------|--------|
| Running/stopped/failed | `systemctl --user` |
| PID | systemd |
| Logs | `journalctl --user` |
| TCP reachability | Local socket check |
| Model served | `/v1/models` or engine-specific endpoint |
| Request/token metrics | Engine metrics where available |
| GPU memory/utilization | Existing node metrics |
| Cloudflare tunnel status | Existing tunnel status path |

Instance resolution happens at profile save time:

1. Validate profile shape, model reference, engine launcher, requested replicas, ports, and GPU policy.
2. Allocate or preserve instance ports.
3. Resolve GPU assignment.
4. Persist `instances`.
5. Render systemd units.
6. Provision optional Cloudflare resources if exposure requires them.

Saving a profile should make the planned layout visible even before the user starts it. Start should not be the first time routine allocation failures appear.

Profile preview uses the same planner in dry-run mode. The UI, REST API, and MCP can submit a draft profile and receive validation blockers, warnings, resolved instance preview, GPU placement, port plan, rendered command, redacted env, systemd unit preview, and Cloudflare provisioning plan without writing registry files, reserving ports, generating secrets, rendering units, starting systemd, or calling Cloudflare.

Save must re-run validation and planning under the profile registry lock before writing. Preview results are advisory because ports, GPU claims, launchers, model inventory, and Cloudflare state can change between preview and save.

### Validation Model

Validation returns blockers and warnings separately. Blockers prevent save/start. Warnings are visible in the editor, command preview, REST responses, and MCP tool output, but they do not prevent save/start.

Blockers:

1. Model artifact or snapshot does not exist on the selected node.
2. Engine launcher path is missing, not executable, or has an invalid engine family.
3. Required profile fields are missing or malformed.
4. Requested GPU IDs do not exist on the selected node.
5. Requested instance count and GPU placement mode cannot resolve.
6. Requested port is unavailable and not already owned by this profile's generated unit.
7. Contiguous port policy cannot allocate the required block.
8. Host/exposure configuration is incoherent.
9. Cloudflare exposure is selected but required local Cloudflare config is missing.
10. Raw args/env cannot be represented as safe argv/env values.
11. A running inframatik profile overlaps the requested GPU assignment while either profile uses `exclusive` GPU claim mode.

Warnings:

1. Likely VRAM oversubscription or poor fit.
2. Current GPU free memory is lower than the profile appears to need.
3. `trust_remote_code` is enabled.
4. LAN or Cloudflare endpoint has no engine API key.
5. Cloudflare exposure has no Access Service Auth policy.
6. Artifact lives under a previous model-store root.
7. Launcher path/env changed on a running profile.
8. Engine-specific field may not be supported by the selected engine version.
9. Manual port is outside the configured inference range.
10. A shared GPU assignment overlaps another running shared inframatik profile.

VRAM fit is warning-only in MVP. It should never block save/start unless the requested GPU IDs are invalid or conflict with another running inframatik profile under GPU claim rules. Fit estimates are too model-, kernel-, quantization-, context-, and concurrency-dependent to be reliable as hard gates in the first version.

Preview response shape:

```json
{
  "valid_for_save": true,
  "blockers": [],
  "warnings": [],
  "resolved_instances": [
    {
      "index": 0,
      "host": "127.0.0.1",
      "port": 10000,
      "gpu_ids": [0],
      "unit": "infra-llm-qwen-small@0.service"
    }
  ],
  "port_plan": {
    "mode": "auto",
    "range": "inference",
    "allocated": [10000],
    "persisted": false
  },
  "gpu_plan": {
    "mode": "one_per_instance",
    "claim_mode": "exclusive",
    "assignments": [{"index": 0, "gpu_ids": [0]}]
  },
  "command_preview": {
    "argv": ["/home/aiml/vllm/bin/vllm", "serve", "/data/models/qwen"],
    "env": {"CUDA_VISIBLE_DEVICES": "0"},
    "redacted_env_keys": []
  },
  "systemd_preview": {
    "units": [{"index": 0, "name": "infra-llm-qwen-small@0.service", "content": "..."}]
  },
  "cloudflare_plan": {
    "would_provision": false,
    "resources": []
  },
  "restart_required": false
}
```

Preview must be side-effect free. It must not create engine API keys or Cloudflare service-token secrets; it should report that a secret would be generated on save when the draft asks inframatik to generate one.

Profile registry shape:

```json
{
  "schema_version": 1,
  "profiles": {
    "qwen-small": {
      "schema_version": 1,
      "id": "qwen-small",
      "display_name": "Qwen Small",
      "engine": "vllm",
      "engine_launcher_id": "vllm-main",
      "model": {
        "artifact_id": "qwen3-8b",
        "snapshot": "main"
      },
      "common": {},
      "engine_config": {},
      "deployment": {
        "mode": "replicated",
        "replicas": 6,
        "port_policy": {
          "mode": "auto",
          "range": "inference",
          "prefer_contiguous": true
        },
        "gpu_policy": {
          "mode": "one_per_instance",
          "claim_mode": "exclusive",
          "gpu_ids": [0, 1, 2, 3, 4, 5]
        }
      },
      "instances": [
        {
          "index": 0,
          "port": 10000,
          "host": "127.0.0.1",
          "gpu_ids": [0],
          "unit": "infra-llm-qwen-small@0.service"
        },
        {
          "index": 1,
          "port": 10001,
          "host": "127.0.0.1",
          "gpu_ids": [1],
          "unit": "infra-llm-qwen-small@1.service"
        }
      ],
      "secrets": {
        "engine_api_key_id": "secret-profile-qwen-small-api-key"
      },
      "cloudflare": {
        "hostname": null,
        "access_app_id": null,
        "access_policy_id": null,
        "service_tokens": []
      },
      "created_at": 1782086400,
      "updated_at": 1782086400
    }
  }
}
```

Start behavior:

1. Verify model artifact snapshot exists locally.
2. Verify the configured engine launcher path exists and is executable on the target node.
3. When explicitly requested from the launcher UI or validation endpoint, run a bounded runtime smoke probe using the launcher executable, base args, working directory, launcher env, and `--help`. The probe must redact secret-looking argv/output and report exit code, timeout, command preview, and recent output. Profile preview remains path-only to keep form edits fast and side-effect free.
4. Verify resolved ports are still free or already owned by this profile's units.
5. Start each resolved unit.
6. Wait for each unit to reach systemd active state and TCP readiness within the profile startup grace period.
7. Treat `/v1/models` and model-name checks as health refinement, not as hard start gates for MVP.
8. If every instance reaches systemd active plus TCP readiness, return success with per-instance results.
9. Instances may continue from `starting` to `healthy` as API/model checks become available.
10. If any instance fails or misses TCP readiness before grace expires, stop every instance that was started by this operation.
11. Return failure with per-instance results, rollback actions, last failure reason, and log pointers.

Restart behavior:

1. Restart is all-or-nothing for profile-level actions.
2. Stop all resolved instances.
3. Start all resolved instances using the same start behavior.
4. If any instance fails after restart begins, stop all instances started by this restart and return failure.
5. Per-instance restart remains available for targeted recovery and does not roll back other instances.
6. Rolling restart orchestration is deferred.

### Operation Records

Long-running inference work should be represented as node-local operation records with progress. Model downloads/imports remain model-storage jobs; inference operations cover profile, systemd, and Cloudflare work.

Default file:

```text
~/.config/inframatik/inference_operations.json
```

Operation kinds:

| Kind | Examples |
|------|----------|
| `profile_create` | Create profile, allocate ports, write registry, render units, optionally provision Cloudflare |
| `profile_update` | Save edits, re-render units, optionally update Cloudflare, optionally restart |
| `profile_delete` | Stop if required by explicit action, remove units, remove local registry, create cleanup record if external cleanup fails |
| `profile_start` | Start all resolved instances and wait for systemd active plus TCP readiness |
| `profile_stop` | Stop all resolved instances |
| `profile_restart` | Stop all, start all transactionally, rollback on failed start |
| `instance_start` / `instance_stop` / `instance_restart` | Target one resolved instance |
| `cloudflare_cleanup_retry` | Retry a pending external cleanup record |

Operation states: `queued`, `running`, `succeeded`, `failed`, `failed_interrupted`, `canceled`.

Operation record shape:

```json
{
  "schema_version": 1,
  "operations": {
    "op_abc123": {
      "id": "op_abc123",
      "kind": "profile_restart",
      "state": "running",
      "profile_id": "qwen-small",
      "node_id": "worker-1",
      "current_step": "waiting_tcp",
      "steps": [
        {"name": "validate", "state": "succeeded"},
        {"name": "stop_units", "state": "succeeded"},
        {"name": "start_units", "state": "succeeded"},
        {"name": "waiting_tcp", "state": "running"}
      ],
      "runtime_status": {
        "phase": "waiting_ready",
        "instance_index": 0,
        "unit": "infra-llm-qwen-small@0.service",
        "host": "127.0.0.1",
        "port": 10000,
        "systemd_state": "active",
        "tcp_reachable": false,
        "restart_count": 0,
        "elapsed_seconds": 42.0,
        "timeout_seconds": 600.0,
        "wait_position": 1,
        "wait_total": 1
      },
      "progress": 70,
      "started_at": 1782086400,
      "updated_at": 1782086460,
      "finished_at": null,
      "error": null,
      "result": null
    }
  }
}
```

Operation behavior:

1. Long-running actions return an operation record immediately, normally with HTTP `202 Accepted`.
2. Short actions may complete before the response, but should still return an operation-shaped result when they use the operation runner.
3. The UI should poll the operation until terminal state, then refresh profile status, instance status, logs, and cleanup records.
4. The worker that owns the target node creates and updates the operation record. Master only proxies and displays it.
5. Operation updates are best-effort progress, not a second source of truth. Actual status still comes from profile registry, systemd, health checks, logs, and Cloudflare cleanup records.
6. At app startup, any operation recorded as `queued` or `running` becomes `failed_interrupted`. The UI should explain that inframatik restarted and then show reconciled live profile/systemd state.
7. Cancelation is deferred except for queued operations that have not begun side effects. Running systemd/Cloudflare operations should finish or fail and then reconcile.
8. Keep recent terminal operations for a bounded retention window, for example the latest 100 operations or seven days per node.
9. Error results should include per-instance status, rollback actions attempted, cleanup record IDs, and bounded log pointers where relevant.
10. While profile or instance start waits for readiness, operation updates should include throttled `runtime_status` facts such as the current instance, target host/port, systemd active state, TCP readiness, restart count, elapsed time, and timeout. These facts are delivered through the normal operation event stream so the UI can explain what is happening before success or failure.

Operation concurrency:

1. Only one mutating operation may run for a profile at a time.
2. Profile create locks the requested profile ID before committing it. If no ID was supplied, ID generation happens under the node planning/write lock.
3. Profile update, delete, start, stop, restart, and per-instance lifecycle operations acquire that profile's operation lock.
4. If another mutating operation is already active for the profile, return `409 Conflict` with the active `operation_id` instead of queueing hidden work.
5. Node-level planning/write work uses a short in-process lock while validating against current profile registry, reserving ports, resolving GPU claims, checking profile IDs, writing registry files, and rendering unit files.
6. Do not hold the node planning/write lock while waiting for model server startup, TCP readiness, systemd transitions, Cloudflare API calls, or log reads.
7. Unrelated profiles may start or restart concurrently after their planning/write phase completes, as long as their resolved ports and GPU claim rules do not conflict.
8. Launcher create/update/delete operations acquire the launcher registry lock and conflict with profile planning only while profile commands are being rendered.
9. Model download/import jobs remain independent, but profile save/start validation must fail or warn according to the current model artifact state at planning time.
10. Cleanup retry operations for the same cleanup record are mutually exclusive. Cleanup retries should not block unrelated profile starts.

Stop behavior:

1. Stop all resolved instances.
2. Treat already stopped or failed units as stopped for profile-level stop.
3. Keep profile config, reserved ports, generated units, engine API key metadata, and Cloudflare exposure metadata.
4. Do not remove tunnel routes, DNS records, Access apps, policies, or service-token metadata.
5. Public endpoints for stopped profiles may return backend/tunnel errors until the profile is started again.
6. Return per-instance stop results.

Edit behavior:

1. Stopped profiles can be edited and re-rendered immediately.
2. Display-only metadata changes on running profiles apply immediately.
3. Runtime, engine, model, placement, port, and exposure changes require a restart before they affect running units.
4. MVP should avoid hidden pending configs. For running profiles, the UI should offer an explicit "save and restart" path for operational changes.
5. The editor should make restart-required changes visible before save.

Delete behavior:

1. Stop running instances.
2. Remove generated units.
3. Release instance port reservations.
4. Remove local profile registry entry.
5. Attempt Cloudflare route, DNS, Access app, and policy cleanup.
6. Offer to delete inframatik-created Access service token when no other profile references it.
7. If Cloudflare cleanup fails, create a retryable cleanup record instead of blocking local deletion.
8. Never delete model artifacts unless the user explicitly asks from model storage.

Cloudflare cleanup record:

```json
{
  "id": "cleanup-qwen-small-1782086400",
  "kind": "inference_cloudflare",
  "profile_id": "qwen-small",
  "hostname": "qwen.example.com",
  "resources": {
    "tunnel_route": true,
    "dns_record_id": "uuid-or-null",
    "access_app_id": "uuid-or-null",
    "access_policy_id": "uuid-or-null",
    "service_tokens": [
      {
        "id": "uuid-or-null",
        "owned": true
      }
    ]
  },
  "state": "pending",
  "last_error": "cloudflare api timeout",
  "created_at": 1782086400,
  "updated_at": 1782086400
}
```

Cleanup behavior:

1. UI shows cleanup records as **Cloudflare cleanup pending**.
2. Retry cleanup attempts only resources still present in the record.
3. Successful resource deletion updates the record so retries are idempotent.
4. Forget cleanup record removes local retry metadata only and never calls Cloudflare.
5. Externally managed service tokens are never deleted automatically.
6. Local profile deletion should not be held hostage by Cloudflare API availability.

Exposure removal behavior:

1. Removing Cloudflare exposure from a profile is explicit and separate from stop.
2. Remove tunnel route, DNS record, Access app, policy, and profile exposure metadata.
3. Offer to delete inframatik-created Access service token when no other profile references it.
4. Regenerate the unit if bind host, port, or API key behavior changes.
5. Require restart if the profile is running.

---

## Port Allocation and Replicated Instances

Normal inframatik services currently allocate ports from `8000-8999`, and the inframatik dashboard listens on `9000`. Inference should use a separate configurable range so model servers and app services do not intermix.

Default ranges:

```json
{
  "port_ranges": {
    "services": {"start": 8000, "end": 8999},
    "inference": {"start": 10000, "end": 10999}
  }
}
```

`10000-10999` is the default inference range because it is clearly separate from normal services and the dashboard. The range is still node-local and configurable because GPU hosts often already run monitoring, model, or vendor tools on arbitrary ports.

Allocator rules:

1. Port allocation runs on the target node. A master asks the worker to allocate worker-local inference ports.
2. A port is available only if it is not present in the normal service registry, not present in the inference profile instance registry, and not currently bound on the local machine.
3. Allocations are persisted before writing units or starting engines to avoid duplicate assignment during concurrent profile creation.
4. Existing instance ports are stable across restarts and profile edits unless the user explicitly changes the port policy.
5. If replica count grows, keep existing instance ports and allocate only the new ones.
6. If replica count shrinks, release the highest-index instance reservations first.
7. Manual ports are allowed for inference profiles but must pass the same collision checks. Ports outside the inference range should show a warning and require explicit confirmation.

Deployment plan:

```json
{
  "deployment": {
    "mode": "replicated",
    "replicas": 6,
    "port_policy": {
      "mode": "auto",
      "range": "inference",
      "prefer_contiguous": true
    },
    "gpu_policy": {
      "mode": "one_per_instance",
      "claim_mode": "exclusive",
      "gpu_ids": [0, 1, 2, 3, 4, 5]
    }
  }
}
```

Resolved instances are persisted with the profile:

```json
{
  "instances": [
    {"index": 0, "port": 10000, "gpu_ids": [0], "unit": "infra-llm-small@0.service"},
    {"index": 1, "port": 10001, "gpu_ids": [1], "unit": "infra-llm-small@1.service"},
    {"index": 2, "port": 10002, "gpu_ids": [2], "unit": "infra-llm-small@2.service"}
  ]
}
```

Replica port policy modes:

| Mode | Behavior |
|------|----------|
| `auto` | Allocate the requested number of free inference ports, preferring contiguous blocks when available |
| `contiguous` | Require one contiguous block; fail validation if not available |
| `explicit` | Use the exact port list supplied by the user |

GPU policy modes:

| Mode | Behavior |
|------|----------|
| `profile` | Use the profile's placement fields as a single instance |
| `one_per_instance` | Assign one GPU per replica |
| `contiguous_groups` | Assign contiguous GPU groups sized by tensor/pipeline parallel requirements |
| `explicit` | Use exact per-instance GPU lists supplied by the user |

GPU claim modes:

| Mode | Behavior |
|------|----------|
| `exclusive` | Default. A running profile blocks overlapping GPU assignment by another running inframatik profile. |
| `shared` | Allows intentional overlap with other shared profiles; overlap is warning-only. |

Claim mode is stored with `deployment.gpu_policy` and applies to every resolved instance in the profile. Overlap is blocked when either the running profile or the requested profile uses `exclusive`. Non-inframatik GPU usage remains advisory because inframatik cannot know whether that process is temporary, compatible, or intentionally sharing the device.

For shell consistency, inference can extend `ports.env` with generated variables:

```text
INFRA_LLM_SMALL_PORT=10000
INFRA_LLM_SMALL_0_PORT=10000
INFRA_LLM_SMALL_1_PORT=10001
```

The unsuffixed variable points to instance `0` for convenience. Normal service variables remain unchanged.

Public exposure for replicated profiles is deferred. MVP should expose local endpoints per instance. A single public hostname for a replicated profile should require a future router/load-balancer profile, not silently pick one replica.

---

## Endpoint Exposure

Inference profiles support the same product choice as normal services: keep the endpoint local, expose it on the LAN, or publish it through Cloudflare. The security model is different from browser dashboard access because inference endpoints are APIs consumed by clients and agents, not primarily humans in a browser.

Exposure modes:

| Mode | Bind host | Cloudflare | Intended use |
|------|-----------|------------|--------------|
| `local` | `127.0.0.1` | No | Local apps, SSH tunnel, same-machine clients |
| `lan` | `0.0.0.0` | No | Private network clients |
| `cloudflare` | `127.0.0.1` | Yes | Public hostname through this node's Cloudflare tunnel |

Cloudflare mode should create:

1. Tunnel ingress route to the resolved local instance port.
2. DNS CNAME for the selected hostname.
3. Cloudflare Access application for the hostname.
4. API-oriented Access policy when requested.

For inference APIs, the default Cloudflare protection should be **Access Service Auth with a Cloudflare Access service token**, not identity-provider login. Cloudflare Access service tokens are a Client ID and Client Secret pair that clients send as request headers:

```text
CF-Access-Client-Id: <client_id>
CF-Access-Client-Secret: <client_secret>
```

This is distinct from the Cloudflare API token stored in inframatik. The Cloudflare API token lets inframatik manage Cloudflare resources. The Access service token is a client credential for calling a protected inference hostname.

Cloudflare exposure schema:

```json
{
  "exposure": {
    "mode": "cloudflare",
    "hostname": "llm.example.com",
    "cloudflare": {
      "access_mode": "service_token",
      "access_app_id": "uuid-or-null",
      "service_tokens": [
        {
          "id": "uuid-or-null",
          "name": "llm-client",
          "client_id": "abc123.access",
          "created_by_inframatik": true,
          "active": true
        }
      ],
      "aud": "access-aud-tag-or-null"
    }
  }
}
```

Access modes:

| Mode | Behavior | MVP |
|------|----------|-----|
| `none` | Tunnel + DNS only; rely on engine API key or upstream controls | Allowed with warning |
| `identity` | Browser-style CF Access login policy | Optional, mostly useful for docs/UI consoles |
| `service_token` | CF Access Service Auth policy for machine clients | Recommended default |
| `mtls` | Cloudflare/API Shield client certificate authentication | Deferred |
| `jwt_validation` | Cloudflare API Shield validates caller JWTs at edge | Deferred |

Service token management:

1. inframatik may create a Cloudflare Access service token through the Cloudflare API if its CF API token has `Access: Service Tokens Write`.
2. The returned Client Secret must be shown once and not stored in profile JSON.
3. Store only metadata needed for cleanup and UI display: service token ID, name, client ID, created time, expiration, active/retired state, and associated profile/hostname.
4. Users may also select or paste an existing Cloudflare service token ID/name if they manage tokens outside inframatik.
5. A profile can have multiple service tokens attached to the same Access Service Auth policy so clients can be rolled without downtime.
6. Deleting an inference profile should remove the Access app, DNS record, tunnel route, and optionally service tokens inframatik created if no other profile references them.

Service token actions:

| Action | Behavior | Secret handling | Use when |
|--------|----------|-----------------|----------|
| Generate new client | Create a new Cloudflare Access service token and add it to the profile's Service Auth policy alongside existing active tokens | New Client ID and Client Secret returned once | Planned rollout, multiple clients, no-downtime credential replacement |
| Rotate existing client | Rotate the selected Cloudflare Access service token secret through Cloudflare and keep the same service token metadata/policy attachment | New Client Secret returned once; old secret should be treated as no longer usable | Emergency secret exposure, single-client replacement, periodic rotation when clients can update immediately |
| Retire client | Remove the service token from the profile's Access policy, and optionally delete it if inframatik owns it and nothing else references it | No secret returned | After clients have moved to a new token |

Generate-new is the preferred no-downtime flow because both old and new service tokens can be accepted by the Access policy during client migration. Rotate-existing is a fast replacement operation and should show a stronger warning that clients using the previous secret may fail until updated.

Provisioning behavior when the user selects Cloudflare + Service Token:

1. Allocate or use the profile instance port.
2. Add Cloudflare tunnel route and DNS record for the hostname.
3. Create or select a Cloudflare Access service token.
4. Create a Service Auth Access policy with `decision: "non_identity"` and active `include.service_token.token_id` entries.
5. Create the Access application with that policy attached.
6. Show the Client ID and Client Secret once, along with a copyable client example.

If an existing service token is selected, inframatik can attach it to the policy but cannot recover or display its Client Secret. The UI should say that clearly.

If an externally managed service token is attached, inframatik should not rotate or delete it by default. The UI may offer a guarded rotate action only when the Cloudflare API token has permission and the user explicitly confirms they understand the previous client secret may stop working.

Engine API keys remain useful with Cloudflare enabled. Recommended public API posture for MVP:

```text
Cloudflare Access service token + engine native API key
```

The Cloudflare service token blocks unauthenticated Internet traffic at the edge. The engine API key protects direct LAN/local access and preserves compatibility with OpenAI-style clients that already send `Authorization: Bearer <key>`.

Single-header service-token mode can be considered later for clients that only support one custom header. For MVP, document the two standard Cloudflare headers and expose them in generated client examples.

---

## Client Connection Bundles

Each profile should expose a client connection view that answers "how do I call this model?" without requiring the user to assemble URLs, headers, and examples manually.

Client bundles are rendered views over profile metadata, not independent credentials. They may persist names, notes, selected exposure mode, selected Cloudflare service-token ID, selected engine API-key reference, and example preferences, but they must never persist raw Cloudflare Client Secrets or raw engine API keys.

Bundle fields:

| Field | Notes |
|-------|-------|
| `id` | Stable local bundle ID |
| `name` | Human label such as `litellm-router`, `desktop-dev`, or `ci-client` |
| `profile_id` | Owning inference profile |
| `target_type` | `profile` for single-instance profiles, `instance` for one resolved replica instance |
| `instance_index` | Required when `target_type` is `instance` |
| `exposure_mode` | `local`, `lan`, or `cloudflare` |
| `base_url` | OpenAI-compatible base URL for the selected exposure path |
| `service_token_id` | Optional Cloudflare Access service token metadata reference |
| `engine_api_key_id` | Optional engine API key secret reference |
| `examples` | Selected examples to render, such as curl, Python OpenAI SDK, or LiteLLM |
| `created_at`, `updated_at` | UI/audit context |

Rendered bundle shape:

```json
{
  "id": "bundle_ci_client",
  "name": "ci-client",
  "profile_id": "qwen-small",
  "target": {"type": "profile", "instance_index": null},
  "base_url": "https://qwen.example.com/v1",
  "headers": {
    "Authorization": "Bearer <engine_api_key>",
    "CF-Access-Client-Id": "abc123.access",
    "CF-Access-Client-Secret": "<shown-once-or-placeholder>"
  },
  "secret_state": {
    "engine_api_key_available": false,
    "cf_client_secret_available": true,
    "missing_secret_actions": ["rotate_inference_api_key", "rotate_cloudflare_service_token"]
  },
  "examples": {
    "curl": "curl ...",
    "python_openai": "from openai import OpenAI\n...",
    "litellm": "model_list:\n..."
  }
}
```

Client bundle behavior:

1. Default profile detail view should always render a basic connection bundle for the active exposure mode when the profile has one resolved instance.
2. Users can create named bundles for specific clients, especially when using multiple Cloudflare service tokens.
3. Replicated profiles must choose an explicit resolved instance for MVP bundles. Do not silently present instance `0` as a profile-level endpoint.
4. For replicated profiles, the default Connect view should show an instance selector or an instance-by-instance endpoint table.
5. Local bundle base URL uses the selected instance, for example `http://127.0.0.1:10000/v1`.
6. LAN bundle base URL uses the node LAN host/IP and selected instance port, for example `http://192.168.1.50:10000/v1`.
7. Cloudflare bundle base URL uses the configured hostname, for example `https://qwen.example.com/v1`.
8. Cloudflare bundles are available only for single-instance profiles in MVP because public exposure for replicated profiles requires a future router/load-balancer.
9. If the raw engine API key or Cloudflare Client Secret is not available in the current response/session, examples must show placeholders and explain which rotate/generate action can produce a new one-time value.
10. Generate-new and rotate Cloudflare token responses should include an optional rendered bundle using the newly returned one-time Client Secret.
11. Engine API-key generate/rotate responses should include an optional rendered bundle using the newly returned one-time engine API key.
12. The bundle renderer may accept user-supplied one-time secrets in the request body to produce copyable examples, but must not store those secrets.
13. Bundles should include `curl`, Python OpenAI SDK, and LiteLLM examples in MVP.
14. Bundles should never include Cloudflare API tokens, MCP tokens, service-management `svc_` tokens, launcher env secrets, or raw model download tokens.

Client bundle UI:

1. Profile detail has a **Connect** action or tab.
2. The top of the view shows base URL, selected model name, exposure path, and required auth layers.
3. A credential strip shows engine API key state and Cloudflare service-token state without revealing unavailable secrets.
4. Replicated profiles show the selected instance index, port, GPU assignment, and status before examples.
5. For Cloudflare Service Auth, the user can choose an active service token, generate a new client, rotate the selected client, or retire it.
6. Example tabs show curl, Python/OpenAI SDK, and LiteLLM.
7. Copy buttons should copy only the selected example or field.
8. If a secret was shown once, the view should make clear that closing the panel will lose the raw value and rotation/generation is required to display a new value later.
9. Named bundles can be saved, renamed, and deleted without affecting credentials.

---

## Engine API Keys

Inference profiles should support engine-native API keys where the selected engine exposes one. These keys protect LAN/local direct access and preserve compatibility with OpenAI-style clients that send `Authorization: Bearer <key>`.

MVP behavior:

1. Local-only profiles skip engine API key generation by default.
2. LAN and Cloudflare profiles recommend an engine API key by default.
3. Engine API keys are recommended, not mandatory. Disabling one on LAN or Cloudflare exposure is allowed with a warning.
4. When inframatik generates a key, show it once and store only a secret reference in the profile.
5. Generated keys should use a distinct prefix, for example `llm_` + 32 random bytes encoded as hex.
6. Command previews, systemd unit previews, REST responses, MCP resources, and logs must redact the raw key.
7. Rotation creates a new key, updates the secret reference, regenerates the unit, and requires restart for running profiles.
8. Deleting a profile deletes inframatik-owned engine API key material for that profile.

Secret storage can be a local node-owned secrets file for MVP:

```text
~/.config/inframatik/inference_secrets.json
```

Example profile reference:

```json
{
  "secrets": {
    "engine_api_key_id": "secret-profile-qwen-small-api-key"
  }
}
```

The secret file must use restrictive permissions and should not be exposed through MCP or ordinary config APIs. Admin UI may show metadata such as creation time and last rotation time, but not the raw key after initial creation.

Engines without clean API-key support should show a warning when exposed beyond local-only mode. The user can still proceed if Cloudflare Access or LAN trust is sufficient.

---

## Engine Launchers

Engine setup is explicit. inframatik should not scan the system, probe Python environments, or guess which package installation the user intended. The user provides a node-local launcher for each engine they want to use.

Default file:

```text
~/.config/inframatik/inference_engine_launchers.json
```

Launcher schema:

```json
{
  "launchers": {
    "vllm-main": {
      "id": "vllm-main",
      "display_name": "vLLM main venv",
      "engine": "vllm",
      "executable": "/home/aiml/vllm/bin/vllm",
      "base_args": [],
      "working_dir": null,
      "env": {
        "VLLM_USE_V1": "1"
      },
      "created_at": 1782086400,
      "updated_at": 1782086400
    },
    "sglang-python": {
      "id": "sglang-python",
      "display_name": "SGLang Python module",
      "engine": "sglang",
      "executable": "/home/aiml/sglang/bin/python",
      "base_args": ["-m", "sglang.launch_server"],
      "working_dir": null,
      "env": {}
    }
  }
}
```

Rules:

1. `executable` should be an absolute path.
2. `base_args` are ordered argv token rows and are prepended before inframatik-rendered engine args.
3. Profile `advanced.args` are appended after normalized and engine-specific args.
4. Launcher env is merged before profile env; profile env wins on key conflict.
5. Validation checks path existence and executable bit on the target node, but it does not search for alternate paths.
6. Version display is optional and user-provided for MVP. inframatik should not run arbitrary commands just to discover a version.
7. Profiles store the launcher ID plus a copied command preview so the UI can explain exactly what will run.

Example rendered commands:

```text
/home/aiml/vllm/bin/vllm serve /models/qwen --host 127.0.0.1 --port 10000
/home/aiml/sglang/bin/python -m sglang.launch_server --model-path /models/qwen --host 127.0.0.1 --port 10000
/home/aiml/llama.cpp/build/bin/llama-server --model /models/qwen/model.gguf --host 127.0.0.1 --port 10000
```

The launcher UI should be deliberately small: engine type, display name, executable path, optional base args, optional working directory, and optional env vars. This keeps engine ownership with the user while still giving inframatik enough structure to render safe systemd units.

The launcher Validate action should be stronger than file validation. It should show path facts and a runtime smoke probe so a Python virtualenv or engine binary that imports incorrectly, lacks CUDA libraries, or has missing package dependencies fails before a profile start attempt. This is diagnostic only; it must not install packages, mutate the launcher, or scan for alternate engine installs.

Launcher deletion:

1. If any running profile uses the launcher, block deletion.
2. If stopped profiles use the launcher, show affected profiles and require explicit confirmation.
3. Deleting a launcher removes only inframatik launcher metadata.
4. Deleting a launcher never deletes virtualenvs, binaries, scripts, repositories, or other executable files from disk.
5. Stopped profiles that referenced a deleted launcher become invalid until edited to select a new launcher.
6. MCP `delete_inference_launcher` follows the same reference rules as REST/UI deletion.

---

## Common Profile Fields

These fields are shared across engines where possible. inframatik should expose a normalized profile surface rather than every engine flag.

Field ownership rule:

1. Surface fields when they are cross-engine, commonly needed, or risky enough to require explicit user intent.
2. Keep engine-specific long-tail flags in an Advanced section with command preview.
3. Preserve raw args and env vars as escape hatches, but render commands from argv arrays.
4. Do not silently enable dangerous options from detected metadata or imported examples.
5. Keep sampling defaults secondary. Most clients send temperature, top-p, stop sequences, and similar values per request.
6. Treat a setting as common only when it has substantially the same operational meaning across engines. Otherwise, expose it as a first-class `engine_config.<engine>` field.

UI grouping:

| Group | Fields |
|-------|--------|
| Basic | Profile name, engine type, engine launcher, model artifact, served model name, target node, host, port, API key, public hostname |
| Runtime | Context length, weight dtype, quantization, load format, KV cache dtype/size/offload, trust remote code, tokenizer/chat template |
| Hardware | GPU selection, GPU memory target, tensor parallel, pipeline parallel, data parallel, expert parallel, context parallel, CPU threads, NUMA binding |
| Capacity | Max active requests, queue limit, batch token budget, prefill limits, chunked prefill, prefix/cache behavior |
| Model Behavior | Reasoning parser, reasoning effort defaults, tool-call parser, structured output backend, multimodal companion, LoRA/adapters, speculative/MTP settings, sampling defaults |
| Engine Tuning | MoE/linear/attention backends, all-to-all backend, load balancing, CUDA graph/compile settings, hierarchical cache/offload |
| Observability | Metrics, log level, request logging, health endpoint behavior |
| Advanced | Engine-specific argv tokens, env vars, generated command/unit preview |

Advanced editor:

1. Raw args are edited as ordered token-list rows.
2. Each token row stores exactly one argv element.
3. The UI must not accept a shell command string and split it.
4. Env vars are edited as key/value rows.
5. Engine-specific structured config can be edited as grouped fields or JSON object where useful, but it still renders to argv/env internally.
6. Command preview shows the final executable, base args, generated args, raw args, and redacted env.
7. Reordering raw args must be supported because some engines treat later flags as overrides.
8. Empty tokens are invalid.
9. Newlines and NUL bytes are invalid in args and env values.
10. Env keys must match a conservative identifier pattern such as `[A-Za-z_][A-Za-z0-9_]*`.
11. Profile env may override launcher env, but the preview must show the final resolved env with secret-looking values redacted.
12. inframatik-managed env keys for host, port, API key, and internal bookkeeping should be protected from accidental override unless the user explicitly confirms.

| Field | Type | Notes |
|-------|------|-------|
| `engine` | string | Runtime family: `llama_cpp`, `vllm`, or `sglang` |
| `engine_launcher_id` | string | Node-local launcher used to render/start the command |
| `served_model_name` | string | Name exposed by OpenAI-compatible APIs |
| `context_length` | int/null | Model context/window override |
| `host` | string | Default `127.0.0.1` |
| `port` | int | Allocated from inference port range for single-instance profiles; replicas use resolved instance ports |
| `dtype` | string | `auto`, `float16`, `bfloat16`, `float32`, engine-specific values |
| `quantization` | string/null | Engine-specific validation |
| `kv_cache_dtype` | string/null | Common selector; exact allowed values are engine-specific |
| `kv_cache_memory_bytes` | string/null | Explicit KV cache target where supported; overrides memory fraction for vLLM |
| `gpu_ids` | list[int]/null | GPU selection |
| `gpu_claim_mode` | string | `exclusive` or `shared`; stored under `deployment.gpu_policy`, default `exclusive` |
| `tensor_parallel` | int | Common for vLLM/SGLang |
| `pipeline_parallel` | int | vLLM/SGLang where supported |
| `data_parallel` | int | vLLM/SGLang serving replicas |
| `expert_parallel` | object/null | MoE expert placement/sharding; engine-specific rendering |
| `context_parallel` | object/null | Long-prefill/decode context partitioning; engine-specific rendering |
| `gpu_memory_utilization` | float/null | Mainly vLLM/SGLang |
| `cpu_offload_gb` | float/null | CPU offload budget where supported |
| `max_concurrent_requests` | int/null | Maps differently by engine |
| `max_batch_tokens` | int/null | Maps differently by engine |
| `max_prefill_tokens` | int/null | SGLang first-class; vLLM uses related scheduler fields |
| `speculative` | object/null | Optional speculative decoding/MTP settings |
| `lora` | object/null | Static/dynamic adapter support and capacity |
| `api_key` | string/null | Optional server-side API key if engine supports it |
| `log_level` | string | Engine log level |

Structured engine config:

```json
{
  "engine_config": {
    "llama_cpp": {
      "n_gpu_layers": -1,
      "main_gpu": 0,
      "split_mode": "layer",
      "tensor_split": null,
      "threads": null,
      "threads_batch": null,
      "batch_size": 2048,
      "ubatch_size": 512,
      "flash_attention": true,
      "cache_type_k": "f16",
      "cache_type_v": "f16",
      "mmproj_ref": null
    },
    "vllm": {
      "load_format": "auto",
      "distributed_executor_backend": null,
      "data_parallel_size_local": null,
      "data_parallel_start_rank": null,
      "data_parallel_address": null,
      "data_parallel_rpc_port": null,
      "data_parallel_backend": null,
      "data_parallel_lb_mode": null,
      "headless": false,
      "api_server_count": null,
      "decode_context_parallel_size": null,
      "prefill_context_parallel_size": null,
      "context_parallel_backend": null,
      "enable_expert_parallel": false,
      "enable_ep_weight_filter": false,
      "all2all_backend": null,
      "enable_eplb": false,
      "eplb_config": {},
      "expert_placement_strategy": null,
      "enable_dbo": false,
      "kv_offloading_size": null,
      "kv_offloading_backend": null,
      "offload_backend": null,
      "max_num_partial_prefills": null,
      "max_long_partial_prefills": null,
      "long_prefill_token_threshold": null,
      "scheduling_policy": null,
      "compilation_config": {},
      "attention_config": {},
      "moe_backend": null,
      "linear_backend": null,
      "chat_template_content_format": null,
      "reasoning_parser_plugin": null,
      "tool_parser_plugin": null,
      "lora": {}
    },
    "sglang": {
      "load_format": null,
      "page_size": null,
      "ep_size": null,
      "enable_dp_attention": false,
      "load_balance_method": null,
      "moe_a2a_backend": null,
      "moe_runner_backend": null,
      "attn_cp_size": null,
      "enable_dsa_prefill_context_parallel": false,
      "dsa_prefill_cp_mode": null,
      "chunked_prefill_size": null,
      "torchao_config": null,
      "sampling_defaults": null,
      "cuda_graph_config": {},
      "hicache": {},
      "grammar_backend": null,
      "lora": {}
    }
  }
}
```

Only the selected engine block is active. Other blocks may be absent.

Normalized mapping:

| Inframatik field | llama.cpp | vLLM | SGLang |
|------------------|-----------|------|--------|
| Model artifact | `--model` GGUF path | `vllm serve <path>` or config `model` | `--model-path` or `--model` alias |
| Served model name | `--alias` | `--served-model-name` | `--served-model-name` |
| Host / port | `--host`, `--port` | `--host`, `--port` | `--host`, `--port` |
| API key | `--api-key` | `--api-key` | `--api-key` |
| Context length | `--ctx-size` | `--max-model-len` | `--context-length` |
| Weight dtype | GGUF-driven | `--dtype` | `--dtype` |
| Quantization | GGUF artifact choice | `--quantization` | `--quantization` |
| KV cache dtype | `--cache-type-k`, `--cache-type-v` | `--kv-cache-dtype` | `--kv-cache-dtype` |
| Trust remote code | Not applicable | `--trust-remote-code` | `--trust-remote-code` |
| GPU selection | `--device`, `--main-gpu` | `CUDA_VISIBLE_DEVICES` | `CUDA_VISIBLE_DEVICES`, `--device`, `--base-gpu-id` |
| GPU memory target | `--fit`, `--fit-target` | `--gpu-memory-utilization` | `--mem-fraction-static` |
| Tensor parallel | `--split-mode`, `--tensor-split` approximation | `--tensor-parallel-size` | `--tensor-parallel-size` / `--tp-size` |
| Pipeline parallel | Not applicable | `--pipeline-parallel-size` | `--pipeline-parallel-size` / `--pp-size` |
| Data parallel | Not applicable | `--data-parallel-size` | `--data-parallel-size` / `--dp-size` |
| Expert parallel | Not applicable | `--enable-expert-parallel` plus DP/TP shape | `--ep-size` |
| Context parallel | Not applicable | `--decode-context-parallel-size`, `--prefill-context-parallel-size` | `--attn-cp-size`, DSA CP flags |
| MoE all-to-all | Not applicable | `--all2all-backend` | `--moe-a2a-backend` |
| MoE/linear backends | Not applicable | `--moe-backend`, `--linear-backend`, `--attention-config` | `--moe-runner-backend`, attention/DSA backend flags |
| Expert load balancing | Not applicable | `--enable-eplb`, `--eplb-config`, `--expert-placement-strategy` | EPLB/DeepEP engine args where supported |
| CPU threads | `--threads`, `--threads-batch` | Advanced/env | Advanced/env |
| Max active requests | `--parallel` slots | `--max-num-seqs` | `--max-running-requests` |
| Queue limit | Not applicable | Advanced/backpressure | `--max-queued-requests` |
| Batch token budget | `--batch-size`, `--ubatch-size` | `--max-num-batched-tokens` | `--max-total-tokens`, `--max-prefill-tokens` |
| Partial prefill limits | Not applicable | `--max-num-partial-prefills`, `--max-long-partial-prefills`, `--long-prefill-token-threshold` | `--chunked-prefill-size`, `--max-prefill-tokens` |
| Chunked prefill | Not applicable in same form | `--enable-chunked-prefill` | `--chunked-prefill-size` |
| Prefix/cache behavior | Context/KV/cache flags | `--enable-prefix-caching` | Radix cache defaults/options |
| Explicit KV cache size | Not applicable | `--kv-cache-memory-bytes` | Advanced / engine-specific |
| KV/offload | Not applicable | `--kv-offloading-size`, `--kv-offloading-backend`, `--cpu-offload-gb` | `--cpu-offload-gb`, offload group fields, HiCache |
| Multimodal companion | `--mmproj` | Model processor/config | `--enable-multimodal` |
| Chat template | `--chat-template`, `--chat-template-file` | `--chat-template` | `--chat-template`, `--hf-chat-template-name` |
| Chat template content | Template-dependent | `--chat-template-content-format` | Template-dependent / request-time kwargs |
| Reasoning mode | `--reasoning-format` | `--reasoning-parser` | `--reasoning-parser` |
| Reasoning effort default | Request-time metadata | Request-time `chat_template_kwargs`/OpenAI field | Request-time metadata/parser behavior |
| Tool calling | `--jinja` plus template | `--enable-auto-tool-choice`, `--tool-call-parser` | `--tool-call-parser` |
| Structured output | Grammar/schema flags | Structured outputs config | `--grammar-backend` |
| LoRA/adapters | `--lora`, `--lora-scaled` | `--enable-lora`, LoRA config | `--enable-lora`, `--lora-paths` |
| Speculative/MTP | Draft model args | `--speculative-config.*` / `--spec-*` | `--speculative-*` args |
| Compile/CUDA graph | Advanced args | `--compilation-config`, cudagraph fields | `--cuda-graph-config`, CUDA graph fields |
| Kernel/backend selection | Advanced args | Kernel/attention/MoE config, env vars | Attention/MoE/kernel backend args |
| Metrics | `--metrics` | `/metrics` endpoint | `--enable-metrics` |
| Logs | Verbosity/log flags | Log level/server flags | `--log-level`, request logging |
| Raw args/env | Append argv/env | Append argv/env | Append argv/env |

Metrics flags are available profile fields, but MVP should render them only when the user explicitly enables metrics in the profile.

Decision notes:

1. `trust_remote_code` must always be an explicit user toggle. Metadata or examples can explain why a model needs it, but cannot enable it silently.
2. vLLM should render the model target as a positional `vllm serve <path>` argument or config-file `model`, not assume a stable `--model` flag.
3. llama.cpp quantization is primarily selected by the GGUF artifact itself; the profile should not present it like vLLM/SGLang weight quantization.
4. Tensor parallel is not truly normalized for llama.cpp. The UI can expose multi-GPU split controls, but warnings should explain they are not equivalent to vLLM/SGLang tensor parallelism.
5. Sampling presets are optional profile metadata, not primary server lifecycle config.
6. Reasoning effort is usually request-time behavior. Profiles may define defaults or examples, but should not hide request-level control.
7. Kernel/backend settings belong in structured engine config or Advanced. Surface them prominently only when the selected profile already uses them.

Decision:

| Decision | Current choice |
|----------|----------------|
| Inference port range | Separate configurable range; default `10000-10999` |

---

## Engine Adapters

Each engine adapter implements:

```text
validate_launcher(launcher) -> ValidationResult
build_command(profile, model_artifact) -> CommandPlan
validate(profile, model_artifact, hardware) -> ValidationResult
health_check(profile) -> HealthResult
metrics(profile) -> MetricsResult
explain(profile, model_artifact, hardware) -> list[Warning]
```

Adapter responsibilities:

1. Validate that the selected launcher shape matches the engine family.
2. Convert common profile fields into engine-specific CLI args.
3. Validate engine-specific config.
4. Preserve raw args and env vars.
5. Generate a command plan without shell interpolation.
6. Provide a health check path.
7. Parse or fetch metrics where possible.
8. Return warnings for likely misconfiguration.

Command plans must be represented as argv arrays internally. Systemd unit rendering can then quote safely.

---

## Engine: llama.cpp

Best for GGUF models, CPU or mixed CPU/GPU serving, smaller single-node deployments, and quantized local models.

Common mappings:

| Common field | llama.cpp arg |
|--------------|---------------|
| model file | `--model` or `-m` |
| host | `--host` |
| port | `--port` |
| context length | `--ctx-size` |
| GPU layers | engine-specific `n_gpu_layers` -> `--n-gpu-layers` |
| threads | engine-specific `threads` -> `--threads` |
| batch size | `--batch-size` |
| ubatch size | `--ubatch-size` |
| tensor split | `--tensor-split` |
| flash attention | `--flash-attn` |
| KV cache type | `--cache-type-k`, `--cache-type-v` |
| LoRA | `--lora` or `--lora-scaled` |

llama.cpp server supports OpenAI-compatible routes, multimodal support, monitoring endpoints, schema-constrained output, function/tool use, continuous batching, and speculative decoding. The adapter should expose only the high-value subset initially and allow raw args for the rest.

MVP model source: GGUF artifact.

---

## Engine: vLLM

Best for high-throughput OpenAI-compatible serving, HF safetensors model repos, GPU servers with batching and parallelism, LoRA, multimodal, quantization, prefix caching, and speculative decoding.

Common mappings:

| Common field | vLLM arg |
|--------------|----------|
| model path | positional `vllm serve <path>` or config `model` |
| served model name | `--served-model-name` |
| host | `--host` |
| port | `--port` |
| dtype | `--dtype` |
| context length | `--max-model-len` |
| tensor parallel | `--tensor-parallel-size` |
| pipeline parallel | `--pipeline-parallel-size` |
| data parallel | `--data-parallel-size` |
| local DP / multi-node DP | `--data-parallel-size-local`, `--data-parallel-start-rank`, `--data-parallel-address`, `--data-parallel-rpc-port` |
| DP load balancing mode | `--data-parallel-hybrid-lb`, `--data-parallel-external-lb`, `--data-parallel-multi-port-external-lb` |
| API server scaling | `--api-server-count` |
| expert parallel | `--enable-expert-parallel` |
| expert placement / loading | `--expert-placement-strategy`, `--enable-ep-weight-filter` |
| expert all-to-all | `--all2all-backend` |
| expert load balancing | `--enable-eplb`, `--eplb-config` |
| decode/prefill context parallel | `--decode-context-parallel-size`, `--prefill-context-parallel-size` |
| GPU memory utilization | `--gpu-memory-utilization` |
| explicit KV cache size | `--kv-cache-memory-bytes` |
| KV offload | `--kv-offloading-size`, `--kv-offloading-backend` |
| CPU offload | `--cpu-offload-gb` |
| quantization | `--quantization` |
| load format | `--load-format`, loader extra config |
| KV cache dtype | `--kv-cache-dtype` |
| trust remote code | `--trust-remote-code` |
| API key | `--api-key` |
| max active requests | `--max-num-seqs` |
| max batch tokens | `--max-num-batched-tokens` |
| partial prefill | `--max-num-partial-prefills`, `--max-long-partial-prefills`, `--long-prefill-token-threshold` |
| prefix caching | `--enable-prefix-caching`, prefix hash options |
| reasoning/tool parsers | `--reasoning-parser`, `--tool-call-parser`, `--enable-auto-tool-choice` |
| parser plugins | `--reasoning-parser-plugin`, `--tool-parser-plugin` |
| chat template content | `--chat-template-content-format` |
| MoE / linear kernels | `--moe-backend`, `--linear-backend` |
| attention config | `--attention-config` |
| compile / CUDA graph | `--compilation-config` and cudagraph fields |
| speculative/MTP | `--speculative-config.*` or `--spec-*` |

vLLM-specific controls worth surfacing early:

1. **MoE parallelism** - `enable_expert_parallel`, all-to-all backend, expert placement, and EPLB config because current large models often depend on them.
2. **DP shape** - local DP size, start rank, headless mode, and API server count for multi-node or external-load-balancer layouts.
3. **Context parallelism** - decode and prefill context parallel fields for long-context models.
4. **KV memory** - explicit KV cache bytes, KV offload, CPU offload, max sequence count, and partial prefill limits.
5. **Kernel selection** - MoE backend, linear backend, attention config, and compilation config.
6. **Parser/template settings** - reasoning parser, tool parser, parser plugins, auto tool choice, and chat template content format.
7. **Load path** - load format and loader config for safetensors, sharded checkpoints, GGUF, bitsandbytes, InstantTensor, or other specialized loading paths.

MVP model source: HF-style safetensors snapshot.

---

## Engine: SGLang

Best for structured generation workloads, prefix/radix cache heavy workflows, advanced serving controls, HF safetensors model repos, and workloads needing reasoning/tool parsers or grammar backends.

Common mappings:

| Common field | SGLang arg |
|--------------|------------|
| model path | `--model-path` or `--model` alias where supported |
| served model name | `--served-model-name` |
| host | `--host` |
| port | `--port` |
| dtype | `--dtype` |
| context length | `--context-length` |
| tensor parallel | `--tp-size` |
| pipeline parallel | `--pp-size` |
| data parallel | `--dp-size` |
| expert parallel | `--ep-size` |
| DP attention | `--enable-dp-attention` |
| DP load balancing | `--load-balance-method` |
| multi-node | `--dist-init-addr`, `--nnodes`, `--node-rank` |
| context parallel / DSA CP | `--attn-cp-size`, `--enable-dsa-prefill-context-parallel`, `--dsa-prefill-cp-mode` |
| GPU memory fraction | `--mem-fraction-static` |
| quantization | `--quantization` |
| torchao quantization | `--torchao-config` |
| KV cache dtype | `--kv-cache-dtype` |
| page size | `--page-size` |
| trust remote code | `--trust-remote-code` |
| API key | `--api-key` |
| admin API key | `--admin-api-key` |
| max active requests | `--max-running-requests` |
| queue limit | `--max-queued-requests` |
| batch/prefill limits | `--max-total-tokens`, `--max-prefill-tokens`, `--chunked-prefill-size` |
| MoE all-to-all / runner | `--moe-a2a-backend`, `--moe-runner-backend` |
| reasoning/tool parsers | `--reasoning-parser`, `--tool-call-parser` |
| chat template | `--chat-template`, `--hf-chat-template-name` |
| sampling defaults | `--sampling-defaults` |
| LoRA | `--enable-lora`, `--lora-paths`, LoRA capacity fields |
| HiCache / LMCache | `--hicache-*`, `--enable-lmcache`, `--lmcache-config-file` |
| CUDA graph | `--cuda-graph-config` and per-phase CUDA graph flags |
| speculative decoding | `--speculative-*` |

SGLang-specific controls worth surfacing early:

1. **MoE parallelism** - `ep_size`, DP attention, MoE all-to-all backend, MoE runner backend, and load balancing method.
2. **Long-context prefill** - DSA context parallel fields, chunked prefill size, max prefill tokens, max running requests, and page size.
3. **Quantization** - normal quantization plus `torchao_config` and model/hardware-specific online quantization choices.
4. **Cache/offload** - memory fraction, CPU offload, HiCache, LMCache, and RadixAttention toggles.
5. **Kernel/debug performance** - CUDA graph config, custom all-reduce, overlap scheduler, tokenizer batch encode/decode, NCCL/NVLS/symmetric memory toggles.
6. **Parser/template settings** - reasoning parser, tool parser, HF named chat template, sampling defaults, and tool server.
7. **LoRA capacity** - max LoRA rank, paths, adapters per batch, loaded adapter cap, eviction policy, and LoRA backend.

MVP model source: HF-style safetensors snapshot.

---

## Special Model Configuration

Special model behavior should be represented directly in profile configuration:

1. **Structured profile fields** - Common and engine-specific settings stored in the profile.
2. **User-entered metadata** - Optional notes or copied values from model docs.
3. **Raw overrides** - Args/env vars for advanced users.

MVP should not perform model compatibility detection or auto-tuning from artifact metadata. Basic manifest data such as kind, format, files, source, and checksums is informational. The user chooses the engine launcher and profile fields explicitly.

Supported special cases:

| Area | Handling |
|------|----------|
| Chat templates | Profile field or raw arg/env override |
| Reasoning parsers | Engine-specific profile field |
| Reasoning effort | Request-time default examples; profile may set a default but client requests can override |
| Tool/function calling | Engine parser setting and explicit profile fields |
| Multimodal assets | Model artifact may include processor, vision files, or GGUF mmproj |
| LoRA/adapters | Separate model artifact referenced by profile |
| Speculative decoding | Built-in MTP or optional draft model reference, represented as structured profile settings |
| Context scaling | Common context field plus engine-specific rope/yarn overrides |
| Hardware-specific kernels | Engine-specific profile fields plus raw args/env when needed |
| Trust remote code | Explicit boolean, never enabled implicitly |
| Quantization | Common selector plus engine-specific validation |

### Case: GLM-5.2 on Blackwell-Class GPUs

GLM-5.2 is a useful stress case for profile fields because the model is new, large, MoE-based, long-context, and runtime support is still moving quickly.

Observed requirements from current upstream guidance:

1. GLM-5.2 is offered as BF16 and FP8 checkpoints. FP8 is the practical local-serving target for single-node systems; BF16 requires far more memory.
2. The model has a 1M-token context target, but whether that is usable depends on KV cache headroom, tensor parallel size, sequence cap, and GPU memory.
3. vLLM examples use GLM-specific parsers (`glm47` tool parser, `glm45` reasoning parser), automatic tool choice, FP8 KV cache, MTP speculative decoding with 5 tokens, and sometimes explicit MoE/linear backend env/flags.
4. vLLM large-MoE recipes for Qwen/DeepSeek-style models often use expert parallelism, data parallelism, all-to-all backend selection, and EPLB-style expert load balancing.
5. SGLang examples target DeepSeek Sparse Attention, MTP/speculative decoding, DP attention, expert parallelism, DSA context parallelism, page size, FP8/NVFP4 quantization, and hardware-specific MoE backends.
6. RTX PRO 6000 Blackwell is Blackwell-class hardware but has a different memory envelope than B200/B300/H200 examples. Profiles must let the user tune context, concurrency, parallelism, and kernel settings directly.

Profile fields needed for GLM-5.2-class models:

| Profile field | Why |
|---------------|-----|
| `model_ref` | Select BF16, FP8, NVFP4/future quantized artifacts explicitly |
| `context_length` | Start below 1M and increase only when KV cache headroom allows |
| `kv_cache_dtype` | FP8 KV cache is often required for long context |
| `kv_cache_memory_bytes`, `kv_offloading`, `cpu_offload_gb` | Explicitly reserve or spill memory when memory-fraction knobs are too coarse |
| `tensor_parallel`, `pipeline_parallel`, `data_parallel` | Express 6-GPU and 8-GPU layouts directly |
| `expert_parallel` | Serve MoE models with expert layers sharded/placed separately from attention layers |
| `context_parallel` | Split long prefill/decode attention for very long prompts where the engine supports it |
| `gpu_ids` | Bind a profile to the intended GPU set |
| `gpu_memory_utilization` / `mem_fraction_static` | Engine-specific VRAM headroom control |
| `max_concurrent_requests` / `max_num_seqs` | Primary long-context fit knob for vLLM-style serving |
| `max_batch_tokens` / `max_prefill_tokens` / `chunked_prefill_size` | Bound prefill and batch memory pressure |
| `page_size` | SGLang MoE/page-table tuning for large models |
| `reasoning_parser` | Store parser such as `glm45` |
| `reasoning_effort_default` | Optional request default; clients can override |
| `tool_call_parser` | Store parser such as `glm47` |
| `enable_auto_tool_choice` | Required by some OpenAI-compatible tool-calling paths |
| `chat_template` / `chat_template_args` / `chat_template_content_format` | Support thinking toggles and model-specific template/content-format knobs |
| `speculative` | Store MTP/speculative method, token count, draft model, top-k, and EAGLE/NGRAM variants |
| `kernel_config` | Store MoE, linear, attention, all-to-all, CUDA graph, and compile choices when needed |
| `load_format` / `loader_config` | Support sharded, safetensors, GGUF, bitsandbytes, and fast-loader paths |
| `lora` | Support adapter capacity and static adapter loading |
| `env` | Store runtime env vars such as JIT/warmup controls |
| `raw_args` | Preserve newly required engine flags before inframatik has a structured field |

For an RTX PRO 6000 6-GPU or future 8-GPU node, inframatik should allow creating a GLM-5.2 profile with any of these fields. The UI should show hardware facts from the node next to profile choices: GPU count, VRAM, tensor/data/expert/context parallel settings, context target, KV cache dtype/size, sequence cap, prefill limits, and kernel/backend overrides.

---

## Systemd Integration

Inference profiles generate systemd user units.

Unit name:

```text
infra-llm-{profile_id}.service
infra-llm-{profile_id}@{instance_index}.service
```

Rules:

1. Unit files are generated from profiles.
2. Manual edits to generated unit files are not the source of truth.
3. Profile edits regenerate the unit and require restart to apply.
4. Inference units should not appear as normal user-created services unless explicitly surfaced as generated.
5. Generated units still use `systemctl --user` for start/stop/restart/logs.
6. Single-instance profiles may use `infra-llm-{profile_id}.service`.
7. Replicated profiles use indexed units (`@0`, `@1`, etc.) so each instance has separate lifecycle, logs, port, and GPU placement.

---

## Logs

MVP logs come directly from stdout/stderr captured by systemd user units. Do not add separate engine log files or a parallel log storage path for the first version.

Log sources:

| Profile shape | Unit | Log command |
|---------------|------|-------------|
| Single instance | `infra-llm-{profile_id}.service` | `journalctl --user -u infra-llm-{profile_id}.service` |
| Replica instance | `infra-llm-{profile_id}@{index}.service` | `journalctl --user -u infra-llm-{profile_id}@{index}.service` |

Behavior:

1. Per-instance logs are the precise source of truth.
2. Profile-level logs default to a recent aggregate tail across all resolved instances.
3. Aggregate logs should prefix each line with the instance index.
4. Aggregate logs should fetch the last N lines per instance and merge for display; full historical cross-unit log search is deferred.
5. The UI should allow filtering aggregate logs by instance.
6. Logs should be shown near systemd state, restart count, PID, and last failure reason.
7. Raw engine API keys and secret-looking env values must be redacted before logs are returned through REST or MCP.
8. Remote worker logs use the existing master-to-worker proxy path, but journal reads execute on the worker.

Default MVP log limits:

| Request | Default | Max |
|---------|---------|-----|
| Per-instance logs | 300 lines | 2000 lines |
| Aggregate profile logs | 150 lines per instance | 1000 lines per instance |

Streaming logs and durable log search are deferred.

---

## Health and Metrics

Health is non-generative by default. Automatic health checks should not send prompts or consume tokens.

Instance health layers:

1. **systemd state** - active, activating, failed, stopped, restarting.
2. **TCP check** - configured host/port accepts a connection.
3. **HTTP check** - preferred OpenAI-compatible `/v1/models`; otherwise engine-specific health/version endpoint where available.
4. **Model match** - served model name appears in `/v1/models` when that endpoint is available.

Instance health states:

| State | Meaning |
|-------|---------|
| `stopped` | Unit is inactive by user intent |
| `starting` | Unit is activating, waiting for TCP readiness, or waiting for optional API/model readiness |
| `healthy` | Unit active, port reachable, and API/model check passes when available |
| `degraded` | Unit active but API/model check is incomplete or mismatched |
| `unhealthy` | Unit active but port/API check fails after grace period |
| `failed` | systemd unit failed |
| `unknown` | Health could not be determined |

Profile health is an aggregate of its resolved instances. Instance health should remain individually visible so one failed replica does not hide behind an aggregate state.

Profile-level start and restart are all-or-nothing, so a newly started profile should not intentionally enter partial capacity. `degraded` still matters for failures that happen after a successful start, manual per-instance actions, or external systemd state changes.

Aggregate profile health:

| Aggregate | Rule |
|-----------|------|
| `healthy` | All resolved instances are healthy |
| `starting` | At least one instance is starting and none are failed/unhealthy |
| `degraded` | Some instances are healthy and some are unhealthy, failed, unknown, or degraded |
| `unhealthy` | No instances are healthy and at least one unit is active but failing checks |
| `failed` | All resolved instances are failed, or a single-instance profile failed |
| `stopped` | All resolved instances are stopped |
| `unknown` | No useful health facts are available |

Large models can take a long time to load. Each profile should have a configurable startup grace period, defaulting to 10 minutes for MVP. During grace, active/activating units with closed ports should show `starting`, not `failed`. Profile-level start succeeds when all instances are systemd-active and TCP-ready; API/model readiness can continue refining health after that point.

Manual test action:

1. Profile detail should include a **Test** action.
2. Test sends a user-triggered minimal request to the selected instance or profile endpoint.
3. Test is never run automatically by health polling.
4. Test result should show latency, status code, selected model, and a short response/error summary.
5. Test prompts must be editable before sending and should default to a tiny harmless prompt.
6. Test request/response history is not stored in MVP.

Test target modes:

| Mode | Purpose | Default |
|------|---------|---------|
| `local_instance` | Verify engine process works on the node-local host/port | Yes |
| `lan_endpoint` | Verify LAN exposure from the node running inframatik | No |
| `cloudflare_endpoint` | Verify public hostname, Cloudflare Access, and engine auth | No |

Local instance test is the default because it answers "is the engine working?" without depending on DNS, tunnel, or external network behavior. LAN/Cloudflare endpoint tests are optional explicit modes for "does the exposed client path work?"

OpenAI-compatible test request defaults:

| Capability | Endpoint | Default body |
|------------|----------|--------------|
| Chat | `POST /v1/chat/completions` | `{"messages":[{"role":"user","content":"Reply with OK."}],"max_tokens":8}` |
| Completion | `POST /v1/completions` | `{"prompt":"Reply with OK.","max_tokens":8}` |
| Embeddings | `POST /v1/embeddings` | `{"input":"test"}` |

Test behavior:

1. Default to chat completion unless the user selects another endpoint.
2. Allow completions, embeddings, or custom endpoint override.
3. Default timeout is 60 seconds.
4. Include engine `Authorization: Bearer <key>` automatically when the profile has an engine API key.
5. For `cloudflare_endpoint`, include Cloudflare Access Service Auth headers only if inframatik has just generated them in the current UI session or the user provides them for the test.
6. For `local_instance`, do not use Cloudflare headers.
7. Show the editable JSON body before sending.
8. Redact secrets from the displayed request after send.

Metrics:

| Metric | Source |
|--------|--------|
| Process status | systemd |
| PID | systemd |
| Uptime | systemd timestamp |
| Restart count | systemd properties where available |
| Last failure reason | systemd/journal summary |
| TCP reachable | local socket check |
| Logs | journalctl |
| GPU utilization | existing system metrics |
| GPU memory | existing system metrics |
| Requests/sec | engine metrics where available |
| Tokens/sec | engine metrics where available |
| Queue depth | engine metrics where available |
| Loaded model | `/v1/models` or engine endpoint |

Metrics behavior:

1. MVP should always show lifecycle metrics from systemd and GPU facts from existing node metrics.
2. Engine request/token/queue metrics are read only when the profile explicitly enables or configures an engine metrics endpoint.
3. inframatik should not silently add metrics flags to llama.cpp, vLLM, or SGLang command lines in MVP.
4. If a user adds metrics flags through structured config or raw args, the profile can display the discovered endpoint and parse what is available.
5. No background Prometheus scraping, historical charts, request tracing, billing/accounting, or automatic benchmark runs in MVP.

MVP can start with health, logs, PID, uptime, restart count, endpoint, and GPU memory visibility.

---

## UI

Add top-level **Inference** area.

Primary views:

1. **Profiles** - Running and stopped inference profiles.
2. **Models** - Model artifact inventory from [Model Storage](model-storage.md).
3. **Launchers** - User-configured engine paths, base args, working directory, and env.
4. **Jobs** - Model download/import jobs plus inference operations such as profile starts/restarts and Cloudflare provisioning.

On a standalone node, these views manage that node. On a master, these views manage the currently selected node. Selecting a worker means editing that worker's local launchers, model inventory, profiles, ports, systemd units, and Cloudflare exposure through the existing proxy path.

Profile rows/cards show name, engine, model, status, endpoint, hardware, context, and start/stop/restart/logs/test/edit/export/delete actions.

Profile detail drawer/page:

1. Header shows profile name, status, engine family, launcher, model, endpoint, and primary lifecycle/test actions.
2. Instance table shows index, port, GPU assignment, PID/status, health, and logs action.
3. Config summary shows the high-impact values: context, dtype, quantization, parallelism, memory target, concurrency, exposure mode.
4. Edit opens the profile editor without losing selected node context.
5. Logs, health, and manual test should be reachable from both the profile row and detail view.
6. Active operation status appears inline when the profile is being created, updated, started, restarted, stopped, deleted, or cleaned up.

Jobs view:

1. Shows active and recent model jobs from model storage.
2. Shows active and recent inference operations from `inference_operations.json`.
3. Groups by selected node on a master.
4. For inference operations, show kind, profile, current step, progress, elapsed time, terminal error, rollback summary, and links to logs/profile detail.
5. Interrupted operations should explain that inframatik restarted and that live profile/systemd state has been reconciled separately.

Launcher setup UI:

1. Add launcher button with engine type selector: llama.cpp, vLLM, SGLang.
2. Required executable path field.
3. Optional base args token-list rows for launches such as `python`, `-m`, `sglang.launch_server`.
4. Optional working directory and env var editor.
5. Validate button that checks the path exists and is executable on the selected node.
6. Command preview showing the launcher before model/profile args are added.

Create profile flow:

1. Select model artifact.
2. Choose engine type and configured launcher, or add a launcher inline.
3. Review dry-run validation blockers, warnings, resolved ports, GPU placement, and command/unit preview.
4. Configure common runtime options.
5. Configure endpoint exposure: local, LAN, or Cloudflare.
6. Advanced engine args/env vars.
7. Preview command and Cloudflare provisioning plan.
8. Save profile.
9. Optional start immediately.

Profile editor UI:

1. Use the same editor for create and edit so users do not learn two different forms.
2. Organize fields into compact sections or tabs: Basics, Runtime, Hardware, Capacity, Behavior, Exposure, Observability, Advanced, Preview.
3. Keep Basics, Runtime, Hardware, and Exposure visible without hunting; collapse the long-tail engine tuning sections by default.
4. Show a sticky footer with validation state, dirty state, command changed indicator, and primary action.
5. For stopped profiles, primary action is **Save** with optional **Save and start**.
6. For running profiles with operational changes, primary action is **Save and restart**; secondary action is **Discard changes**.
7. For running profiles with display-only changes, primary action is **Save**.
8. Show a restart-required banner listing the fields that changed and the affected instances.
9. Show validation blockers and warnings inline beside fields and summarized at the top.
10. Show generated API keys once, with copy controls, when the user enables key generation.
11. Show a generated command/unit preview with old-vs-new diff when editing an existing profile.
12. Preserve existing port and GPU assignments unless the user changes placement or port policy.
13. Do not include tuning presets in MVP; expose grouped fields cleanly and rely on validation/MCP help for guidance.

GPU placement UI:

1. Show each GPU with index, model name, total VRAM, used/free VRAM, utilization, current inframatik profile assignments, and whether a non-inframatik process is using meaningful memory.
2. Show a live resolved layout preview for replicated profiles: instance index, GPU set, host, port, and unit name.
3. Let the user switch between automatic placement, one replica per GPU, contiguous GPU groups, and explicit GPU assignment.
4. Let the user choose GPU claim mode with `exclusive` as the default and `shared` as an explicit opt-in.
5. Treat current free VRAM as advisory during profile creation because memory can change before start.
6. Block only concrete conflicts, such as invalid GPU IDs or overlapping running inframatik profiles where either side uses `exclusive`.
7. Warn, but do not block, on likely oversubscription when requested context, concurrency, memory fraction, shared assignments, and current GPU memory make the profile unlikely to fit.
8. Reuse existing node metrics for GPU memory and utilization; do not add a slow profile-editor polling path.

Example placement preview:

| Instance | GPUs | Port | State |
|----------|------|------|-------|
| 0 | `0` | `10000` | planned |
| 1 | `1` | `10001` | planned |
| 2 | `2` | `10002` | planned |

Cloudflare exposure UI:

1. Hostname field.
2. Protection selector: Service Token (recommended), Identity Login, No Access policy.
3. Existing or new Cloudflare Access service token selector when Service Token is selected.
4. Service-token table showing name, Client ID, expiration, active/retired state, ownership, last rotation time, and profile/hostname attachment.
5. **Generate new client** action that creates another Cloudflare Access service token, attaches it to the policy, and shows Client ID/Client Secret once.
6. **Rotate** action for a selected service token that returns a new Client Secret once and warns that existing clients may need immediate update.
7. **Retire** action that removes a service token from the Access policy, with optional delete for inframatik-owned tokens no longer referenced.
8. One-time display of generated or rotated Client ID and Client Secret.
9. Engine API key toggle shown as recommended, not required, when endpoint mode is LAN or Cloudflare.
10. Client example showing required Cloudflare headers plus engine `Authorization` header where enabled.

---

## Cluster Behavior

Inference configuration is node-local. There is no cluster-wide inference profile registry in the MVP.

Each node owns:

1. Inference registry files.
2. Engine launchers.
3. Model artifacts.
4. Inference profiles.
5. Resolved instances, ports, and GPU assignments.
6. Generated systemd user units.
7. Cloudflare routes, DNS records, Access apps, and service-token metadata for that node's inference hostnames.
8. Named client connection bundle metadata.

Master view:

1. Shows inference state for the selected node.
2. Shows aggregate model inventory across workers.
3. Can trigger model downloads/import jobs on the selected worker.
4. Can create/edit launcher config on the selected worker.
5. Can create/edit/start/stop/restart profiles on the selected worker through the existing proxy pattern.

Worker view:

1. Manages its own local models, launchers, and profiles.
2. Reports model/profile/launcher summaries to master.
3. Runs inference locally.

Remote-management rules:

1. A profile can start only on a node that has the referenced model artifact snapshot.
2. If missing, UI offers to download/import the model on that node before start.
3. A profile can start only on a node that has the referenced engine launcher.
4. If a user configures a worker from master, API calls execute on the worker and persist to the worker's local config files.
5. Public endpoints for worker profiles use that worker's Cloudflare tunnel when configured.
6. Inference traffic never routes through master by default.

Copying a profile from one node to another is deferred convenience behavior, not the default model. If added later, copy should create a new node-local profile and require explicit selection of that node's launcher and model artifact.

---

## Profile Export

MVP profile export is for backup, support, and debugging. It is not a profile-sharing or cross-node deployment feature.

Export behavior:

1. Export one profile as JSON from the node that owns it.
2. Include profile config, resolved instances, command preview, launcher ID, model artifact ID/snapshot, exposure metadata, and validation summary.
3. Do not include raw engine API keys, Cloudflare Access service token secrets, Cloudflare API tokens, launcher env values that look secret, or MCP/service tokens.
4. Include secret references only as redacted metadata.
5. Include a warning that launcher paths, model artifacts, ports, GPU IDs, and Cloudflare resources are node-local.
6. Exported JSON should be stable enough for debugging but not treated as a portable backup format in MVP.

Profile import is deferred. If added later, import should create a stopped node-local profile, validate missing launcher/model/ports, and require explicit confirmation before provisioning Cloudflare resources.

---

## API Sketch

Local-node endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/inference/profiles` | List profiles with status |
| POST | `/api/inference/profiles/preview` | Dry-run validate and plan a draft create/update without writing state |
| POST | `/api/inference/profiles` | Create profile |
| GET | `/api/inference/profiles/{id}` | Get profile detail |
| PUT | `/api/inference/profiles/{id}` | Update profile |
| DELETE | `/api/inference/profiles/{id}` | Delete profile |
| GET | `/api/inference/profiles/{id}/export` | Export redacted profile JSON for backup/debug |
| POST | `/api/inference/profiles/{id}/start` | Start all resolved instances transactionally |
| POST | `/api/inference/profiles/{id}/stop` | Stop all resolved instances for profile |
| POST | `/api/inference/profiles/{id}/restart` | Restart all resolved instances transactionally |
| GET | `/api/inference/profiles/{id}/instances` | List resolved instances with ports, GPUs, units, and status |
| POST | `/api/inference/profiles/{id}/instances/{index}/start` | Start one instance |
| POST | `/api/inference/profiles/{id}/instances/{index}/stop` | Stop one instance |
| POST | `/api/inference/profiles/{id}/instances/{index}/restart` | Restart one instance |
| GET | `/api/inference/profiles/{id}/logs` | Read recent aggregate logs. Query: `lines`, optional `instance` |
| GET | `/api/inference/profiles/{id}/instances/{index}/logs` | Read recent logs for one instance. Query: `lines` |
| GET | `/api/inference/profiles/{id}/health` | Aggregate health check |
| GET | `/api/inference/profiles/{id}/instances/{index}/health` | One instance health check |
| POST | `/api/inference/profiles/{id}/test` | Run manual non-health test request. Body selects `target_mode`, optional `instance`, body override |
| POST | `/api/inference/profiles/{id}/instances/{index}/test` | Run manual test request against one instance. Body selects `target_mode`, body override |
| POST | `/api/inference/profiles/{id}/render` | Render command/unit preview for all instances |
| GET | `/api/inference/profiles/{id}/client-bundles` | List saved client connection bundle metadata plus default bundle |
| POST | `/api/inference/profiles/{id}/client-bundles/render` | Render a client bundle for a profile/instance target with optional one-time secrets supplied in request body; never stores secrets |
| POST | `/api/inference/profiles/{id}/client-bundles` | Save named client bundle metadata without raw secrets; replicated profiles require explicit `instance_index` |
| PUT | `/api/inference/profiles/{id}/client-bundles/{bundle_id}` | Update named client bundle metadata without raw secrets |
| DELETE | `/api/inference/profiles/{id}/client-bundles/{bundle_id}` | Delete named client bundle metadata only |
| POST | `/api/inference/profiles/{id}/api-key` | Generate or rotate engine API key. Raw key returned once. |
| DELETE | `/api/inference/profiles/{id}/api-key` | Disable engine API key and require restart if running |
| POST | `/api/inference/profiles/{id}/cloudflare/service-tokens` | Generate and attach a new Cloudflare Access service token. Secret returned once, optionally with rendered client bundle. |
| POST | `/api/inference/profiles/{id}/cloudflare/service-tokens/{token_id}/rotate` | Rotate an attached Cloudflare Access service token secret. Secret returned once, optionally with rendered client bundle. |
| DELETE | `/api/inference/profiles/{id}/cloudflare/service-tokens/{token_id}` | Retire token from profile policy; optionally delete if inframatik-owned and unreferenced |
| GET | `/api/inference/ports/next` | Preview next inference port or block. Query: `count`, `contiguous` |
| GET | `/api/inference/operations` | List recent inference operations. Query: optional `profile_id`, `state` |
| GET | `/api/inference/operations/{id}` | Get one inference operation and progress/result |
| POST | `/api/inference/operations/{id}/cancel` | Cancel queued operation if no side effects have begun |
| GET | `/api/inference/launchers` | List configured engine launchers |
| POST | `/api/inference/launchers` | Create a launcher with user-provided executable path |
| PUT | `/api/inference/launchers/{id}` | Update launcher path, base args, working directory, or env |
| DELETE | `/api/inference/launchers/{id}` | Delete launcher metadata when reference checks pass |
| POST | `/api/inference/launchers/{id}/validate` | Validate launcher path and, by default, run a bounded runtime smoke probe. Query: `runtime=false` for path-only. |
| GET | `/api/inference/cleanup` | List pending external cleanup records |
| POST | `/api/inference/cleanup/{id}/retry` | Retry Cloudflare cleanup record |
| DELETE | `/api/inference/cleanup/{id}` | Forget cleanup record without calling Cloudflare |

Preview endpoint behavior:

1. Accepts the same profile body used for create/update plus optional `existing_profile_id` for update previews.
2. Uses current registry state to preserve existing ports/GPU assignments where possible, but does not persist newly suggested reservations.
3. Returns validation blockers, warnings, resolved instance preview, port/GPU plan, command/env preview, systemd preview, Cloudflare plan, and restart-required status.
4. Never writes registries, creates secrets, renders units, starts services, or calls Cloudflare.
5. Create/update endpoints must call the same planner again while holding the profile registry lock before committing state.

Operation API conflict behavior:

1. A mutating request that conflicts with an active operation for the same profile returns `409 Conflict`.
2. The response includes `active_operation_id`, active operation kind, current step, and a link/path to poll it.
3. The UI should open or highlight the active operation instead of starting a duplicate action.
4. Requests for unrelated profiles should not be blocked unless they contend on a short planning/write lock or concrete port/GPU/launcher conflicts.

Remote worker access should follow the existing proxy convention for node-specific APIs.

---

## MCP Access

Inference management should be available through the existing built-in MCP endpoint as a scoped extension. The goal is to let an AI assistant inspect local facts, propose profile settings, validate them, and apply changes when the token has the required write/lifecycle/model scopes.

MCP is not a separate source of truth. Tools call the same profile/model/launcher services as the REST API, and profile registries remain authoritative.

### Resources

Read-only MCP resources:

| Resource | Purpose |
|----------|---------|
| `inframatik://nodes` | List known nodes and roles |
| `inframatik://node/{id}/hardware` | CPU, RAM, GPU model/count/VRAM, driver/runtime hints |
| `inframatik://node/{id}/models` | Local model artifact inventory and manifest summaries |
| `inframatik://node/{id}/inference/launchers` | Configured engine launchers, redacted env, validation summary |
| `inframatik://node/{id}/inference/profiles` | Profile summaries and current status |
| `inframatik://node/{id}/inference/profile/{profile_id}` | Full profile config, rendered command, health summary |
| `inframatik://node/{id}/inference/profile/{profile_id}/client-bundles` | Saved client bundle metadata and default rendered connection metadata without raw secrets |
| `inframatik://node/{id}/inference/operations` | Recent inference operations and progress |
| `inframatik://node/{id}/inference/logs/{profile_id}` | Recent aggregate journald logs, bounded by token scope and line limits |
| `inframatik://node/{id}/system/metrics` | Current system/GPU utilization snapshot |

Resources should redact secrets and raw env values that look sensitive.

### Tools

Read/render tools:

| Tool | Scope | Purpose |
|------|-------|---------|
| `validate_inference_profile` | `mcp:inference:render` | Dry-run validate and plan a draft profile using the shared preview planner |
| `render_inference_command` | `mcp:inference:render` | Return argv/env/systemd preview from the shared no-write planner |
| `estimate_inference_fit` | `mcp:inference:render` | Best-effort fit analysis for VRAM, context, parallelism, and concurrency |
| `render_inference_client_bundle` | `mcp:inference:render` | Render connection examples for a profile/instance target with optional caller-supplied one-time secrets; never stores secrets |
| `get_inference_operation` | `mcp:read` | Read one inference operation status/result |

Write tools:

| Tool | Scope | Purpose |
|------|-------|---------|
| `create_inference_profile` | `mcp:inference:write` | Create a stopped profile |
| `update_inference_profile` | `mcp:inference:write` | Patch an existing stopped profile |
| `delete_inference_profile` | `mcp:inference:write` | Delete a stopped profile |
| `save_inference_client_bundle` | `mcp:inference:write` | Save named client bundle metadata without raw secrets |
| `delete_inference_client_bundle` | `mcp:inference:write` | Delete named client bundle metadata only |
| `create_inference_launcher` | `mcp:inference:write` | Create a node-local launcher |
| `update_inference_launcher` | `mcp:inference:write` | Update launcher path, base args, working directory, or env |
| `delete_inference_launcher` | `mcp:inference:write` | Delete launcher metadata when reference checks pass; never deletes files from disk |
| `rotate_inference_api_key` | `mcp:inference:write` | Generate or rotate a profile engine API key. Raw key returned once. |
| `disable_inference_api_key` | `mcp:inference:write` | Disable a profile engine API key |
| `generate_cloudflare_service_token` | `mcp:inference:write` | Generate and attach a new Cloudflare Access service token. Raw secret returned once. |
| `rotate_cloudflare_service_token` | `mcp:inference:write` | Rotate an attached Cloudflare Access service token secret. Raw secret returned once. |
| `retire_cloudflare_service_token` | `mcp:inference:write` | Remove a service token from a profile policy and optionally delete if owned/unreferenced |

Lifecycle tools:

| Tool | Scope | Purpose |
|------|-------|---------|
| `start_inference_profile` | `mcp:inference:lifecycle` | Start generated systemd unit |
| `stop_inference_profile` | `mcp:inference:lifecycle` | Stop generated systemd unit |
| `restart_inference_profile` | `mcp:inference:lifecycle` | Restart generated systemd unit |

Write and lifecycle tools that start long-running work should return `operation_id`, initial state, and polling guidance instead of blocking until a large model has loaded or Cloudflare provisioning has completed.

Model tools:

| Tool | Scope | Purpose |
|------|-------|---------|
| `resolve_model_source` | `mcp:model:read` | Inspect HF/direct/local source and return a file plan |
| `download_model` | `mcp:model:download` | Start a model download/import job |
| `verify_model` | `mcp:model:write` | Verify a local artifact |
| `delete_model` | `mcp:model:write` | Delete a model artifact/snapshot when reference checks pass; never stops profiles automatically |

### Token Scopes

Inference MCP access should use scoped MCP tokens, not the existing single-service `svc_` token model. Suggested scopes:

| Scope | Allows |
|-------|--------|
| `mcp:read` | Basic node/profile/model read resources |
| `mcp:logs` | Log resources |
| `mcp:inference:render` | Validate/render/estimate profiles and render client bundles |
| `mcp:inference:write` | Create/update/delete stopped profiles, manage launchers and client bundles, rotate/disable engine API keys, manage Cloudflare service tokens |
| `mcp:inference:lifecycle` | Start/stop/restart profiles |
| `mcp:model:read` | Resolve model sources and read model inventory |
| `mcp:model:download` | Start download/import jobs |
| `mcp:model:write` | Verify/delete model artifacts where allowed |

Admin UI should generate these tokens explicitly, show the token once, and list only name, scopes, node/profile restrictions, creation time, and last-used time afterward.

Default token scoping:

1. Tokens default to the currently selected node.
2. Tokens created from a profile detail page may default to that profile for lifecycle presets.
3. Tokens intended to create new profiles should keep profile restrictions empty but keep the selected-node restriction.
4. Full-cluster tokens are permitted only when the admin explicitly selects a cluster-wide option.
5. Full-cluster tokens should be visually distinct in the UI and token list.

### Safety Rules

1. Write and lifecycle tools require explicit scopes; read-only tokens cannot mutate profiles, launchers, keys, or models.
2. `trust_remote_code`, public exposure, launcher path changes, raw args, raw env, and model downloads must be flagged by validation output.
3. Validation/render tools call the same side-effect-free planner as `POST /api/inference/profiles/preview`.
4. Tool responses should include validation blockers, warnings, and a rendered command preview before lifecycle operations.
5. MCP mutation tools do not require browser approval in MVP. The token scopes are the permission boundary.
6. Token scope should restrict node IDs by default; full-cluster tokens are allowed only when explicitly created.
7. MCP resources should never return API keys, service-token secrets, Cloudflare API tokens, profile API keys, or raw secret env values.
8. Master-to-worker behavior follows existing proxy rules; workers execute local profile operations and serve inference locally.
9. Operation resources and `get_inference_operation` must redact secrets and should include log pointers, not unbounded logs.
10. Cloudflare service-token generation/rotation tools may return a raw Client Secret only in the immediate tool result; stored resources expose metadata only.
11. Client bundle rendering may use caller-supplied one-time secrets for examples, but must not persist them or expose them through resources.

MCP model download rules:

1. `mcp:model:download` covers Hugging Face downloads, direct URL downloads, and local imports.
2. Direct URL downloads do not require a separate stronger scope in MVP.
3. Direct URL downloads must pass the same URL safety checks as REST/UI downloads: `https`, no private/link-local targets by default, archive safety, and max-size enforcement.
4. Local imports over MCP must be under configured import allowlist roots on the target node.
5. Downloads started by MCP never auto-create profiles, auto-start profiles, or auto-enable `trust_remote_code`.
6. Tool responses should include the planned source, target node, estimated size when known, artifact ID, and warnings before returning the started job.

---

## Security

1. Validate profile IDs and model refs.
2. Build commands as argv arrays, not shell strings.
3. Validate raw args as argv tokens; never shell-split user input.
4. Redact env vars that look like secrets.
5. Never enable `trust_remote_code` implicitly.
6. Restrict profile file paths to model artifacts or explicitly allowed local imports.
7. Do not expose inference endpoints publicly unless user configures hostname/Access.
8. Treat launcher path and env changes as sensitive and show the exact command preview.
9. Avoid deleting model artifacts that active profiles reference.
10. Do not store Cloudflare Access service token client secrets after creation; show once.
11. In Cloudflare mode, bind engine servers to `127.0.0.1` and route through the local tunnel.
12. Warn when LAN or Cloudflare exposure has no engine API key.
13. Never expose raw engine API keys through MCP resources, command previews, logs, or normal config APIs.

---

## MVP

MVP inference includes:

1. Dedicated node-local profile, launcher, secret, and cleanup registry files.
2. User-configured launchers for llama.cpp, vLLM, and SGLang.
3. Command rendering for the three engine families.
4. Generated systemd units.
5. Start/stop/restart/logs/health.
6. Single-instance and replicated profile deployment on one node.
7. Separate inference port range with stable per-instance reservations.
8. Side-effect-free profile preview/validation planner for UI, REST, and MCP.
9. Persisted inference operation records with progress and interrupted-operation recovery.
10. UI profile list and create/edit flow.
11. Model artifact selection from model store.
12. Raw args/env var passthrough.
13. Optional Cloudflare hostname integration for single-instance profiles.
14. Cloudflare Access Service Auth token creation, selection, generate-new, rotate, and retire flows for inference APIs.
15. Engine API key generation, one-time display, redaction, and rotation.
16. Client connection bundle view with saved metadata, rendered examples, and no raw secret persistence.
17. Journald-backed per-instance logs and recent aggregate profile logs.
18. Non-generative health checks with per-instance and aggregate status.
19. Manual test request action.
20. Lifecycle/GPU metrics and optional user-enabled engine metrics.
21. Redacted profile export for backup/debug.
22. Retryable Cloudflare cleanup records for failed external cleanup.
23. MCP read/render/validate, write, lifecycle, and model download/write tools with scoped token authorization.

## Implementation Build Order

Inference MVP should be implemented in this order:

1. Model storage backend basics.
2. Model storage UI.
3. Engine launcher registry.
4. Profile preview planner and command rendering.
5. Profile registry and generated systemd units.
6. Inference operation runner.
7. Inference UI.
8. Cloudflare exposure and client connection bundles.
9. Inference MCP resources and tools.

The detailed planned Step 9, acceptance criteria, and test strategy live in [Build Order](build-order.md). The sequence is intentionally local-first: prove model references, launchers, command rendering, systemd units, and lifecycle before adding Cloudflare exposure and MCP mutation surfaces.

Deferred:

1. Engine detection.
2. Engine installation automation.
3. Engine upgrades.
4. Advanced metrics dashboard.
5. Multi-node bulk profile deployment.
6. Built-in router/load balancer.
7. Model-specific auto-tuning database.
8. Single public hostname load balancing across replicated instances.
9. Cloudflare API Shield mTLS/JWT validation.
10. Streaming logs and full historical cross-instance log search.
11. Automatic generative health checks.
12. Automatic engine metrics flag injection.
13. Prometheus scraping, historical metrics, request tracing, billing/accounting, and automatic benchmarks.
14. Profile import and cross-node profile sharing.
15. Rolling restart orchestration.

---

## Open Decisions

| Decision | Options | Current leaning |
|----------|---------|-----------------|
| Inference port range | Reuse service range, separate 9xxx, separate 10xxx, configurable | Separate configurable range; default `10000-10999` |
| Inference state location | `node.json`, `services.json`, dedicated JSON files, SQLite | Dedicated node-local JSON files; `node.json` keeps only node-level settings |
| Profile preview | Save-only validation, separate validate/render calls, shared no-write planner | Shared side-effect-free planner used by UI, REST, and MCP; save re-plans under lock |
| Long-running operations | Block HTTP until done, background operation records, external queue | Node-local persisted operation records; queued/running become `failed_interrupted` after app restart |
| Operation concurrency | Serialize all inference work, queue per profile, per-profile mutex plus short node planning lock | Per-profile mutating operations are exclusive; unrelated profiles can run concurrently after planning |
| Engine setup | Detect automatically, install via UI, user-supplied launcher path | User-supplied launcher path; no detection or install for MVP |
| Raw args format | Plain text shell box, argv token rows, key/value | Ordered argv token rows; no shell splitting |
| Profile start after create | Always ask, default yes, default no | Ask with default no |
| Public exposure | Same as normal services, inference-specific route UI | Local/LAN/Cloudflare modes with API-oriented protection |
| API keys for inference servers | Mandatory, recommended, disabled, proxy auth | Recommended for LAN/Cloudflare, not mandatory |
| Cloudflare API protection | Browser login, Service Auth, API Shield mTLS/JWT, no Access | Service Auth tokens for MVP; mTLS/JWT deferred |
| Cloudflare client credential rollover | Rotate only, create replacement only, both | Support both: rotate an existing service token and generate new service tokens for no-downtime rollover |
| Client connection bundles | Ad-hoc examples only, persist full secrets, persist metadata only | Persist named bundle metadata and render examples; never persist raw secrets |
| Replicated client bundles | Implicit instance 0, aggregate endpoint, explicit instance target | Explicit instance target in MVP; aggregate endpoint requires future router/load-balancer |
| MCP write approval | Token-only, browser approval required, configurable per token | Token-only; explicit scopes grant write/lifecycle/model permissions |
| Tuning presets | Simple/throughput/long-context presets, grouped fields only | Grouped fields only for MVP |
| VRAM fit validation | Hard block, warning-only, no validation | Warning-only unless GPU IDs/conflicts are concrete blockers |
| GPU ownership | Exclusive-only, shared-only, per-profile claim mode | Per-profile `exclusive`/`shared`, default `exclusive`; shared overlap warns only |
| Replicated logs | Pick instance first, aggregate by default, streaming merge | Recent aggregate tail by default; per-instance logs available |
| Generative health checks | Automatic prompt, manual test only, none | Manual test only; health polling is non-generative |
| Engine metrics | Auto-enable flags, user-enabled only, ignore | User-enabled only; do not mutate command just for metrics |
| Manual test target | Local only, public endpoint only, both modes | Both modes; default local instance |
| Profile export/import | Export only, import/export, none | Redacted export only for MVP; import deferred |
| Model compatibility checks | Strict, advisory, none | None for MVP; user explicitly chooses engine/profile config |
| Profile start/restart failure | Best-effort partial capacity, all-or-nothing rollback | All-or-nothing for profile-level lifecycle |
| Startup readiness gate | systemd only, TCP, API/model, test prompt | systemd active plus TCP readiness; API/model refines health |
| Profile restart mode | Stop-all/start-all, rolling restart | Stop-all then transactional start-all for MVP |
| Stop with Cloudflare exposure | Preserve resources, remove resources, disable route | Preserve resources; cleanup on delete or explicit exposure removal |
| Cloudflare cleanup failure | Block deletion, retryable cleanup record, ignore | Local delete succeeds; retryable cleanup record remains |
| Launcher deletion with references | Block all refs, confirm stopped refs, delete files too | Block running refs; confirm stopped refs; never delete executable files |

---

## External References

- llama.cpp server: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
- vLLM online serving: https://docs.vllm.ai/en/latest/serving/online_serving/
- vLLM engine arguments: https://docs.vllm.ai/en/stable/configuration/engine_args/
- vLLM expert parallel deployment: https://docs.vllm.ai/en/latest/serving/expert_parallel_deployment/
- vLLM data parallel deployment: https://docs.vllm.ai/en/latest/serving/data_parallel_deployment/
- SGLang server arguments: https://docs.sglang.io/docs/advanced_features/server_arguments
- SGLang speculative decoding: https://docs.sglang.io/docs/advanced_features/speculative_decoding
- SGLang quantization: https://docs.sglang.io/docs/advanced_features/quantization
- GLM-5.2 model card: https://huggingface.co/zai-org/GLM-5.2
- GLM-5.2 vLLM deployment guide: https://recipes.vllm.ai/zai-org/GLM-5.2
- GLM-5.2 SGLang cookbook: https://docs.sglang.io/cookbook/autoregressive/GLM/GLM-5.2
- Qwen3-Coder vLLM usage guide: https://docs.vllm.ai/projects/recipes/en/latest/Qwen/Qwen3-Coder-480B-A35B.html
- Qwen3-Coder SGLang cookbook: https://docs.sglang.io/cookbook/autoregressive/Qwen/Qwen3-Coder
- Cloudflare Access service tokens: https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/
- Cloudflare Access policies: https://developers.cloudflare.com/cloudflare-one/access-controls/policies/
- Cloudflare Access application token: https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/
- Cloudflare API Shield mTLS: https://developers.cloudflare.com/api-shield/security/mtls/
- Cloudflare API Shield JWT validation: https://developers.cloudflare.com/api-shield/security/jwt-validation/
- NVIDIA RTX PRO 6000 Blackwell Server Edition: https://www.nvidia.com/en-us/data-center/rtx-pro-6000-blackwell-server-edition/
