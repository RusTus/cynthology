"""Backup: encrypted, deduplicated, versioned backups of shared folders with restic.

Each task backs up a set of shared folders into its own restic repository at a destination:
  local  - a folder on this NAS (another volume) or on an external USB drive (mounted by UUID)
  sftp   - another NAS / Linux server over SSH (key generated here, see /api/backup/sshkey)
  s3     - any S3-compatible storage (Backblaze B2, Wasabi, AWS S3, MinIO, ...)
  rest   - a restic REST server
Runs happen in transient systemd units (`python3 -m cynthology.backup run <id>`), so they survive
web UI restarts; `python3 -m cynthology.backup due` (timer, every 5 min) starts scheduled runs.
"""
import datetime
import fcntl
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import shares as shares_mod
from .auth import require_admin
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/backup", tags=["backup"])
DIR = os.path.join(STATE_DIR, "backup")
TASKS = os.path.join(DIR, "tasks.json")
SSH_KEY = os.path.join(DIR, "id_ed25519")
KNOWN_HOSTS = os.path.join(DIR, "known_hosts")
MEDIA = os.path.join(STATE_DIR, "media") if DEV else "/media"
os.makedirs(DIR, mode=0o700, exist_ok=True)


# ---------- storage of tasks / secrets / status ----------

def load_tasks() -> list[dict]:
    try:
        with open(TASKS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_tasks(tasks: list[dict]) -> None:
    tmp = TASKS + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # holds cloud keys
    with os.fdopen(fd, "w") as f:
        json.dump(tasks, f, indent=2)
    os.replace(tmp, TASKS)


def get_task(tid: str) -> dict:
    t = next((t for t in load_tasks() if t["id"] == tid), None)
    if not t:
        raise HTTPException(404, "no such backup task")
    return t


def _pw_file(tid: str) -> str:
    return os.path.join(DIR, f"{tid}.pass")


def _status_file(tid: str) -> str:
    return os.path.join(DIR, f"{tid}.status.json")


def _log_file(tid: str) -> str:
    return os.path.join(DIR, f"{tid}.log")


def read_status(tid: str) -> dict:
    try:
        with open(_status_file(tid)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"state": "never"}


def write_status(tid: str, **values) -> dict:
    s = read_status(tid)
    s.update(values)
    with open(_status_file(tid), "w") as f:
        json.dump(s, f)
    return s


# ---------- destinations ----------

def usb_drives() -> list[dict]:
    """External (USB) filesystems that can be backup targets."""
    out = subprocess.run(["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,TYPE,TRAN,FSTYPE,LABEL,UUID,MOUNTPOINT,HOTPLUG,MODEL"],
                         capture_output=True, text=True).stdout or '{"blockdevices":[]}'
    drives = []

    def walk(dev, usb, model):
        usb = usb or dev.get("tran") == "usb" or bool(dev.get("hotplug"))
        model = dev.get("model") or model
        if dev.get("fstype") and dev.get("uuid") and usb and dev.get("fstype") not in ("linux_raid_member", "LVM2_member", "swap"):
            drives.append({"uuid": dev["uuid"], "label": dev.get("label") or "", "fstype": dev["fstype"],
                           "size": int(dev.get("size") or 0), "device": dev["path"], "model": (model or "").strip(),
                           "mounted_at": dev.get("mountpoint")})
        for c in dev.get("children") or []:
            walk(c, usb, model)

    for d in json.loads(out)["blockdevices"]:
        walk(d, False, d.get("model"))
    return drives


def _mount_usb(uuid: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9-]+", uuid):
        raise RuntimeError("bad drive id")
    # already mounted (e.g. as usbshareN by External Devices)? use that mount
    cur = subprocess.run(["findmnt", "-rno", "TARGET", f"UUID={uuid}"], capture_output=True, text=True).stdout.split()
    if cur:
        return cur[0]
    mp = os.path.join(MEDIA, f"cyn-{uuid}")
    if DEV:
        os.makedirs(mp, exist_ok=True)
        return mp
    if os.path.ismount(mp):
        return mp
    dev = f"/dev/disk/by-uuid/{uuid}"
    if not os.path.exists(dev):
        raise RuntimeError("the backup drive is not connected")
    os.makedirs(mp, exist_ok=True)
    subprocess.run(["mount", dev, mp], check=True, capture_output=True, text=True)
    return mp


def repo_and_env(t: dict) -> tuple[str, dict]:
    """restic repository string + environment for a task (mounts a USB drive if needed)."""
    d = t["dest"]
    env = {"RESTIC_PASSWORD_FILE": _pw_file(t["id"]), "RESTIC_CACHE_DIR": os.path.join(DIR, "cache"),
           "PATH": "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": DIR}
    sub = d.get("path", "").strip("/")
    if d["type"] == "local":
        base = _mount_usb(d["uuid"]) if d.get("uuid") else "/"
        return os.path.join(base, sub), env
    if d["type"] == "sftp":
        port = int(d.get("port") or 22)
        # (host and user end up in an ssh command line: no spaces, quotes or leading dashes)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", d.get("user", "")) or \
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}|\[[0-9a-fA-F:.]{2,45}\]", d.get("host", "")) or not 0 < port < 65536:
            raise RuntimeError("the SFTP server name, user or port isn't valid")
        ssh = (f"ssh -i {SSH_KEY} -p {port} -o UserKnownHostsFile={KNOWN_HOSTS} -o StrictHostKeyChecking=accept-new "
               f"-o BatchMode=yes -o ServerAliveInterval=60 {d['user']}@{d['host']} -s sftp")
        env["CYN_SFTP_COMMAND"] = ssh
        # relative to the user's home on the server unless an absolute path is given
        return f"sftp:{d['user']}@{d['host']}:{d.get('path', '').rstrip('/') or '.'}", env
    if d["type"] == "s3":
        env["AWS_ACCESS_KEY_ID"] = d.get("access_key", "")
        env["AWS_SECRET_ACCESS_KEY"] = d.get("secret_key", "")
        if d.get("region"):
            env["AWS_DEFAULT_REGION"] = d["region"]
        endpoint = d.get("endpoint", "").rstrip("/") or "https://s3.amazonaws.com"
        return f"s3:{endpoint}/{d['bucket']}/{sub}".rstrip("/"), env
    if d["type"] == "rest":
        return f"rest:{d['url'].rstrip('/')}/{sub}".rstrip("/"), env
    raise RuntimeError("unknown destination type")


