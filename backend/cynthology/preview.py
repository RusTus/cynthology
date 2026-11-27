"""File previews (File Station and sharing links): thumbnails, media streaming, text viewing.

Thumbnails are made by thumbworker.py as the unprivileged cyn-thumb account (never as root),
with CPU, memory and time limits; it only gets the file descriptor opened here after the
access check. Results are cached in <state>/thumbs (keyed by file, size and modification
time; oldest removed beyond 1 GB).

Streaming: the browser's <video>/<audio>/PDF viewer can't send our Authorization header and
makes many range requests, so it gets a short-lived link for exactly one file.
"""
import hashlib
import mimetypes
import os
import resource
import secrets
import subprocess
import sys
import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import files
from .auth import current_user
from .privileges import require_app
from .util import DEV, STATE_DIR

router = APIRouter(prefix="/api/preview", tags=["preview"])
FILES_USER = require_app("files")       # (Control Panel > Application Privileges)
CACHE = os.path.join(STATE_DIR, "thumbs")
CACHE_MAX = 1 << 30
SIZES = (256, 1600)
IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif"}
VIDEO = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".mpg", ".mpeg", ".3gp", ".ts", ".mts", ".m2ts"}
AUDIO = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wav"}
TEXT = {".txt", ".md", ".log", ".csv", ".json", ".xml", ".yml", ".yaml", ".ini", ".conf", ".cfg", ".sh", ".py",
        ".js", ".css", ".html", ".htm", ".srt", ".nfo", ".toml", ".env", ".sql", ".c", ".h", ".cpp", ".java", ".go", ".rs"}
SANDBOX = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; media-src 'self'"}
_streams: dict[str, tuple[float, str]] = {}
_sem = threading.BoundedSemaphore(3)           # (a folder of 500 photos must not start 500 decoders)


def kind_of(name: str) -> str | None:
    ext = os.path.splitext(name.lower())[1]
    if ext in IMAGE:
        return "image"
    if ext in VIDEO:
        return "video"
    if ext == ".pdf":
        return "pdf"
    if ext in AUDIO:
        return "audio"
    if ext in TEXT:
        return "text"
    return None


def _limits():                                  # (runs in the worker before exec)
    resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_FSIZE, (300 << 20, 300 << 20))


def make_thumb(full: str, size: int) -> bytes:
    kind = kind_of(full)
    if kind not in ("image", "video", "pdf"):
        raise HTTPException(415, "no thumbnail for this kind of file")
    st = os.stat(full)
    key = hashlib.sha256(f"{full}|{st.st_ino}|{st.st_size}|{st.st_mtime_ns}|{size}".encode()).hexdigest()
    path = os.path.join(CACHE, key[:2], key + ".webp")
    try:
        with open(path, "rb") as f:
            os.utime(path)                      # (recently used: keep it)
            return f.read()
    except OSError:
        pass
    fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cmd = [sys.executable, "-m", "cynthology.thumbworker", kind, str(size), str(fd)]
        if not DEV:
            cmd = ["setpriv", "--reuid=cyn-thumb", "--regid=cyn-thumb", "--clear-groups", "--no-new-privs",
                   "--inh-caps=-all", "--bounding-set=-all", "--"] + cmd
        with _sem:
            r = subprocess.run(cmd, capture_output=True, timeout=60, cwd=backend, preexec_fn=_limits, pass_fds=(fd,),
                               env={"PATH": "/usr/bin:/bin", "PYTHONPATH": backend, "HOME": "/nonexistent"})
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "the thumbnail took too long")
    finally:
        os.close(fd)
    if r.returncode != 0 or not r.stdout:
        raise HTTPException(422, "can't make a thumbnail of this file")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(r.stdout)
    os.replace(path + ".tmp", path)
    _trim_cache()
    return r.stdout


_last_trim = 0.0


def _trim_cache() -> None:
    global _last_trim
    if time.time() - _last_trim < 600:
        return
    _last_trim = time.time()
    entries = []
    for dp, _, fs in os.walk(CACHE):
        for f in fs:
            p = os.path.join(dp, f)
            try:
                st = os.stat(p)
                entries.append((st.st_mtime, st.st_size, p))
            except OSError:
                pass
    total = sum(e[1] for e in entries)
    for _, sz, p in sorted(entries):
        if total <= CACHE_MAX * 0.8:
            break
        try:
            os.remove(p)
            total -= sz
        except OSError:
            pass


# ---------- API ----------

@router.get("/thumb")
def thumb(path: str, size: int = 256, user=Depends(FILES_USER)):
    _, full = files.resolve(user, path)
    if not os.path.isfile(full):
        raise HTTPException(404, "not a file")
    data = make_thumb(full, 1600 if size > 256 else 256)
    return Response(data, media_type="image/webp", headers={"Cache-Control": "private, max-age=86400", **SANDBOX})


class StreamReq(BaseModel):
    path: str


@router.post("/stream")
def stream_link(body: StreamReq, user=Depends(FILES_USER)):
    """A link the browser's player can use directly (6 hours, this one file, many range requests)."""
    _, full = files.resolve(user, body.path)
    if not os.path.isfile(full) or kind_of(full) not in ("video", "audio", "pdf", "image"):
        raise HTTPException(415, "this file can't be played in the browser")
    now = time.time()
    for k in [k for k, v in _streams.items() if v[0] < now]:
        del _streams[k]
    tok = secrets.token_urlsafe(24)
    _streams[tok] = (now + 6 * 3600, full)
    from urllib.parse import quote
    return {"url": f"/api/preview/s/{tok}/{quote(os.path.basename(full))}"}


@router.get("/s/{tok}/{name}")
def stream(tok: str, name: str):
    exp, full = _streams.get(tok, (0, ""))
    if exp < time.time() or not os.path.isfile(full):
        raise HTTPException(403, "this preview link has expired - open the file again")
    mt = mimetypes.guess_type(full)[0] or "application/octet-stream"
    if kind_of(full) == "pdf":
        # (shown by our own PDF viewer page, which fetches it - never rendered as a document itself)
        return FileResponse(full, media_type="application/pdf", headers={**SANDBOX, "Content-Disposition": "attachment"})
    return FileResponse(full, media_type=mt, headers={**SANDBOX, "Content-Disposition": "inline"})


@router.get("/text")
def text(path: str, user=Depends(FILES_USER)):
    share, full = files.resolve(user, path)
    if not os.path.isfile(full):
        raise HTTPException(404, "not a file")
    limit = 1 << 20
    with open(full, "rb") as f:
        raw = f.read(limit + 1)
    if b"\x00" in raw[:8192]:
        raise HTTPException(415, "this looks like a binary file")
    try:
        body = raw[:limit].decode("utf-8")
        enc = "utf-8"
    except UnicodeDecodeError:
        body, enc = raw[:limit].decode("latin-1"), "latin-1"
    return {"text": body, "truncated": len(raw) > limit, "encoding": enc,
            "writable": files.access(user, share) == "rw" if share.get("name") != "home" else True,
            "mtime": int(os.path.getmtime(full))}
