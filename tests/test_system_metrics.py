"""Unit tests for system metric collection helpers."""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import system


class _Patch:
    def __init__(self, patches):
        self.patches = patches
        self.originals = []

    def __enter__(self):
        for obj, name, value in self.patches:
            self.originals.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        for obj, name, value in reversed(self.originals):
            setattr(obj, name, value)
        return False


def test_get_temperatures_reads_cpu_and_nvme_sensors():
    sensor = lambda current: types.SimpleNamespace(current=current)

    def fake_sensors():
        return {
            "coretemp": [sensor(61.5)],
            "k10temp": [sensor(55.0)],
            "nvme": [sensor(43.0)],
        }

    with _Patch([(system.psutil, "sensors_temperatures", fake_sensors)]):
        result = system._get_temperatures()

    assert result == {"cpu": 55.0, "nvme": 43.0}


def test_get_gpus_nvidia_parses_csv_output():
    def fake_run(cmd, capture_output, text, timeout):
        assert cmd[0] == "nvidia-smi"
        assert capture_output is True
        assert text is True
        assert timeout == 5
        return types.SimpleNamespace(
            returncode=0,
            stdout="0, RTX 4090, 62, 91, 12000, 24576, 420.5\n",
        )

    with _Patch([(system.subprocess, "run", fake_run)]):
        result = system._get_gpus_nvidia()

    assert result == [
        {
            "index": 0,
            "name": "RTX 4090",
            "temp_c": 62.0,
            "util_percent": 91.0,
            "mem_used_mb": 12000.0,
            "mem_total_mb": 24576.0,
            "power_w": 420.5,
        }
    ]


def test_get_gpus_amd_parses_rocm_json():
    def fake_run(cmd, capture_output, text, timeout):
        assert cmd[0] == "rocm-smi"
        return types.SimpleNamespace(
            returncode=0,
            stdout=(
                '{"card1":{"Card Series":"Radeon",'
                '"Temperature (Sensor edge) (C)":"70",'
                '"GPU use (%)":"85",'
                '"VRAM Total Memory (B)":"2147483648",'
                '"VRAM Total Used Memory (B)":"536870912",'
                '"Average Graphics Package Power (W)":"125.5"}}'
            ),
        )

    with _Patch([(system.subprocess, "run", fake_run)]):
        result = system._get_gpus_amd()

    assert result == [
        {
            "index": 0,
            "name": "Radeon",
            "temp_c": 70.0,
            "util_percent": 85.0,
            "mem_used_mb": 512.0,
            "mem_total_mb": 2048.0,
            "power_w": 125.5,
        }
    ]


def test_get_disks_filters_unsupported_mounts_duplicates_and_permission_errors():
    parts = [
        types.SimpleNamespace(mountpoint="/", device="/dev/sda1", fstype="ext4"),
        types.SimpleNamespace(mountpoint="/snap/app", device="/dev/loop0", fstype="squashfs"),
        types.SimpleNamespace(mountpoint="/run", device="tmpfs", fstype="tmpfs"),
        types.SimpleNamespace(mountpoint="/", device="/dev/sda1", fstype="ext4"),
        types.SimpleNamespace(mountpoint="/secret", device="/dev/sdb1", fstype="ext4"),
    ]

    def fake_usage(mount):
        if mount == "/secret":
            raise PermissionError("denied")
        return types.SimpleNamespace(total=100, used=40, free=60, percent=40.0)

    with _Patch([
        (system.psutil, "disk_partitions", lambda: parts),
        (system.psutil, "disk_usage", fake_usage),
    ]):
        result = system._get_disks()

    assert result == [
        {
            "mount": "/",
            "device": "/dev/sda1",
            "fstype": "ext4",
            "total": 100,
            "used": 40,
            "free": 60,
            "percent": 40.0,
        }
    ]


def test_get_net_interfaces_skips_loopback_and_down_interfaces():
    af_inet = types.SimpleNamespace(name="AF_INET")
    addrs = {
        "lo": [types.SimpleNamespace(family=af_inet, address="127.0.0.1")],
        "eth0": [types.SimpleNamespace(family=af_inet, address="10.0.0.5")],
        "wlan0": [types.SimpleNamespace(family=af_inet, address="10.0.0.6")],
        "eth1": [],
    }
    stats = {
        "eth0": types.SimpleNamespace(isup=True, speed=1000),
        "wlan0": types.SimpleNamespace(isup=False, speed=100),
        "eth1": types.SimpleNamespace(isup=True, speed=100),
    }
    counters = {"eth0": types.SimpleNamespace(bytes_sent=123, bytes_recv=456)}

    with _Patch([
        (system.psutil, "net_if_addrs", lambda: addrs),
        (system.psutil, "net_if_stats", lambda: stats),
        (system.psutil, "net_io_counters", lambda pernic=True: counters),
    ]):
        result = system._get_net_interfaces()

    assert result == [
        {
            "name": "eth0",
            "ip": "10.0.0.5",
            "speed_mbps": 1000,
            "bytes_sent": 123,
            "bytes_recv": 456,
        },
        {
            "name": "eth1",
            "ip": None,
            "speed_mbps": 100,
            "bytes_sent": 0,
            "bytes_recv": 0,
        },
    ]


