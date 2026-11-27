"""WebDAV server (DSM: WebDAV Server package): shared folders as a network drive over HTTP(S).

    python3 -m cynthology.webdav        (cynthology-webdav.service, ports 5005 / 5006 by default)

A separate process from the web interface, but the same rules as File Station: users sign in with
their NAS password (PAM), see only the shared folders they may use, can only write where they have
read/write permission, and every path is confined to its shared folder (files.resolve/_confine).
Deleted items go to the shared folder's recycle bin. Locks (LOCK/UNLOCK) are accepted but not
enforced - enough for Windows, macOS Finder and office apps, which insist on them.

URLs: /<shared folder>/<path>  -  "/" lists your shared folders (and "home" with the home service).
"""
import asyncio
import base64
import hashlib
import mimetypes
import os
import re
import shutil
import stat
import threading
import time
import urllib.parse
import uuid
from email.utils import formatdate
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from fastapi import HTTPException
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Route

from . import files, shares as shares_mod
from .util import DEV, STATE_DIR

REALM = "Cynthology"
METHODS = ["OPTIONS", "GET", "HEAD", "PUT", "DELETE", "MKCOL", "COPY", "MOVE", "PROPFIND", "PROPPATCH", "LOCK", "UNLOCK"]
SECURITY = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'"}


# ---------- sign-in (Basic auth over PAM, with the same lockout rules as the web interface) ----------

MAX_FAILS, FAIL_WINDOW = 5, 15 * 60
_fails: dict[str, list[float]] = {}
_cache: dict[str, tuple[float, dict]] = {}     # sha256(user:password) -> (expires, user) - clients send it on every request
_lock = threading.Lock()


def _log(event: str, user: str, ip: str) -> None:
    try:
        from .logcenter import auth_event
        auth_event(event, user, ip, "WebDAV")
    except Exception:
        pass


def authenticate(header: str, ip: str) -> dict | None:
    if not header.lower().startswith("basic "):
        return None
    try:
        name, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    if not name or not pw or len(name) > 128 or len(pw) > 1024:
        return None
    key = hashlib.sha256(f"{name}\0{pw}".encode()).hexdigest()
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    from .auth import _canonical, _is_admin, _pam_ok
    keys = (f"u:{name.lower()}|{ip}", f"ip:{ip}")
    with _lock:                                   # count first, so parallel guesses can't slip past the limit
        for k in keys:
            _fails[k] = [t for t in _fails.get(k, []) if now - t < FAIL_WINDOW]
        if any(len(_fails[k]) >= MAX_FAILS for k in keys):
            _log("locked", name, ip)
            return None
        for k in keys:
            _fails[k].append(now)
    if not _pam_ok(name, pw):
        _log("failed", name, ip)
        time.sleep(1)
        return None
    with _lock:
        for k in keys:
            _fails.pop(k, None)
    user = _canonical(name)
    u = {"user": user, "admin": _is_admin(user)}
    if len(_cache) > 1000:
        _cache.clear()
    _cache[key] = (now + 300, u)                   # re-check the password (and admin rights) every 5 minutes
    _log("login", user, ip)
    return u


# ---------- helpers ----------

def _dav_path(request: Request) -> str:
    """URL path -> File Station path ('' for the root)."""
    return urllib.parse.unquote(request.url.path).strip("/")


def _href(path: str, is_dir: bool) -> str:
    h = "/" + urllib.parse.quote(path.strip("/"), safe="/")
    return h + "/" if is_dir and not h.endswith("/") else h


def _shares_for(user: dict) -> list[str]:
    out = [s["name"] for s in shares_mod.load()
           if not s.get("homes") and files.access(user, s) and not (s.get("encrypted") and not shares_mod.unlocked(s))]
    home = shares_mod.home_path(user["user"])
    if home and os.path.isdir(home):
        out.append("home")
    return sorted(out)


def _err(e: HTTPException) -> Response:
    return Response(str(e.detail), status_code=e.status_code, media_type="text/plain")


def _etag(st: os.stat_result) -> str:
    return f'"{st.st_ino:x}-{st.st_mtime_ns:x}-{st.st_size:x}"'


