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
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import shares as shares_mod
from .auth import current_user
from .privileges import require_app
from .util import DEV

router = APIRouter(prefix="/api/files", tags=["files"])
FILES_USER = require_app("files")       # (Control Panel > Application Privileges)


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
    for e in entries:   # (domain names are case-insensitive: CORP\\Alice == corp\\alice)
        if e.lower() == user.lower() or (e.startswith("@") and (DEV or _in_group(user, e[1:]))):
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
    if parts[0] == "home":                 # the signed-in user's own home folder
        home = shares_mod.home_path(user["user"])
        if not home or not os.path.isdir(home):
            raise HTTPException(404, "the user home service is off (Control Panel > Users & Groups)")
        share = {"name": "home", "path": home, "recycle_bin": True}
        mode = "rw"
    else:
        share = next((s for s in shares_mod.load() if s["name"] == parts[0]), None)
        if not share:
            raise HTTPException(404, "no such shared folder")
        if share.get("encrypted") and not shares_mod.unlocked(share):
            raise HTTPException(423, f"{share['name']} is encrypted and locked - unlock it in Control Panel > Shared Folders")
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


def _confine(path: str, root: str) -> str:
    """Re-check right before writing that the real location is still inside the share (a share user
    could have swapped a folder for a symlink since resolve() ran). Returns the real path."""
    real = os.path.realpath(path)
    if real != root and not real.startswith(root + os.sep):
        raise HTTPException(403, "path leaves the shared folder")
    return real


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
def my_shares(user=Depends(FILES_USER)):
    out = []
    if shares_mod.homes_share():
        try:
            if shares_mod.ensure_home(user["user"]):
                out.append({"name": "home", "description": "Your home folder", "access": "rw"})
        except HTTPException:
            pass
    for s in shares_mod.load():
        mode = access(user, s)
        if mode:
            out.append({"name": s["name"], "description": s.get("description", ""), "access": mode,
                        "locked": bool(s.get("encrypted")) and not shares_mod.unlocked(s)})
    return out


def _hidden(name: str) -> bool:
    return name.startswith(".") or name in ("#recycle", "@eaDir")


@router.get("/list")
def list_dir(path: str, hidden: bool = False, dirs_only: bool = False, user=Depends(FILES_USER)):
    share, full = resolve(user, path)
    if not os.path.isdir(full):
        raise HTTPException(404, "not a folder")
    items = []
    for name in os.listdir(full):
        if not hidden and _hidden(name):
            continue
        if dirs_only and not os.path.isdir(os.path.join(full, name)):
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
def mkdir(body: NameOp, user=Depends(FILES_USER)):
    share, full = resolve(user, body.path, write=True)
    target = os.path.join(_confine(full, os.path.realpath(share["path"])), _check_name(body.name))
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
def rename(body: Rename, user=Depends(FILES_USER)):
    share, full = resolve(user, body.path, write=True)
    if full == os.path.realpath(share["path"]):
        raise HTTPException(400, "rename shared folders in Control Panel")
    target = os.path.join(_confine(os.path.dirname(full), os.path.realpath(share["path"])), _check_name(body.new_name))
    if os.path.lexists(target):
        raise HTTPException(409, "an item with that name already exists")
    os.rename(full, target)
    return {"ok": True}


class Paths(BaseModel):
    paths: list[str]


