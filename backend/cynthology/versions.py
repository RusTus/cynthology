"""Previous versions of a file or folder (File Station > right-click > Previous versions).

  snapshots - every distinct earlier version still kept in the shared folder's snapshots
              (anyone who may open the folder sees them; restoring needs write permission)
  backups   - versions in the backup tasks that include the shared folder (administrators only)
Restore either replaces the current item (which goes to the recycle bin when that is on) or puts
the old version next to it as "name (restored <date>).ext". The old version is first copied under
a temporary name inside the destination folder and then renamed into place, so a half-finished
restore never leaves a broken file behind.
"""
import datetime
import json
import os
import re
import secrets
import shutil
import subprocess

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import files, shares as shares_mod, snapshots
from .auth import current_user

router = APIRouter(prefix="/api/versions", tags=["versions"])


def _split(user: dict, path: str) -> tuple[dict, str, str]:
    """(share, real share root, path relative to it) - checks the user may read the item."""
    share, full = files.resolve(user, path)
    root = os.path.realpath(share["path"])
    rel = os.path.relpath(full, root)
    if rel == "." or rel.split(os.sep)[0] in (snapshots.SNAPDIR, "#recycle"):
        raise HTTPException(400, "choose a file or folder inside a shared folder")
    return share, root, rel


def _sig(p: str) -> tuple | None:
    try:
        st = os.lstat(p)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, os.path.isdir(p))


@router.get("/snapshots")
def snapshot_versions(path: str, user: dict = Depends(current_user)):
    share, root, rel = _split(user, path)
    if share["name"] == "home" or not snapshots.supported(share):
        return {"versions": [], "note": "This folder has no snapshots (Snapshots app)."}
    cur = _sig(os.path.join(root, rel))
    out, seen = [], set()
    for name in sorted(snapshots.list_snaps(share), reverse=True):        # newest first
        p = os.path.join(root, snapshots.SNAPDIR, name, rel)
        sig = _sig(p)
        if not sig or sig in seen:
            continue                                  # (not in this snapshot, or the same version as a newer one)
        seen.add(sig)
        out.append({"source": "snapshot", "id": name, "time": int(snapshots.snap_time(name).timestamp()),
                    "mtime": sig[0] // 10 ** 9, "size": sig[1] if not sig[2] else None, "dir": sig[2],
                    "same_as_current": cur is not None and sig[:2] == cur[:2],
                    "download": f"/{share['name']}/{snapshots.SNAPDIR}/{name}/{rel}" if not sig[2] else None})
    return {"versions": out, "can_restore": files.access(user, share) == "rw"}


@router.get("/backups")
def backup_versions(path: str, user: dict = Depends(current_user)):
    if not user.get("admin"):
        return {"versions": [], "note": "Only administrators can restore from backups."}
    from . import backup
    share, root, rel = _split(user, path)
    full = os.path.join(root, rel)
    out, errors = [], []
    for t in backup.load_tasks():
        try:
            sources = backup.source_paths(t)
        except Exception:
            continue                                    # (a task whose folders were deleted)
        if not any(full == os.path.realpath(sp) or full.startswith(os.path.realpath(sp).rstrip("/") + "/") for sp in sources):
            continue
        try:
            snaps = {s["id"]: s for s in json.loads(backup.restic(t, "snapshots", "--tag", f"cyn-{t['id']}", json_out=True, timeout=300).stdout or "[]")}
            r = backup.restic(t, "find", "--tag", f"cyn-{t['id']}", "--json", full, timeout=600)
            found = json.loads(r.stdout or "[]") if r.returncode == 0 else []
        except Exception as e:                          # (a cloud repository may be unreachable right now)
            errors.append(f"{t['name']}: {e}")
            continue
        finally:
            backup._unmount_if_ours(t)
        seen = set()
        for hit in sorted(found, key=lambda h: snaps.get(h.get("snapshot"), {}).get("time", ""), reverse=True):
            m = next((x for x in hit.get("matches", []) if x.get("path") == full), None)
            s = snaps.get(hit.get("snapshot"))
            if not m or not s:
                continue
            sig = (m.get("mtime"), m.get("size"))
            if sig in seen:
                continue
            seen.add(sig)
            out.append({"source": "backup", "task": t["id"], "task_name": t["name"], "id": s["id"],
                        "time": _restic_time(s.get("time", "")), "mtime": _restic_time(m.get("mtime", "")),
                        "size": m.get("size"), "dir": m.get("type") == "dir"})
    return {"versions": out, "errors": errors, "can_restore": True}