def _prop_response(href: str, st: os.stat_result | None, name: str, is_dir: bool, quota: tuple[int, int] | None = None) -> str:
    p = [f"<D:displayname>{escape(name)}</D:displayname>"]
    if is_dir:
        p.append("<D:resourcetype><D:collection/></D:resourcetype>")
    else:
        p.append("<D:resourcetype/>")
    if st:
        p.append(f"<D:getlastmodified>{formatdate(st.st_mtime, usegmt=True)}</D:getlastmodified>")
        p.append(f"<D:creationdate>{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(st.st_ctime))}</D:creationdate>")
        if not is_dir:
            p.append(f"<D:getcontentlength>{st.st_size}</D:getcontentlength>")
            p.append(f"<D:getcontenttype>{escape(mimetypes.guess_type(name)[0] or 'application/octet-stream')}</D:getcontenttype>")
            p.append(f"<D:getetag>{escape(_etag(st))}</D:getetag>")
    if quota:
        p.append(f"<D:quota-available-bytes>{quota[0]}</D:quota-available-bytes><D:quota-used-bytes>{quota[1]}</D:quota-used-bytes>")
    p.append("<D:supportedlock><D:lockentry><D:lockscope><D:exclusive/></D:lockscope><D:locktype><D:write/></D:locktype>"
             "</D:lockentry></D:supportedlock>")
    return (f"<D:response><D:href>{escape(href)}</D:href><D:propstat><D:prop>{''.join(p)}</D:prop>"
            "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")


def _multistatus(parts: list[str]) -> Response:
    body = '<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">' + "".join(parts) + "</D:multistatus>"
    return Response(body, status_code=207, media_type='application/xml; charset="utf-8"')


def _hidden(name: str) -> bool:
    return name.startswith(".") or name in ("@eaDir", "lost+found")


# ---------- methods ----------

def propfind(user: dict, path: str, depth: str) -> Response:
    if not path:                                           # the root: one folder per shared folder
        now = os.stat("/")
        parts = [_prop_response("/", now, "Cynthology", True)]
        if depth != "0":
            for name in _shares_for(user):
                try:
                    _, full = files.resolve(user, name)
                    st = os.stat(full)
                except (HTTPException, OSError):
                    continue
                parts.append(_prop_response(_href(name, True), st, name, True))
        return _multistatus(parts)
    _, full = files.resolve(user, path)
    try:
        st = os.stat(full)
    except FileNotFoundError:
        return Response(status_code=404)
    is_dir = stat.S_ISDIR(st.st_mode)
    quota = None
    if is_dir:
        v = os.statvfs(full)
        quota = (v.f_bavail * v.f_frsize, (v.f_blocks - v.f_bfree) * v.f_frsize)
    parts = [_prop_response(_href(path, is_dir), st, os.path.basename(path), is_dir, quota)]
    if is_dir and depth != "0":                            # (Depth: infinity is answered like 1)
        with os.scandir(full) as it:
            for e in it:
                if _hidden(e.name):
                    continue
                try:
                    est = e.stat()
                except OSError:
                    continue
                d = stat.S_ISDIR(est.st_mode)
                parts.append(_prop_response(_href(f"{path}/{e.name}", d), est, e.name, d))
    return _multistatus(parts)


def _listing_html(user: dict, path: str) -> Response:
    if path:
        _, full = files.resolve(user, path)
        names = sorted((e.name + ("/" if e.is_dir() else "")) for e in os.scandir(full) if not _hidden(e.name))
    else:
        names = [n + "/" for n in _shares_for(user)]
    rows = "".join(f'<li><a href="{escape(_href(path + "/" + n, n.endswith("/")))}">{escape(n)}</a></li>' for n in names)
    up = '<li><a href="../">..</a></li>' if path else ""
    html = (f"<!doctype html><meta charset=utf-8><title>{escape('/' + path)}</title>"
            "<style>body{font:15px system-ui;background:#111827;color:#e5e7eb;margin:2em}a{color:#93c5fd}</style>"
            f"<h2>Cynthology WebDAV: /{escape(path)}</h2><ul>{up}{rows}</ul>")
    return Response(html, media_type="text/html; charset=utf-8",
                    headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})


def get(user: dict, path: str, request: Request) -> Response:
    if not path:
        return _listing_html(user, path)
    _, full = files.resolve(user, path)
    if os.path.isdir(full):
        return _listing_html(user, path)
    if not os.path.isfile(full):
        return Response(status_code=404)
    st = os.stat(full)
    # (never shown as a page in the browser: a file a user uploaded must not run script with your sign-in)
    return FileResponse(full, stat_result=st, headers={**SECURITY, "ETag": _etag(st)},
                        content_disposition_type="attachment" if request.query_params.get("download") else "inline",
                        filename=os.path.basename(full))