def restic(t: dict, *args: str, json_out: bool = False, timeout: int | None = None,
           log: bool = False) -> subprocess.CompletedProcess:
    repo, env = repo_and_env(t)
    cmd = ["restic", "-r", repo]
    if "CYN_SFTP_COMMAND" in env:
        cmd += ["-o", f"sftp.command={env.pop('CYN_SFTP_COMMAND')}"]
    if json_out:
        cmd.append("--json")
    cmd += list(args)
    if log:
        with open(_log_file(t["id"]), "a") as lf:
            lf.write(f"\n$ restic {' '.join(shlex.quote(a) for a in args)}\n")
            lf.flush()
            return subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=lf, text=True, timeout=timeout)
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)


def _err(r: subprocess.CompletedProcess) -> str:
    msg = (r.stderr or r.stdout or "").strip().splitlines()
    return msg[-1] if msg else f"restic exited with code {r.returncode}"


# ---------- running a backup ----------

def source_paths(t: dict) -> list[str]:
    by_name = {s["name"]: s for s in shares_mod.load()}
    missing = [n for n in t["sources"] if n not in by_name]
    if missing:
        raise RuntimeError(f"shared folder(s) no longer exist: {', '.join(missing)}")
    return [by_name[n]["path"] for n in t["sources"]]


