"""Btrfs snapshots of shared folders: on demand and on a schedule, with retention.

Snapshots live in <share>/.snapshots/@GMT-YYYY.MM.DD-HH.MM.SS (UTC). That name is Samba's
shadow_copy2 default, so Windows shows them under "Previous Versions" on the SMB share.
A systemd timer runs `python3 -m cynthology.snapshots` hourly to take due snapshots and prune.
"""
import datetime
import json
import os
import time
import re
import subprocess

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import shares as shares_mod
from .auth import current_user, require_admin
from .util import DEV, STATE_DIR, read_only, run

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])
SNAPDIR = ".snapshots"
FMT = "@GMT-%Y.%m.%d-%H.%M.%S"
NAME_RE = re.compile(r"^@GMT-\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}\Z")
INTERVAL_H = {"hourly": 1, "daily": 24, "weekly": 24 * 7}
LOCKS = os.path.join(STATE_DIR, "snapshot-locks.json")


# ---------- immutable snapshots ----------
# A shared folder can lock each new snapshot for N days: until then nothing in Cynthology deletes it -
# not the Delete button, not retention, not deleting the shared folder with its data. (The lock is
# set when the snapshot is taken; shortening the setting later doesn't shorten existing locks.)

def _locks() -> dict:
    try:
        return json.load(open(LOCKS))
    except (OSError, ValueError):
        return {}


def lock_until(share: dict, name: str) -> int | None:
    until = _locks().get(f"{share['name']}/{name}")
    return until if until and until > time.time() else None


def locked_snaps(share: dict) -> list[str]:
    return [n for n in list_snaps(share) if lock_until(share, n)] if os.path.isdir(share["path"]) else []


def _set_lock(share: dict, name: str, days: int) -> None:
    locks = {k: v for k, v in _locks().items() if v > time.time()}       # (forget expired ones)
    locks[f"{share['name']}/{name}"] = int(time.time()) + days * 86400
    with open(LOCKS + ".new", "w") as f:
        json.dump(locks, f)
    os.replace(LOCKS + ".new", LOCKS)


def _share(name: str) -> dict:
    s = next((x for x in shares_mod.load() if x["name"] == name), None)
    if not s:
        raise HTTPException(404, "no such shared folder")
    return s


def supported(share: dict) -> bool:
    if DEV:
        return True
    return bool(read_only(["btrfs", "subvolume", "show", share["path"]]))


def list_snaps(share: dict) -> list[str]:
    d = os.path.join(share["path"], SNAPDIR)
    return sorted(n for n in os.listdir(d) if NAME_RE.match(n)) if os.path.isdir(d) else []


def snap_time(name: str) -> datetime.datetime:
    return datetime.datetime.strptime(name, FMT).replace(tzinfo=datetime.timezone.utc)


def take(share: dict) -> str:
    if not supported(share):
        raise HTTPException(400, "snapshots need a Btrfs volume")
    name = datetime.datetime.now(datetime.timezone.utc).strftime(FMT)
    d = os.path.join(share["path"], SNAPDIR)
    if DEV:   # no Btrfs in dev mode: a plain copy stands in for the snapshot
        import shutil
        shutil.copytree(share["path"], os.path.join(d, name), ignore=shutil.ignore_patterns(SNAPDIR, "#recycle"))
        if int(share.get("snap_lock_days") or 0) > 0:
            _set_lock(share, name, int(share["snap_lock_days"]))
        return name
    run(["mkdir", "-p", d])
    run(["btrfs", "subvolume", "snapshot", "-r", share["path"], os.path.join(d, name)])
    for sub in _home_subvolumes(share):     # user homes are subvolumes of their own (not in the snapshot above)
        # (never let one user's full home stop the others' snapshots)
        run(["mkdir", "-p", os.path.join(sub, SNAPDIR)], check=False)
        run(["btrfs", "subvolume", "snapshot", "-r", sub, os.path.join(sub, SNAPDIR, name)], check=False)
    if int(share.get("snap_lock_days") or 0) > 0:
        _set_lock(share, name, int(share["snap_lock_days"]))
    return name


def _home_subvolumes(share: dict) -> list[str]:
    if not share.get("homes") or DEV:
        return []
    out = []
    for u in sorted(os.listdir(share["path"])):
        p = os.path.join(share["path"], u)
        if not u.startswith((".", "#", "@")) and os.path.isdir(p) and read_only(["btrfs", "subvolume", "show", p]):
            out.append(p)
    return out