def _parent_for_write(user: dict, path: str) -> tuple[dict, str, str]:
    """(share, real parent folder, new name) for creating `path`; raises 409 if the parent is missing."""
    parent, _, name = path.rpartition("/")
    if not parent:
        raise HTTPException(403, "shared folders are created in Control Panel")
    files._check_name(name)
    share, pfull = files.resolve(user, parent, write=True)
    if not os.path.isdir(pfull):
        raise HTTPException(409, "the parent folder doesn't exist")
    return share, files._confine(pfull, os.path.realpath(share["path"])), name


async def put(user: dict, path: str, request: Request) -> Response:
    share, folder, name = await run_in_threadpool(_parent_for_write, user, path)
    target = os.path.join(folder, name)
    if os.path.isdir(target) and not os.path.islink(target):
        return Response("a folder has this name", status_code=405)
    existed = os.path.lexists(target)
    tmp = os.path.join(folder, f".{name[:64]}.{uuid.uuid4().hex[:8]}.uploading")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o664)
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        files._confine(folder, os.path.realpath(share["path"]))
        os.replace(tmp, target)
    finally:
        if os.path.lexists(tmp):
            os.remove(tmp)
    files._own(target, user)
    return Response(status_code=204 if existed else 201, headers={"ETag": _etag(os.stat(target))})


def delete(user: dict, path: str) -> Response:
    if "/" not in path:
        raise HTTPException(403, "shared folders are deleted in Control Panel")
    _, full = files.resolve(user, path, write=True)
    if not os.path.lexists(full):
        return Response(status_code=404)
    files.delete(files.Paths(paths=[path]), user)          # (to the recycle bin when it is on)
    return Response(status_code=204)


def mkcol(user: dict, path: str) -> Response:
    _, folder, name = _parent_for_write(user, path)
    target = os.path.join(folder, name)
    if os.path.lexists(target):
        return Response(status_code=405)
    os.mkdir(target, 0o2775)
    files._own(target, user)
    return Response(status_code=201)


def copy_move(user: dict, path: str, request: Request, move: bool) -> Response:
    dest_url = request.headers.get("destination", "")
    if not dest_url:
        raise HTTPException(400, "Destination header missing")
    dpath = urllib.parse.unquote(urllib.parse.urlsplit(dest_url).path).strip("/")
    overwrite = request.headers.get("overwrite", "T").upper() != "F"
    if "/" not in path or not dpath:
        raise HTTPException(403, "shared folders can't be copied or moved")
    sshare, src = files.resolve(user, path, write=move)
    if not os.path.lexists(src):
        return Response(status_code=404)
    dshare, folder, name = _parent_for_write(user, dpath)
    target = os.path.join(folder, name)
    if target == src:
        return Response(status_code=403)
    if os.path.isdir(src) and (target + os.sep).startswith(src + os.sep):
        raise HTTPException(409, "can't copy or move a folder into itself")
    existed = os.path.lexists(target)
    if existed:
        if not overwrite:
            return Response(status_code=412)
        files.delete(files.Paths(paths=[dpath]), user)     # the replaced item goes to the recycle bin
    files._confine(folder, os.path.realpath(dshare["path"]))
    if move:
        files._confine(src, os.path.realpath(sshare["path"]))
        shutil.move(src, target)
    elif os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, target, symlinks=True)
    else:
        shutil.copy2(src, target, follow_symlinks=False)
    files._own(target, user)
    return Response(status_code=204 if existed else 201)


def proppatch(user: dict, path: str, body: bytes) -> Response:
    """Pretend to store the properties (Windows sets timestamps this way): answer 200 for each."""
    files.resolve(user, path, write=True)
    names = []
    try:
        for el in ET.fromstring(body or b"<x/>").iter():
            if el.tag.endswith("}prop"):
                names += [c.tag for c in el]
    except ET.ParseError:
        pass
    props = "".join(f'<ns0:{t.split("}")[-1]} xmlns:ns0="{escape(t[1:].split("}")[0]) if t.startswith("{") else ""}"/>' for t in names)
    return _multistatus([f"<D:response><D:href>{escape(_href(path, False))}</D:href><D:propstat><D:prop>{props}</D:prop>"
                         "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"])