@router.post("/delete")
def delete(body: Paths, user=Depends(FILES_USER)):
    """Moves to the share's #recycle (keeping the folder structure) when its recycle bin is on."""
    for p in body.paths:
        share, full = resolve(user, p, write=True)
        root = os.path.realpath(share["path"])
        if full == root:
            raise HTTPException(400, "delete shared folders in Control Panel")
        rel = os.path.relpath(full, root)
        if share.get("recycle_bin") and not rel.startswith("#recycle"):
            bin_ = os.path.join(root, "#recycle")
            if os.path.islink(bin_):
                raise HTTPException(403, "the recycle bin of this shared folder is a link - remove it first")
            dest = os.path.join(bin_, rel)
            _confine(os.path.dirname(dest) if os.path.exists(os.path.dirname(dest)) else bin_, root)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            _confine(os.path.dirname(dest), os.path.realpath(bin_))
            if os.path.lexists(dest):
                dest += time.strftime(" (%Y-%m-%d %H.%M.%S)")
            _confine(full, root)
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
def transfer(body: Transfer, user=Depends(FILES_USER)):
    dshare, dest = resolve(user, body.dest, write=True)
    droot = os.path.realpath(dshare["path"])
    if not os.path.isdir(dest):
        raise HTTPException(400, "destination is not a folder")
    for p in body.paths:
        sshare, src = resolve(user, p, write=body.move)
        if os.path.realpath(src) == os.path.realpath(sshare["path"]) or os.path.realpath(src) == os.path.realpath(dshare["path"]):
            raise HTTPException(400, "a whole shared folder can't be copied or moved - open it and select its contents")
        target = os.path.join(dest, os.path.basename(src))
        if target == src or (os.path.isdir(src) and (target + os.sep).startswith(src + os.sep)):
            raise HTTPException(400, "can't copy or move a folder into itself")
        if os.path.lexists(target):
            raise HTTPException(409, f"{os.path.basename(src)} already exists in the destination")
        _confine(dest, droot)
        if body.move:
            shutil.move(src, target)
        elif os.path.isdir(src):
            shutil.copytree(src, target, symlinks=True)
        else:
            shutil.copy2(src, target)
        _own(target, user)
    return {"ok": True}


@router.post("/upload")
async def upload(request: Request, path: str, name: str, overwrite: bool = False, user=Depends(FILES_USER)):
    """Raw file bytes in the body (streamed, so large files are fine)."""
    ushare, folder = resolve(user, path, write=True)
    folder = _confine(folder, os.path.realpath(ushare["path"]))
    target = os.path.join(folder, _check_name(name))
    if os.path.lexists(target) and not overwrite:
        raise HTTPException(409, f"{name} already exists")
    import secrets
    tmp = os.path.join(folder, f".{name[:64]}.{secrets.token_hex(4)}.uploading")
    try:
        # new file only, never through a symlink someone planted (we write as root)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o664)
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        _confine(folder, os.path.realpath(ushare["path"]))
        os.replace(tmp, target)
    finally:
        if os.path.lexists(tmp):
            os.remove(tmp)
    _own(target, user)
    return {"ok": True}


_TICKETS: dict[str, tuple[float, dict, list[str]]] = {}


class Ticket(BaseModel):
    path: str = ""
    paths: list[str] = []        # several items (or a folder) download as one .zip


@router.post("/ticket")
def ticket(body: Ticket, user=Depends(FILES_USER)):
    """Single-use, 60 s download link (a plain <a href> can't send the Authorization header)."""
    import secrets
    paths = body.paths or ([body.path] if body.path else [])
    if not paths:
        raise HTTPException(400, "nothing to download")
    for p in paths:
        resolve(user, p)
    now = time.time()
    for k in [k for k, v in _TICKETS.items() if v[0] < now]:
        del _TICKETS[k]
    t = secrets.token_urlsafe(24)
    _TICKETS[t] = (now + 60, user, paths)
    return {"url": f"/api/files/download?ticket={t}"}


ACTIVE_TYPES = (".html", ".htm", ".xhtml", ".svg", ".svgz", ".xml", ".xsl", ".js", ".mjs", ".shtml", ".swf", ".pdf")


