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

from pydantic import BaseModel

from . import settings
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
    """Sort key: 0.9.0 < 1.0.0-alpha < 1.0.0-beta.1 < 1.0.0-beta.2 < 1.0.0-rc.1 < 1.0.0 (old '-alpha' tags too)."""
    main, _, pre = v.lstrip("v").partition("-")
    nums = tuple(int(n) for n in re.findall(r"\d+", main))
    if not pre:
        return nums + (3, 0)
    rank = 0 if pre.startswith("alpha") else 1 if pre.startswith("beta") else 2 if pre.startswith("rc") else 0
    n = re.findall(r"\d+", pre)
    return nums + (rank, int(n[0]) if n else 0)


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
    return {"log": text[-20000:], "running": not DEV and (unit_active("cynthology-update") or unit_active("cynthology-rollback")),
            "current": current_version()}


# ---------- Debian packages ----------

# ---------- rollback points (saved by get.sh before every update; see rootfs/usr/lib/cynthology/rollback) ----------

ROLLBACK = "/usr/lib/cynthology/rollback"
RESULT = os.path.join(STATE_DIR, "update-result")


@router.get("/rollback")
def rollback_points(_=Depends(require_admin)):
    if DEV or not os.path.exists(ROLLBACK):
        return {"points": [], "running": False}
    r = subprocess.run([ROLLBACK, "list"], capture_output=True, text=True, timeout=30)
    try:
        points = json.loads(r.stdout or "[]")
    except ValueError:
        points = []
    return {"points": points, "running": unit_active("cynthology-rollback")}


@router.post("/rollback/{point}")
def rollback(point: str, _=Depends(require_admin)):
    if DEV:
        raise HTTPException(400, "not available in dev mode")
    if not re.fullmatch(r"\d{8}-\d{6}-[0-9A-Za-z.+~-]{1,40}", point):
        raise HTTPException(400, "bad rollback point")
    if not any(p["id"] == point for p in rollback_points(None)["points"]):
        raise HTTPException(404, "no such rollback point")
    if unit_active("cynthology-update") or unit_active("cynthology-rollback"):
        raise HTTPException(409, "an update or rollback is already running")
    open(LOG, "w").close()
    run(["systemctl", "reset-failed", "cynthology-rollback"], check=False)
    run(["systemd-run", "--unit=cynthology-rollback", "--collect",
         "sh", "-c", f"echo manual-rollback > {RESULT}; {ROLLBACK} restore {point} >> {LOG} 2>&1 || {{ rm -f {RESULT}; exit 1; }}"])
    return {"ok": True}


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


# ---------- automatic updates ----------
# Checked every hour (cynthology-autoupdate.timer); acts once per day/week, in the hours after the
# chosen time. Never while the NAS is busy (backup, cloud sync, scan, RAID rebuild/scrub, open SMB
# files, another update): it simply tries again the next hour (for up to 6 hours).

AUTO_STATE = os.path.join(STATE_DIR, "auto-update.json")
BUSY_UNITS = ("cyn-backup-*", "cyn-backup-check-*", "cyn-cloudsync-*", "cyn-antivirus-scan", "cynthology-scrub",
              "cyn-job-*", "cynthology-update", "cynthology-apt-upgrade", "cyn-repl-*")


def auto_config() -> dict:
    c = settings.get("auto_update") or {}
    return {"enabled": bool(c.get("enabled")), "frequency": c.get("frequency", "daily"), "time": c.get("time", "03:30"),
            "day": int(c.get("day", 6)), "debian": bool(c.get("debian"))}


class AutoUpdate(BaseModel):
    enabled: bool = False
    frequency: str = "daily"        # daily | weekly
    time: str = "03:30"
    day: int = 6                    # weekly: 0 = Monday
    debian: bool = False            # also Debian's package updates


@router.get("/auto")
def get_auto(_=Depends(require_admin)):
    st = _auto_state()
    return {**auto_config(), "last_check": st.get("last_check"), "last_result": st.get("last_result", "")}


@router.put("/auto")
def put_auto(body: AutoUpdate, _=Depends(require_admin)):
    if body.frequency not in ("daily", "weekly") or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", body.time) or not 0 <= body.day <= 6:
        raise HTTPException(400, "invalid schedule")
    settings.update(auto_update=body.model_dump())
    return get_auto(None)