def lock(user: dict, path: str, request: Request) -> Response:
    if "/" not in path:
        raise HTTPException(403, "locking a whole shared folder isn't supported")
    share, full = files.resolve(user, path, write=True)
    created = False
    if not os.path.lexists(full):                          # LOCK on a new name creates an empty file
        share, folder, name = _parent_for_write(user, path)
        full = os.path.join(folder, name)
        os.close(os.open(full, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o664))
        files._own(full, user)
        created = True
    m = re.search(r"<(opaquelocktoken:[0-9a-fA-F-]{36})>", request.headers.get("if", ""))   # a refresh keeps its token
    token = m.group(1) if m else f"opaquelocktoken:{uuid.uuid4()}"
    timeout = "Second-3600"
    body = ('<?xml version="1.0" encoding="utf-8"?>\n<D:prop xmlns:D="DAV:"><D:lockdiscovery><D:activelock>'
            "<D:locktype><D:write/></D:locktype><D:lockscope><D:exclusive/></D:lockscope><D:depth>0</D:depth>"
            f"<D:owner>{escape(user['user'])}</D:owner><D:timeout>{timeout}</D:timeout>"
            f"<D:locktoken><D:href>{token}</D:href></D:locktoken>"
            f"<D:lockroot><D:href>{escape(_href(path, False))}</D:href></D:lockroot>"
            "</D:activelock></D:lockdiscovery></D:prop>")
    return Response(body, status_code=201 if created else 200, media_type='application/xml; charset="utf-8"',
                    headers={"Lock-Token": f"<{token}>"})


# ---------- the app ----------

async def dav(request: Request) -> Response:
    m = request.method
    if m == "OPTIONS":
        return Response(headers={"DAV": "1, 2", "MS-Author-Via": "DAV", "Allow": ", ".join(METHODS)})
    ip = request.client.host if request.client else "?"
    user = await run_in_threadpool(authenticate, request.headers.get("authorization", ""), ip)
    if not user:
        return Response("Sign in with your Cynthology user name and password.", status_code=401,
                        headers={"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'})
    path = _dav_path(request)
    if "\x00" in path or any(p in ("..", ".") for p in path.split("/")):
        return Response(status_code=400)
    try:
        if m == "PROPFIND":
            return await run_in_threadpool(propfind, user, path, request.headers.get("depth", "1"))
        if m in ("GET", "HEAD"):
            return await run_in_threadpool(get, user, path, request)
        if m == "PUT":
            return await put(user, path, request)
        if m == "DELETE":
            return await run_in_threadpool(delete, user, path)
        if m == "MKCOL":
            if await request.body():
                return Response(status_code=415)
            return await run_in_threadpool(mkcol, user, path)
        if m in ("COPY", "MOVE"):
            return await run_in_threadpool(copy_move, user, path, request, m == "MOVE")
        if m == "PROPPATCH":
            return await run_in_threadpool(proppatch, user, path, await request.body())
        if m == "LOCK":
            await request.body()
            return await run_in_threadpool(lock, user, path, request)
        if m == "UNLOCK":
            return Response(status_code=204)
    except HTTPException as e:
        return _err(e)
    except FileNotFoundError:
        return Response(status_code=404)
    except (PermissionError, IsADirectoryError, NotADirectoryError) as e:
        return Response(str(e.strerror or e), status_code=409 if isinstance(e, NotADirectoryError) else 403)
    return Response(status_code=405)


app = Starlette(routes=[Route("/{path:path}", dav, methods=METHODS)])


# ---------- run ----------

async def main() -> None:
    import uvicorn
    from . import fileservices, tls
    cfg = fileservices.load()["webdav"]
    host = os.environ.get("CYN_BIND", "0.0.0.0")
    configs = []
    if cfg.get("http"):
        configs.append(uvicorn.Config(app, host=host, port=int(cfg["http_port"]), proxy_headers=False, log_level="warning"))
    if cfg.get("https") and tls.ensure_cert():
        configs.append(uvicorn.Config(app, host=host, port=int(cfg["https_port"]), ssl_certfile=tls.CERT, ssl_keyfile=tls.KEY,
                                      proxy_headers=False, log_level="warning"))
    if not configs:
        print("WebDAV: neither HTTP nor HTTPS is switched on")
        return
    servers = [uvicorn.Server(c) for c in configs]
    for s in servers[1:]:
        s.install_signal_handlers = lambda: None
    first = asyncio.ensure_future(servers[0].serve())
    rest = [asyncio.ensure_future(s.serve()) for s in servers[1:]]
    await first
    for s in servers[1:]:
        s.should_exit = True
    await asyncio.gather(*rest, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
