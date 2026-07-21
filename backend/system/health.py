"""Real system telemetry. Never invented — every number here is measured.

The LLM used to improvise plausible-sounding stats ("CPU efficiency at 94%")
because it had no data. It now calls get_system_health() and speaks these
actual values instead.
"""
import logging
import shutil
import subprocess

import psutil

log = logging.getLogger("jarvis.health")


def _gpu() -> dict | None:
    """NVIDIA GPU stats via nvidia-smi. None if absent/failing — never faked."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        util, temp, used, total = [p.strip() for p in out.stdout.strip().splitlines()[0].split(",")]
        return {
            "gpu_utilization_percent": int(util),
            "gpu_temperature_c": int(temp),
            "gpu_memory_used_mb": int(used),
            "gpu_memory_total_mb": int(total),
        }
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        log.info("nvidia-smi unavailable (%s) — omitting GPU stats", e)
        return None


def get_system_health() -> dict:
    """CPU/RAM/disk/battery/uptime + GPU when present. All measured values."""
    cpu = psutil.cpu_percent(interval=0.4)      # short sample; 0 would be a lie
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("C:\\")
    data: dict = {
        "cpu_percent": round(cpu, 1),
        "ram_percent": round(mem.percent, 1),
        "ram_used_gb": round(mem.used / 1e9, 1),
        "ram_total_gb": round(mem.total / 1e9, 1),
        "disk_c_percent_used": round(disk.percent, 1),
        "disk_c_free_gb": round(disk.free / 1e9, 1),
    }
    try:
        freq = psutil.cpu_freq()
        if freq:
            data["cpu_ghz"] = round(freq.current / 1000, 2)
    except Exception:
        pass

    batt = psutil.sensors_battery()
    if batt is not None:
        data["battery_percent"] = round(batt.percent)
        data["on_ac_power"] = bool(batt.power_plugged)

    gpu = _gpu()
    if gpu:
        data.update(gpu)          # omitted entirely when no NVIDIA GPU

    log.info("system health: %s", data)
    return data


# Friendly names for common multi-process apps, so the spoken answer says
# "Chrome" / "VS Code" rather than "chrome.exe".
_APP_NAMES = {
    "chrome.exe": "Chrome", "msedge.exe": "Edge", "brave.exe": "Brave",
    "firefox.exe": "Firefox", "code.exe": "VS Code", "code - insiders.exe": "VS Code",
    "explorer.exe": "File Explorer", "spotify.exe": "Spotify",
    "discord.exe": "Discord", "slack.exe": "Slack", "steam.exe": "Steam",
    "python.exe": "Python", "pythonw.exe": "Python", "node.exe": "Node",
    "electron.exe": "Electron", "javaw.exe": "Java", "java.exe": "Java",
    "dwm.exe": "Windows (desktop)", "memcompression": "Windows (memory compression)",
    "svchost.exe": "Windows services", "searchindexer.exe": "Windows Search",
    "whatsapp.exe": "WhatsApp", "teams.exe": "Teams", "obs64.exe": "OBS",
}


def _app_label(exe: str) -> str:
    return _APP_NAMES.get(exe.lower(), exe[:-4] if exe.lower().endswith(".exe") else exe)


def get_top_processes(by: str = "memory", limit: int = 5) -> dict:
    """Top applications by RAM or CPU, AGGREGATED across all processes that share
    an executable. Apps like Chrome/VS Code spawn many processes; summing them is
    the only way the numbers match the overall usage the user sees.
    """
    by = "cpu" if str(by).lower().startswith("cpu") else "memory"
    total_ram = psutil.virtual_memory().total

    # Prime CPU counters (first cpu_percent() call always returns 0.0).
    if by == "cpu":
        for p in psutil.process_iter():
            try:
                p.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        import time as _t
        _t.sleep(0.4)

    # Not real consumers: idle process is FREE cpu, and pid-0/System are kernel.
    _IGNORE = {"system idle process", "idle", "system", ""}

    agg: dict[str, dict] = {}
    ncpu = psutil.cpu_count() or 1
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            name = (p.info["name"] or "unknown")
            key = name.lower()
            if key in _IGNORE:
                continue
            slot = agg.setdefault(key, {"exe": name, "ram_bytes": 0, "cpu": 0.0, "count": 0})
            slot["count"] += 1
            mi = p.info.get("memory_info")
            if mi:
                slot["ram_bytes"] += mi.rss
            if by == "cpu":
                slot["cpu"] += p.cpu_percent(None) / ncpu   # normalize to 0-100 total
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    items = list(agg.values())
    if by == "cpu":
        items.sort(key=lambda s: s["cpu"], reverse=True)
    else:
        items.sort(key=lambda s: s["ram_bytes"], reverse=True)

    top = []
    for s in items[:max(1, int(limit))]:
        entry = {"app": _app_label(s["exe"]), "processes": s["count"]}
        if by == "cpu":
            entry["cpu_percent"] = round(s["cpu"], 1)
        entry["ram_mb"] = round(s["ram_bytes"] / 1e6)
        entry["ram_percent"] = round(100 * s["ram_bytes"] / total_ram, 1)
        top.append(entry)

    log.info("top processes by %s: %s", by, top)
    return {"by": by, "top": top, "total_ram_percent": round(psutil.virtual_memory().percent, 1)}


def summary_line() -> str:
    """Compact factual string for the LLM to phrase in character."""
    h = get_system_health()
    bits = [f"CPU {h['cpu_percent']}%", f"RAM {h['ram_percent']}%"]
    if "gpu_utilization_percent" in h:
        bits.append(f"GPU {h['gpu_utilization_percent']}% at {h['gpu_temperature_c']}C")
    if "battery_percent" in h:
        bits.append(f"battery {h['battery_percent']}%"
                    + (" on AC" if h.get("on_ac_power") else " on battery"))
    bits.append(f"disk C: {h['disk_c_free_gb']}GB free")
    return ", ".join(bits)
