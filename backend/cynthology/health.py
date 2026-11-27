"""Health monitoring + email notifications.

`python3 -m cynthology.health` runs every 10 minutes (cynthology-health.timer). It collects the
current problems, compares them with the previous run and emails only what changed
(new problem / problem resolved), so a degraded array doesn't send a mail every 10 minutes.
"""
import json
import os
import smtplib
import socket
import ssl
import time
from email.message import EmailMessage

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import storage, tls
from .auth import current_user, require_admin
from .util import DEV, STATE_DIR

router = APIRouter(prefix="/api/health", tags=["health"])
SMTP_FILE = os.path.join(STATE_DIR, "smtp.json")
STATE_FILE = os.path.join(STATE_DIR, "health-state.json")
LOG_FILE = os.path.join(STATE_DIR, "alerts.json")
FULL_PCT = 90


# ---------- checks ----------

def collect() -> dict[str, dict]:
    """key -> {level: 'critical'|'warning', title, detail}. Keys are stable so changes can be detected."""
    issues = {}
    for p in storage.pools():
        if p["status"] == "degraded":
            issues[f"pool-{p['id']}-degraded"] = {
                "level": "critical", "title": f"{p['label']} is degraded",
                "detail": f"A drive in {p['label']} ({p['mount']}) has failed or is missing. Data is still "
                          f"available but no longer protected. Replace the drive and use Storage Manager → Repair"
                          f" (tip: a hot spare would have started the rebuild automatically)."}
        elif p["status"].startswith("rebuilding"):
            issues[f"pool-{p['id']}-rebuilding"] = {
                "level": "warning", "title": f"{p['label']} is rebuilding",
                "detail": f"{p['label']} is {p['status']}. Avoid powering off until it completes."}
        if p.get("failed") and p["status"] == "healthy":
            issues[f"pool-{p['id']}-failed-replaced"] = {
                "level": "warning", "title": f"Hot spare took over in {p['label']}",
                "detail": f"Drive(s) {', '.join(p['failed'])} failed and the hot spare rebuilt the pool - it is protected "
                          f"again. Replace the failed drive and add the new one as hot spare (Storage Manager)."}
        if p.get("total") and p["used"] / p["total"] * 100 >= FULL_PCT:
            pct = round(p["used"] / p["total"] * 100)
            issues[f"pool-{p['id']}-full"] = {
                "level": "warning", "title": f"{p['label']} is {pct}% full",
                "detail": f"Only {storage_fmt(p['total'] - p['used'])} left on {p['label']}."}
    for d in storage.list_disks():
        if d.get("system") or not d.get("in_use"):
            continue
        try:
            s = storage.smart(d["name"])
        except Exception:
            continue
        if s.get("supported") and s.get("passed") is False:
            issues[f"disk-{d.get('serial') or d['name']}-smart"] = {
                "level": "critical", "title": f"Drive {d['name']} is failing (SMART)",
                "detail": f"{d['model']} (serial {d.get('serial')}) reports a SMART failure. Back up and replace it soon."}
        elif s.get("supported") and s.get("last_test") and s["last_test"].get("passed") is False:
            issues[f"disk-{d.get('serial') or d['name']}-selftest"] = {
                "level": "critical", "title": f"Drive {d['name']} failed its self-test",
                "detail": f"{d['model']} (serial {d.get('serial')}): {s['last_test'].get('type')} self-test: "
                          f"{s['last_test'].get('result')}. Back up and replace it soon."}
        elif s.get("supported") and (s.get("reallocated") or 0) > 0:
            issues[f"disk-{d.get('serial') or d['name']}-realloc"] = {
                "level": "warning", "title": f"Drive {d['name']} has reallocated sectors",
                "detail": f"{d['model']} (serial {d.get('serial')}): {s['reallocated']} reallocated sectors. Keep an eye on it."}
    try:
        from . import ups
        issues.update(ups.health_issues())
    except Exception:
        pass
    c = tls.cert_info()
    if c.get("present") and c.get("kind") == "uploaded" and c.get("days_left", 999) < 14:
        issues["cert-expiry"] = {"level": "warning", "title": "HTTPS certificate expires soon",
                                 "detail": f"The certificate expires on {c['expires']}. Upload a renewed one in Control Panel → Security."}
    return issues


