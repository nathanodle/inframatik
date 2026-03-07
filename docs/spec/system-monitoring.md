# System Monitoring Specification

**Status:** Complete

## Overview

inframatik collects real-time system metrics from each node and exposes them via a single API endpoint. The browser polls this endpoint every 5 seconds to update the dashboard. Metrics cover CPU, memory, disk, network, GPU (NVIDIA and AMD), temperatures, top processes, load averages, and uptime.

---

## Related Specs

| Spec | Relationship |
|------|-------------|
| [Stack](stack.md) | psutil dependency, no database, JSON responses |
| [UI](ui.md) | Metric cards, progress bars, tabs, host info bar |
| [Clustering](clustering.md) | Master proxies `/api/nodes/{id}/system` to workers |

---

## Requirements

1. **Single endpoint** -- All system metrics returned in one `GET /api/system` call to minimize round trips.
2. **Low latency** -- Response must be fast enough for 5-second polling. CPU measurement uses 0.5s interval.
3. **No persistence** -- Metrics are point-in-time snapshots; no historical storage.
4. **GPU auto-detection** -- Try NVIDIA (nvidia-smi) first, fall back to AMD (rocm-smi). If neither is available, return empty list.
5. **Safe collection** -- All metric collection wrapped in try/except to prevent partial failures from crashing the endpoint.
6. **CPU model caching** -- Read from `/proc/cpuinfo` once and cache globally since it never changes.

---

## Metrics Collected

| Metric | Source | Details |
|--------|--------|---------|
| CPU overall | `psutil.cpu_percent(interval=0.5)` | Blocks for 0.5s to measure |
| CPU per-core | `psutil.cpu_percent(interval=0, percpu=True)` | Instantaneous after the 0.5s measurement |
| CPU count | `psutil.cpu_count()` | Logical core count |
| CPU frequency | `psutil.cpu_freq()` | Current frequency in MHz |
| CPU model | `/proc/cpuinfo` ("model name" field) | Cached after first read; falls back to `platform.processor()` |
| Memory | `psutil.virtual_memory()` | total, used, available, percent |
| Swap | `psutil.swap_memory()` | total, used, percent |
| Disks | `psutil.disk_partitions()` + `psutil.disk_usage()` | Per-partition; filters out snap mounts, squashfs, tmpfs |
| Network totals | `psutil.net_io_counters()` | Cumulative bytes_sent, bytes_recv |
| Network interfaces | `psutil.net_if_addrs()` + `psutil.net_if_stats()` + `psutil.net_io_counters(pernic=True)` | Per-interface: name, IPv4 address, speed, counters; excludes loopback and down interfaces |
| Temperatures | `psutil.sensors_temperatures()` | CPU temp from k10temp (AMD) or coretemp (Intel); NVMe temp from nvme sensor |
| GPUs (NVIDIA) | `nvidia-smi --query-gpu=...` | index, name, temp, utilization, VRAM used/total, power draw |
| GPUs (AMD) | `rocm-smi --json` | Same fields as NVIDIA, parsed from JSON output; VRAM converted from bytes to MB |
| Top processes | `psutil.process_iter()` | Top 8 by CPU%, then memory%; fields: pid, name, cpu, mem |
| Load averages | `psutil.getloadavg()` | 1min, 5min, 15min |
| Uptime | `psutil.boot_time()` | Computed as `time.time() - boot_time`; formatted as "Xd Xh Xm" |
| Host info | `platform.node()`, `platform.system()`, `platform.release()`, `platform.freedesktop_os_release()` | hostname, OS string, distro pretty name |

---

## API Endpoint

### `GET /api/system`

Returns all system metrics in a single JSON response.

**Authentication:** Required (session token, CF JWT, or API key via middleware).

**Response schema:**

```json
{
  "host": {
    "hostname": "my-server",
    "os": "Linux 6.8.0-101-generic",
    "distro": "Ubuntu 24.04.1 LTS",
    "cpu_model": "AMD Ryzen 9 7950X 16-Core Processor"
  },
  "cpu": {
    "percent": 12.3,
    "per_cpu": [5.1, 8.2, 15.0, 3.1, ...],
    "count": 32,
    "freq_mhz": 4500
  },
  "memory": {
    "total": 68719476736,
    "used": 34359738368,
    "available": 34359738368,
    "percent": 50.0
  },
  "swap": {
    "total": 8589934592,
    "used": 0,
    "percent": 0.0
  },
  "disks": [
    {
      "mount": "/",
      "device": "/dev/nvme0n1p2",
      "fstype": "ext4",
      "total": 1000204886016,
      "used": 500102443008,
      "free": 500102443008,
      "percent": 50.0
    }
  ],
  "network": {
    "bytes_sent": 1234567890,
    "bytes_recv": 9876543210,
    "interfaces": [
      {
        "name": "enp5s0",
        "ip": "192.168.1.100",
        "speed_mbps": 1000,
        "bytes_sent": 1234567890,
        "bytes_recv": 9876543210
      }
    ]
  },
  "temps": {
    "cpu": 52.0,
    "nvme": 38.0
  },
  "gpus": [
    {
      "index": 0,
      "name": "NVIDIA GeForce RTX 4090",
      "temp_c": 45.0,
      "util_percent": 0.0,
      "mem_used_mb": 512.0,
      "mem_total_mb": 24576.0,
      "power_w": 25.0
    }
  ],
  "processes": [
    {
      "pid": 1234,
      "name": "python3",
      "cpu": 45.2,
      "mem": 3.1
    }
  ],
  "load": {
    "1min": 0.5,
    "5min": 0.3,
    "15min": 0.2
  },
  "uptime": "5d 12h 30m",
  "uptime_seconds": 475800
}
```