def do_backup(tid: str) -> None:
    """Runs inside the transient systemd unit."""
    t = get_task(tid)
    lock = open(os.path.join(DIR, f"{tid}.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # already running
    open(_log_file(tid), "w").close()
    started = time.time()
    write_status(tid, state="running", started=int(started), progress=None, error=None)
    try:
        paths = source_paths(t)
        r = restic(t, "cat", "config")
        if r.returncode != 0:  # first run: create the repository
            r = restic(t, "init", log=True)
            if r.returncode != 0:
                raise RuntimeError("could not create backup repository: " + (_tail_error(tid) or _err(r)))
        excludes = []
        for p in t.get("exclude", []) + ["#recycle", ".snapshots", "@eaDir", ".DS_Store", "Thumbs.db"]:
            excludes += ["--exclude", p]
        args = ["backup", "--tag", f"cyn-{tid}", "--host", socket.gethostname(), *excludes, *paths]
        repo, env = repo_and_env(t)
        cmd = ["restic", "-r", repo]
        if "CYN_SFTP_COMMAND" in env:
            cmd += ["-o", f"sftp.command={env.pop('CYN_SFTP_COMMAND')}"]
        cmd += ["--json", *args]
        summary, last_progress = {}, 0.0
        with open(_log_file(tid), "a") as lf:
            lf.write(f"\n$ restic {' '.join(args)}\n")
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=lf, text=True)
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("message_type") == "status" and time.time() - last_progress > 2:
                    last_progress = time.time()
                    write_status(tid, progress={"percent": round(100 * msg.get("percent_done", 0), 1),
                                                "files": msg.get("files_done", 0), "bytes": msg.get("bytes_done", 0),
                                                "total_bytes": msg.get("total_bytes", 0)})
                elif msg.get("message_type") == "summary":
                    summary = msg
            rc = proc.wait()
        # restic exit code 3 = some files could not be read; the snapshot is still made
        if rc not in (0, 3):
            raise RuntimeError(_tail_error(tid) or f"backup failed (exit code {rc})")
        ret = t.get("retention") or {}
        keep = [f"--keep-{k}={int(v)}" for k, v in ret.items() if k in ("last", "daily", "weekly", "monthly", "yearly") and int(v or 0) > 0]
        if keep:
            r = restic(t, "forget", "--tag", f"cyn-{tid}", "--prune", *keep, log=True)
            if r.returncode != 0:
                raise RuntimeError("cleaning up old versions failed: " + (_tail_error(tid) or _err(r)))
        write_status(tid, state="warning" if rc == 3 else "success", finished=int(time.time()), progress=None,
                     error="some files could not be read (see log)" if rc == 3 else None,
                     summary={"files_new": summary.get("files_new"), "files_changed": summary.get("files_changed"),
                              "data_added": summary.get("data_added"), "total_files": summary.get("total_files_processed"),
                              "total_bytes": summary.get("total_bytes_processed"),
                              "duration": round(time.time() - started)})
        if rc == 3:
            _alert(t, "warning", f"Backup '{t['name']}' finished with warnings", "Some files could not be read. See the backup log.")
    except Exception as e:
        write_status(tid, state="failed", finished=int(time.time()), progress=None, error=str(e))
        _alert(t, "critical", f"Backup '{t['name']}' failed", str(e))
    finally:
        _unmount_if_ours(t)


def do_check(tid: str) -> None:
    t = get_task(tid)
    write_status(tid, check={"state": "running", "started": int(time.time())})
    try:
        r = restic(t, "check", "--read-data-subset=5%", log=True, timeout=24 * 3600)
        ok = r.returncode == 0
        write_status(tid, check={"state": "ok" if ok else "failed", "finished": int(time.time()),
                                 "error": None if ok else _tail_error(tid)})
        if not ok:
            _alert(t, "critical", f"Backup '{t['name']}' failed its integrity check", _tail_error(tid) or "")
    finally:
        _unmount_if_ours(t)


def _tail_error(tid: str) -> str:
    try:
        lines = [ln.strip() for ln in open(_log_file(tid)) if ln.strip() and not ln.startswith("$")]
        errs = [ln for ln in lines if re.search(r"error|fatal|denied|unable|failed|wrong password", ln, re.I)]
        return (errs or lines or [""])[-1][:500]
    except OSError:
        return ""


def _unmount_if_ours(t: dict) -> None:
    """Unmount a USB drive after the job so it can be unplugged safely."""
    if DEV or t["dest"]["type"] != "local" or not t["dest"].get("uuid"):
        return
    mp = os.path.join(MEDIA, f"cyn-{t['dest']['uuid']}")
    if os.path.ismount(mp):
        subprocess.run(["umount", mp], capture_output=True)


def _alert(t: dict, level: str, title: str, detail: str) -> None:
    from . import health
    health._log([{"level": level, "title": title, "detail": detail, "event": "backup", "time": int(time.time())}])
    cfg = health.load_smtp()
    if cfg.get("enabled") and cfg.get("host"):
        try:
            health.send_mail(title, detail, cfg)
        except Exception:
            pass


def start(tid: str, action: str = "run") -> None:
    """Start a run in its own systemd unit (dev mode: in-process)."""
    if DEV:
        (do_backup if action == "run" else do_check)(tid)
        return
    unit = f"cyn-backup-{tid}" if action == "run" else f"cyn-backup-check-{tid}"
    r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
    if r.stdout.strip() in ("active", "activating"):
        raise HTTPException(409, "already running")
    run(["systemd-run", f"--unit={unit}", "--collect", "--property=Nice=10", "--property=IOSchedulingClass=idle",
         f"--setenv=CYN_STATE={STATE_DIR}", "--working-directory=/opt/cynthology/backend",
         "/usr/bin/python3", "-m", "cynthology.backup", action, tid])


# ---------- schedule ----------

def next_run(t: dict, now: datetime.datetime | None = None) -> datetime.datetime | None:
    sch = t.get("schedule") or {}
    if not sch.get("enabled") or not sch.get("days"):
        return None
    now = now or datetime.datetime.now()
    hh, mm = map(int, sch.get("time", "02:00").split(":"))
    for add in range(8):
        day = (now + datetime.timedelta(days=add)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if day.weekday() in sch["days"] and day > now:
            return day
    return None


def due(now: datetime.datetime | None = None) -> list[str]:
    """Task ids whose scheduled time passed since their last start (checked every 5 minutes)."""
    now = now or datetime.datetime.now()
    out = []
    for t in load_tasks():
        sch = t.get("schedule") or {}
        if not sch.get("enabled"):
            continue
        hh, mm = map(int, sch.get("time", "02:00").split(":"))
        today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now.weekday() not in sch.get("days", []) or now < today or now - today > datetime.timedelta(hours=12):
            continue
        last = read_status(t["id"]).get("started") or 0
        if last < today.timestamp():
            out.append(t["id"])
    return out


# ---------- API ----------

class Dest(BaseModel):
    type: str                      # local | sftp | s3 | rest
    path: str = ""                 # folder inside the destination
    uuid: str = ""                 # local: USB drive filesystem UUID (empty = a folder on this NAS)
    host: str = ""
    port: int = 22
    user: str = ""
    endpoint: str = ""
    bucket: str = ""
    region: str = ""
    access_key: str = ""
    secret_key: str | None = None  # None = keep stored
    url: str = ""


class Task(BaseModel):
    name: str
    sources: list[str]
    dest: Dest
    schedule: dict = {"enabled": True, "time": "02:00", "days": [0, 1, 2, 3, 4, 5, 6]}
    retention: dict = {"daily": 7, "weekly": 4, "monthly": 12}
    exclude: list[str] = []
    password: str | None = None    # encryption password; required on create


def _validate(body: Task) -> None:
    if not body.name.strip():
        raise HTTPException(400, "give the task a name")
    if not body.sources:
        raise HTTPException(400, "choose at least one shared folder")
    names = {s["name"] for s in shares_mod.load()}
    if any(s not in names for s in body.sources):
        raise HTTPException(400, "unknown shared folder")
    d = body.dest
    if d.type == "sftp":
        try:
            repo_and_env({"id": "x", "dest": d.model_dump()})
        except RuntimeError as e:
            raise HTTPException(400, str(e))
    if d.type == "local" and not d.uuid and not d.path.startswith("/"):
        raise HTTPException(400, "choose a USB drive or enter a full folder path (e.g. /volume2/backups)")
    if d.type == "local" and not d.uuid:
        real = os.path.realpath(d.path)
        for p in source_paths({"sources": body.sources}):
            if (real + "/").startswith(os.path.realpath(p) + "/"):
                raise HTTPException(400, "the destination can't be inside a folder that is being backed up")
    if d.type == "sftp" and not (d.host and d.user):
        raise HTTPException(400, "SFTP needs a host and user")
    if d.type == "s3" and not (d.bucket and d.access_key):
        raise HTTPException(400, "S3 needs a bucket and access key")
    if d.type == "rest" and not d.url.startswith(("http://", "https://")):
        raise HTTPException(400, "REST server URL must start with http:// or https://")
    if d.type not in ("local", "sftp", "s3", "rest"):
        raise HTTPException(400, "unknown destination type")
    if not re.fullmatch(r"\d{1,2}:\d{2}", body.schedule.get("time", "02:00")):
        raise HTTPException(400, "time must be HH:MM")


def _public(t: dict) -> dict:
    d = {k: v for k, v in t["dest"].items() if k != "secret_key"}
    d["has_secret"] = bool(t["dest"].get("secret_key"))
    st = read_status(t["id"])
    nr = next_run(t)
    return {**t, "dest": d, "status": st, "next_run": int(nr.timestamp()) if nr else None}


@router.get("")
def list_tasks(_=Depends(require_admin)):
    return [_public(t) for t in load_tasks()]


@router.post("")
def create(body: Task, _=Depends(require_admin)):
    _validate(body)
    if not body.password or len(body.password) < 8:
        raise HTTPException(400, "the encryption password must be at least 8 characters")
    tid = secrets.token_hex(4)
    t = body.model_dump(exclude={"password"})
    t["id"] = tid
    t["dest"]["secret_key"] = body.dest.secret_key or ""
    fd = os.open(_pw_file(tid), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body.password)
    tasks = load_tasks()
    tasks.append(t)
    save_tasks(tasks)
    return _public(t)


@router.put("/{tid}")
def update(tid: str, body: Task, _=Depends(require_admin)):
    _validate(body)
    tasks = load_tasks()
    t = next((x for x in tasks if x["id"] == tid), None)
    if not t:
        raise HTTPException(404, "no such backup task")
    secret = t["dest"].get("secret_key", "")
    t.update(body.model_dump(exclude={"password", "dest"}))
    t["dest"] = body.dest.model_dump()
    t["dest"]["secret_key"] = body.dest.secret_key if body.dest.secret_key is not None else secret
    save_tasks(tasks)
    return _public(t)


@router.delete("/{tid}")
def delete(tid: str, _=Depends(require_admin)):
    """Removes the task only; the backup data at the destination is left alone."""
    get_task(tid)
    save_tasks([t for t in load_tasks() if t["id"] != tid])
    for f in (_status_file(tid), _log_file(tid), _pw_file(tid), os.path.join(DIR, f"{tid}.lock")):
        if os.path.exists(f):
            os.remove(f)
    return {"ok": True}


@router.post("/{tid}/run")
def run_now(tid: str, _=Depends(require_admin)):
    get_task(tid)
    start(tid, "run")
    return {"ok": True}


@router.post("/{tid}/check")
def check(tid: str, _=Depends(require_admin)):
    get_task(tid)
    start(tid, "check")
    return {"ok": True}


@router.get("/{tid}/log")
def log(tid: str, _=Depends(require_admin)):
    get_task(tid)
    try:
        with open(_log_file(tid)) as f:
            return {"log": f.read()[-20000:]}
    except OSError:
        return {"log": ""}


@router.get("/{tid}/password")
def password(tid: str, _=Depends(require_admin)):
    """Shown on request so the admin can store it safely (it is needed for disaster recovery)."""
    get_task(tid)
    with open(_pw_file(tid)) as f:
        return {"password": f.read()}


@router.post("/test")
def test_destination(body: Task, _=Depends(require_admin)):
    """Try to reach the destination (and open the repository if it already exists)."""
    t = body.model_dump()
    t["id"] = "test-" + secrets.token_hex(3)
    if body.dest.secret_key is None:   # editing: reuse the stored secret
        stored = next((x for x in load_tasks() if x["dest"].get("bucket") == body.dest.bucket), None)
        t["dest"]["secret_key"] = stored["dest"].get("secret_key", "") if stored else ""
    pw = _pw_file(t["id"])
    with open(pw, "w") as f:
        f.write(body.password or "probe-only")
    try:
        r = restic(t, "cat", "config", timeout=60)
        out = (r.stderr or "") + (r.stdout or "")
        if r.returncode == 0:
            return {"ok": True, "message": "Connected - an existing backup repository was found and the password works."}
        if re.search(r"wrong password|no key found", out, re.I):
            return {"ok": True, "message": "Connected - a backup repository exists here but the password does not match it."}
        if re.search(r"does not exist|no such file|unable to open config|NoSuchKey|404|Is there a repository", out, re.I):
            return {"ok": True, "message": "Connected - a new backup repository will be created on the first run."}
        raise HTTPException(400, "could not reach the destination: " + _err(r))
    except subprocess.TimeoutExpired:
        raise HTTPException(400, "timed out reaching the destination")
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        os.remove(pw)
        _unmount_if_ours(t)


@router.get("/drives/usb")
def drives(_=Depends(require_admin)):
    return usb_drives()


@router.get("/sshkey")
def sshkey(_=Depends(require_admin)):
    """Public key to add to ~/.ssh/authorized_keys on the SFTP backup target."""
    if not os.path.exists(SSH_KEY):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"cynthology-backup@{socket.gethostname()}",
                        "-f", SSH_KEY], check=True)
    with open(SSH_KEY + ".pub") as f:
        return {"public_key": f.read().strip()}


