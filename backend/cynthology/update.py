"""Updates: Cynthology itself (GitHub releases) and Debian's packages.

The update runs in its own transient systemd unit (cynthology-update), so it keeps going while the
installer restarts the web UI. Command line: `sudo cynthology update` (see rootfs/usr/local/sbin/cynthology).
"""
import json
import os
import re
import subprocess
import time
import urllib.request

from fastapi import APIRouter, Depends, HTTPException

from .auth import current_user, require_admin
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/update", tags=["update"])
REPO = "RusTus/cynthology"
RELEASE_FILE = "/etc/cynthology-release"
LOG = os.path.join(STATE_DIR, "update.log")
APT_LOG = os.path.join(STATE_DIR, "apt-upgrade.log")
_cache: dict = {"t": 0, "data": None}


def current_version() -> str:
    try:
        return open(RELEASE_FILE).read().strip() or "dev"
    except OSError:
        return "dev"


def vkey(v: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", v.split("-")[0]))


def latest(force: bool = False) -> dict:
    """Newest release. The version comes from GitHub's /releases/latest redirect (no rate limit);
    release notes from the API when available (60 unauthenticated calls per hour per IP)."""
    if not force and _cache["data"] and time.time() - _cache["t"] < 3600:
        return _cache["data"]
    req = urllib.request.Request(f"https://github.com/{REPO}/releases/latest", headers={"User-Agent": "Cynthology-updater"}, method="HEAD")
    with urllib.request.urlopen(req, timeout=15) as r:
        tag = r.geturl().rstrip("/").rsplit("/", 1)[-1]
    if not tag.startswith("v"):
        raise RuntimeError("no release found")
    data = {"version": tag.lstrip("v"), "tag": tag, "notes": "", "url": f"https://github.com/{REPO}/releases/tag/{tag}"}
    try:
        api = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/tags/{tag}",
                                     headers={"User-Agent": "Cynthology-updater", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(api, timeout=15) as r:
            rel = json.loads(r.read())
        data.update(notes=rel.get("body") or "", published=rel.get("published_at"), name=rel.get("name"))
    except Exception:
        data["notes"] = f"Release notes: {data['url']}"
    _cache.update(t=time.time(), data=data)
    return data


def unit_active(unit: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0


@router.get("")
def status(refresh: bool = False, _=Depends(current_user)):
    cur = current_version()
    out = {"current": cur, "running": not DEV and unit_active("cynthology-update")}
    try:
        lat = latest(force=refresh)
        out["latest"] = lat
        out["update_available"] = cur == "dev" or vkey(lat["version"]) > vkey(cur)
    except Exception as e:
        out["error"] = f"could not check for updates: {e}"
    return out


@router.post("/start")
def start(_=Depends(require_admin)):
    if DEV:
        raise HTTPException(400, "not available in dev mode")
    if unit_active("cynthology-update"):
        raise HTTPException(409, "an update is already running")
    lat = latest(force=True)
    open(LOG, "w").close()
    run(["systemctl", "reset-failed", "cynthology-update"], check=False)
    run(["systemd-run", "--unit=cynthology-update", "--collect", f"--setenv=CYN_VERSION={lat['tag']}",
         "sh", "-c", f"exec /usr/lib/cynthology/get.sh >> {LOG} 2>&1"])
    return {"ok": True, "version": lat["version"]}


@router.get("/log")
def log(_=Depends(require_admin)):
    try:
        text = open(LOG).read()
    except OSError:
        text = ""
    return {"log": text[-20000:], "running": not DEV and unit_active("cynthology-update"), "current": current_version()}


# ---------- Debian packages ----------

@router.get("/debian")
def debian(_=Depends(require_admin)):
    if DEV:
        return {"count": 0, "packages": [], "running": False}
    run(["apt-get", "update", "-q"], check=False)
    out = subprocess.run(["apt", "list", "--upgradable"], capture_output=True, text=True).stdout
    pkgs = [line.split("/")[0] for line in out.splitlines() if "/" in line and "upgradable" in line]
    security = [line.split("/")[0] for line in out.splitlines() if "-security" in line]
    return {"count": len(pkgs), "packages": pkgs[:200], "security": security, "running": unit_active("cynthology-apt-upgrade")}


@router.post("/debian/start")
def debian_start(_=Depends(require_admin)):
    if unit_active("cynthology-apt-upgrade"):
        raise HTTPException(409, "already running")
    open(APT_LOG, "w").close()
    run(["systemctl", "reset-failed", "cynthology-apt-upgrade"], check=False)
    run(["systemd-run", "--unit=cynthology-apt-upgrade", "--collect", "--setenv=DEBIAN_FRONTEND=noninteractive",
         "sh", "-c", f"exec apt-get -y -q -o Dpkg::Options::=--force-confold upgrade >> {APT_LOG} 2>&1"])
    return {"ok": True}


@router.get("/debian/log")
def debian_log(_=Depends(require_admin)):
    try:
        text = open(APT_LOG).read()
    except OSError:
        text = ""
    return {"log": text[-20000:], "running": unit_active("cynthology-apt-upgrade"),
            "reboot_required": os.path.exists("/var/run/reboot-required")}
