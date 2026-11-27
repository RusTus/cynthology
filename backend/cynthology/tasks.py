"""Task Scheduler (Control Panel > Task Scheduler): your own scripts, SMART tests, recycle-bin cleanup.

Each task is a systemd timer + service pair (cyn-task-<id>), so it survives reboots and missed runs
(Persistent=true). The service runs `python3 -m cynthology.tasks run <id>`, which records the output,
exit code and time, and sends an alert email when a task fails (if notifications are set up).

Drives in SMART tasks are stored by serial number - device names (sda, sdb...) change between boots.
"""
import json
import os
import re
import secrets
import shlex
import subprocess
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_admin
from .util import DEV, STATE_DIR, run, valid_name

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
DIR = os.path.join(STATE_DIR, "tasks")
CFG = os.path.join(DIR, "tasks.json")
UNIT_DIR = os.path.join(STATE_DIR, "units") if DEV else "/etc/systemd/system"
KINDS = {"script": "Run a script", "smart": "S.M.A.R.T. test", "recycle": "Empty recycle bins"}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
os.makedirs(DIR, mode=0o700, exist_ok=True)


class Schedule(BaseModel):
    every: str = "daily"           # hourly | daily | weekly | monthly | boot
    time: str = "03:00"            # HH:MM (minute only for hourly)
    days: list[str] = []           # weekly: mon..sun
    day_of_month: int = 1          # monthly


class Task(BaseModel):
    id: str = ""
    name: str
    kind: str = "script"
    enabled: bool = True
    schedule: Schedule = Schedule()
    notify_on_failure: bool = True
    # script
    script: str = ""
    user: str = "root"
    # smart
    test: str = "short"            # short | long
    disks: list[str] = []          # serial numbers; empty = all data drives
    # recycle
    shares: list[str] = []         # empty = all shared folders
    older_than_days: int = 30


def load() -> list[dict]:
    try:
        return json.load(open(CFG))
    except (OSError, ValueError):
        return []


def save(items: list[dict]) -> None:
    with open(CFG + ".new", "w") as f:
        json.dump(items, f, indent=1)
    os.replace(CFG + ".new", CFG)


def on_calendar(s: dict) -> str | None:
    """systemd OnCalendar= for a schedule (pure; unit tested). None = at boot."""
    every = s.get("every", "daily")
    if every == "boot":
        return None
    hh, mm = (s.get("time") or "03:00").split(":")
    hh, mm = int(hh), int(mm)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise HTTPException(400, "time must be HH:MM")
    if every == "hourly":
        return f"*-*-* *:{mm:02d}:00"
    if every == "daily":
        return f"*-*-* {hh:02d}:{mm:02d}:00"
    if every == "weekly":
        days = [d for d in DAYS if d in (s.get("days") or [])]
        if not days:
            raise HTTPException(400, "choose at least one day")
        return f"{','.join(d.capitalize() for d in days)} *-*-* {hh:02d}:{mm:02d}:00"
    if every == "monthly":
        dom = int(s.get("day_of_month") or 1)
        if not 1 <= dom <= 28:
            raise HTTPException(400, "day of the month: 1-28 (so it exists in every month)")
        return f"*-*-{dom:02d} {hh:02d}:{mm:02d}:00"
    raise HTTPException(400, "bad schedule")


def describe(s: dict) -> str:
    every = s.get("every")
    if every == "boot":
        return "At startup"
    if every == "hourly":
        return f"Every hour at :{s['time'].split(':')[1]}"
    if every == "weekly":
        return f"{', '.join(d.capitalize() for d in s.get('days', []))} at {s['time']}"
    if every == "monthly":
        return f"Day {s.get('day_of_month', 1)} of every month at {s['time']}"
    return f"Every day at {s['time']}"


def _unit(t: dict) -> str:
    return f"cyn-task-{t['id']}"


