"""Status digest: a daily or weekly summary email (and optionally a short push) of how the NAS is doing.

Health, storage pools, drives, backups, cloud sync, antivirus, updates and the alerts since the last
digest. Checked from the health timer (every 10 minutes); sent once per day/week after the chosen time.
"""
import datetime
import json
import os
import socket
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import settings
from .auth import require_admin
from .util import STATE_DIR

router = APIRouter(prefix="/api/notify/digest", tags=["notify"])
STATE = os.path.join(STATE_DIR, "digest-state.json")


def config() -> dict:
    c = settings.get("digest") or {}
    return {"frequency": c.get("frequency", "off"), "time": c.get("time", "08:00"), "day": int(c.get("day", 0)),
            "push": bool(c.get("push", False))}


def _fmt(b: int | float | None) -> str:
    from .health import storage_fmt
    return storage_fmt(int(b or 0))


def _when(ts: int | None) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"


def build(since: int) -> tuple[str, str, str]:
    """(subject, body, one-line summary) - every section is best-effort: one broken part never stops the digest."""
    lines, problems = [], 0

    def section(title, fn):
        nonlocal problems
        lines.append(f"\n{title}\n" + "-" * len(title))
        try:
            n = fn()
            problems += n or 0
        except Exception as e:
            lines.append(f"  (couldn't read: {e})")

    def health():
        from .health import collect
        issues = collect()
        if not issues:
            lines.append("  ✓ No problems.")
        for i in issues.values():
            lines.append(f"  {'✗' if i['level'] == 'critical' else '!'} {i['title']}")
        return len(issues)

    def pools():
        from . import storage
        n = 0
        for p in storage.pools(_=None):
            pct = round(100 * p["used"] / p["total"]) if p.get("total") else 0
            ok = p["status"] == "healthy"
            n += 0 if ok else 1
            lines.append(f"  {'✓' if ok else '✗'} {p['label']} ({p['raid'].upper()}): {p['status']}, {_fmt(p['used'])} of {_fmt(p['total'])} used ({pct}%)")
        if not n and not lines[-1].startswith("  "):
            lines.append("  (no storage pools)")
        return n

    def drives():
        from . import storage
        for d in storage.list_disks():
            if d.get("system") or not d.get("in_use"):
                continue
            try:
                s = storage.smart(d["name"], let_sleep=True)
            except Exception:
                s = {}
            state = "asleep" if s.get("asleep") else ("SMART OK" if s.get("passed") else "SMART FAILED" if s.get("passed") is False else "no SMART")
            temp = f", {s['temperature']} °C" if s.get("temperature") else ""
            lines.append(f"  {d['name']} {d.get('model', '')}: {state}{temp}")

    def backups():
        from . import backup
        n = 0
        tasks = backup.load_tasks()
        for t in tasks:
            st = backup.read_status(t["id"])
            bad = st.get("state") in ("failed",)
            n += 1 if bad else 0
            lines.append(f"  {'✗' if bad else '✓'} {t['name']}: {st.get('state', 'never')}, last run {_when(st.get('finished') or st.get('started'))}")
        if not tasks:
            lines.append("  (no backup tasks - a NAS with RAID still needs a backup)")
        return n

    def cloudsync():
        from . import cloudsync as cs
        n = 0
        for t in cs.load_tasks():
            st = cs.read_status(t["id"])
            bad = st.get("state") == "failed"
            n += 1 if bad else 0
            lines.append(f"  {'✗' if bad else '✓'} {t['name']}: {st.get('state', 'never')}, last sync {_when(st.get('finished'))}")
        return n

    def antivirus():
        from . import antivirus as av
        if not av.installed():
            lines.append("  (not installed)")
            return 0
        st = av._read(av.STATUS, {})
        lines.append(f"  Last scan {_when(st.get('finished'))}: {st.get('state', 'never')}" + (f", {st.get('found')} found" if st.get("found") else ""))
        return 1 if st.get("state") == "infected" else 0

    def updates():
        from . import update
        s = update.status(refresh=False, _=None)
        if s.get("update_available"):
            lines.append(f"  ↑ Cynthology {s['latest']['version']} is available (you have {s['current']}).")
        else:
            lines.append(f"  ✓ Cynthology {s.get('current')} is up to date.")

    def alerts():
        from .health import LOG_FILE
        try:
            log = json.load(open(LOG_FILE))
        except (OSError, ValueError):
            log = []
        new = [a for a in log if a.get("time", 0) > since]
        if not new:
            lines.append("  None.")
        for a in new[:25]:
            lines.append(f"  {_when(a.get('time'))}  [{a.get('level')}] {a.get('title')}")
        if len(new) > 25:
            lines.append(f"  ... and {len(new) - 25} more (Log Center).")

    section("Health", health)
    section("Storage pools", pools)
    section("Drives", drives)
    section("Backups", backups)
    section("Cloud Sync", cloudsync)
    section("Antivirus", antivirus)
    section("Updates", updates)
    section(f"Alerts since {_when(since)}", alerts)
    host = socket.gethostname()
    summary = "everything looks fine" if not problems else f"{problems} thing(s) need attention"
    subject = f"Status report: {summary}"
    body = f"Status report for {host} - {datetime.datetime.now().strftime('%A %Y-%m-%d %H:%M')}\n" + "\n".join(lines)
    return subject, body, summary


