# Model Storage Specification

**Status:** Draft

## Overview

inframatik manages model files as first-class local artifacts instead of relying on engine-specific default caches such as the Hugging Face cache. Downloaders may use Hugging Face APIs, direct URLs, or local imports, but the final runtime source of truth is an inframatik-managed model store on each node.

The model store exists to make inference profiles stable, inspectable, movable, and safe to delete. Profiles reference model artifacts by ID and snapshot, not arbitrary cache paths.

---

## Related Specs

| Spec | Relationship |
|------|-------------|
| [Inference](inference.md) | Profiles reference model artifacts stored by this system |
| [Backend](backend.md) | JSON registries, filesystem layout, route conventions |
| [UI](ui.md) | Model inventory, downloader progress, import flows |
| [Clustering](clustering.md) | Model artifacts are stored per node and reported to master |
| [Cloudflare Integration](cloudflare.md) | Optional public access for inference endpoints created from profiles |
| [Build Order](build-order.md) | Planned inference implementation sequence, acceptance criteria, and test strategy |

---

## Goals

1. **Own the runtime store** - Engines run against inframatik-managed paths, not transient downloader cache paths.
2. **Support multiple acquisition sources** - Hugging Face, direct URL, local import, and future object stores.
3. **Make artifacts inspectable** - Each artifact has a manifest with source, files, format, size, and checksums.
4. **Support node-local storage** - Workers store model files locally. Master coordinates but does not proxy inference traffic or model file reads.
5. **Enable safe deletion** - Deletion must understand profile references and avoid removing files still in use.
6. **Support resume and verification** - Downloads land in staging and are verified before becoming visible artifacts.
7. **Keep engine-specific caches optional** - Engines may have internal caches, but profile command generation points to the managed artifact path.

---

## Non-Goals

1. **Do not build a model marketplace** - inframatik stores and downloads models, but does not curate a public catalog in MVP.
2. **Do not proxy model downloads through master by default** - Workers download from configured source directly.
3. **Do not require Hugging Face** - HF is one source, not the storage backend.
4. **Do not infer every model quirk in code** - Model-specific behavior is represented through profile fields and user overrides.
5. **Do not implement distributed shared storage in MVP** - NFS, object-store-backed caches, and peer-to-peer seeding are future options.

---

## Terminology

| Term | Definition |
|------|------------|
| Model artifact | A locally managed model package, such as a safetensors repo snapshot, GGUF file, LoRA adapter, tokenizer-only bundle, or multimodal companion file set |
| Snapshot | A specific resolved version of an artifact, usually pinned by source commit, revision, or content hash |
| Manifest | JSON metadata describing an artifact, its source, files, checksums, and basic format |
| Source | Where a model was acquired from: Hugging Face repo, direct URL, local path, future object store |
| Import | Creating a managed artifact from files already present on disk |

---

## Storage Root

Default root:

```text
~/.local/share/inframatik/models
```

The root must be configurable before implementation begins. The likely configuration key is stored per node:

```json
{
  "model_store_root": "/data/models/inframatik"
}
```

Open decisions:

| Decision | Current leaning | Notes |
|----------|-----------------|-------|
| Storage root location | XDG default plus UI-configurable override | GPU servers often use large mounted volumes |
| Config location | `node.json` | Simple and consistent with existing configuration model |
| Per-node override | Yes | Workers may have different disk layouts |

Root change behavior:

1. The model store root is a per-node setting.
2. Changing the root is allowed only when no model download/import/verify/delete jobs are running on that node.
3. Changing the root does not move existing artifacts in MVP.
4. After a root change, new downloads/imports write to the new root.
5. Existing registry entries continue to point at their original snapshot paths and remain visible if those paths still exist.
6. The UI should show artifacts outside the current root as "previous root" or "external managed path" and should not silently delete or move them.
7. Automatic migration between roots is deferred.

---

## Filesystem Layout

Proposed layout:

```text
~/.local/share/inframatik/models/
  artifacts/
    qwen3-coder-30b-a3b/
      manifest.json
      snapshots/
        2026-06-21-main/
          config.json
          tokenizer.json
          model-00001-of-00008.safetensors
  gguf/
    qwen3-14b-q4-k-m/
      manifest.json
      snapshots/
        2026-06-21/
          model.gguf
          mmproj.gguf
  adapters/
    my-lora/
      manifest.json
      snapshots/
        v1/
          adapter_model.safetensors
```

Rules:

1. Runtime paths exposed to engines should be stable snapshot directories or files.
2. Staging downloads must not appear in artifact inventory until complete.
3. MVP stores real files directly in snapshot directories.
4. Content-addressed blob dedupe is deferred.
5. UI and profile schemas reference artifact IDs and snapshot IDs, not internal file paths.
6. Artifact IDs are lowercase DNS-label-like slugs with hyphens.
7. Every managed file is SHA-256 hashed before the artifact snapshot is marked `ready`.
8. Hashing happens in the download/import job, not as a later background cleanup step.

---

## Registry

Model inventory should be stored separately from service registry.

Default file:

```text
~/.config/inframatik/models.json
```

Shape:

```json
{
  "artifacts": {
    "qwen3-coder-30b-a3b": {
      "id": "qwen3-coder-30b-a3b",
      "kind": "hf_snapshot",
      "format": "safetensors",
      "active_snapshot": "2026-06-21-main",
      "created_at": 1782000000,
      "updated_at": 1782000300,
      "size_bytes": 61234567890,
      "snapshots": {
        "2026-06-21-main": {
          "manifest_path": "artifacts/qwen3-coder-30b-a3b/manifest.json",
          "state": "ready"
        }
      }
    }
  },
  "downloads": {
    "dl_abc123": {
      "artifact_id": "qwen3-coder-30b-a3b",
      "state": "running",
      "progress": 42.5
    }
  }
}
```

The registry is an index. The manifest remains the authoritative artifact description.

---

## Manifest Schema

Each artifact snapshot writes a `manifest.json`.

```json
{
  "schema_version": 1,
  "id": "qwen3-coder-30b-a3b",
  "snapshot": "2026-06-21-main",
  "display_name": "Qwen3 Coder 30B A3B",
  "kind": "hf_snapshot",
  "format": "safetensors",
  "source": {
    "type": "huggingface",
    "repo": "Qwen/example",
    "revision": "main",
    "commit": "abc123"
  },
  "files": [
    {
      "path": "config.json",
      "size": 1234,
      "sha256": "abc123"
    }
  ],
  "metadata": {}
}
```

Required fields:

| Field | Required | Notes |
|-------|----------|-------|
| `schema_version` | Yes | Enables future migration |
| `id` | Yes | Stable artifact ID |
| `snapshot` | Yes | Stable snapshot ID |
| `kind` | Yes | `hf_snapshot`, `gguf`, `adapter`, `local_dir`, `url_file` |
| `format` | Yes | `safetensors`, `gguf`, `pytorch`, `adapter`, `mixed`, `unknown` |
| `source` | Yes | Source metadata |
| `files` | Yes | Path, size, SHA-256 checksum |
| `metadata` | No | Optional user-provided notes or parsed metadata; not used for compatibility gating |

---

## Artifact Kinds

| Kind | Description | Runtime path |
|------|-------------|--------------|
| `hf_snapshot` | Hugging Face-style repo snapshot with config/tokenizer/weights | Snapshot directory |
| `gguf` | One GGUF file, optionally with `mmproj` or companion files | GGUF file path |
| `adapter` | LoRA or similar adapter files | Snapshot directory |
| `local_dir` | Imported directory that does not fit a known source | Snapshot directory |
| `url_file` | Single downloaded file from a direct URL | File path |
| `url_archive` | Extracted archive downloaded from a direct URL | Snapshot directory |

---

## Download Sources

### Hugging Face

HF support should use APIs only as an acquisition mechanism.

Flow:

1. User enters repo ID and optional revision.
2. Backend resolves repo metadata and file list.
3. UI presents a download plan.
4. User selects preset or custom file list.
5. Backend downloads files into staging.
6. Backend verifies size/checksum when available.
7. Backend computes SHA-256 for every downloaded file.
8. Backend writes snapshot files, manifest, and registry entry.

Download presets:

| Preset | Included files |
|--------|----------------|
| Full repo | All model, tokenizer, config, and processor files |
| Safetensors only | `*.safetensors`, config/tokenizer/processor files |
| Tokenizer/config only | No weights |
| GGUF file | Selected `*.gguf` file plus optional multimodal companion |
| Custom | User-selected files |

HF auth:

1. HF tokens are secrets.
2. Token storage must use restrictive permissions.
3. Token should never be written into artifact manifests.
4. Downloads should support token from environment as a non-persisted option.

### Direct URL

Flow:

1. User enters URL.
2. User optionally enters artifact ID, display name, expected filename, and SHA-256 checksum.
3. Backend downloads into staging.
4. If user provides checksum, verify it.
5. Compute SHA-256 for every downloaded or extracted file.
6. Detect file type by extension and metadata.
7. Write manifest.

Direct URL should support single-file artifacts and simple archives in MVP. This is mainly for GGUF files, `mmproj` companion files, vendor-hosted weight files, and vendor-hosted `.zip`, `.tar`, `.tar.gz`, or `.tgz` bundles.

Archive handling rules:

1. Download archive into staging before extraction.
2. Extract only after size/checksum validation when a checksum was provided.
3. Reject archive entries with absolute paths, `..`, symlinks, hardlinks, device files, or paths outside the staging root.
4. Detect artifact shape after extraction.
5. Delete incomplete staging directories on failure.

Direct URL source metadata:

```json
{
  "source": {
    "type": "url",
    "url": "https://example.com/models/qwen.gguf",
    "filename": "qwen.gguf",
    "sha256": "optional-user-provided-checksum",
    "extract": false,
    "etag": "optional-http-etag",
    "last_modified": "optional-http-last-modified"
  }
}
```

### Local Import

Flow:

1. User provides local path on the node.
2. Backend scans files and estimates format.
3. Backend copies files into the inframatik model store.
4. Backend computes SHA-256 for every copied file.
5. Backend writes the manifest.

Local import is copy-only for MVP. Profiles should never point at arbitrary external paths, because that makes deletion, verification, backups, and worker-local inventory harder to reason about.

Reference-only imports can be reconsidered later for very large model volumes, but they should require clear warnings and different ownership semantics.

---

## Download Jobs

Downloads should be represented as jobs with progress.

Job fields:

```json
{
  "id": "dl_abc123",
  "source": {"type": "huggingface", "repo": "Qwen/example"},
  "artifact_id": "qwen3-coder-30b-a3b",
  "state": "running",
  "current_file": "model-00003-of-00008.safetensors",
  "downloaded_bytes": 123456,
  "total_bytes": 987654,
  "hashed_bytes": 0,
  "hash_total_bytes": 987654,
  "progress": 12.5,
  "started_at": 1782000000,
  "error": null
}
```

States: `queued`, `running`, `hashing`, `verifying`, `ready`, `failed`, `failed_interrupted`, `canceled`.

MVP can use an in-process job runner with JSON-persisted job records. Active work does not resume across inframatik restarts.

Restart behavior:

1. On startup, any job recorded as `queued`, `running`, `hashing`, or `verifying` becomes `failed_interrupted`.
2. Ready artifacts remain valid because registry entries are written only after download/import, immediate hashing, and manifest write complete.
3. Staging directories for interrupted jobs are preserved.
4. UI should show interrupted jobs with **Clean staging** and **Start new download/import** actions.
5. Cleaning staging deletes only the staging directory recorded on that job and never deletes ready artifacts.
6. Byte-range resume and automatic restart of interrupted downloads are deferred.

---

## Verification

Verification levels:

| Level | Description |
|-------|-------------|
| `size` | Size matches source metadata |
| `etag` | Provider ETag checked where meaningful |
| `sha256` | Strong checksum computed and stored |

