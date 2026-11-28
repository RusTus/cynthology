"""Snapshot replication: copy shared-folder (or iSCSI LUN) snapshots to another Cynthology NAS
with btrfs send/receive over SSH (incremental after the first run). LUN replicas are stored on
the receiver as "lun.<name>" and can be turned into a LUN there (SAN Manager).

Sender  (this NAS has the share): a task takes a snapshot, asks the receiver which snapshots it
        already has, streams `btrfs send [-p <newest common>] <new>` over SSH, then prunes.
Receiver (the other NAS): an administrator pastes the sender's public key. It is installed for the
        system user `cynrepl` with a forced command, so the key can only run the receive helper:
            command="sudo -n /usr/lib/cynthology/repl-receive <source> \"$SSH_ORIGINAL_COMMAND\"",restrict
        Replicas are stored read-only in <volume>/@cyn-replicas/<source>/<share>/@GMT-... and can be
        turned into a normal writable shared folder (failover) with one click.
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
from . import snapshots
from .auth import require_admin
from .storage import load_pools
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/replication", tags=["replication"])
DIR = os.path.join(STATE_DIR, "repl")
TASKS = os.path.join(DIR, "tasks.json")
KEY = os.path.join(DIR, "id_ed25519")
KNOWN = os.path.join(DIR, "known_hosts")
RECV_USER = "cynrepl"
RECV_HOME = "/var/lib/cynrepl"
AUTH_KEYS = os.path.join(STATE_DIR, "cynrepl-authorized_keys") if DEV else f"{RECV_HOME}/.ssh/authorized_keys"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
SRC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\Z")
INTERVAL_H = {"hourly": 1, "daily": 24, "weekly": 24 * 7}
os.makedirs(DIR, mode=0o700, exist_ok=True)


# ======================= receiver side =======================

def replica_root() -> str:
    pools = load_pools()
    if not pools:
        raise RuntimeError("this NAS has no storage pool to receive replicas on")
    vol = pools[0]["mount"]
    return os.path.join(STATE_DIR, "root", vol.lstrip("/"), "@cyn-replicas") if DEV else os.path.join(vol, "@cyn-replicas")


def _rdir(source: str, share: str) -> str:
    if not SRC_RE.match(source) or not NAME_RE.match(share):
        raise RuntimeError("bad source or share name")
    return os.path.join(replica_root(), source, share)


def serve(source: str, command: str) -> int:
    """Entry point of the forced SSH command (runs as root via sudo). Returns exit code."""
    args = command.split()
    if not args:
        print("no command", file=sys.stderr)
        return 2
    op = args[0]
    try:
        if op == "hello":
            print(json.dumps({"host": socket.gethostname(), "root": replica_root()}))
        elif op == "list" and len(args) == 2:
            d = _rdir(source, args[1])
            names = sorted(n for n in os.listdir(d) if snapshots.NAME_RE.match(n)) if os.path.isdir(d) else []
            print(json.dumps(names))
        elif op == "receive" and len(args) == 2:
            d = _rdir(source, args[1])
            os.makedirs(d, exist_ok=True)
            before = set(os.listdir(d))
            # btrfs receive reads the stream from stdin and creates the read-only subvolume in d.
            # The stream comes from another machine: --chroot confines every path it contains to d.
            rc = subprocess.run(["btrfs", "receive", "--chroot", "-e", d], stdin=sys.stdin.buffer).returncode
            for n in set(os.listdir(d)) - before:          # only snapshot-named subvolumes are kept
                if not snapshots.NAME_RE.match(n):
                    subprocess.run(["btrfs", "subvolume", "delete", os.path.join(d, n)], capture_output=True)
                    print(f"rejected unexpected item {n!r}", file=sys.stderr)
                    rc = rc or 3
            return rc
        elif op == "prune" and len(args) == 3 and args[2].isdigit():
            d = _rdir(source, args[1])
            keep = int(args[2])
            names = sorted(n for n in os.listdir(d) if snapshots.NAME_RE.match(n)) if os.path.isdir(d) else []
            for n in names[:-keep] if keep > 0 else []:
                subprocess.run(["btrfs", "subvolume", "delete", os.path.join(d, n)], capture_output=True)
            print(json.dumps({"deleted": max(0, len(names) - keep) if keep > 0 else 0}))
        else:
            print(f"unknown command: {command}", file=sys.stderr)
            return 2
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _read_auth_keys() -> list[dict]:
    out = []
    try:
        lines = open(AUTH_KEYS).read().splitlines()
    except OSError:
        return out
    for line in lines:
        m = re.match(r'^command="sudo -n /usr/lib/cynthology/repl-receive ([a-z0-9-]+) .*?",restrict (ssh-\S+ \S+)(?: (.*))?$', line)
        if m:
            out.append({"source": m.group(1), "key": m.group(2), "comment": m.group(3) or ""})
    return out


def _write_auth_keys(entries: list[dict]) -> None:
    lines = [f'command="sudo -n /usr/lib/cynthology/repl-receive {e["source"]} \\"$SSH_ORIGINAL_COMMAND\\"",restrict '
             f'{e["key"]} {e["comment"]}'.rstrip() for e in entries]
    os.makedirs(os.path.dirname(AUTH_KEYS), exist_ok=True)
    with open(AUTH_KEYS + ".tmp", "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    os.replace(AUTH_KEYS + ".tmp", AUTH_KEYS)
    if not DEV:
        run(["chown", f"{RECV_USER}:{RECV_USER}", AUTH_KEYS, os.path.dirname(AUTH_KEYS)])
        os.chmod(AUTH_KEYS, 0o600)
        os.chmod(os.path.dirname(AUTH_KEYS), 0o700)


class Sender(BaseModel):
    source: str        # a short name for the sending NAS, e.g. "nas1"
    public_key: str    # its replication key (shown on the sender)


@router.get("/senders")
def senders(_=Depends(require_admin)):
    return [{"source": e["source"], "comment": e["comment"], "fingerprint": e["key"][-16:]} for e in _read_auth_keys()]


@router.post("/senders")
def add_sender(body: Sender, _=Depends(require_admin)):
    src = body.source.strip().lower()
    if not SRC_RE.match(src):
        raise HTTPException(400, "name: lowercase letters, digits and '-' only")
    m = re.match(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) ([A-Za-z0-9+/=]+)(?: (\S+))?$", body.public_key.strip())
    if not m:
        raise HTTPException(400, "that doesn't look like an SSH public key (it starts with ssh-ed25519 ...)")
    entries = [e for e in _read_auth_keys() if e["source"] != src and e["key"] != f"{m.group(1)} {m.group(2)}"]
    entries.append({"source": src, "key": f"{m.group(1)} {m.group(2)}", "comment": m.group(3) or ""})
    _write_auth_keys(entries)
    return {"ok": True}


@router.delete("/senders/{source}")
def remove_sender(source: str, _=Depends(require_admin)):
    _write_auth_keys([e for e in _read_auth_keys() if e["source"] != source])
    return {"ok": True}


@router.get("/received")
def received(_=Depends(require_admin)):
    out = []
    try:
        root = replica_root()
    except RuntimeError:
        return out
    for src in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        for share in sorted(os.listdir(os.path.join(root, src))):
            d = os.path.join(root, src, share)
            snaps = sorted(n for n in os.listdir(d) if snapshots.NAME_RE.match(n))
            out.append({"source": src, "share": share, "count": len(snaps), "snapshots": list(reversed(snaps)),
                        "latest": snaps[-1] if snaps else None,
                        "lun": share[4:] if share.startswith("lun.") else None})
    return out


class Failover(BaseModel):
    source: str
    share: str
    snapshot: str
    new_name: str       # name of the new, writable shared folder
    replace: bool = False   # failback: put the replica in place of the existing shared folder of that name


@router.post("/failover")
def failover(body: Failover, _=Depends(require_admin)):
    """Make a replica usable: writable copy (instant, space-sharing) registered as a shared folder."""
    try:
        d = _rdir(body.source, body.share)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    if not snapshots.NAME_RE.match(body.snapshot) or not os.path.isdir(os.path.join(d, body.snapshot)):
        raise HTTPException(404, "no such replica")
    existing = next((s for s in shares_mod.load() if s["name"].lower() == body.new_name.lower()), None)
    if existing and body.replace:
        return _replace_live(existing, os.path.join(d, body.snapshot), body)
    if not NAME_RE.match(body.new_name) or existing:
        raise HTTPException(400, "choose a new, unused shared folder name")
    pool = load_pools()[0]
    target = os.path.join(pool["mount"], body.new_name)
    if DEV:
        target = os.path.join(STATE_DIR, "root", pool["mount"].lstrip("/"), body.new_name)
        import shutil
        shutil.copytree(os.path.join(d, body.snapshot), target)
    else:
        run(["btrfs", "subvolume", "snapshot", os.path.join(d, body.snapshot), target])
        run(["chgrp", "users", target])
        run(["chmod", "2775", target])
    items = shares_mod.load()
    items.append({"name": body.new_name, "pool_id": pool["id"], "path": target, "smb": True, "nfs": False,
                  "nfs_clients": "192.168.0.0/16", "recycle_bin": True, "read_users": [], "write_users": ["@users"],
                  "description": f"Replica of {body.share} from {body.source} ({body.snapshot})"})
    shares_mod.write_configs(items)
    return {"ok": True, "share": body.new_name, "path": target}


def _replace_live(share: dict, replica: str, body) -> dict:
    """Failback: the replica (the data as it was on the other NAS) becomes this shared folder again. The
    folder as it was here is kept beside it as '<name>.before-failback-<date>' (nothing is deleted)."""
    if share.get("homes") or share.get("encrypted") or share.get("usb"):
        raise HTTPException(400, "this kind of shared folder can't be replaced by a replica")
    path = share["path"]
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    aside = f"{path}.before-failback-{stamp}"
    if DEV:
        import shutil
        os.rename(path, aside)
        shutil.copytree(replica, path)
    else:
        if not snapshots.supported(share):
            raise HTTPException(400, "the shared folder isn't a Btrfs subvolume")
        os.rename(path, aside)
        try:
            run(["btrfs", "subvolume", "snapshot", replica, path])
        except Exception:
            os.rename(aside, path)           # (put it back as it was)
            raise
        run(["chgrp", "users", path])
        run(["chmod", "2775", path])
        # (the old version's snapshots live inside it and moved along with it; new ones start here)
        os.makedirs(os.path.join(path, snapshots.SNAPDIR), exist_ok=True)
    shares_mod.write_configs(shares_mod.load())
    from .notify import notify
    notify("info", f"Shared folder '{share['name']}' was replaced by its replica from {body.source}",
           f"The previous contents are kept in {aside}.", event="replication")
    return {"ok": True, "share": share["name"], "path": path, "kept": aside}


# ---------- watching the primary NAS (this NAS receives its replicas) ----------

WATCH = os.path.join(DIR, "watch.json")


def _watch() -> dict:
    try:
        return json.load(open(WATCH))
    except (OSError, ValueError):
        return {}


class Watch(BaseModel):
    max_age_h: int = 26          # alert when no new replica for this long (0 = don't)
    address: str = ""            # the primary NAS's address: alert when it stops answering
    port: int = 5001


@router.get("/watch")
def get_watch(_=Depends(require_admin)):
    return _watch()


@router.put("/watch/{source}")
def put_watch(source: str, body: Watch, _=Depends(require_admin)):
    if not SRC_RE.match(source):
        raise HTTPException(400, "bad source")
    if not 0 <= body.max_age_h <= 24 * 60 or not 1 <= body.port <= 65535:
        raise HTTPException(400, "invalid value")
    addr = body.address.strip()
    if addr and not re.fullmatch(r"[A-Za-z0-9.:\[\]-]{1,253}", addr):
        raise HTTPException(400, "address: a host name or IP address")
    w = _watch()
    w[source] = {"max_age_h": body.max_age_h, "address": addr, "port": body.port, "down_since": None}
    with open(WATCH, "w") as f:
        json.dump(w, f)
    return w[source]


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host.strip("[]"), port), timeout=5):
            return True
    except OSError:
        return False


def health_issues(now: float | None = None) -> dict:
    """For health.collect(): replicas that stopped arriving, primaries that stopped answering."""
    now = now or time.time()
    issues, w = {}, _watch()
    try:
        recv = received(None)
    except Exception:
        recv = []
    changed = False
    for src, cfg in w.items():
        mine = [r for r in recv if r["source"] == src and r["latest"]]
        if cfg.get("max_age_h") and mine:
            newest = max(snapshots.snap_time(r["latest"]).timestamp() for r in mine)
            age_h = (now - newest) / 3600
            if age_h > cfg["max_age_h"]:
                issues[f"repl-{src}-stale"] = {
                    "level": "warning", "title": f"No new replica from {src} for {int(age_h)} hours",
                    "detail": f"The newest copy of {src}'s shared folders here is from {int(age_h)} hours ago. "
                              f"Check {src} and its replication task."}
        if cfg.get("address"):
            up = DEV or _reachable(cfg["address"], int(cfg.get("port") or 5001))
            if up and cfg.get("down_since"):
                cfg["down_since"], changed = None, True
            elif not up:
                if not cfg.get("down_since"):
                    cfg["down_since"], changed = int(now), True
                elif now - cfg["down_since"] >= 15 * 60:            # (2 checks in a row: not just a blip)
                    issues[f"repl-{src}-down"] = {
                        "level": "critical", "title": f"{src} isn't answering",
                        "detail": f"{src} ({cfg['address']}) hasn't answered for {int((now - cfg['down_since']) / 60)} minutes. "
                                  f"If it's down, its shared folders can be brought up here from the latest replica: "
                                  f"Snapshots > Replication > Receive > Use as shared folder (same name)."}
    if changed:
        with open(WATCH, "w") as f:
            json.dump(w, f)
    return issues


class DeleteReplicas(BaseModel):
    source: str
    share: str


@router.post("/received/delete")
def delete_replicas(body: DeleteReplicas, _=Depends(require_admin)):
    try:
        d = _rdir(body.source, body.share)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    for n in os.listdir(d) if os.path.isdir(d) else []:
        if snapshots.NAME_RE.match(n):
            run(["btrfs", "subvolume", "delete", os.path.join(d, n)])
    if not DEV and os.path.isdir(d):
        os.rmdir(d)
    return {"ok": True}


# ======================= sender side =======================

def load_tasks() -> list[dict]:
    try:
        with open(TASKS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_tasks(tasks: list[dict]) -> None:
    with open(TASKS + ".tmp", "w") as f:
        json.dump(tasks, f, indent=2)
    os.replace(TASKS + ".tmp", TASKS)


def get_task(tid: str) -> dict:
    t = next((t for t in load_tasks() if t["id"] == tid), None)
    if not t:
        raise HTTPException(404, "no such replication task")
    return t


def _status_file(tid): return os.path.join(DIR, f"{tid}.status.json")
def _log_file(tid): return os.path.join(DIR, f"{tid}.log")


def read_status(tid: str) -> dict:
    try:
        with open(_status_file(tid)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"state": "never"}


def write_status(tid: str, **v) -> None:
    s = read_status(tid)
    s.update(v)
    with open(_status_file(tid), "w") as f:
        json.dump(s, f)


def ensure_key() -> str:
    if not os.path.exists(KEY):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"cynthology-replication@{socket.gethostname()}",
                        "-f", KEY], check=True)
    with open(KEY + ".pub") as f:
        return f.read().strip()


def ssh_cmd(t: dict, remote: str) -> list[str]:
    return ["ssh", "-i", KEY, "-p", str(int(t.get("port") or 22)), "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={KNOWN}",
            "-o", "ServerAliveInterval=30", "-o", "ConnectTimeout=15", f"{RECV_USER}@{t['host']}", remote]


def remote(t: dict, command: str, timeout: int = 60) -> str:
    r = subprocess.run(ssh_cmd(t, command), capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip().splitlines()
        raise RuntimeError(err[-1] if err else f"ssh failed ({r.returncode})")
    return r.stdout


def do_replicate(tid: str) -> None:
    t = get_task(tid)
    lock = open(os.path.join(DIR, f"{tid}.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    started = time.time()
    write_status(tid, state="running", started=int(started), error=None)
    log = open(_log_file(tid), "w")
    try:
        if t.get("lun"):
            from . import san
            lun = next((l for l in san.load()["luns"] if l["name"] == t["lun"]), None)
            if not lun:
                raise RuntimeError(f"LUN {t['lun']} no longer exists")
            new = san.take_lun_snapshot(lun)
            local = san.list_lun_snaps(lun)
            snapdir, rname = san.snap_dir(lun), f"lun.{t['lun']}"
        else:
            share = next((s for s in shares_mod.load() if s["name"] == t["share"]), None)
            if not share:
                raise RuntimeError(f"shared folder {t['share']} no longer exists")
            new = snapshots.take(share)
            local = snapshots.list_snaps(share)
            snapdir, rname = os.path.join(share["path"], snapshots.SNAPDIR), t["share"]
        have = json.loads(remote(t, f"list {rname}") or "[]")
        common = [n for n in local if n in have and n != new]
        parent = common[-1] if common else None
        send = ["btrfs", "send", *(["-p", os.path.join(snapdir, parent)] if parent else []), os.path.join(snapdir, new)]
        log.write(f"$ {shlex.join(send)} | ssh {t['host']} receive {rname}\n")
        log.flush()
        sender = subprocess.Popen(send, stdout=subprocess.PIPE, stderr=log)
        recv = subprocess.run(ssh_cmd(t, f"receive {rname}"), stdin=sender.stdout, stdout=log, stderr=log, timeout=7 * 24 * 3600)
        sender.stdout.close()
        if sender.wait() != 0 or recv.returncode != 0:
            raise RuntimeError(_last_error(tid) or "send/receive failed")
        if int(t.get("keep") or 0) > 0:
            remote(t, f"prune {rname} {int(t['keep'])}")
        write_status(tid, state="success", finished=int(time.time()), last_snapshot=new,
                     incremental=bool(parent), duration=round(time.time() - started))
    except Exception as e:
        write_status(tid, state="failed", finished=int(time.time()), error=str(e))
        from .backup import _alert
        what = f"LUN {t['lun']}" if t.get("lun") else t["share"]
        _alert({"name": what}, "critical", f"Replication of '{what}' to {t['host']} failed", str(e))
    finally:
        log.close()


def _last_error(tid: str) -> str:
    try:
        lines = [ln.strip() for ln in open(_log_file(tid)) if ln.strip() and not ln.startswith("$")]
        return (lines or [""])[-1][:400]
    except OSError:
        return ""


def start(tid: str) -> None:
    if DEV:
        do_replicate(tid)
        return
    unit = f"cyn-repl-{tid}"
    if subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip() in ("active", "activating"):
        raise HTTPException(409, "already running")
    run(["systemd-run", f"--unit={unit}", "--collect", "--property=Nice=10", f"--setenv=CYN_STATE={STATE_DIR}",
         "--working-directory=/opt/cynthology/backend", "/usr/bin/python3", "-m", "cynthology.repl", "run", tid])


def due(now: float | None = None) -> list[str]:
    now = now or time.time()
    out = []
    for t in load_tasks():
        hours = INTERVAL_H.get(t.get("schedule") or "manual")
        if not hours:
            continue
        last = read_status(t["id"]).get("started") or 0
        if now - last >= hours * 3600 - 300:
            out.append(t["id"])
    return out


class Task(BaseModel):
    share: str = ""              # a shared folder ...
    lun: str = ""                # ... or an iSCSI LUN
    host: str
    port: int = 22
    schedule: str = "daily"      # manual | hourly | daily | weekly
    keep: int = 30               # replicas kept on the receiver (0 = all)


def _validate(body: Task) -> None:
    if body.lun:
        from . import san
        lun = next((l for l in san.load()["luns"] if l["name"] == body.lun), None)
        if not lun:
            raise HTTPException(400, "unknown LUN")
        if not san.snaps_supported(lun):
            raise HTTPException(400, "replication needs the LUN to be on a Btrfs pool")
        body.share = ""
    elif not any(s["name"] == body.share for s in shares_mod.load()):
        raise HTTPException(400, "unknown shared folder")
    if not re.fullmatch(r"[A-Za-z0-9.:-]+", body.host):
        raise HTTPException(400, "enter the other NAS's name or IP address")
    if body.schedule not in ("manual", *INTERVAL_H):
        raise HTTPException(400, "bad schedule")
    if not body.lun and not snapshots.supported(next(s for s in shares_mod.load() if s["name"] == body.share)):
        raise HTTPException(400, "replication needs the shared folder to be on a Btrfs volume")


@router.get("/key")
def key(_=Depends(require_admin)):
    return {"public_key": ensure_key(), "hostname": socket.gethostname()}


@router.get("/tasks")
def tasks(_=Depends(require_admin)):
    return [{**t, "status": read_status(t["id"])} for t in load_tasks()]


@router.post("/tasks")
def create(body: Task, _=Depends(require_admin)):
    _validate(body)
    t = {"id": secrets.token_hex(4), **body.model_dump()}
    ts = load_tasks()
    ts.append(t)
    save_tasks(ts)
    return t


@router.put("/tasks/{tid}")
def update(tid: str, body: Task, _=Depends(require_admin)):
    _validate(body)
    ts = load_tasks()
    t = next((x for x in ts if x["id"] == tid), None)
    if not t:
        raise HTTPException(404, "no such task")
    t.update(body.model_dump())
    save_tasks(ts)
    return t


@router.delete("/tasks/{tid}")
def delete(tid: str, _=Depends(require_admin)):
    save_tasks([t for t in load_tasks() if t["id"] != tid])
    for f in (_status_file(tid), _log_file(tid)):
        if os.path.exists(f):
            os.remove(f)
    return {"ok": True}


@router.post("/tasks/{tid}/run")
def run_now(tid: str, _=Depends(require_admin)):
    get_task(tid)
    start(tid)
    return {"ok": True}


class Test(BaseModel):
    host: str
    port: int = 22


@router.post("/test")
def test(body: Test, _=Depends(require_admin)):
    ensure_key()
    try:
        info = json.loads(remote({"host": body.host, "port": body.port}, "hello", timeout=30))
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
        msg = str(e)
        if "Permission denied" in msg:
            msg = "the other NAS doesn't accept this NAS yet - add this NAS's key there (Snapshots → Receive)"
        raise HTTPException(400, msg)
    return {"ok": True, "message": f"Connected to {info['host']} - replicas will be stored in {info['root']}"}


@router.get("/tasks/{tid}/log")
def log(tid: str, _=Depends(require_admin)):
    try:
        return {"log": open(_log_file(tid)).read()[-20000:]}
    except OSError:
        return {"log": ""}


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"] and len(sys.argv) == 4:
        sys.exit(serve(sys.argv[2], sys.argv[3]))
    elif sys.argv[1:2] == ["run"]:
        do_replicate(sys.argv[2])
    elif sys.argv[1:2] == ["due"]:
        for tid in due():
            start(tid)
            print("started", tid)