def install_units(t: dict) -> None:
    unit = _unit(t)
    service = (f"[Unit]\nDescription=Cynthology task: {t['name']}\n\n[Service]\nType=oneshot\n"
               f"WorkingDirectory=/opt/cynthology/backend\nEnvironment=CYN_STATE={STATE_DIR}\n"
               f"ExecStart=/usr/bin/python3 -m cynthology.tasks run {t['id']}\n")
    cal = on_calendar(t["schedule"])
    timer = (f"[Unit]\nDescription=Cynthology task timer: {t['name']}\n\n[Timer]\n" +
             (f"OnCalendar={cal}\nPersistent=true\n" if cal else "OnBootSec=2min\n") +
             f"Unit={unit}.service\n\n[Install]\nWantedBy=timers.target\n")
    os.makedirs(UNIT_DIR, exist_ok=True)
    for name, text in ((f"{unit}.service", service), (f"{unit}.timer", timer)):
        with open(os.path.join(UNIT_DIR, name), "w") as f:
            f.write(text)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable" if t.get("enabled", True) else "disable", "--now", f"{unit}.timer"], check=False)


def remove_units(t: dict) -> None:
    unit = _unit(t)
    run(["systemctl", "disable", "--now", f"{unit}.timer"], check=False)
    for name in (f"{unit}.service", f"{unit}.timer"):
        p = os.path.join(UNIT_DIR, name)
        if os.path.exists(p):
            os.remove(p)
    run(["systemctl", "daemon-reload"], check=False)


def _history_file(tid: str) -> str:
    return os.path.join(DIR, f"{tid}.history.json")


def history(tid: str) -> list[dict]:
    try:
        return json.load(open(_history_file(tid)))
    except (OSError, ValueError):
        return []


def _record(tid: str, entry: dict) -> None:
    h = ([entry] + history(tid))[:20]
    with open(_history_file(tid), "w") as f:
        json.dump(h, f)


# ---------- the jobs ----------

def _sh(cmd: list[str], timeout: int = 6 * 3600) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr)


def do_script(t: dict) -> tuple[int, str]:
    """The script goes to bash on stdin (no file a non-root user would need to read)."""
    script = t["script"].replace("\r\n", "\n")
    if t["user"] == "root":
        cmd, cwd = ["bash", "-s"], "/root"
    else:
        import pwd
        cmd, cwd = ["runuser", "-u", t["user"], "--", "bash", "-s"], pwd.getpwnam(t["user"]).pw_dir
        cwd = cwd if os.path.isdir(cwd) else "/tmp"
    r = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=6 * 3600, cwd=cwd)
    return r.returncode, r.stdout + r.stderr


def do_smart(t: dict) -> tuple[int, str]:
    from .storage import list_disks
    disks = list_disks()
    disks = [d for d in disks if d.get("serial") in t["disks"]] if t["disks"] else [d for d in disks if not d.get("system")]
    if not disks:
        return 1, "none of the selected drives is connected"
    out, rc = [], 0
    for d in disks:
        info = json.loads(subprocess.run(["smartctl", "-j", "-i", d["path"]], capture_output=True, text=True).stdout or "{}")
        if not (info.get("smart_support") or {}).get("available") and "nvme" not in d["name"]:
            out.append(f"{d['name']} ({d.get('model') or '?'}): no S.M.A.R.T. support - skipped")
            if t["disks"]:
                rc = 1            # a drive the user picked can't be tested
            continue
        code, text = _sh(["smartctl", "-t", t.get("test", "short"), d["path"]], timeout=60)
        msg = next((ln.strip() for ln in text.splitlines() if "Testing has begun" in ln or "Please wait" in ln), "")
        out.append(f"{d['name']} ({d.get('model') or '?'} {d.get('serial') or ''}): {msg or 'could not start the test'}")
        if code & 0b11 or not msg:
            rc = 1
    out.append("The test runs inside the drive; results appear in Storage Manager → Drives (and an alert if a drive fails it).")
    return rc, "\n".join(out)


def do_recycle(t: dict) -> tuple[int, str]:
    from . import shares as shares_mod
    days = max(0, int(t.get("older_than_days") or 0))
    out, rc = [], 0
    for s in shares_mod.load():
        if t["shares"] and s["name"] not in t["shares"]:
            continue
        bin_ = os.path.join(s["path"], "#recycle")
        if not os.path.isdir(bin_):
            continue
        age = ["-mtime", f"+{days - 1}"] if days else []      # +29 = at least 30 full days old
        code, text = _sh(["find", bin_, "-mindepth", "1", *age, "-delete", "-print"])
        n = len([ln for ln in text.splitlines() if ln.startswith(bin_)])
        subprocess.run(["find", bin_, "-mindepth", "1", "-type", "d", "-empty", "-delete"], capture_output=True)
        out.append(f"{s['name']}: removed {n} item(s)")
        rc |= code
    return rc, "\n".join(out) or "no recycle bins"


