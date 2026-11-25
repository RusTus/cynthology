"""File Station: browse and manage files inside shared folders from the web UI.

Every path is "<share>/<relative path>". Access is checked against the share's read/write lists
(admins may access everything), and paths are resolved and confined to the share root so that
'..' or symlinks cannot escape it. New files are owned by the signed-in user.
"""
import grp
import os
import pwd
import shutil
import stat
import time
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import shares as shares_mod
from .auth import current_user
from .util import DEV

router = APIRouter(prefix="/api/files", tags=["files"])


def _in_group(user: str, group: str) -> bool:
    try:
        g = grp.getgrnam(group)
    except KeyError:
        return False
    if user in g.gr_mem:
        return True
    try:
        return pwd.getpwnam(user).pw_gid == g.gr_gid
    except KeyError:
        return False


def _matches(user: str, entries: list[str]) -> bool:
    for e in entries:
        if e == user or (e.startswith("@") and (DEV or _in_group(user, e[1:]))):
            return True
    return False


def access(user: dict, share: dict) -> str | None:
    """'rw', 'ro' or None."""
    if user["admin"]:
        return "rw"
    if _matches(user["user"], share.get("write_users", [])):
        return "rw"
    if _matches(user["user"], share.get("read_users", [])):
        return "ro"
    return None


def resolve(user: dict, path: str, write: bool = False) -> tuple[dict, str]:
    """'<share>/<rel>' -> (share, absolute path inside the share). Raises 403/404."""
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        raise HTTPException(400, "path must start with a shared folder")
    share = next((s for s in shares_mod.load() if s["name"] == parts[0]), None)
    if not share:
        raise HTTPException(404, "no such shared folder")
    mode = access(user, share)
    if not mode or (write and mode != "rw"):
        raise HTTPException(403, "you don't have permission for this folder")
    root = os.path.realpath(share["path"])
    full = os.path.realpath(os.path.join(root, *parts[1:]))
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(403, "path leaves the shared folder")
    if write and (full + os.sep).startswith(os.path.join(root, ".snapshots") + os.sep):
        raise HTTPException(403, "snapshots are read-only - copy files out of them to restore")
    return share, full


def _own(path: str, user: dict) -> None:
    """Give a new file/folder to the signed-in user (group 'users'), like an SMB upload would."""
    if DEV:
        return
    try:
        pw = pwd.getpwnam(user["user"])
        os.lchown(path, pw.pw_uid, grp.getgrnam("users").gr_gid)
    except (KeyError, OSError):
        pass


def _entry(full: str, name: str) -> dict | None:
    try:
        st = os.lstat(full)
    except OSError:
        return None
    is_dir = stat.S_ISDIR(st.st_mode)
    return {"name": name, "dir": is_dir, "size": 0 if is_dir else st.st_size, "mtime": int(st.st_mtime),
            "link": stat.S_ISLNK(st.st_mode)}


@router.get("/shares")
def my_shares(user=Depends(current_user)):
    out = []
    for s in shares_mod.load():
        mode = access(user, s)
        if mode:
            out.append({"name": s["name"], "description": s.get("description", ""), "access": mode})
    return out


@router.get("/list")
def list_dir(path: str, hidden: bool = False, user=Depends(current_user)):
    share, full = resolve(user, path)
    if not os.path.isdir(full):
        raise HTTPException(404, "not a folder")
    items = []
    for name in os.listdir(full):
        if not hidden and (name.startswith(".") or name in ("#recycle", "@eaDir")):
            continue
        e = _entry(os.path.join(full, name), name)
        if e:
            items.append(e)
    items.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"path": path.strip("/"), "access": access(user, share), "items": items}


class NameOp(BaseModel):
    path: str          # folder to act in
    name: str


