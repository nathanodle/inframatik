import json
import logging
import platform
import subprocess
import time

import psutil

logger = logging.getLogger("inframatik.system")


def _get_cpu_model() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if "model name" in line:
                    return line.split(":")[1].strip()
    except (OSError, ValueError, IndexError) as e:
        logger.debug("Failed to read CPU model from /proc/cpuinfo: %s", e)
    return platform.processor() or "Unknown"


def _get_temperatures() -> dict:
    result = {}
    try:
        temps = psutil.sensors_temperatures()
        # CPU temp (k10temp for AMD, coretemp for Intel)
        for key in ("k10temp", "coretemp"):
            if key in temps and temps[key]:
                result["cpu"] = temps[key][0].current
                break
        # NVMe temp
        if "nvme" in temps and temps["nvme"]:
            result["nvme"] = temps["nvme"][0].current
    except (AttributeError, OSError, NotImplementedError, psutil.Error) as e:
        logger.debug("Failed to read temperature sensors: %s", e)
    return result


def _get_gpus_nvidia() -> list[dict]:
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return []
        gpus = []
        for line in r.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 7:
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "temp_c": float(parts[2]),
                    "util_percent": float(parts[3]),
                    "mem_used_mb": float(parts[4]),
                    "mem_total_mb": float(parts[5]),
                    "power_w": float(parts[6]),
                })
        return gpus
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as e:
        logger.debug("Failed to collect NVIDIA GPU metrics: %s", e)
        return []


def _get_gpus_amd() -> list[dict]:
    try:
        r = subprocess.run(
            ["rocm-smi", "--showtemp", "--showuse", "--showmeminfo", "vram",
             "--showpower", "--showproductname", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return []
        data = json.loads(r.stdout)
        gpus = []
        for idx, (card, info) in enumerate(sorted(data.items())):
            if not card.startswith("card"):
                continue
            vram_total = int(info.get("VRAM Total Memory (B)", 0))
            vram_used = int(info.get("VRAM Total Used Memory (B)", 0))
            gpus.append({
                "index": idx,
                "name": info.get("Card Series", "AMD GPU"),
                "temp_c": float(info.get("Temperature (Sensor edge) (C)", 0)),
                "util_percent": float(info.get("GPU use (%)", 0)),
                "mem_used_mb": vram_used / 1048576,
                "mem_total_mb": vram_total / 1048576,
                "power_w": float(info.get("Average Graphics Package Power (W)", 0)),
            })
        return gpus
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.SubprocessError) as e:
        logger.debug("Failed to collect AMD GPU metrics: %s", e)
        return []


def _get_gpus() -> list[dict]:
    gpus = _get_gpus_nvidia()
    if gpus:
        return gpus
    return _get_gpus_amd()


def _get_disks() -> list[dict]:
    disks = []
    seen = set()
    for part in psutil.disk_partitions():
        # Skip snap mounts and tmpfs
        if "snap" in part.mountpoint or part.fstype in ("squashfs", "tmpfs"):
            continue
        if part.mountpoint in seen:
            continue
        seen.add(part.mountpoint)
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "mount": part.mountpoint,
                "device": part.device,
                "fstype": part.fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": usage.percent,
            })
        except PermissionError:
            continue
    return disks


def _get_top_processes(n: int = 8) -> list[dict]:
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            info = p.info
            if info["cpu_percent"] is not None:
                procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    # Sort by CPU, then memory
    procs.sort(key=lambda p: (p["cpu_percent"], p["memory_percent"]), reverse=True)
    return [
        {
            "pid": p["pid"],
            "name": p["name"],
            "cpu": round(p["cpu_percent"], 1),
            "mem": round(p["memory_percent"], 1),
        }
        for p in procs[:n]
    ]


def _get_net_interfaces() -> list[dict]:
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    counters = psutil.net_io_counters(pernic=True)
    interfaces = []
    for name in sorted(addrs.keys()):
        if name == "lo":
            continue
        stat = stats.get(name)
        if not stat or not stat.isup:
            continue
        counter = counters.get(name)
        ipv4 = None
        for addr in addrs[name]:
            if addr.family.name == "AF_INET":
                ipv4 = addr.address
                break
        interfaces.append({
            "name": name,
            "ip": ipv4,
            "speed_mbps": stat.speed,
            "bytes_sent": counter.bytes_sent if counter else 0,
            "bytes_recv": counter.bytes_recv if counter else 0,
        })
    return interfaces


def _get_distro() -> str:
    try:
        return platform.freedesktop_os_release().get("PRETTY_NAME", "")
    except (OSError, AttributeError):
        return ""


# Cache CPU model since it never changes
_cpu_model = None


def get_system_metrics() -> dict:
    global _cpu_model
    if _cpu_model is None:
        _cpu_model = _get_cpu_model()

    cpu_percent = psutil.cpu_percent(interval=0.5)
    cpu_freq = psutil.cpu_freq()
    cpu_count = psutil.cpu_count()
    per_cpu = psutil.cpu_percent(interval=0, percpu=True)

    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    net = psutil.net_io_counters()
    load = psutil.getloadavg()
    boot_time = psutil.boot_time()
    uptime_seconds = int(time.time() - boot_time)

    days, remainder = divmod(uptime_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days > 0:
        uptime_str = f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        uptime_str = f"{hours}h {minutes}m"
    else:
        uptime_str = f"{minutes}m"

    return {
        "host": {
            "hostname": platform.node(),
            "os": f"{platform.system()} {platform.release()}",
            "distro": _get_distro(),
            "cpu_model": _cpu_model,
        },
        "cpu": {
            "percent": cpu_percent,
            "per_cpu": per_cpu,
            "count": cpu_count,
            "freq_mhz": round(cpu_freq.current) if cpu_freq else None,
        },
        "memory": {
            "total": mem.total,
            "used": mem.used,
            "available": mem.available,
            "percent": mem.percent,
        },
        "swap": {
            "total": swap.total,
            "used": swap.used,
            "percent": swap.percent,
        },
        "disks": _get_disks(),
        "network": {
            "bytes_sent": net.bytes_sent,
            "bytes_recv": net.bytes_recv,
            "interfaces": _get_net_interfaces(),
        },
        "temps": _get_temperatures(),
        "gpus": _get_gpus(),
        "processes": _get_top_processes(),
        "load": {
            "1min": load[0],
            "5min": load[1],
            "15min": load[2],
        },
        "uptime": uptime_str,
        "uptime_seconds": uptime_seconds,
    }