def _disposition(name: str, inline: bool = False) -> dict:
    # Files from shares are untrusted: content that can run script (HTML, SVG...) is never shown
    # inline on the NAS's origin, and the sandbox/nosniff headers stop a browser from treating
    # anything else as a web page.
    if name.lower().endswith(ACTIVE_TYPES):
        inline = False
    return {"Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{urllib.parse.quote(name)}",
            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; media-src 'self'"}


class _Sink:
    """Write-only, unseekable buffer for streaming a zip as it is built."""
    def __init__(self):
        self.chunks: list[bytes] = []
        self.pos = 0

    def write(self, b):
        self.chunks.append(bytes(b))
        self.pos += len(b)
        return len(b)

    def tell(self):
        return self.pos

    def flush(self):
        pass

    def drain(self) -> bytes:
        out = b"".join(self.chunks)
        self.chunks.clear()
        return out


def zip_stream(items: list[tuple[str, str]]):
    """items: (absolute path, name in the zip). Folders are added recursively, hidden items skipped."""
    import zipfile
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        def add_file(path: str, arc: str):
            try:
                with open(path, "rb") as src, zf.open(arc, "w", force_zip64=True) as dst:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        dst.write(chunk)
                        yield sink.drain()
            except OSError:
                return
        for full, arc in items:
            if os.path.isdir(full) and not os.path.islink(full):
                for dirpath, dirs, files in os.walk(full):
                    dirs[:] = sorted(d for d in dirs if not _hidden(d))
                    rel = os.path.relpath(dirpath, os.path.dirname(full))
                    base = os.path.join(os.path.dirname(arc), rel) if os.path.dirname(arc) else rel
                    if not files and not dirs:
                        zf.writestr(base + "/", b"")
                    for f in sorted(files):
                        if not _hidden(f) and not os.path.islink(os.path.join(dirpath, f)):
                            yield from add_file(os.path.join(dirpath, f), os.path.join(base, f))
            elif os.path.isfile(full):
                yield from add_file(full, arc)
    yield sink.drain()


@router.get("/download")
def download(ticket: str, inline: bool = False):
    exp, user, paths = _TICKETS.pop(ticket, (0, None, []))
    if exp < time.time():
        raise HTTPException(403, "download link expired - try again")
    resolved = [resolve(user, p)[1] for p in paths]
    if len(resolved) == 1 and os.path.isfile(resolved[0]):
        return FileResponse(resolved[0], headers=_disposition(os.path.basename(resolved[0]), inline))
    for full in resolved:
        if not os.path.exists(full):
            raise HTTPException(404, f"{os.path.basename(full)} no longer exists")
    name = (os.path.basename(resolved[0]) if len(resolved) == 1 else os.path.basename(os.path.dirname(resolved[0])) or "files") + ".zip"
    return StreamingResponse(zip_stream([(f, os.path.basename(f)) for f in resolved]), media_type="application/zip",
                             headers=_disposition(name))


@router.get("/search")
def search(path: str, q: str, hidden: bool = False, user=Depends(FILES_USER)):
    """Find files and folders whose name contains q, below path (or in every share when path is empty)."""
    q = q.strip().lower()
    if len(q) < 2:
        raise HTTPException(400, "type at least 2 characters")
    roots = [path.strip("/")] if path.strip("/") else [s["name"] for s in shares_mod.load() if access(user, s)]
    out, deadline = [], time.time() + 8
    for root_path in roots:
        share, full = resolve(user, root_path)
        share_root = os.path.realpath(share["path"])
        for dirpath, dirs, files in os.walk(full):
            if not hidden:
                dirs[:] = [d for d in dirs if not _hidden(d)]
            for name in dirs + files:
                if q in name.lower() and (hidden or not _hidden(name)):
                    e = _entry(os.path.join(dirpath, name), name)
                    if e:
                        rel = os.path.relpath(dirpath, share_root)
                        e["folder"] = share["name"] if rel == "." else f"{share['name']}/{rel}"
                        out.append(e)
            if len(out) >= 500 or time.time() > deadline:
                return {"items": out, "truncated": True}
    return {"items": out, "truncated": False}


@router.get("/usage")
def usage(path: str, user=Depends(FILES_USER)):
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