def _auto_state() -> dict:
    try:
        return json.load(open(AUTO_STATE))
    except (OSError, ValueError):
        return {}


def _save_auto_state(**v) -> None:
    st = {**_auto_state(), **v}
    with open(AUTO_STATE, "w") as f:
        json.dump(st, f)


def busy() -> str:
    """Why now is a bad moment to update ('' = fine)."""
    if DEV:
        return ""
    r = subprocess.run(["systemctl", "list-units", "--state=active,activating", "--no-legend", "--plain", *BUSY_UNITS],
                       capture_output=True, text=True)
    if r.stdout.strip():
        return "busy: " + r.stdout.split()[0]
    try:
        md = open("/proc/mdstat").read()
        if re.search(r"(recovery|resync|reshape|check)\s*=", md):
            return "a storage pool is being rebuilt or checked"
    except OSError:
        pass
    r = subprocess.run(["smbstatus", "-L", "-n"], capture_output=True, text=True)
    if r.returncode == 0 and re.search(r"^\d+\s+\d+\s+\S+\s+0x", r.stdout, re.M):
        return "files are open over SMB"
    return ""


def auto_due(now=None) -> bool:
    import datetime
    c = auto_config()
    if not c["enabled"]:
        return False
    now = now or datetime.datetime.now()
    hh, mm = map(int, c["time"].split(":"))
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < slot or now - slot > datetime.timedelta(hours=6) or (c["frequency"] == "weekly" and now.weekday() != c["day"]):
        return False
    return (_auto_state().get("done_for") or 0) < slot.timestamp()


def auto_run() -> None:
    """From the hourly timer."""
    import datetime
    if not auto_due():
        return
    why = busy()
    now = int(time.time())
    if why:
        _save_auto_state(last_check=now, last_result=f"waiting ({why}) - trying again in an hour")
        return
    c = auto_config()
    result = []
    try:
        cur, lat = current_version(), latest(force=True)
        if cur != "dev" and vkey(lat["version"]) > vkey(cur):
            _save_auto_state(pending=lat["version"], pending_since=now)
            start(None)
            result.append(f"updating Cynthology {cur} -> {lat['version']}")
        else:
            result.append(f"Cynthology {cur} is up to date")
    except Exception as e:
        result.append(f"couldn't check for Cynthology updates: {e}")
    if c["debian"]:
        try:
            run(["apt-get", "update", "-q"], check=False)
            if not unit_active("cynthology-apt-upgrade"):
                debian_start(None)
                result.append("installing Debian updates")
        except Exception as e:
            result.append(f"Debian updates failed to start: {e}")
    _save_auto_state(last_check=now, last_result="; ".join(result), done_for=now)
    del datetime


def after_restart() -> None:
    """At startup: tell the admin how an update went (rolled back by itself?) and how an automatic one went."""
    from .notify import notify
    try:
        result = open(RESULT).read().strip()
        os.remove(RESULT)
    except OSError:
        result = ""
    if result == "rolled-back":
        notify("warning", "An update was undone", f"The new version didn't start properly, so Cynthology went back to "
               f"{current_version()} by itself. Your settings are as they were before the update. See Control Panel > "
               "Update & Restore (log).", event="update")
        _save_auto_state(pending=None, last_result=f"update failed - went back to {current_version()}")
        return
    if result == "manual-rollback":
        notify("info", f"Cynthology went back to {current_version()}", "The rollback you started finished.", event="update")
    st = _auto_state()
    want = st.get("pending")
    if not want:
        return
    if current_version() == want:
        notify("info", f"Cynthology was updated to {want}", "The automatic update finished.", event="update")
        _save_auto_state(pending=None, last_result=f"updated to {want}")
    elif time.time() - (st.get("pending_since") or 0) > 3600:
        notify("warning", "An automatic update didn't finish", f"Updating to {want} didn't complete - see Control Panel > Update & Restore (log).", event="update")
        _save_auto_state(pending=None, last_result=f"update to {want} failed")


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["auto"]:
        auto_run()