def due(now: datetime.datetime | None = None) -> bool:
    c = config()
    if c["frequency"] == "off":
        return False
    now = now or datetime.datetime.now()
    hh, mm = map(int, c["time"].split(":"))
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < slot or (c["frequency"] == "weekly" and now.weekday() != c["day"]):
        return False
    last = _state().get("last_sent", 0)
    return last < slot.timestamp()


def _state() -> dict:
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def send(push: bool | None = None) -> dict:
    from . import health, notify
    since = _state().get("last_sent") or int(time.time() - (7 if config()["frequency"] == "weekly" else 1) * 86400)
    subject, body, summary = build(since)
    sent, errors = [], []
    cfg = health.load_smtp()
    to = notify.recipients(cfg)
    if cfg.get("host") and to:
        try:                                       # (email trouble must not stop the push - and vice versa)
            health.send_mail(subject, body, {**cfg, "to": to})
            sent.append("email")
        except Exception as e:
            errors.append(f"email: {e}")
    if push if push is not None else config()["push"]:
        notify.push("info", subject, summary + " - the full report went by email." if sent else body[:900])
        sent.append("push")
    with open(STATE, "w") as f:
        json.dump({"last_sent": int(time.time())}, f)
    if errors and not sent:
        raise RuntimeError("; ".join(errors))
    return {"sent": sent, "subject": subject, "errors": errors}


def maybe_send() -> None:
    """From the health timer."""
    if due():
        try:
            send()
        except Exception as e:
            print("digest:", e)


class Digest(BaseModel):
    frequency: str = "off"      # off | daily | weekly
    time: str = "08:00"
    day: int = 0                # weekly: 0 = Monday
    push: bool = False


@router.get("")
def get(_=Depends(require_admin)):
    return config()


@router.put("")
def put(body: Digest, _=Depends(require_admin)):
    import re
    if body.frequency not in ("off", "daily", "weekly") or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", body.time) or not 0 <= body.day <= 6:
        raise HTTPException(400, "invalid schedule")
    settings.update(digest=body.model_dump())
    return config()


@router.get("/preview")
def preview(_=Depends(require_admin)):
    subject, body, _s = build(_state().get("last_sent") or int(time.time() - 86400))
    return {"subject": subject, "body": body}


@router.post("/send")
def send_now(_=Depends(require_admin)):
    from . import health
    if not (health.load_smtp().get("host")) and not config()["push"]:
        raise HTTPException(400, "set up email (or turn on 'also send to push services') first")
    try:
        return send()
    except Exception as e:
        raise HTTPException(502, f"couldn't send: {e}")