Every managed file must have a local SHA-256 recorded in the manifest even when the source does not provide one. Immediate hashing is required for MVP because it supports verification, manifest integrity, safe deletion, and future dedupe.

Verify operation:

1. Read manifest.
2. Check every file exists.
3. Recompute SHA-256.
4. Report missing, changed, or extra files.
5. Mark artifact degraded if runtime files are missing.

---

## Deletion Rules

Delete artifact:

1. Check profile registry for references.
2. If any running profile references the artifact or any of its snapshots, reject deletion.
3. If stopped profiles reference the artifact, show the affected profiles and require explicit confirmation.
4. Model deletion never stops profiles automatically in MVP.
5. Remove registry entries and files under the artifact directory only after reference checks pass.

Delete snapshot:

1. Only remove selected snapshot.
2. If any running profile references the snapshot, reject deletion.
3. If stopped profiles reference the snapshot, show the affected profiles and require explicit confirmation.
4. If deleting the active snapshot, user must choose a new active snapshot or delete the whole artifact.
5. Deleting a non-active unused snapshot is allowed after confirmation.

MCP deletion:

1. `delete_model` follows the same artifact and snapshot reference rules as the REST/UI path.
2. It returns blockers for running profile references.
3. It returns warnings and required confirmation details for stopped profile references.
4. It never stops profiles automatically.

Garbage collect:

1. Find artifact directories and staging directories not referenced by the registry.
2. Delete only after confirmation.
3. Never delete files outside the model store root.

---

## Cluster Behavior

Model storage is node-local.

Master responsibilities:

1. Show model inventory for selected node.
2. Aggregate worker model summaries.
3. Trigger worker download/import jobs.
4. Let the user configure a selected worker's local inference profiles only after the required artifact exists or can be downloaded there.

Worker responsibilities:

1. Store models locally.
2. Run downloads locally.
3. Report artifact summaries to master.
4. Serve inference locally from local storage.

No service network traffic or model file reads should route through master by default.

---

## API Sketch

