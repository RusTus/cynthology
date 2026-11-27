"""Antivirus (ClamAV): scan shared folders on a schedule or by hand; quarantine what's found.

ClamAV isn't part of the base install (its signatures take a few hundred MB and a scan needs
about 1.5 GB of RAM): the app installs it on request. clamav-freshclam keeps the signatures up
to date by itself. Scans run `clamscan` in a transient systemd unit (low CPU/IO priority) and skip
snapshots (read-only copies). Found files are moved to <shared folder>/.cyn-quarantine (root only, same
subvolume - a rename, nothing is copied), with a record so they can be restored or deleted.
"""
import datetime
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import shares as shares_mod
from .auth import require_admin
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/antivirus", tags=["antivirus"])
DIR = os.path.join(STATE_DIR, "antivirus")
CFG = os.path.join(DIR, "config.json")
STATUS = os.path.join(DIR, "status.json")
FOUND = os.path.join(DIR, "quarantine.json")
LOG = os.path.join(DIR, "scan.log")
DB_DIR = "/var/lib/clamav"
PACKAGES = ["clamav", "clamav-freshclam"]
UNIT = "cyn-antivirus-scan"
INSTALL_UNIT = "cyn-antivirus-install"
_lock = threading.Lock()
os.makedirs(DIR, mode=0o700, exist_ok=True)


def _read(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path: str, data) -> None:
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, indent=1)
    os.replace(path + ".tmp", path)


def config() -> dict:
    c = _read(CFG, {})
    return {"shares": c.get("shares", []), "all_shares": c.get("all_shares", True), "schedule": c.get("schedule", "off"),
            "time": c.get("time", "03:00"), "day": c.get("day", 6), "action": c.get("action", "quarantine"),
            "max_mb": int(c.get("max_mb", 200))}


def installed() -> bool:
    return DEV or shutil.which("clamscan") is not None


def _unit_active(unit: str) -> bool:
    if DEV:
        return False
    r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
    return r.stdout.strip() in ("active", "activating")


def signatures() -> dict:
    """Version and date of the signatures (from clamscan --version: 'ClamAV 1.4.6/27841/Tue Nov 25 09:37:01 2026')."""
    if DEV:
        return {"ready": True, "version": "27841", "date": "2026-11-25"}
    have = os.path.isdir(DB_DIR) and any(f.startswith(("main.", "daily.")) for f in os.listdir(DB_DIR))
    if not have:
        return {"ready": False}
    out = subprocess.run(["clamscan", "--version"], capture_output=True, text=True).stdout.strip()
    parts = out.split("/")
    return {"ready": True, "engine": parts[0].replace("ClamAV ", ""), "version": parts[1] if len(parts) > 1 else "",
            "date": parts[2] if len(parts) > 2 else ""}


# ---------- install ----------

@router.post("/install")
def install(_=Depends(require_admin)):
    if installed():
        return {"ok": True}
    if _unit_active(INSTALL_UNIT):
        raise HTTPException(409, "already installing")
    script = ("set -e; export DEBIAN_FRONTEND=noninteractive; apt-get update -q || true; "
              f"apt-get install -y -q --no-install-recommends {' '.join(PACKAGES)}; "
              "systemctl enable --now clamav-freshclam; "
              # first download of the signatures (freshclam's service does it too, but we wait for it here)
              f"for i in $(seq 1 120); do ls {DB_DIR}/daily.* >/dev/null 2>&1 && exit 0; sleep 5; done")
    run(["systemd-run", f"--unit={INSTALL_UNIT}", "--collect", f"--property=StandardOutput=file:{LOG}.install",
         "--property=StandardError=file:" + LOG + ".install", "sh", "-c", script])
    return {"ok": True}


# ---------- status / config ----------

@router.get("")
def status(_=Depends(require_admin)):
    import psutil
    st = _read(STATUS, {})
    st["running"] = _unit_active(UNIT)
    found = _read(FOUND, [])
    return {"installed": installed(), "installing": _unit_active(INSTALL_UNIT), "signatures": signatures() if installed() else {},
            "config": config(), "status": st, "quarantine": len([f for f in found if f["state"] == "quarantined"]),
            "memory_ok": psutil.virtual_memory().total >= 1.8 * 1024 ** 3, "next": _next(config())}


class Config(BaseModel):
    all_shares: bool = True
    shares: list[str] = []
    schedule: str = "off"          # off | daily | weekly
    time: str = "03:00"
    day: int = 6                   # 0 = Monday (weekly)
    action: str = "quarantine"     # quarantine | report
    max_mb: int = 200


@router.put("/config")
def put_config(body: Config, _=Depends(require_admin)):
    names = {s["name"] for s in shares_mod.load()}
    if any(s not in names for s in body.shares):
        raise HTTPException(400, "unknown shared folder")
    if body.schedule not in ("off", "daily", "weekly") or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", body.time) or not 0 <= body.day <= 6:
        raise HTTPException(400, "invalid schedule")
    if body.action not in ("quarantine", "report") or not 1 <= body.max_mb <= 4000:
        raise HTTPException(400, "invalid setting")
    _write(CFG, body.model_dump())
    return config()


