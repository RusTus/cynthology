"""System overview + live resource monitor."""
import platform
import socket
import time

import psutil
from fastapi import APIRouter, Depends

from .auth import current_user, require_admin
from .util import run

router = APIRouter(prefix="/api/system", tags=["system"])
BOOT = psutil.boot_time()


@router.get("/info")
def info(_=Depends(current_user)):
    cpu_model = platform.processor()
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu_model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return {
        "hostname": socket.gethostname(),
        "os": "CairnOS",
        "kernel": platform.release(),
        "cpu": cpu_model,
        "cores": psutil.cpu_count(),
        "memory_total": psutil.virtual_memory().total,
        "uptime": int(time.time() - BOOT),
    }


@router.get("/stats")
def stats(_=Depends(current_user)):
    net = psutil.net_io_counters()
    disk = psutil.disk_io_counters()
    temps = {}
    try:
        for name, entries in (psutil.sensors_temperatures() or {}).items():
            if entries:
                temps[name] = entries[0].current
    except (AttributeError, OSError):
        pass
    return {
        "t": time.time(),
        "cpu": psutil.cpu_percent(interval=None),
        "per_cpu": psutil.cpu_percent(interval=None, percpu=True),
        "mem": psutil.virtual_memory()._asdict(),
        "swap": psutil.swap_memory()._asdict(),
        "net": {"sent": net.bytes_sent, "recv": net.bytes_recv},
        "disk": {"read": disk.read_bytes if disk else 0, "write": disk.write_bytes if disk else 0},
        "load": psutil.getloadavg(),
        "temps": temps,
    }


@router.get("/processes")
def processes(_=Depends(require_admin)):
    procs = []
    for p in psutil.process_iter(["pid", "name", "username", "cpu_percent", "memory_info"]):
        i = p.info
        procs.append({"pid": i["pid"], "name": i["name"], "user": i["username"],
                      "cpu": i["cpu_percent"], "rss": i["memory_info"].rss if i["memory_info"] else 0})
    return sorted(procs, key=lambda x: x["rss"], reverse=True)[:50]


@router.post("/power/{action}")
def power(action: str, _=Depends(require_admin)):
    cmd = {"reboot": ["systemctl", "reboot"], "shutdown": ["systemctl", "poweroff"]}.get(action)
    if not cmd:
        return {"error": "unknown action"}
    run(cmd)
    return {"ok": True}