def test_get_system_metrics_aggregates_helpers_and_formats_uptime():
    cpu_percent_calls = []

    def fake_cpu_percent(interval=None, percpu=False):
        cpu_percent_calls.append((interval, percpu))
        return [10.0, 20.0] if percpu else 42.0

    system._reset_metrics_cache_for_tests()
    with _Patch([
        (system, "_cpu_model", None),
        (system, "_get_cpu_model", lambda: "Test CPU"),
        (system, "_get_distro", lambda: "Test Distro"),
        (system, "_get_disks", lambda: [{"mount": "/"}]),
        (system, "_get_net_interfaces", lambda: [{"name": "eth0"}]),
        (system, "_get_temperatures", lambda: {"cpu": 50.0}),
        (system, "_get_gpus", lambda: [{"name": "GPU"}]),
        (system, "_get_top_processes", lambda: [{"pid": 1}]),
        (system.psutil, "cpu_percent", fake_cpu_percent),
        (system.psutil, "cpu_freq", lambda: types.SimpleNamespace(current=3199.6)),
        (system.psutil, "cpu_count", lambda: 8),
        (system.psutil, "virtual_memory", lambda: types.SimpleNamespace(total=10, used=4, available=6, percent=40.0)),
        (system.psutil, "swap_memory", lambda: types.SimpleNamespace(total=2, used=1, percent=50.0)),
        (system.psutil, "net_io_counters", lambda: types.SimpleNamespace(bytes_sent=111, bytes_recv=222)),
        (system.psutil, "getloadavg", lambda: (1.0, 2.0, 3.0)),
        (system.psutil, "boot_time", lambda: 1000),
        (system.time, "time", lambda: 1000 + 90061),
        (system.platform, "node", lambda: "host-a"),
        (system.platform, "system", lambda: "Linux"),
        (system.platform, "release", lambda: "6.1"),
    ]):
        result = system.get_system_metrics()

    assert cpu_percent_calls == [(None, False), (None, True)]
    assert result["host"] == {
        "hostname": "host-a",
        "os": "Linux 6.1",
        "distro": "Test Distro",
        "cpu_model": "Test CPU",
    }
    assert result["cpu"] == {
        "percent": 42.0,
        "per_cpu": [10.0, 20.0],
        "count": 8,
        "freq_mhz": 3200,
    }
    assert result["memory"]["percent"] == 40.0
    assert result["network"]["interfaces"] == [{"name": "eth0"}]
    assert result["load"] == {"1min": 1.0, "5min": 2.0, "15min": 3.0}
    assert result["uptime"] == "1d 1h 1m"
    assert result["uptime_seconds"] == 90061


def test_get_system_metrics_caches_slow_helpers_between_fast_refreshes():
    now = {"monotonic": 100.0}
    calls = {"disks": 0, "interfaces": 0, "temps": 0, "gpus": 0, "processes": 0, "cpu": 0}

    def fake_cpu_percent(interval=None, percpu=False):
        calls["cpu"] += 1
        return [5.0, 6.0] if percpu else 11.0

    def fake_disks():
        calls["disks"] += 1
        return [{"mount": "/"}]

    def fake_interfaces():
        calls["interfaces"] += 1
        return [{"name": "eth0"}]

    def fake_temps():
        calls["temps"] += 1
        return {"cpu": 40.0}

    def fake_gpus():
        calls["gpus"] += 1
        return []

    def fake_processes():
        calls["processes"] += 1
        return []

    system._reset_metrics_cache_for_tests()
    with _Patch([
        (system, "_cpu_model", "Test CPU"),
        (system, "_get_distro", lambda: "Test Distro"),
        (system, "_get_disks", fake_disks),
        (system, "_get_net_interfaces", fake_interfaces),
        (system, "_get_temperatures", fake_temps),
        (system, "_get_gpus", fake_gpus),
        (system, "_get_top_processes", fake_processes),
        (system.time, "monotonic", lambda: now["monotonic"]),
        (system.time, "time", lambda: 1100),
        (system.psutil, "cpu_percent", fake_cpu_percent),
        (system.psutil, "cpu_freq", lambda: types.SimpleNamespace(current=2400)),
        (system.psutil, "cpu_count", lambda: 2),
        (system.psutil, "virtual_memory", lambda: types.SimpleNamespace(total=10, used=4, available=6, percent=40.0)),
        (system.psutil, "swap_memory", lambda: types.SimpleNamespace(total=2, used=1, percent=50.0)),
        (system.psutil, "net_io_counters", lambda: types.SimpleNamespace(bytes_sent=111, bytes_recv=222)),
        (system.psutil, "getloadavg", lambda: (1.0, 2.0, 3.0)),
        (system.psutil, "boot_time", lambda: 1000),
        (system.platform, "node", lambda: "host-a"),
        (system.platform, "system", lambda: "Linux"),
        (system.platform, "release", lambda: "6.1"),
    ]):
        first = system.get_system_metrics()
        second = system.get_system_metrics()
        now["monotonic"] += system.SYSTEM_METRICS_CACHE_TTL + 0.1
        third = system.get_system_metrics()

    assert first is second
    assert third["disks"] == [{"mount": "/"}]
    assert calls["cpu"] == 4
    assert calls["disks"] == 1
    assert calls["interfaces"] == 1
    assert calls["temps"] == 1
    assert calls["gpus"] == 1
    assert calls["processes"] == 1


if __name__ == "__main__":
    print("Running system metric tests...\n")
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {test.__name__}: {e}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)