### Proxied Endpoint (Master)

`GET /api/nodes/{node_id}/system` -- Master proxies to a worker's `/api/system` endpoint via `proxy_to_node()`. Returns the same schema.

---

## Data Model

No persistent data model. All values are computed at request time from OS APIs and external tools.

### GPU Detection Logic

```
_get_gpus():
  1. Try _get_gpus_nvidia() (subprocess: nvidia-smi)
  2. If result is non-empty, return it
  3. Otherwise, try _get_gpus_amd() (subprocess: rocm-smi --json)
  4. Return AMD results (may be empty)
```

Both nvidia-smi and rocm-smi calls have a 5-second timeout. `FileNotFoundError` (tool not installed) returns empty list silently.

### Disk Filtering

Partitions are filtered to exclude:
- Snap mounts (mountpoint contains "snap")
- squashfs and tmpfs filesystems
- Duplicate mountpoints (tracked via `seen` set)
- Partitions that raise `PermissionError` on `disk_usage()`

### Network Interface Filtering

Interfaces are filtered to exclude:
- Loopback (`lo`)
- Interfaces that are down (`stat.isup == False`)

---

## UI Components

### Host Info Bar

Displayed at the top of the main content area. Shows: distro, CPU model, core count, total RAM.

Format: `Ubuntu 24.04.1 LTS | AMD Ryzen 9 7950X | 32 cores | 64.0 GB RAM`

### Overview Tab (default)

Six metric cards in a responsive grid:

| Card | Label | Value | Sub-text | Extra |
|------|-------|-------|----------|-------|
| CPU | "CPU" | `percent%` | "N cores @ Xmhz" | Progress bar + per-core mini bars |
| Memory | "Memory" | `percent%` | "X.X GB / Y.Y GB" | Progress bar |
| Disk | "Disk /" | `percent%` | "X.X GB / Y.Y GB" | Progress bar |
| Network | "Network" | Send rate | Recv rate | Computed from delta between polls |
| Load | "Load Average" | 1min value | "5min / 15min (5m/15m)" | None |
| Temperature | "Temperature" | CPU temp "C" | NVMe temp label | Hidden if no temps |

Network rates are computed client-side by storing previous `bytes_sent`/`bytes_recv` and timestamps, then calculating bytes per second.

### GPUs Tab

One metric card per GPU in the metrics grid. Each GPU card contains:
- GPU name as the label
- Temperature as the main value
- Stats grid: VRAM (used/total + progress bar), Utilization (% + progress bar), Power draw
- Min width: 300px

### Processes Tab

Table with header row (PID, Name, CPU%, Mem%) and up to 8 process rows. JetBrains Mono font. Sorted by CPU usage descending.

### Network Tab

One metric card per network interface showing:
- Interface name as label
- IP address as main value
- Speed in Mbps as sub-text
- Bytes sent/received counters

### Storage Tab

One metric card per disk partition showing:
- Mount point as label
- Usage percentage as main value
- Used/total as sub-text
- Device name and filesystem type
- Progress bar

---

## Error Handling

| Scenario | Handling |
|----------|---------|
| nvidia-smi not found | Returns empty GPU list silently |
| rocm-smi not found | Returns empty GPU list silently |
| nvidia-smi/rocm-smi timeout (5s) | Returns empty GPU list |
| Temperature sensors unavailable | `temps` dict is empty; UI hides temp card |
| Disk permission error | Partition skipped |
| `/proc/cpuinfo` unreadable | Falls back to `platform.processor()` then "Unknown" |
| 401 from API | Browser clears token, redirects to login |
| Network error during polling | Logged to console, next poll retries |

---

## Decisions

| Decision | Alternatives Considered | Why This Choice |
|----------|------------------------|-----------------|
| Single `/api/system` endpoint | Separate endpoints per metric type | Reduces HTTP round trips for 5s polling. Single call is simpler. |
| 0.5s CPU measurement interval | 0 (instantaneous), 1s | 0.5s balances accuracy vs. response time. 0 gives unreliable readings. |
| Top 8 processes | All processes, top 20 | 8 is enough for a dashboard. More would slow the endpoint and clutter the UI. |
| Client-side rate calculation | Server-side rate tracking | No server state needed. Browser computes deltas between polls. |
| NVIDIA before AMD detection | Parallel check, config flag | Sequential is simpler. NVIDIA is more common. Both being present simultaneously is unusual. |
| No metric history | SQLite time-series, RRD | Out of scope for v1. Future consideration noted in index.md. |
