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

from . import safepath
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


def _owner_ids(user: dict) -> tuple[int, int] | None:
    if DEV:
        return None
    try:
        return pwd.getpwnam(user["user"]).pw_uid, grp.getgrnam("users").gr_gid
    except KeyError:
        return None


def _own_at(dfd: int, name: str, user: dict) -> None:
    ids = _owner_ids(user)
    if ids:
        try:
            safepath.chown_at(dfd, name, *ids)
        except OSError:
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
    name = _check_name(body.name)
    fd = safepath.open_dir(share["path"], full)
    try:
        try:
            os.mkdir(name, 0o2775, dir_fd=fd)
        except FileExistsError:
            raise HTTPException(409, "already exists")
        _own_at(fd, name, user)
    finally:
        os.close(fd)
    return {"ok": True}


class Rename(BaseModel):
    path: str          # item to rename
    new_name: str


@router.post("/rename")
def rename(body: Rename, user=Depends(FILES_USER)):
    share, full = resolve(user, body.path, write=True)
    if full == os.path.realpath(share["path"]):
        raise HTTPException(400, "rename shared folders in Control Panel")
    new = _check_name(body.new_name)
    fd, name = safepath.open_parent(share["path"], full)
    try:
        if safepath.lstat_at(fd, new) is not None:
            raise HTTPException(409, "an item with that name already exists")
        os.rename(name, new, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        os.close(fd)
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
        pfd, name = safepath.open_parent(root, full)
        try:
            if safepath.lstat_at(pfd, name) is None:
                raise HTTPException(404, f"{name} doesn't exist")
            if share.get("recycle_bin") and not rel.startswith("#recycle"):
                # (into #recycle keeping the folder structure; every folder made without following links)
                rfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                try:
                    bfd = safepath.makedirs_at(rfd, "#recycle/" + os.path.dirname(rel))
                finally:
                    os.close(rfd)
                try:
                    dest = name
                    if safepath.lstat_at(bfd, dest) is not None:
                        dest += time.strftime(" (%Y-%m-%d %H.%M.%S)")
                    safepath.move_at(pfd, name, bfd, dest)
                finally:
                    os.close(bfd)
            else:
                safepath.remove_at(pfd, name)
        finally:
            os.close(pfd)
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
        dfd = safepath.open_dir(droot, dest)
        try:
            sfd, sname = safepath.open_parent(sshare["path"], src)
            try:
                if safepath.lstat_at(dfd, sname) is not None:
                    raise HTTPException(409, f"{sname} already exists in the destination")
                if body.move:
                    safepath.move_at(sfd, sname, dfd, sname)
                else:
                    safepath.copy_at(sfd, sname, dfd, sname, owner=_owner_ids(user))
            finally:
                os.close(sfd)
            _own_at(dfd, sname, user)
        finally:
            os.close(dfd)
    return {"ok": True}


@router.post("/upload")
async def upload(request: Request, path: str, name: str, overwrite: bool = False, user=Depends(FILES_USER)):
    """Raw file bytes in the body (streamed, so large files are fine)."""
    ushare, folder = resolve(user, path, write=True)
    name = _check_name(name)
    import secrets
    fd = safepath.open_dir(ushare["path"], folder)
    tmp = f".{name[:64]}.{secrets.token_hex(4)}.uploading"
    try:
        if safepath.lstat_at(fd, name) is not None and not overwrite:
            raise HTTPException(409, f"{name} already exists")
        # new file only, in the checked folder, never through a symlink someone planted (we write as root)
        ffd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o664, dir_fd=fd)
        with os.fdopen(ffd, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        _own_at(fd, tmp, user)
        os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        if safepath.lstat_at(fd, tmp) is not None:
            os.unlink(tmp, dir_fd=fd)
        os.close(fd)
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


def file_response(root: str, full: str, **kw) -> FileResponse:
    """FileResponse for a checked path, served from the file opened without following links (the name
    could be swapped for a link to a system file between the check and the read)."""
    from starlette.background import BackgroundTask
    fd = safepath.open_file(root, full)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise HTTPException(404, "not a file")
    import mimetypes
    kw.setdefault("media_type", mimetypes.guess_type(full)[0] or "application/octet-stream")
    return FileResponse(safepath.proc_path(fd), stat_result=os.fstat(fd), background=BackgroundTask(os.close, fd), **kw)


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


def zip_stream(items: list[tuple[str, str]], root: str | None = None):
    """items: (absolute path inside root, name in the zip). Folders are added recursively, hidden items and
    links skipped. Everything is opened relative to folders opened without following links (root: the
    shared folder the paths were checked against; by default each item's own folder)."""
    import zipfile
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        def add_file(dfd: int, name: str, arc: str):
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
            except OSError:
                return
            with os.fdopen(fd, "rb") as src, zf.open(arc, "w", force_zip64=True) as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
                    yield sink.drain()
        for full, arc in items:
            try:
                pfd, name = safepath.open_parent(root or os.path.dirname(full), full)
            except HTTPException:
                continue
            try:
                st = safepath.lstat_at(pfd, name)
                if st is None:
                    continue
                if stat.S_ISREG(st.st_mode):
                    yield from add_file(pfd, name, arc)
                elif stat.S_ISDIR(st.st_mode):
                    top = os.open(name, safepath.DIR_FLAGS, dir_fd=pfd)
                    try:
                        # fwalk: every folder is opened relative to its parent, links are never followed
                        for dirpath, dirs, fnames, dfd in os.fwalk(".", dir_fd=top, follow_symlinks=False):
                            dirs[:] = sorted(d for d in dirs if not _hidden(d))
                            base = os.path.normpath(os.path.join(arc, dirpath))
                            if not fnames and not dirs:
                                zf.writestr(base + "/", b"")
                            for f in sorted(fnames):
                                if not _hidden(f):
                                    fst = safepath.lstat_at(dfd, f)
                                    if fst and stat.S_ISREG(fst.st_mode):
                                        yield from add_file(dfd, f, os.path.join(base, f))
                    finally:
                        os.close(top)
            finally:
                os.close(pfd)
    yield sink.drain()


@router.get("/download")
def download(ticket: str, inline: bool = False):
    exp, user, paths = _TICKETS.pop(ticket, (0, None, []))
    if exp < time.time():
        raise HTTPException(403, "download link expired - try again")
    found = [resolve(user, p) for p in paths]
    resolved = [f for _, f in found]
    if len(resolved) == 1 and os.path.isfile(resolved[0]):
        return file_response(found[0][0]["path"], resolved[0], headers=_disposition(os.path.basename(resolved[0]), inline))
    for full in resolved:
        if not os.path.exists(full):
            raise HTTPException(404, f"{os.path.basename(full)} no longer exists")
    name = (os.path.basename(resolved[0]) if len(resolved) == 1 else os.path.basename(os.path.dirname(resolved[0])) or "files") + ".zip"
    roots = {os.path.realpath(sh["path"]) for sh, _ in found}
    return StreamingResponse(zip_stream([(f, os.path.basename(f)) for f in resolved], root=roots.pop() if len(roots) == 1 else None),
                             media_type="application/zip",
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