def _restic_time(s: str) -> int | None:
    try:
        return int(datetime.datetime.fromisoformat(re.sub(r"(\.\d{6})\d*", r"\1", s).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


class RestoreBody(BaseModel):
    path: str
    source: str              # snapshot | backup
    id: str                  # snapshot name, or restic snapshot id
    task: str = ""           # backup task id
    mode: str = "copy"       # copy | replace


def _copy_name(name: str, when: int) -> str:
    stamp = datetime.datetime.fromtimestamp(when).strftime("%Y-%m-%d %H.%M")
    base, ext = os.path.splitext(name) if "." in name[1:] else (name, "")
    return f"{base} (restored {stamp}){ext}"


@router.post("/restore")
def restore(body: RestoreBody, user: dict = Depends(current_user)):
    if body.mode not in ("copy", "replace"):
        raise HTTPException(400, "mode: copy or replace")
    share, root, rel = _split(user, body.path)
    files.resolve(user, os.path.dirname(body.path.rstrip("/")) or body.path, write=True)       # (needs write permission)
    parent_rel, name = os.path.split(rel)
    parent = files._confine(os.path.join(root, parent_rel), root)
    if not os.path.isdir(parent):
        raise HTTPException(409, "its folder doesn't exist any more - restore the folder first")
    tmp = os.path.join(parent, f".restoring-{secrets.token_hex(4)}")
    try:
        if body.source == "snapshot":
            when = _from_snapshot(share, root, rel, body.id, tmp)
        elif body.source == "backup":
            if not user.get("admin"):
                raise HTTPException(403, "only administrators can restore from backups")
            when = _from_backup(body, os.path.join(root, rel), tmp)
        else:
            raise HTTPException(400, "source: snapshot or backup")
        files._confine(parent, root)                     # (re-check right before the rename)
        dest_name = name if body.mode == "replace" else _copy_name(name, when)
        dest = os.path.join(parent, dest_name)
        if os.path.lexists(dest):
            if body.mode != "replace":
                raise HTTPException(409, f"'{dest_name}' already exists")
            files.delete(files.Paths(paths=[body.path]), user)      # (current version -> recycle bin)
        os.rename(tmp, dest)
    finally:
        if os.path.lexists(tmp):
            shutil.rmtree(tmp, ignore_errors=True) if os.path.isdir(tmp) and not os.path.islink(tmp) else os.remove(tmp)
    return {"ok": True, "name": dest_name}


def _from_snapshot(share: dict, root: str, rel: str, name: str, tmp: str) -> int:
    if share["name"] == "home":
        raise HTTPException(400, "no snapshots here")
    snap_root = snapshots._snap_root({**share, "path": root}, name)
    src = snapshots._inside(snap_root, rel)
    if not os.path.lexists(src):
        raise HTTPException(404, "not in this snapshot")
    # cp -a keeps owner, permissions and times; --reflink makes it instant (no extra space) on Btrfs
    r = subprocess.run(["cp", "-a", "--reflink=auto", "--", src, tmp], capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(500, "copy failed: " + r.stderr.strip()[-200:])
    return int(snapshots.snap_time(name).timestamp())


def _from_backup(body: RestoreBody, full: str, tmp: str) -> int:
    from . import backup
    t = backup.get_task(body.task)
    if not re.fullmatch(r"[0-9a-f]{8,64}", body.id):
        raise HTTPException(400, "bad version")
    work = tmp + ".d"
    os.makedirs(work, mode=0o700)
    try:
        parent, name = os.path.split(full)
        r = backup.restic(t, "restore", f"{body.id}:{parent}", "--target", work, "--include", "/" + name, timeout=24 * 3600)
        if r.returncode != 0 or not os.path.lexists(os.path.join(work, name)):
            raise HTTPException(500, "restore failed: " + backup._err(r))
        os.rename(os.path.join(work, name), tmp)
    finally:
        backup._unmount_if_ours(t)
        shutil.rmtree(work, ignore_errors=True)
    snaps = json.loads(backup.restic(t, "snapshots", body.id, json_out=True, timeout=120).stdout or "[]")
    return (_restic_time(snaps[0]["time"]) if snaps else None) or int(datetime.datetime.now().timestamp())