def delete(share: dict, name: str) -> None:
    if not NAME_RE.match(name):
        raise HTTPException(400, "bad snapshot name")
    path = os.path.join(share["path"], SNAPDIR, name)
    if not os.path.isdir(path):
        raise HTTPException(404, "no such snapshot")
    until = lock_until(share, name)
    if until:
        raise HTTPException(423, "this snapshot is locked (immutable) until " +
                            datetime.datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M") + " - it can't be deleted before then")
    if DEV:
        os.rmdir(path)
    else:
        run(["btrfs", "subvolume", "delete", path])
        for sub in _home_subvolumes(share):
            if os.path.isdir(os.path.join(sub, SNAPDIR, name)):
                run(["btrfs", "subvolume", "delete", os.path.join(sub, SNAPDIR, name)], check=False)


def prune(share: dict) -> int:
    keep = int(share.get("snap_keep") or 0)
    snaps = list_snaps(share)
    if keep <= 0 or len(snaps) <= keep:
        return 0
    gone = 0
    for name in snaps[:-keep]:
        if lock_until(share, name):        # (locked ones stay until their lock runs out)
            continue
        delete(share, name)
        gone += 1
    return gone


def run_due(now: datetime.datetime | None = None) -> list[str]:
    """Take snapshots that are due and prune. Called hourly by cynthology-snapshots.timer."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    done = []
    for s in shares_mod.load():
        hours = INTERVAL_H.get(s.get("snap_schedule") or "off")
        if not hours or not os.path.isdir(s["path"]) or not supported(s):
            continue
        snaps = list_snaps(s)
        # 5 minutes of slack so an hourly timer doesn't skip a beat
        if not snaps or now - snap_time(snaps[-1]) >= datetime.timedelta(hours=hours, minutes=-5):
            take(s)
            done.append(s["name"])
        prune(s)
    return done


@router.get("")
def overview(_=Depends(current_user)):
    out = []
    for s in shares_mod.load():
        snaps = list_snaps(s) if os.path.isdir(s["path"]) else []
        out.append({"share": s["name"], "supported": supported(s), "schedule": s.get("snap_schedule") or "off",
                    "keep": s.get("snap_keep") or 0, "lock_days": s.get("snap_lock_days") or 0,
                    "count": len(snaps), "latest": snaps[-1] if snaps else None})
    return out


class Policy(BaseModel):
    schedule: str = "off"
    keep: int = 0
    lock_days: int = 0             # immutable for this many days after each snapshot (0 = off)


@router.put("/{share}")
def set_policy(share: str, body: Policy, _=Depends(require_admin)):
    if body.schedule not in ("off", *INTERVAL_H):
        raise HTTPException(400, "schedule must be off, hourly, daily or weekly")
    if not 0 <= body.keep <= 1000:
        raise HTTPException(400, "keep must be between 0 (keep all) and 1000")
    items = shares_mod.load()
    s = next((x for x in items if x["name"] == share), None)
    if not s:
        raise HTTPException(404, "no such shared folder")
    if not 0 <= body.lock_days <= 3650:
        raise HTTPException(400, "lock between 0 (off) and 3650 days")
    s["snap_schedule"], s["snap_keep"], s["snap_lock_days"] = body.schedule, body.keep, body.lock_days
    shares_mod.write_configs(items)
    prune(s)
    return {"ok": True}


@router.get("/{share}")
def get_snaps(share: str, _=Depends(current_user)):
    s = _share(share)
    return [{"name": n, "time": snap_time(n).isoformat(), "locked_until": lock_until(s, n)} for n in reversed(list_snaps(s))]


@router.post("/{share}")
def snapshot_now(share: str, _=Depends(require_admin)):
    s = _share(share)
    name = take(s)
    prune(s)
    return {"snapshot": name}


@router.delete("/{share}/{name}")
def delete_snap(share: str, name: str, _=Depends(require_admin)):
    delete(_share(share), name)
    return {"ok": True}


# ---------- browse & restore ----------

def _snap_root(share: dict, name: str) -> str:
    if not NAME_RE.match(name):
        raise HTTPException(400, "bad snapshot name")
    p = os.path.join(share["path"], SNAPDIR, name)
    if not os.path.isdir(p):
        raise HTTPException(404, "no such snapshot")
    return p


def _inside(root: str, rel: str) -> str:
    full = os.path.realpath(os.path.join(root, rel.strip("/")))
    if full != os.path.realpath(root) and not full.startswith(os.path.realpath(root) + os.sep):
        raise HTTPException(400, "path leaves the snapshot")
    return full


@router.get("/{share}/{name}/browse")
def browse(share: str, name: str, path: str = "", _=Depends(require_admin)):
    s = _share(share)
    root = _snap_root(s, name)
    d = _inside(root, path)
    if not os.path.isdir(d):
        raise HTTPException(404, "not a folder")
    items = []
    for n in sorted(os.listdir(d), key=str.lower):
        if n in (SNAPDIR, "#recycle"):
            continue
        full = os.path.join(d, n)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        cur = os.path.join(s["path"], path.strip("/"), n)
        items.append({"name": n, "dir": os.path.isdir(full) and not os.path.islink(full), "size": st.st_size,
                      "mtime": int(st.st_mtime), "exists_now": os.path.lexists(cur),
                      "changed": not os.path.lexists(cur) or (not os.path.isdir(full) and int(os.lstat(cur).st_mtime) != int(st.st_mtime))})
    items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
    return items


class Restore(BaseModel):
    paths: list[str]          # relative to the share root
    to: str = "original"      # original | copy


@router.post("/{share}/{name}/restore")
def restore(share: str, name: str, body: Restore, _=Depends(require_admin)):
    """Copy files/folders back out of a snapshot (copies are instant on Btrfs: reflinks)."""
    s = _share(share)
    root = _snap_root(s, name)
    if not body.paths:
        raise HTTPException(400, "choose files or folders to restore")
    if body.to == "copy":
        stamp = snap_time(name).astimezone().strftime("%Y-%m-%d %H.%M")
        dest_root = os.path.join(s["path"], f"Restored {stamp}")
    elif body.to == "original":
        dest_root = s["path"]
    else:
        raise HTTPException(400, "to must be original or copy")
    restored = 0
    for rel in body.paths:
        src = _inside(root, rel)
        if not os.path.lexists(src):
            raise HTTPException(404, f"{rel} is not in this snapshot")
        dst = os.path.join(dest_root, rel.strip("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        # the live folder may have changed since the snapshot: never follow a user's symlink out of it
        real_parent = os.path.realpath(os.path.dirname(dst))
        share_real = os.path.realpath(s["path"])
        if real_parent != share_real and not real_parent.startswith(share_real + os.sep) or os.path.islink(dst):
            raise HTTPException(400, f"{rel}: the folder it goes back into is now a link - move it out of the way first")
        # cp -a keeps owners/permissions/times; --reflink makes it instant and free of extra space on Btrfs
        if os.path.isdir(src):
            os.makedirs(dst, exist_ok=True)
            r = subprocess.run(["cp", "-a", "--reflink=auto", src + "/.", dst], capture_output=True, text=True)
        else:
            r = subprocess.run(["cp", "-a", "--reflink=auto", src, dst], capture_output=True, text=True)
        if r.returncode != 0:
            raise HTTPException(500, f"restoring {rel} failed: {r.stderr.strip()}")
        restored += 1
    return {"ok": True, "restored": restored, "target": dest_root}


@router.post("/{share}/{name}/revert")
def revert(share: str, name: str, _=Depends(require_admin)):
    """Put the whole shared folder back to a snapshot. A safety snapshot of the current state is taken first."""
    s = _share(share)
    root = _snap_root(s, name)
    safety = take(s)
    r = subprocess.run(["rsync", "-a", "--delete", "--exclude", f"/{SNAPDIR}", "--exclude", "/#recycle",
                        root + "/", s["path"] + "/"], capture_output=True, text=True)
    if r.returncode not in (0, 24):     # 24 = some files vanished meanwhile
        raise HTTPException(500, f"revert failed: {r.stderr.strip()[-300:]}")
    return {"ok": True, "safety_snapshot": safety}


if __name__ == "__main__":
    taken = run_due()
    try:
        from . import san
        taken += san.snap_due()
    except Exception as e:      # LUN snapshots must never stop shared-folder ones
        print("LUN snapshots failed:", e)
    print("snapshots taken:", ", ".join(taken) or "none")