# ----- restore -----

@router.get("/{tid}/versions")
def versions(tid: str, _=Depends(require_admin)):
    t = get_task(tid)
    try:
        r = restic(t, "snapshots", "--tag", f"cyn-{tid}", json_out=True, timeout=300)
    finally:
        _unmount_if_ours(t)
    if r.returncode != 0:
        raise HTTPException(400, _err(r))
    snaps = json.loads(r.stdout or "[]")
    return [{"id": s["short_id"], "time": s["time"], "paths": s.get("paths", []),
             "files": (s.get("summary") or {}).get("total_files_processed"),
             "bytes": (s.get("summary") or {}).get("total_bytes_processed")} for s in reversed(snaps)]


@router.get("/{tid}/browse")
def browse(tid: str, version: str, path: str = "/", _=Depends(require_admin)):
    t = get_task(tid)
    if not re.fullmatch(r"[0-9a-f]{8,64}|latest", version):
        raise HTTPException(400, "bad version")
    path = "/" + path.strip("/")
    try:
        r = restic(t, "ls", version, path, json_out=True, timeout=300)
    finally:
        _unmount_if_ours(t)
    if r.returncode != 0:
        raise HTTPException(400, _err(r))
    items = []
    for line in r.stdout.splitlines():
        try:
            n = json.loads(line)
        except ValueError:
            continue
        if n.get("struct_type") != "node" or n.get("path") == path:
            continue
        if os.path.dirname(n["path"].rstrip("/")) != path.rstrip("/") and not (path == "/" and n["path"].count("/") == 1):
            continue
        items.append({"name": n["name"], "path": n["path"], "dir": n["type"] == "dir", "size": n.get("size", 0),
                      "mtime": n.get("mtime")})
    items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
    return items