def _next(c: dict, now: datetime.datetime | None = None) -> int | None:
    if c["schedule"] == "off":
        return None
    now = now or datetime.datetime.now()
    hh, mm = map(int, c["time"].split(":"))
    for add in range(8):
        d = (now + datetime.timedelta(days=add)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if d > now and (c["schedule"] == "daily" or d.weekday() == c["day"]):
            return int(d.timestamp())
    return None


def due(now: datetime.datetime | None = None) -> bool:
    c = config()
    if c["schedule"] == "off" or not installed():
        return False
    now = now or datetime.datetime.now()
    hh, mm = map(int, c["time"].split(":"))
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < slot or now - slot > datetime.timedelta(hours=6) or (c["schedule"] == "weekly" and now.weekday() != c["day"]):
        return False
    return (_read(STATUS, {}).get("started") or 0) < slot.timestamp()


# ---------- scanning ----------

def targets(c: dict, only: list[str] | None = None) -> list[dict]:
    out = []
    for s in shares_mod.load():
        if only is not None and s["name"] not in only:
            continue
        if only is None and not c["all_shares"] and s["name"] not in c["shares"]:
            continue
        if s.get("encrypted") and not shares_mod.unlocked(s):
            continue
        if os.path.isdir(s["path"]):
            out.append(s)
    return out


class ScanNow(BaseModel):
    shares: list[str] | None = None


@router.post("/scan")
def scan_now(body: ScanNow, _=Depends(require_admin)):
    if not installed():
        raise HTTPException(400, "install the antivirus first")
    if not signatures().get("ready"):
        raise HTTPException(400, "the virus signatures are still downloading - try again in a few minutes")
    if _unit_active(UNIT):
        raise HTTPException(409, "a scan is already running")
    start(body.shares)
    return {"ok": True}


def start(only: list[str] | None = None) -> None:
    if DEV:
        return
    run(["systemd-run", f"--unit={UNIT}", "--collect", "--property=Nice=15", "--property=IOSchedulingClass=idle",
         f"--setenv=CYN_STATE={STATE_DIR}", "--working-directory=/opt/cynthology/backend",
         "/usr/bin/python3", "-m", "cynthology.antivirus", "scan", *(only or [])])


@router.post("/stop")
def stop(_=Depends(require_admin)):
    if _unit_active(UNIT):
        run(["systemctl", "stop", UNIT], check=False)
        _write(STATUS, {**_read(STATUS, {}), "state": "stopped", "finished": int(time.time())})
    return {"ok": True}


def command(c: dict, paths: list[str]) -> list[str]:
    cmd = ["clamscan", "--recursive", "--infected", "--no-summary", "--stdout", f"--max-filesize={c['max_mb']}M",
           f"--max-scansize={c['max_mb'] * 2}M", "--exclude-dir=/\\.snapshots$", "--exclude-dir=/\\.cyn-quarantine$",
           "--exclude-dir=/@eaDir$", "--cross-fs=no"]
    return cmd + ["--", *paths]


FOUND_RE = re.compile(r"^(?P<path>/.*): (?P<sig>[^:]+) FOUND$")


def parse(output: str) -> list[tuple[str, str]]:
    return [(m["path"], m["sig"]) for m in map(FOUND_RE.match, output.splitlines()) if m]


def _share_root(path: str) -> str:
    for sh in shares_mod.load():
        root = os.path.realpath(sh["path"])
        if path.startswith(root.rstrip("/") + "/"):
            return root
    raise OSError(f"{path} isn't inside a shared folder")


def _open_parent(path: str) -> tuple[int, str]:
    """A directory fd for path's folder, reached one component at a time WITHOUT following symlinks
    (files belong to users: a folder swapped for a link to /etc must never be followed)."""
    root = _share_root(path)
    parts = path[len(root):].strip("/").split("/")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for comp in parts[:-1]:
            if comp in ("", ".", ".."):
                raise OSError("bad path")
            nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd, parts[-1]


def _qdir(path: str) -> str:
    """<shared folder>/.cyn-quarantine: each shared folder is its own Btrfs subvolume, and files can
    only be renamed (not copied) within one - so the quarantine lives inside the folder (root only)."""
    qdir = os.path.join(_share_root(path), ".cyn-quarantine")
    try:
        os.mkdir(qdir, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(qdir)
    import stat as stat_mod
    if not stat_mod.S_ISDIR(st.st_mode) or st.st_uid != 0:
        raise OSError(f"{qdir} isn't a folder owned by the system")
    os.chown(qdir, 0, 0)
    os.chmod(qdir, 0o700)
    if not DEV:     # (the shared folder's default ACLs would otherwise be inherited)
        subprocess.run(["setfacl", "-b", "-k", qdir], capture_output=True)
    return qdir


def quarantine(path: str, sig: str) -> dict:
    import stat as stat_mod
    qid = secrets.token_hex(8)
    dest = os.path.join(_qdir(path), qid)
    fd, name = _open_parent(path)
    try:
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat_mod.S_ISREG(st.st_mode):
            raise OSError("not a regular file any more")
        qfd = os.open(os.path.dirname(dest), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.rename(name, qid, src_dir_fd=fd, dst_dir_fd=qfd)     # (same pool: a rename, never a copy)
        finally:
            os.close(qfd)
    finally:
        os.close(fd)
    os.chmod(dest, 0o000)
    if not DEV:
        subprocess.run(["setfacl", "-b", dest], capture_output=True)
    return {"id": qid, "path": path, "signature": sig, "time": int(time.time()), "state": "quarantined", "stored": dest,
            "size": st.st_size, "uid": st.st_uid, "gid": st.st_gid, "mode": st.st_mode & 0o7777}


def do_scan(only: list[str] | None) -> None:
    c = config()
    tg = targets(c, only)
    _write(STATUS, {"state": "running", "started": int(time.time()), "shares": [s["name"] for s in tg], "found": 0})
    if not tg:
        _write(STATUS, {**_read(STATUS, {}), "state": "ok", "finished": int(time.time())})
        return
    with open(LOG, "w") as logf:
        r = subprocess.run(command(c, [s["path"] for s in tg]), stdout=subprocess.PIPE, stderr=logf, text=True)
    hits = parse(r.stdout)
    records = []
    for path, sig in hits:
        rec = {"id": secrets.token_hex(8), "path": path, "signature": sig, "time": int(time.time()), "state": "reported"}
        if c["action"] == "quarantine":
            try:
                rec = quarantine(path, sig)
            except OSError as e:
                rec["state"] = "reported"
                rec["error"] = str(e)
        records.append(rec)
    with _lock:
        _write(FOUND, (_read(FOUND, []) + records)[-2000:])
    # clamscan: 0 = clean, 1 = found something, 2 = errors (unreadable files etc. - the scan still ran)
    state = "infected" if hits else ("warnings" if r.returncode == 2 else "ok")
    _write(STATUS, {**_read(STATUS, {}), "state": state, "finished": int(time.time()), "found": len(hits),
                    "errors": _errors()})
    if hits:
        from .notify import notify
        names = "\n".join(f"{p}  ({s})" for p, s in hits[:20])
        notify("critical", f"Antivirus found {len(hits)} infected file(s)",
               (("Moved to quarantine" if c["action"] == "quarantine" else "Reported only (not moved)") + ":\n" + names), event="antivirus")


def _errors() -> int:
    try:
        return sum(1 for ln in open(LOG, errors="replace") if "ERROR" in ln or "WARNING" in ln)
    except OSError:
        return 0


# ---------- quarantine ----------

@router.get("/found")
def found(_=Depends(require_admin)):
    return [{k: v for k, v in f.items() if k != "stored"} for f in reversed(_read(FOUND, []))][:500]


@router.post("/found/{qid}/restore")
def restore(qid: str, _=Depends(require_admin)):
    with _lock:
        items = _read(FOUND, [])
        f = next((x for x in items if x["id"] == qid and x["state"] == "quarantined"), None)
        if not f:
            raise HTTPException(404, "not in quarantine")
        try:
            fd, name = _open_parent(f["path"])           # (the folder must still exist - and not be a link)
        except OSError:
            raise HTTPException(409, "its folder is gone (or was replaced) - restore isn't possible")
        try:
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
                raise HTTPException(409, "a file with that name exists again - move it away first")
            except FileNotFoundError:
                pass
            qfd = os.open(os.path.dirname(f["stored"]), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.rename(os.path.basename(f["stored"]), name, src_dir_fd=qfd, dst_dir_fd=fd)
            finally:
                os.close(qfd)
            # (permissions through a handle on the file itself: never through a name a user could swap)
            ffd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                os.fchown(ffd, f.get("uid", 0), f.get("gid", 0))
                os.fchmod(ffd, f.get("mode", 0o664) & 0o777)
            finally:
                os.close(ffd)
        finally:
            os.close(fd)
        f["state"] = "restored"
        _write(FOUND, items)
    return {"ok": True}


@router.delete("/found/{qid}")
def delete(qid: str, _=Depends(require_admin)):
    with _lock:
        items = _read(FOUND, [])
        f = next((x for x in items if x["id"] == qid), None)
        if not f:
            raise HTTPException(404, "not found")
        if f["state"] == "quarantined":
            try:
                os.remove(f["stored"])
            except FileNotFoundError:
                pass
            f["state"] = "deleted"
        elif f["state"] == "reported":
            try:
                fd, name = _open_parent(f["path"])
                try:
                    os.unlink(name, dir_fd=fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
            f["state"] = "deleted"
        else:
            items.remove(f)                    # (restored/deleted: just forget the entry)
        _write(FOUND, items)
    return {"ok": True}


@router.get("/log")
def log(_=Depends(require_admin)):
    try:
        return {"log": open(LOG, errors="replace").read()[-30000:]}
    except OSError:
        return {"log": ""}


if __name__ == "__main__":
    if sys.argv[1:2] == ["scan"]:
        do_scan(sys.argv[2:] or None)
    elif sys.argv[1:2] == ["due"]:
        if due() and not _unit_active(UNIT):
            start()