def _check_name(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\0" in name or len(name.encode()) > 255:
        raise HTTPException(400, "invalid name")
    return name


@router.post("/mkdir")
def mkdir(body: NameOp, user=Depends(current_user)):
    _, full = resolve(user, body.path, write=True)
    target = os.path.join(full, _check_name(body.name))
    try:
        os.mkdir(target, 0o2775)
    except FileExistsError:
        raise HTTPException(409, "already exists")
    _own(target, user)
    return {"ok": True}


class Rename(BaseModel):
    path: str          # item to rename
    new_name: str


@router.post("/rename")
def rename(body: Rename, user=Depends(current_user)):
    share, full = resolve(user, body.path, write=True)
    if full == os.path.realpath(share["path"]):
        raise HTTPException(400, "rename shared folders in Control Panel")
    target = os.path.join(os.path.dirname(full), _check_name(body.new_name))
    if os.path.lexists(target):
        raise HTTPException(409, "an item with that name already exists")
    os.rename(full, target)
    return {"ok": True}


class Paths(BaseModel):
    paths: list[str]


@router.post("/delete")
def delete(body: Paths, user=Depends(current_user)):
    """Moves to the share's #recycle (keeping the folder structure) when its recycle bin is on."""
    for p in body.paths:
        share, full = resolve(user, p, write=True)
        root = os.path.realpath(share["path"])
        if full == root:
            raise HTTPException(400, "delete shared folders in Control Panel")
        rel = os.path.relpath(full, root)
        if share.get("recycle_bin") and not rel.startswith("#recycle"):
            dest = os.path.join(root, "#recycle", rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.lexists(dest):
                dest += time.strftime(" (%Y-%m-%d %H.%M.%S)")
            shutil.move(full, dest)
        elif os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
    return {"ok": True}


class Transfer(BaseModel):
    paths: list[str]
    dest: str           # destination folder
    move: bool = False


@router.post("/transfer")
def transfer(body: Transfer, user=Depends(current_user)):
    _, dest = resolve(user, body.dest, write=True)
    if not os.path.isdir(dest):
        raise HTTPException(400, "destination is not a folder")
    for p in body.paths:
        _, src = resolve(user, p, write=body.move)
        target = os.path.join(dest, os.path.basename(src))
        if target == src or (os.path.isdir(src) and (target + os.sep).startswith(src + os.sep)):
            raise HTTPException(400, "can't copy or move a folder into itself")
        if os.path.lexists(target):
            raise HTTPException(409, f"{os.path.basename(src)} already exists in the destination")
        if body.move:
            shutil.move(src, target)
        elif os.path.isdir(src):
            shutil.copytree(src, target, symlinks=True)
        else:
            shutil.copy2(src, target)
        _own(target, user)
    return {"ok": True}


@router.post("/upload")
async def upload(request: Request, path: str, name: str, overwrite: bool = False, user=Depends(current_user)):
    """Raw file bytes in the body (streamed, so large files are fine)."""
    _, folder = resolve(user, path, write=True)
    target = os.path.join(folder, _check_name(name))
    if os.path.lexists(target) and not overwrite:
        raise HTTPException(409, f"{name} already exists")
    tmp = os.path.join(folder, f".{name}.uploading")
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    _own(target, user)
    return {"ok": True}


_TICKETS: dict[str, tuple[float, dict, str]] = {}


class Ticket(BaseModel):
    path: str


@router.post("/ticket")
def ticket(body: Ticket, user=Depends(current_user)):
    """Single-use, 60 s download link (a plain <a href> can't send the Authorization header)."""
    import secrets
    resolve(user, body.path)
    now = time.time()
    for k in [k for k, v in _TICKETS.items() if v[0] < now]:
        del _TICKETS[k]
    t = secrets.token_urlsafe(24)
    _TICKETS[t] = (now + 60, user, body.path)
    return {"url": f"/api/files/download?ticket={t}"}


@router.get("/download")
def download(ticket: str, inline: bool = False):
    exp, user, path = _TICKETS.pop(ticket, (0, None, ""))
    if exp < time.time():
        raise HTTPException(403, "download link expired - try again")
    _, full = resolve(user, path)
    if not os.path.isfile(full):
        raise HTTPException(404, "not a file")
    name = os.path.basename(full)
    disp = "inline" if inline else "attachment"
    return FileResponse(full, headers={"Content-Disposition": f"{disp}; filename*=UTF-8''{urllib.parse.quote(name)}"})


@router.get("/usage")
def usage(path: str, user=Depends(current_user)):
    _, full = resolve(user, path)
    total, count = 0, 0
    for dirpath, _dirs, files in os.walk(full):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
                count += 1
            except OSError:
                pass
    return {"bytes": total, "files": count}
