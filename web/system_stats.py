"""Host resource snapshot for the System tab (CPU, memory, disk, load).

Feeds ``GET /api/system`` so the operator can watch the box from a phone.

``psutil`` is imported lazily, inside :func:`snapshot`: a clone that has not
reinstalled dependencies still serves every other tab, and only the System
page degrades (the endpoint answers 503 with a readable reason).
"""

from __future__ import annotations

import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# psutil's non-blocking CPU counters are deltas since the previous call, so the
# first call of a fresh process is meaningless. Flag the first pass and take a
# real (blocking) sample so the first render is not a misleading 0%.
_CPU_PRIMED = False
# pid -> (accumulated cpu seconds, monotonic timestamp) for per-process deltas.
_PREV_PROC: dict[int, tuple[float, float]] = {}


def _psutil():
    import psutil  # local import on purpose; see the module docstring

    return psutil


def _cpu(psutil) -> dict[str, Any]:
    global _CPU_PRIMED
    if _CPU_PRIMED:
        percent = psutil.cpu_percent(interval=None)
    else:
        _CPU_PRIMED = True
        percent = psutil.cpu_percent(interval=0.15)
    per_cpu = psutil.cpu_percent(interval=None, percpu=True)
    load: Optional[list[float]]
    try:
        load = [round(v, 2) for v in os.getloadavg()]
    except (OSError, AttributeError):
        load = None
    return {
        "percent": round(float(percent), 1),
        "per_cpu": [round(float(v), 1) for v in per_cpu],
        "count": psutil.cpu_count(logical=True) or len(per_cpu),
        "load": load,
    }


def _cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine() or "unknown"


def _disks(psutil) -> list[dict[str, Any]]:
    """Root filesystem plus the one holding this repo (deduped by device)."""
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for path in ("/", str(PROJECT_ROOT)):
        try:
            device = os.stat(path).st_dev
            if device in seen:
                continue
            seen.add(device)
            usage = psutil.disk_usage(path)
        except OSError:
            continue
        out.append({
            "mount": path,
            "total": int(usage.total),
            "used": int(usage.used),
            "free": int(usage.free),
            "percent": round(float(usage.percent), 1),
        })
    return out


def _limit(value: Any) -> Optional[float]:
    """A sensor limit, or None when unset — the kernel uses huge sentinels
    (e.g. 65261.8°C) for "no limit", which must not reach the page."""
    if value is None:
        return None
    celsius = float(value)
    return round(celsius, 1) if 0 < celsius < 200 else None


def _temperatures(psutil) -> list[dict[str, Any]]:
    sensors = getattr(psutil, "sensors_temperatures", None)
    if sensors is None:
        return []
    try:
        data = sensors()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for chip, entries in (data or {}).items():
        for entry in entries:
            if _limit(entry.current) is None:
                continue
            out.append({
                "chip": chip,
                "label": entry.label or chip,
                "current": round(float(entry.current), 1),
                "high": _limit(entry.high),
                "critical": _limit(entry.critical),
            })
    return out


def _processes(psutil, top_n: int) -> tuple[list[dict], list[dict]]:
    """Top processes by CPU and by resident memory.

    CPU is a delta against the previous sample. Only pid/name/owner and usage
    are returned — never the command line, which can carry tokens.
    """
    if top_n <= 0:
        return [], []
    now = time.monotonic()
    rows: list[dict[str, Any]] = []
    live: set[int] = set()
    for proc in psutil.process_iter(
        ["pid", "name", "username", "memory_info", "memory_percent", "cpu_times"]
    ):
        try:
            info = proc.info
            pid = int(info["pid"])
            cpu_percent = 0.0
            times = info.get("cpu_times")
            if times is not None:
                total = float(times.user + times.system)
                prev = _PREV_PROC.get(pid)
                if prev is not None:
                    elapsed = now - prev[1]
                    if elapsed > 0:
                        cpu_percent = max(0.0, (total - prev[0]) / elapsed * 100.0)
                _PREV_PROC[pid] = (total, now)
            live.add(pid)
            mem_info = info.get("memory_info")
            rows.append({
                "pid": pid,
                "name": str(info.get("name") or "?"),
                "username": str(info.get("username") or ""),
                "cpu_percent": round(cpu_percent, 1),
                "memory_percent": round(float(info.get("memory_percent") or 0.0), 1),
                "rss": int(getattr(mem_info, "rss", 0) or 0),
            })
        except Exception:
            # Vanished mid-iteration or not ours to inspect.
            continue
    for pid in [p for p in _PREV_PROC if p not in live]:
        _PREV_PROC.pop(pid, None)
    by_cpu = sorted(rows, key=lambda r: r["cpu_percent"], reverse=True)[:top_n]
    by_mem = sorted(rows, key=lambda r: r["rss"], reverse=True)[:top_n]
    return by_cpu, by_mem


def snapshot(top_n: int = 6) -> dict[str, Any]:
    """One point-in-time reading of the host. Raises ImportError without psutil."""
    psutil = _psutil()
    top_n = max(0, min(int(top_n), 20))

    virtual = psutil.virtual_memory()
    swap = psutil.swap_memory()
    net = psutil.net_io_counters()
    boot_time = float(psutil.boot_time())
    now = time.time()
    by_cpu, by_mem = _processes(psutil, top_n)

    return {
        "host": {
            "hostname": platform.node() or "",
            "platform": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "cpu_model": _cpu_model(),
            "cpu_count": psutil.cpu_count(logical=True) or 0,
            "boot_time": boot_time,
            "uptime_seconds": max(0.0, now - boot_time),
        },
        "cpu": _cpu(psutil),
        "memory": {
            "total": int(virtual.total),
            "used": int(virtual.used),
            "available": int(virtual.available),
            "percent": round(float(virtual.percent), 1),
        },
        "swap": {
            "total": int(swap.total),
            "used": int(swap.used),
            "free": int(swap.free),
            "percent": round(float(swap.percent), 1),
        },
        "disks": _disks(psutil),
        "network": {
            "bytes_sent": int(net.bytes_sent),
            "bytes_recv": int(net.bytes_recv),
        },
        "temperatures": _temperatures(psutil),
        "processes": {"by_cpu": by_cpu, "by_mem": by_mem},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
