"""System overview + live resource monitor."""
import concurrent.futures
import platform
import re
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
        "os": "Cynthology",
        "kernel": platform.release(),
        "cpu": cpu_model,
        "cores": psutil.cpu_count(),
        "memory_total": psutil.virtual_memory().total,
        "uptime": int(time.time() - BOOT),
        "ips": lan_ips(),
    }


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _cmd(args: list[str]) -> str:
    import subprocess
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


@router.get("/details")
def details(_=Depends(require_admin)):
    """Everything the Info Center shows (admins only: it includes addresses and versions)."""
    import os
    import re
    from . import settings, storage, update
    from .util import DEV
    s = settings.get_all()
    # general
    os_rel = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', _read("/etc/os-release"), re.M))
    tz = _cmd(["timedatectl", "show", "-p", "Timezone", "--value"]) if not DEV else "UTC"
    ntp = _cmd(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]) if not DEV else "yes"
    # (the slow parts - the update check may ask GitHub - run side by side)
    pool = concurrent.futures.ThreadPoolExecutor(4)
    upd_f = pool.submit(lambda: update.status(refresh=False, _=None))
    services = pool.submit(_services_running, DEV)
    versions_f = pool.submit(_versions)
    stor_f = pool.submit(_storage_summary)
    try:
        upd = upd_f.result(timeout=8)
    except Exception:
        upd = {}
    fqdn = socket.getfqdn()
    general = {"hostname": socket.gethostname(), "fqdn": fqdn if "." in fqdn and not fqdn.startswith("localhost") else "", "version": update.current_version(),
               "latest": (upd.get("latest") or {}).get("version"), "update_available": bool(upd.get("update_available")),
               "debian": os_rel.get("PRETTY_NAME", ""), "kernel": platform.release(), "timezone": tz,
               "time": int(time.time()), "ntp": ntp == "yes", "uptime": int(time.time() - BOOT), "boot": int(BOOT)}
    # network
    stats = psutil.net_if_stats()
    nics = []
    for name, addrs in sorted(psutil.net_if_addrs().items()):
        if name == "lo" or name.startswith(("docker", "br-", "veth", "virbr", "tap")):
            continue
        st = stats.get(name)
        if not (st and st.isup) and not any(a.family == socket.AF_INET for a in addrs):
            continue                      # (unused virtual devices)
        nics.append({"name": name, "up": bool(st and st.isup), "speed": st.speed if st else 0, "mtu": st.mtu if st else 0,
                     "mac": next((a.address for a in addrs if a.family == psutil.AF_LINK), ""),
                     "ipv4": [f"{a.address}" for a in addrs if a.family == socket.AF_INET],
                     "ipv6": [a.address.split("%")[0] for a in addrs if a.family == socket.AF_INET6 and not a.address.startswith("fe80")]})
    gw = _cmd(["ip", "-4", "route", "show", "default"]).split()
    ns = lambda f: [ln.split()[1] for ln in _read(f).splitlines() if ln.startswith("nameserver") and len(ln.split()) > 1]
    dns = ns("/etc/resolv.conf")
    if dns == ["127.0.0.53"]:                  # systemd-resolved's local stub: show the servers it really asks
        dns = ns("/run/systemd/resolve/resolv.conf") or dns
    try:
        from . import ddns
        names = [e["hostname"] for e in ddns.load() if e.get("enabled", True)]
    except Exception:
        names = []
    network = {"nics": nics, "gateway": gw[2] if len(gw) > 2 and gw[0] == "default" else "", "dns": dns, "ddns": names,
               "http_port": s["http_port"], "https_port": s["https_port"]}
    # hardware
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    dmi = lambda k: _read(f"/sys/class/dmi/id/{k}")
    temps = []
    try:
        for entries in (psutil.sensors_temperatures() or {}).values():
            temps += [e.current for e in entries if e.current]
    except (AttributeError, OSError):
        pass
    cpu = info(_=None)["cpu"]
    hardware = {"vendor": dmi("sys_vendor"), "model": dmi("product_name"), "board": " ".join(x for x in (dmi("board_vendor"), dmi("board_name")) if x),
                "bios": " ".join(x for x in (dmi("bios_version"), dmi("bios_date")) if x), "cpu": cpu, "cores": psutil.cpu_count(logical=False),
                "threads": psutil.cpu_count(), "memory_total": vm.total, "memory_used": vm.total - vm.available,
                "swap_total": sw.total, "swap_used": sw.used, "temp": max(temps) if temps else None,
                "load": [round(x, 2) for x in os.getloadavg()]}
    stor = stor_f.result()
    svc, versions = services.result(), versions_f.result()
    pool.shutdown(wait=False)
    return {"general": general, "network": network, "hardware": hardware, "storage": stor, "services": svc,
            "versions": versions}


SERVICE_UNITS = (("SMB", "smbd"), ("NFS", "nfs-server"), ("SSH", "ssh"), ("WebDAV", "cynthology-webdav"), ("rsync", "rsync"),
                 ("Web Station", "nginx"), ("VPN", "wg-quick@wg0"), ("Docker", "docker"), ("iSCSI", "rtslib-fb-targetctl"),
                 ("Backup Vault", "restic-rest-server"), ("UPS", "nut-monitor"), ("Firewall", "cynthology-firewall"))


def _services_running(dev: bool) -> dict:
    if dev:
        return {k: True for k, _ in SERVICE_UNITS}
    # one call: systemctl prints one state per unit, in order
    states = _cmd(["systemctl", "is-active", *(u for _, u in SERVICE_UNITS)]).split()
    return {k: i < len(states) and states[i] == "active" for i, (k, _) in enumerate(SERVICE_UNITS)}


def _versions() -> dict:
    pk = {}
    for ln in _cmd(["dpkg-query", "-W", "-f", "${Package} ${Version}\\n", "docker-ce", "docker.io", "samba", "nginx", "php8.4-fpm"]).splitlines():
        name, _, ver = ln.partition(" ")
        if ver:
            pk[name] = re.sub(r"^\d+:", "", ver).split("+")[0].split("~")[0].rsplit("-", 1)[0]
    out = {"Docker": pk.get("docker-ce") or pk.get("docker.io", ""), "Samba": pk.get("samba", ""), "nginx": pk.get("nginx", ""),
           "PHP": pk.get("php8.4-fpm", ""), "Python": platform.python_version()}
    return {k: v for k, v in out.items() if v}


def _storage_summary() -> dict:
    from . import storage
    try:
        pools = [{"label": p["label"], "raid": p["raid"], "status": p["status"], "total": p["total"], "used": p["used"], "mount": p["mount"]}
                 for p in storage.pools(_=None)]
        disks = storage.list_disks()
    except Exception:
        pools, disks = [], []
    return {"pools": pools, "drives": len([d for d in disks if not d.get("system")]),
            "system_drive": next((d["model"] or d["name"] for d in disks if d.get("system")), "")}


@router.get("/public-ip")
def public_ip(_=Depends(require_admin)):
    """Asked for explicitly (it contacts an outside service to see the address the internet sees)."""
    from fastapi import HTTPException
    from .ddns import public_ip as lookup
    try:
        return {"ip": lookup()}
    except RuntimeError as e:
        raise HTTPException(502, str(e))


def lan_ips() -> list[str]:
    """IPv4 addresses on real interfaces (Docker/virtual bridges skipped)."""
    import socket
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        if name == "lo" or name.startswith(("docker", "br-", "veth", "virbr", "tun", "tap", "wg")):
            continue
        out += [a.address for a in addrs if a.family == socket.AF_INET]
    return out


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