def run_task(tid: str) -> dict:
    t = next((x for x in load() if x["id"] == tid), None)
    if not t:
        raise SystemExit(f"no task {tid}")
    started = time.time()
    try:
        code, output = {"script": do_script, "smart": do_smart, "recycle": do_recycle}[t["kind"]](t)
    except subprocess.TimeoutExpired:
        code, output = 124, "stopped: took longer than 6 hours"
    except Exception as e:
        code, output = 1, f"error: {e}"
    entry = {"started": int(started), "duration": round(time.time() - started, 1), "exit_code": code, "output": output[-20000:]}
    _record(tid, entry)
    if code != 0 and t.get("notify_on_failure", True):
        from .backup import _alert
        _alert({"name": t["name"]}, "warning", f"Scheduled task '{t['name']}' failed (exit code {code})", output[-2000:])
    return entry


# ---------- API ----------

def _validate(body: Task) -> None:
    if not body.name.strip() or len(body.name) > 80:
        raise HTTPException(400, "give the task a name")
    if body.kind not in KINDS:
        raise HTTPException(400, "unknown task type")
    on_calendar(body.schedule.model_dump())
    if body.kind == "script":
        if not body.script.strip():
            raise HTTPException(400, "enter the script to run")
        if body.user != "root":
            valid_name(body.user, "user")
            import pwd
            try:
                pwd.getpwnam(body.user)
            except KeyError:
                if not DEV:
                    raise HTTPException(400, f"no user {body.user}")
    if body.kind == "smart" and body.test not in ("short", "long"):
        raise HTTPException(400, "test must be short or long")
    if body.kind == "recycle" and not 0 <= body.older_than_days <= 3650:
        raise HTTPException(400, "days: 0-3650")


def _view(t: dict) -> dict:
    h = history(t["id"])
    nxt = None
    if not DEV and t.get("enabled", True):
        r = subprocess.run(["systemctl", "show", f"{_unit(t)}.timer", "-p", "NextElapseUSecRealtime", "--value"], capture_output=True, text=True)
        nxt = r.stdout.strip() or None
    return {**t, "when": describe(t["schedule"]), "last": h[0] if h else None, "next": nxt,
            "running": not DEV and subprocess.run(["systemctl", "is-active", "--quiet", f"{_unit(t)}.service"]).returncode == 0}


@router.get("")
def list_tasks(_=Depends(require_admin)):
    return {"tasks": [_view(t) for t in load()], "kinds": KINDS}


@router.post("")
def save_task(body: Task, _=Depends(require_admin)):
    _validate(body)
    items = load()
    t = body.model_dump()
    t["id"] = body.id or secrets.token_hex(4)
    items = [x for x in items if x["id"] != t["id"]] + [t]
    save(items)
    if not DEV:
        install_units(t)
    return _view(t)


@router.delete("/{tid}")
def delete_task(tid: str, _=Depends(require_admin)):
    t = next((x for x in load() if x["id"] == tid), None)
    if not t:
        raise HTTPException(404, "no such task")
    if not DEV:
        remove_units(t)
    save([x for x in load() if x["id"] != tid])
    for p in (_history_file(tid), os.path.join(DIR, f"{tid}.sh")):
        if os.path.exists(p):
            os.remove(p)
    return {"ok": True}


@router.post("/{tid}/run")
def run_now(tid: str, _=Depends(require_admin)):
    t = next((x for x in load() if x["id"] == tid), None)
    if not t:
        raise HTTPException(404, "no such task")
    if DEV:
        return run_task(tid)
    run(["systemctl", "start", "--no-block", f"{_unit(t)}.service"])
    return {"ok": True}


@router.get("/{tid}/history")
def get_history(tid: str, _=Depends(require_admin)):
    return history(tid)


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["run"] and len(sys.argv) == 3:
        e = run_task(sys.argv[2])
        print(e["output"])
        sys.exit(0 if e["exit_code"] == 0 else 1)