def storage_fmt(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if b < 1000:
            return f"{b:.0f} {unit}"
        b /= 1000
    return f"{b:.0f} EB"


# ---------- email ----------

def load_smtp() -> dict:
    try:
        with open(SMTP_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def send_mail(subject: str, body: str, cfg: dict | None = None) -> None:
    cfg = cfg or load_smtp()
    if not cfg.get("host") or not cfg.get("to"):
        raise RuntimeError("email notifications are not set up")
    msg = EmailMessage()
    msg["Subject"] = f"[{socket.gethostname()}] {subject}"
    msg["From"] = cfg.get("sender") or cfg.get("username") or f"cynthology@{socket.gethostname()}"
    msg["To"] = cfg["to"]
    msg.set_content(body + f"\n\n-- \nCynthology on {socket.gethostname()}\n")
    port = int(cfg.get("port") or 587)
    ctx = ssl.create_default_context()
    if cfg.get("security") == "ssl":
        server = smtplib.SMTP_SSL(cfg["host"], port, context=ctx, timeout=20)
    else:
        server = smtplib.SMTP(cfg["host"], port, timeout=20)
        if cfg.get("security", "starttls") == "starttls":
            server.starttls(context=ctx)
    with server:
        if cfg.get("username"):
            server.login(cfg["username"], cfg.get("password", ""))
        server.send_message(msg)


def _log(entries: list[dict]) -> None:
    try:
        with open(LOG_FILE) as f:
            log = json.load(f)
    except (OSError, ValueError):
        log = []
    log = (entries + log)[:200]
    with open(LOG_FILE, "w") as f:
        json.dump(log, f)


def run_check() -> list[dict]:
    """One monitoring pass: detect changes, log them, email them."""
    # power events are alerted instantly by the UPS hook (ups.py); don't repeat them here
    now = {k: v for k, v in collect().items() if k not in ("ups-onbatt", "ups-lowbatt")}
    try:
        with open(STATE_FILE) as f:
            before = json.load(f)
    except (OSError, ValueError):
        before = {}
    changes = []
    for k, v in now.items():
        if k not in before:
            changes.append({**v, "key": k, "event": "new", "time": int(time.time())})
    for k, v in before.items():
        if k not in now:
            changes.append({**v, "key": k, "event": "resolved", "level": "info",
                            "title": f"Resolved: {v['title']}", "time": int(time.time())})
    with open(STATE_FILE, "w") as f:
        json.dump(now, f)
    if changes:
        _log(changes)
        cfg = load_smtp()
        if cfg.get("enabled") and cfg.get("host"):
            worst = "critical" if any(c["level"] == "critical" for c in changes) else "warning"
            subject = changes[0]["title"] if len(changes) == 1 else f"{len(changes)} health changes ({worst})"
            body = "\n\n".join(f"{'⚠' if c['level'] != 'info' else '✓'} {c['title']}\n{c.get('detail', '')}" for c in changes)
            try:
                send_mail(subject, body, cfg)
            except Exception as e:  # mail trouble must never break monitoring
                _log([{"level": "warning", "title": "Could not send notification email", "detail": str(e),
                       "event": "mail", "time": int(time.time())}])
    return changes


# ---------- API ----------

@router.get("")
def health(_=Depends(current_user)):
    issues = collect()
    worst = "critical" if any(i["level"] == "critical" for i in issues.values()) else \
            "warning" if issues else "healthy"
    return {"status": worst, "issues": list(issues.values())}


@router.get("/alerts")
def alerts(_=Depends(require_admin)):
    try:
        with open(LOG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


class Smtp(BaseModel):
    enabled: bool = True
    host: str = ""
    port: int = 587
    security: str = "starttls"     # starttls | ssl | none
    username: str = ""
    password: str | None = None    # None = keep the stored password
    sender: str = ""
    to: str = ""


@router.get("/smtp")
def get_smtp(_=Depends(require_admin)):
    cfg = load_smtp()
    cfg.pop("password", None)
    return {**Smtp(password=None).model_dump(exclude={"password"}), **cfg, "has_password": bool(load_smtp().get("password"))}


@router.put("/smtp")
def put_smtp(body: Smtp, _=Depends(require_admin)):
    if body.security not in ("starttls", "ssl", "none"):
        raise HTTPException(400, "security must be starttls, ssl or none")
    cfg = body.model_dump()
    if body.password is None:
        cfg["password"] = load_smtp().get("password", "")
    fd = os.open(SMTP_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    os.replace(SMTP_FILE + ".tmp", SMTP_FILE)
    return {"ok": True}


@router.post("/smtp/test")
def test_smtp(_=Depends(require_admin)):
    if DEV:
        return {"ok": True, "note": "dev mode: not sent"}
    try:
        send_mail("Test message", "Email notifications from your Cynthology NAS are working.")
    except Exception as e:
        raise HTTPException(400, f"sending failed: {e}")
    return {"ok": True}


if __name__ == "__main__":
    for c in run_check():
        print(c["event"], c["title"])
