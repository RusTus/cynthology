"""Log Center: system journal, sign-in events, SMB file access (per shared folder) and alerts.

- System: journald (journalctl -o json), filtered by priority, unit and text.
- Sign-ins: written by auth.py / firewall.py to <state>/logs/auth.jsonl (last 20000 events).
- File access: Samba's full_audit module on shares with "log file access" on; it logs through
  syslog (identifier smbd_audit) into the journal. Opens for reading are recorded too - that is
  what makes "who looked at this file" answerable - so it is off by default.
"""
import csv
import io
import json
import os
import re
import subprocess
import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from .auth import require_admin
from .util import DEV, STATE_DIR

router = APIRouter(prefix="/api/logs", tags=["logs"])
DIR = os.path.join(STATE_DIR, "logs")
AUTH_LOG = os.path.join(DIR, "auth.jsonl")
MAX_AUTH = 20000
_lock = threading.Lock()
os.makedirs(DIR, mode=0o700, exist_ok=True)


# ---------- sign-in events ----------

def auth_event(event: str, user: str, ip: str, detail: str = "") -> None:
    """event: login | failed | 2fa-failed | locked | blocked | logout"""
    rec = {"time": int(time.time()), "event": event, "user": user[:64], "ip": ip[:64], "detail": detail[:200]}
    with _lock:
        with open(AUTH_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
        try:
            if os.path.getsize(AUTH_LOG) > MAX_AUTH * 220:      # trim now and then
                lines = open(AUTH_LOG).readlines()[-MAX_AUTH:]
                with open(AUTH_LOG + ".new", "w") as f:
                    f.writelines(lines)
                os.replace(AUTH_LOG + ".new", AUTH_LOG)
        except OSError:
            pass


def _auth_events() -> list[dict]:
    try:
        lines = open(AUTH_LOG).readlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


# ---------- journal ----------

PRIOS = {"0": "emergency", "1": "alert", "2": "critical", "3": "error", "4": "warning", "5": "notice", "6": "info", "7": "debug"}


def _journal(args: list[str], limit: int) -> list[dict]:
    if DEV:
        return [{"time": int(time.time()) - i * 60, "unit": "smbd.service", "priority": "info", "message": f"dev log line {i}"} for i in range(5)]
    r = subprocess.run(["journalctl", "-o", "json", "--no-pager", "-r", "-n", str(limit), *args],
                       capture_output=True, text=True, timeout=30)
    out = []
    for ln in r.stdout.splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        msg = e.get("MESSAGE")
        if isinstance(msg, list):          # binary messages come as byte arrays
            msg = bytes(x for x in msg if isinstance(x, int)).decode(errors="replace")
        out.append({"time": int(e.get("__REALTIME_TIMESTAMP", "0")) // 1_000_000,
                    "unit": e.get("_SYSTEMD_UNIT") or e.get("SYSLOG_IDENTIFIER") or "kernel",
                    "priority": PRIOS.get(str(e.get("PRIORITY", "6")), "info"), "message": (msg or "")[:2000]})
    return out


@router.get("/system")
def system_log(priority: str = "6", unit: str = "", q: str = "", since_hours: int = 24, limit: int = 500, _=Depends(require_admin)):
    if priority not in PRIOS:
        raise HTTPException(400, "bad priority")
    if unit and not re.fullmatch(r"[A-Za-z0-9@._:-]{1,100}", unit):
        raise HTTPException(400, "bad unit name")
    args = ["-p", priority, "--since", f"-{max(1, min(since_hours, 24 * 90))}h"]
    if unit:
        args += ["-u", unit]
    if q:
        args += ["-g", re.escape(q)[:200], "--case-sensitive=false"]
    return _journal(args, max(1, min(limit, 5000)))


@router.get("/units")
def units(_=Depends(require_admin)):
    """Services worth filtering by."""
    base = ["cynthology-ui", "smbd", "nmbd", "winbind", "nfs-server", "ssh", "docker", "nginx", "wg-quick@wg0",
            "rtslib-fb-targetctl", "nut-monitor", "cynthology-backup", "cynthology-snapshots", "cynthology-health",
            "cynthology-firewall", "kernel"]
    return base


@router.get("/signins")
def signins(q: str = "", event: str = "", limit: int = 1000, _=Depends(require_admin)):
    ev = list(reversed(_auth_events()))
    if event:
        ev = [e for e in ev if e["event"] == event]
    if q:
        ql = q.lower()
        ev = [e for e in ev if ql in (e["user"] + " " + e["ip"] + " " + e.get("detail", "")).lower()]
    return ev[:max(1, min(limit, MAX_AUTH))]


# ---------- SMB file access (full_audit) ----------

AUDIT_RE = re.compile(r"^(?P<user>[^|]*)\|(?P<ip>[^|]*)\|(?P<share>[^|]*)\|(?P<op>[a-z_]+)\|(?P<ok>ok|fail)\|(?P<rest>.*)$")
OPS = {"openat": "open", "renameat": "rename", "unlinkat": "delete", "mkdirat": "create folder"}


def parse_audit(msg: str) -> dict | None:
    """'alice|192.168.1.5|family|openat|ok|r|Photos/a.jpg' -> dict (pure; unit tested)."""
    m = AUDIT_RE.match(msg.strip())
    if not m:
        return None
    op, rest = m.group("op"), m.group("rest")
    action, path = OPS.get(op, op), rest
    if op == "openat":
        mode, _, path = rest.partition("|")
        action = "write" if "w" in mode else "read"
    elif op == "renameat":
        path = rest.replace("|", " → ")
    elif op == "unlinkat":
        path = rest.split("|")[-1]
    share = m.group("share")
    # Samba logs full paths: show them relative to the shared folder
    rel = lambda x: re.sub(rf"^/[^|]*?/{re.escape(share)}(/|$)", "", x.strip())
    if op == "renameat":
        src, _, dst = rest.partition("|")
        src, dst = rel(src), rel(dst)
        if dst.startswith("#recycle/"):
            action, path = "delete (to recycle bin)", src
        else:
            path = f"{src} → {dst}"
    else:
        path = rel(path)
    return {"user": m.group("user"), "ip": m.group("ip"), "share": share, "action": action,
            "ok": m.group("ok") == "ok", "path": path}


@router.get("/files")
def file_access(q: str = "", share: str = "", since_hours: int = 24, limit: int = 1000, _=Depends(require_admin)):
    raw = _journal(["-t", "smbd_audit", "--since", f"-{max(1, min(since_hours, 24 * 90))}h"], max(1, min(limit * 2, 20000)))
    out = []
    for e in raw:
        rec = parse_audit(e["message"]) if not DEV else {"user": "alice", "ip": "192.168.1.5", "share": "family", "action": "read", "ok": True, "path": "Photos/a.jpg"}
        if not rec or (share and rec["share"] != share):
            continue
        if q and q.lower() not in (rec["user"] + " " + rec["path"] + " " + rec["ip"]).lower():
            continue
        if rec["path"] in (".", "") or rec["path"].endswith(("/", ":AFP_AfpInfo", ":com.apple.metadata")) or \
                rec["path"].startswith("#recycle/") or ("." not in rec["path"].rsplit("/", 1)[-1] and rec["action"] == "read"):
            continue                      # (folder listings, recycle bin internals, Mac metadata streams: noise)
        out.append({"time": e["time"], **rec})
        if len(out) >= limit:
            break
    return out


@router.get("/export")
def export(kind: str, _=Depends(require_admin)):
    rows = {"system": lambda: system_log(limit=5000, _=None), "signins": lambda: signins(limit=MAX_AUTH, _=None),
            "files": lambda: file_access(limit=20000, _=None)}.get(kind)
    if not rows:
        raise HTTPException(400, "kind: system, signins or files")
    data = rows()
    buf = io.StringIO()
    if data:
        w = csv.DictWriter(buf, fieldnames=list(data[0].keys()))
        w.writeheader()
        for r in data:
            w.writerow({**r, "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["time"]))})
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="cynthology-{kind}-log.csv"'})