class Restore(BaseModel):
    version: str
    paths: list[str]             # paths inside the backup (from /browse); empty = everything
    to: str = "new"              # "new" = <share>/Restored <date>/..., "original" = overwrite in place
    share: str = ""              # with to=new: the shared folder to restore into


@router.post("/{tid}/restore")
def restore(tid: str, body: Restore, _=Depends(require_admin)):
    t = get_task(tid)
    if not re.fullmatch(r"[0-9a-f]{8,64}|latest", body.version):
        raise HTTPException(400, "bad version")
    for p in body.paths:
        if ".." in p.split("/") or not p.startswith("/"):
            raise HTTPException(400, "bad path")
    source = body.version
    includes = [a for p in body.paths for a in ("--include", p)]
    if body.to == "original":
        target = "/"
    else:
        share = next((s for s in shares_mod.load() if s["name"] == body.share), None)
        if not share:
            raise HTTPException(400, "choose a shared folder to restore into")
        target = os.path.join(share["path"], "Restored " + datetime.datetime.now().strftime("%Y-%m-%d %H.%M.%S"))
        os.makedirs(target, exist_ok=True)
        # restore relative to the common parent ("version:/dir" needs restic >= 0.17), so the result
        # is "Restored <date>/photos/..." rather than "Restored <date>/volume1/family/photos/..."
        if body.paths:
            parent = os.path.commonpath([os.path.dirname(p.rstrip("/")) for p in body.paths])
            if parent != "/":
                source = f"{body.version}:{parent}"
                includes = [a for p in body.paths for a in ("--include", "/" + os.path.relpath(p, parent))]
    try:
        r = restic(t, "restore", source, "--target", target, *includes, log=True, timeout=24 * 3600)
    finally:
        _unmount_if_ours(t)
    if r.returncode != 0:
        raise HTTPException(500, "restore failed: " + (_tail_error(tid) or _err(r)))
    return {"ok": True, "target": target}


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "due"
    if action == "run":
        do_backup(sys.argv[2])
    elif action == "check":
        do_check(sys.argv[2])
    elif action == "due":
        for tid in due():
            start(tid, "run")
            print("started", tid)