Local-node endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/models` | List local artifacts and download jobs |
| POST | `/api/models/resolve` | Inspect HF, URL, or local source and return file plan |
| POST | `/api/models/download` | Start HF or direct URL download job |
| POST | `/api/models/import` | Import local path |
| GET | `/api/models/jobs/{id}` | Get download/import job status |
| POST | `/api/models/jobs/{id}/cancel` | Cancel job |
| DELETE | `/api/models/jobs/{id}/staging` | Clean preserved staging files for failed/interrupted job |
| POST | `/api/models/{id}/verify` | Verify artifact |
| DELETE | `/api/models/{id}` | Delete artifact |
| GET | `/api/models/{id}/manifest` | Return manifest |
| GET | `/api/models/storage` | Return root, disk usage, active jobs, and previous-root artifacts |
| PUT | `/api/models/storage` | Update model store root when no model jobs are active |

Download request shape:

```json
{
  "source": {
    "type": "url",
    "url": "https://example.com/models/qwen.gguf",
    "sha256": "optional",
    "extract": false
  },
  "artifact_id": "qwen-gguf",
  "display_name": "Qwen GGUF",
  "snapshot": "2026-06-22"
}
```

`source.type` can be `huggingface` or `url` for `/api/models/download`. Local paths use `/api/models/import`.

Master proxy behavior follows existing node proxy conventions.

---

## UI

Model storage appears under the Inference area as a Models subview.

Primary views:

1. **Inventory** - Cards/table of local artifacts.
2. **Downloads** - Active and recent jobs.
3. **Add Model** - Hugging Face repo, Direct URL, or Local path import.
4. **Storage** - Disk usage, store root, garbage collection.

Downloads view:

1. Shows active, completed, failed, interrupted, and canceled jobs.
2. Interrupted jobs explain that inframatik restarted before completion.
3. Interrupted jobs offer **Clean staging** and **Start new download/import** actions.
4. Cleaning staging should show the exact staging path and require confirmation.

Storage view:

1. Shows current model store root for the selected node.
2. Shows disk usage for the current root.
3. Allows changing the root when no model jobs are active.
4. Warns that changing the root does not move existing artifacts.
5. Shows artifacts still registered from a previous root with clear path/status text.

Add Model source modes:

| Mode | Required input | Optional input |
|------|----------------|----------------|
| Hugging Face | Repo ID | Revision, token, file preset, allow/ignore patterns |
| Direct URL | URL | Artifact ID, display name, filename, SHA-256, extract archive |
| Local Import | Node-local path | Artifact ID, display name |

Artifact row content:

| Field | Notes |
|-------|-------|
| Display name | Manifest `display_name` or artifact ID |
| Format | Safetensors, GGUF, adapter, mixed |
| Size | Total bytes |
| Source | HF repo, URL host, local import |
| Snapshot | Revision/commit/snapshot |
| Profiles | Profiles using the artifact |
| Actions | Create profile, verify, delete, rescan |

The UI should be operational and dense, not a model marketplace.

---

## Security

1. Validate all artifact IDs and paths.
2. Prevent path traversal from archive or remote file names.
3. Never execute downloaded files.
4. Store HF/access tokens outside manifests.
5. Redact tokens from logs and UI.
6. Reject deletion outside configured model store root.
7. Avoid shell commands for file operations where Python APIs are safer.
8. Treat raw URLs as untrusted input.
9. Direct URL downloads must use `https` by default.
10. Reject direct URLs that resolve to private, loopback, link-local, multicast, or otherwise non-public IP ranges unless a future admin override exists.
11. Enforce node-level maximum download size before and during download when content length is known or bytes exceed the limit.
12. Local imports triggered through API/MCP must come from configured import allowlist roots.
13. Optional `trust_remote_code` must be explicit at profile level, never implied by download.

Suggested node config:

```json
{
  "model_download_max_bytes": 107374182400,
  "model_import_roots": ["/data/models", "/mnt/models"]
}
```

---

## MVP

MVP model storage includes:

1. Configurable model store root.
2. Model registry file.
3. Manifest schema v1.
4. Local import of GGUF and HF-style directories.
5. Hugging Face download for selected files.
6. Direct URL single-file and archive download.
7. Real files copied into artifact snapshot directories.
8. Download progress jobs.
9. Immediate SHA-256 hashing for every managed file before ready state.
10. Verify operation.
11. Delete with profile-reference checks.
12. Cluster reporting of model inventory summaries.
13. Node-level max download size.
14. Local import allowlist roots for API/MCP imports.
15. Persisted job records with interrupted jobs marked `failed_interrupted`.
16. Explicit staging cleanup action.

Deferred:

1. Object storage mirrors.
2. Peer-to-peer worker seeding.
3. Full resumable download persistence across restarts.
4. Rich public model catalog.
5. Automated remote model-specific tuning updates.
6. Content-addressed blob dedupe.
7. Automatic model store root migration.

---

## Open Decisions

| Decision | Options | Current leaning |
|----------|---------|-----------------|
| Store root | XDG path, `/data/inframatik/models`, user-configured only | XDG default plus per-node UI override; no automatic moves |
| HF commit pinning | Always pin, optional pin, branch-only | Always pin resolved commit in manifest |
| Reference-only imports | Allow, disallow | Disallow in MVP; reconsider later with warnings |
| Immediate hashing | Required, optional, deferred background job | Required before artifact is marked ready |
| Blob dedupe | Symlink, hardlink, copy, defer | Defer; MVP copies files into snapshots |
| Download engine | stdlib/httpx, huggingface_hub library | Use HF API where useful, but own final store |
| Job persistence | In-memory, JSON persisted, systemd transient unit | JSON persisted records; active work marked interrupted after restart |
| Interrupted staging cleanup | Auto-delete, preserve with cleanup action, preserve forever | Preserve staging and expose explicit cleanup |
| MCP direct URL scope | Existing `mcp:model:download`, separate URL scope | Existing scope with URL safety and max-size enforcement |
| Delete models referenced by profiles | Block, auto-stop and delete, confirm stopped refs only | Block running refs; confirm stopped refs; never auto-stop |
