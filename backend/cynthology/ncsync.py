"""Nextcloud-compatible sync: the official Nextcloud desktop and phone apps can sync with the NAS.

Server address in the app: https://<nas>:5001 (the normal web interface). Implemented:
  /status.php                               - "this is a Nextcloud-compatible server"
  /index.php/login/v2 (+ /poll, /flow/...)  - Login Flow v2 (desktop): sign in in the browser, the app
                                              gets an app password of its own (revocable, Personal > Security)
  /index.php/login/flow                     - Login Flow v1 (phone apps): same, ends at nc://login/...
  /ocs/v{1,2}.php/cloud/capabilities, /user - what the apps ask about the server and the account
  /remote.php/dav/files/<user>/...          - files (WebDAV + Nextcloud properties), also /remote.php/webdav/
  /remote.php/dav/uploads/<user>/<id>/...   - chunked, resumable uploads of big files ("chunking NG")
What the apps see is what File Station shows: one folder per shared folder you may open (and "home").
Same permissions, same recycle bin. Folder ETags change whenever anything below them changes (the apps
rely on that to find remote changes); they are computed from the tree and cached per shared folder
until the Btrfs generation of the folder's subvolume moves.

Sign-in: an app password, or - only for accounts without 2-step verification or passkeys - the account
password (e.g. for nextcloudcmd). Control Panel > Application Privileges > "Nextcloud apps" applies.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
import urllib.parse
from email.utils import formatdate
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.routing import Route

from . import files, shares as shares_mod, webdav
from .auth import current_user
from .util import DEV, STATE_DIR

router = APIRouter(tags=["nextcloud"])          # public paths the apps expect (no /api prefix)
api = APIRouter(prefix="/api/ncsync", tags=["nextcloud"])
FILE = os.path.join(STATE_DIR, "ncsync.json")
VERSION = {"major": 28, "minor": 0, "micro": 4, "string": "28.0.4", "edition": "", "extendedSupport": False}
NS = 'xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns"'
_lock = threading.Lock()
FLOWS: dict[str, dict] = {}       # poll token -> {login: token, exp, granted: {...}}


# ---------- app passwords ----------

def _load() -> dict:
    try:
        with open(FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    fd = os.open(FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(FILE + ".tmp", FILE)


def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()       # (random 64-character passwords: a plain hash is enough)


def new_app_password(user: str, name: str) -> str:
    pw = secrets.token_urlsafe(48)
    with _lock:
        d = _load()
        lst = d.setdefault("apppw", {}).setdefault(user.lower(), [])
        lst.append({"id": secrets.token_hex(6), "name": re.sub(r"[\x00-\x1f\x7f]", "", name)[:80] or "Nextcloud app",
                    "hash": _hash(pw), "created": int(time.time()), "last_used": None})
        _save(d)
    return pw


def remove_user(user: str) -> None:
    with _lock:
        d = _load()
        if d.get("apppw", {}).pop(user.lower(), None) is not None:
            _save(d)


_seen: dict[str, float] = {}


def _check_app_password(user: str, pw: str) -> bool:
    h = _hash(pw)
    for e in _load().get("apppw", {}).get(user.lower(), []):
        if secrets.compare_digest(e["hash"], h):
            if time.time() - _seen.get(e["id"], 0) > 600:          # (note "last used" now and then, not on every request)
                _seen[e["id"]] = time.time()
                with _lock:
                    d = _load()
                    for x in d.get("apppw", {}).get(user.lower(), []):
                        if x["id"] == e["id"]:
                            x["last_used"] = int(time.time())
                    _save(d)
            return True
    return False


def authenticate(request: Request) -> dict | None:
    """Basic auth: an app password - or the account password if the account has no second factor."""
    from .auth import _canonical, _is_admin
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return None
    try:
        name, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    if not name or not pw or len(name) > 128 or len(pw) > 1024:
        return None
    user = _canonical(name)
    if _check_app_password(user, pw):
        return {"user": user, "admin": _is_admin(user)}
    from . import passkeys, twofa
    if twofa.enabled(user) or passkeys.has(user):
        return None                                   # (a password alone isn't enough for these accounts)
    ip = request.client.host if request.client else "?"
    return webdav.authenticate(header, ip)


def _deny(msg="Sign in with your NAS user name and an app password.") -> Response:
    return Response(msg, status_code=401, headers={"WWW-Authenticate": 'Basic realm="Cynthology", charset="UTF-8"'})


# ---------- server info ----------

def _base(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


@router.get("/status.php")
def status_php():
    return {"installed": True, "maintenance": False, "needsDbUpgrade": False, "version": "28.0.4.1",
            "versionstring": "28.0.4", "edition": "", "productname": "Cynthology", "extendedSupport": False}


def _ocs(request: Request, data, status: int = 200, message: str = "OK") -> JSONResponse:
    v1 = "/ocs/v1.php/" in request.url.path
    ok = status < 300
    code = (100 if ok else status) if v1 else status
    return JSONResponse({"ocs": {"meta": {"status": "ok" if ok else "failure", "statuscode": code, "message": message},
                                 "data": data}}, status_code=200 if v1 or ok else status)


CAPS = {"core": {"pollinterval": 60, "webdav-root": "remote.php/webdav"},
        "dav": {"chunking": "1.0"},
        "files": {"bigfilechunking": True, "blacklisted_files": [".htaccess"], "undelete": False, "versioning": False},
        "theming": {"name": "Cynthology", "url": "", "slogan": "", "color": "#2bb3c0", "color-text": "#ffffff",
                    "color-element": "#2bb3c0", "logo": "", "background": "", "background-plain": True, "background-default": True}}


@router.get("/ocs/v1.php/cloud/capabilities")
@router.get("/ocs/v2.php/cloud/capabilities")
def capabilities(request: Request):
    return _ocs(request, {"version": VERSION, "capabilities": CAPS})


@router.get("/ocs/v1.php/cloud/user")
@router.get("/ocs/v2.php/cloud/user")
def cloud_user(request: Request):
    u = authenticate(request)
    if not u:
        return _deny()
    from .account import _full_name, profile
    v = None
    try:
        root = shares_mod.home_path(u["user"]) or next((s["path"] for s in shares_mod.load()), "/")
        v = os.statvfs(root)
    except OSError:
        pass
    free = v.f_bavail * v.f_frsize if v else 0
    used = (v.f_blocks - v.f_bfree) * v.f_frsize if v else 0
    return _ocs(request, {"id": u["user"], "display-name": _full_name(u["user"]) or u["user"], "email": profile(u["user"]).get("email") or None,
                          "enabled": True, "quota": {"free": free, "used": used, "total": free + used, "relative": 0, "quota": -3}})


@router.api_route("/ocs/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
def ocs_other(rest: str, request: Request):
    return _ocs(request, [], 404, "not available on this server")


# ---------- Login Flow v2 (desktop) and v1 (phone apps) ----------

def _clean_flows() -> None:
    now = time.time()
    for k in [k for k, v in FLOWS.items() if v["exp"] < now]:
        FLOWS.pop(k, None)


@router.post("/index.php/login/v2")
def flow_v2_start(request: Request):
    _clean_flows()
    if len(FLOWS) > 200:
        raise HTTPException(429, "too many sign-ins in progress")
    poll, login = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    FLOWS[poll] = {"login": login, "exp": time.time() + 1200, "granted": None,
                   "agent": request.headers.get("user-agent", "")[:200]}
    base = _base(request)
    return {"poll": {"token": poll, "endpoint": f"{base}/index.php/login/v2/poll"}, "login": f"{base}/index.php/login/v2/flow/{login}"}


@router.post("/index.php/login/v2/poll")
async def flow_v2_poll(request: Request):
    body = (await request.body())[:4096].decode("utf-8", "replace")
    token = urllib.parse.parse_qs(body).get("token", [""])[0] or request.query_params.get("token", "")
    f = FLOWS.get(token)
    if not f or not f["granted"]:
        return JSONResponse([], status_code=404)
    FLOWS.pop(token, None)
    return f["granted"]


def _login_page() -> FileResponse:
    here = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(here, "..", "..", "frontend", "nclogin.html"), headers={"Cache-Control": "no-store"})


@router.get("/index.php/login/v2/flow/{login}")
def flow_v2_page(login: str):
    if not any(f["login"] == login for f in FLOWS.values()):
        raise HTTPException(404, "this sign-in link expired - start again in the app")
    return _login_page()


@router.get("/index.php/login/flow")
def flow_v1_page():
    return _login_page()


@router.get("/index.php/login/v2/info/{login}")
def flow_info(login: str):
    f = next((f for f in FLOWS.values() if f["login"] == login), None)
    if not f:
        raise HTTPException(404, "this sign-in link expired - start again in the app")
    return {"app": _app_name(f["agent"])}


def _app_name(agent: str) -> str:
    m = re.search(r"\(([^)]+)\)", agent or "")
    return f"Nextcloud app ({m.group(1)[:60]})" if m else ("Nextcloud app" if not agent else agent[:60])


@api.post("/grant")
def grant(request: Request, flow: str = "", user: dict = Depends(current_user)):
    """The signed-in user allows an app: v2 hands the app password to the polling app, v1 returns nc://..."""
    from .privileges import allowed
    ip = request.client.host if request.client else None
    if not allowed("sync", user["user"], ip, user.get("admin")):
        raise HTTPException(403, "You don't have permission to use the Nextcloud apps (Control Panel > Application Privileges).")
    base = _base(request)
    if flow:
        poll = next((k for k, f in FLOWS.items() if f["login"] == flow and f["exp"] > time.time()), None)
        if not poll:
            raise HTTPException(404, "this sign-in link expired - start again in the app")
        f = FLOWS[poll]
        pw = new_app_password(user["user"], _app_name(f["agent"]))
        f["granted"] = {"server": base, "loginName": user["user"], "appPassword": pw}
        return {"ok": True}
    pw = new_app_password(user["user"], _app_name(request.headers.get("user-agent", "")))
    q = urllib.parse.quote
    return {"redirect": f"nc://login/server:{base}&user:{q(user['user'], safe='')}&password:{q(pw, safe='')}"}


@api.get("/apps")
def my_apps(user: dict = Depends(current_user)):
    return [{k: e[k] for k in ("id", "name", "created", "last_used")} for e in _load().get("apppw", {}).get(user["user"].lower(), [])]


@api.delete("/apps/{aid}")
def revoke(aid: str, user: dict = Depends(current_user)):
    with _lock:
        d = _load()
        lst = d.get("apppw", {}).get(user["user"].lower(), [])
        keep = [e for e in lst if e["id"] != aid]
        if len(keep) == len(lst):
            raise HTTPException(404, "no such app")
        d["apppw"][user["user"].lower()] = keep
        _save(d)
    return {"ok": True}


# ---------- folder ETags (change when anything below changes) ----------

_tree: dict[str, tuple[str, dict]] = {}      # share root -> (generation, {dir: (etag, size)})
_gen_cache: dict[str, tuple[float, str]] = {}


def _generation(root: str) -> str:
    hit = _gen_cache.get(root)
    if hit and time.time() - hit[0] < 3:
        return hit[1]
    gen = ""
    if not DEV:
        out = subprocess.run(["btrfs", "subvolume", "show", root], capture_output=True, text=True).stdout
        m = re.search(r"^\s*Generation:\s*(\d+)", out, re.M)
        gen = m.group(1) if m else ""
    if not gen:                                   # (not Btrfs: recompute at most every 20 seconds)
        gen = f"t{int(time.time() // 20)}"
    _gen_cache[root] = (time.time(), gen)
    return gen


def _hidden(name: str) -> bool:
    return name.startswith(".") or name in ("@eaDir", "lost+found", "#recycle")


def _walk(root: str) -> dict:
    """{dir: (etag, size)} for every folder below root: from the newest change time and total size beneath it."""
    out = {}

    def visit(d: str) -> tuple[int, int, int]:
        try:
            st = os.stat(d)
            newest, size, count = st.st_mtime_ns, 0, 0
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            return 0, 0, 0
        for e in entries:
            if _hidden(e.name):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    n, s, c = visit(e.path)
                    newest, size, count = max(newest, n), size + s, count + c + 1
                else:
                    est = e.stat(follow_symlinks=False)
                    newest, size, count = max(newest, est.st_mtime_ns), size + est.st_size, count + 1
            except OSError:
                continue
        out[d] = (hashlib.sha1(f"{d}|{newest}|{count}|{size}".encode()).hexdigest()[:20], size)
        return newest, size, count
    visit(root)
    return out


def dir_info(root: str, full: str) -> tuple[str, int]:
    gen = _generation(root)
    hit = _tree.get(root)
    if not hit or hit[0] != gen:
        hit = (gen, _walk(root))
        _tree[root] = hit
        if len(_tree) > 200:
            _tree.clear()
    return hit[1].get(full) or (hashlib.sha1(f"{full}|{gen}".encode()).hexdigest()[:20], 0)


def _fileid(st: os.stat_result) -> int:
    return int.from_bytes(hashlib.sha1(f"{st.st_dev}:{st.st_ino}".encode()).digest()[:6], "big")


# ---------- WebDAV with Nextcloud properties ----------

def _prop(href: str, name: str, st: os.stat_result, is_dir: bool, etag: str, size: int, perms: str,
          quota: tuple[int, int] | None = None) -> str:
    fid = _fileid(st)
    p = [f"<d:displayname>{escape(name)}</d:displayname>",
         f"<d:getlastmodified>{formatdate(st.st_mtime, usegmt=True)}</d:getlastmodified>",
         f"<d:getetag>&quot;{etag}&quot;</d:getetag>",
         "<d:resourcetype><d:collection/></d:resourcetype>" if is_dir else "<d:resourcetype/>",
         f"<oc:id>{fid:08d}ocnas</oc:id><oc:fileid>{fid}</oc:fileid>",
         f"<oc:permissions>{perms}</oc:permissions>", f"<oc:size>{size}</oc:size>",
         "<oc:share-types/><nc:has-preview>false</nc:has-preview><oc:downloadURL></oc:downloadURL><oc:dDC></oc:dDC>"]
    if not is_dir:
        import mimetypes
        p.append(f"<d:getcontentlength>{st.st_size}</d:getcontentlength>")
        p.append(f"<d:getcontenttype>{escape(mimetypes.guess_type(name)[0] or 'application/octet-stream')}</d:getcontenttype>")
    if quota:
        p.append(f"<d:quota-available-bytes>{quota[0]}</d:quota-available-bytes><d:quota-used-bytes>{quota[1]}</d:quota-used-bytes>")
    return (f"<d:response><d:href>{escape(href)}</d:href><d:propstat><d:prop>{''.join(p)}</d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")


def _multistatus(parts: list[str]) -> Response:
    body = f'<?xml version="1.0" encoding="utf-8"?>\n<d:multistatus {NS}>' + "".join(parts) + "</d:multistatus>"
    return Response(body, status_code=207, media_type='application/xml; charset="utf-8"')


def _file_etag(st: os.stat_result) -> str:
    return hashlib.sha1(f"{st.st_ino}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()[:20]


def _perms(mode: str, is_dir: bool, top: bool) -> str:
    if mode != "rw":
        return "G"
    if top:
        return "GCK"                               # (shared folders themselves can't be renamed or deleted here)
    return "RGDNVCK" if is_dir else "RGDNVW"


def _share_of(user: dict, path: str) -> tuple[dict, str, str]:
    """(share, mode, real root) for a File Station path."""
    if path.split("/")[0] == "docker":
        raise HTTPException(404, "container data isn't synced")
    share, _ = files.resolve(user, path.split("/")[0])
    mode = "rw" if share["name"] == "home" else files.access(user, share)
    return share, mode, os.path.realpath(share["path"])


def propfind(user: dict, path: str, depth: str, base: str) -> Response:
    href = lambda p, d: base + urllib.parse.quote(p, safe="/") + ("/" if d and p else "")       # noqa: E731
    if not path:
        # (container data - live databases - must never be synced to laptops)
        names = [n for n in webdav._shares_for(user) if n != "docker"]
        tops = []
        for n in names:
            try:
                share, mode, root = _share_of(user, n)
                st = os.stat(root)
                etag, size = dir_info(root, root)
                tops.append((n, st, etag, size, mode))
            except (HTTPException, OSError):
                continue
        st0 = os.stat(STATE_DIR if os.path.isdir(STATE_DIR) else "/")
        root_etag = hashlib.sha1("|".join(f"{n}:{e}" for n, _, e, _, _ in tops).encode()).hexdigest()[:20]
        parts = [_prop(base, user["user"], st0, True, root_etag, sum(t[3] for t in tops), "G")]
        if depth != "0":
            parts += [_prop(href(n, True), n, st, True, e, size, _perms(m, True, True)) for n, st, e, size, m in tops]
        return _multistatus(parts)
    share, mode, root = _share_of(user, path)
    _, full = files.resolve(user, path)
    try:
        st = os.stat(full)
    except FileNotFoundError:
        return Response(status_code=404)
    is_dir = stat.S_ISDIR(st.st_mode)
    top = "/" not in path
    if not is_dir:
        return _multistatus([_prop(href(path, False), os.path.basename(path), st, False, _file_etag(st), st.st_size, _perms(mode, False, False))])
    etag, size = dir_info(root, full)
    v = os.statvfs(full)
    parts = [_prop(href(path, True), os.path.basename(path), st, True, etag, size, _perms(mode, True, top),
                   (v.f_bavail * v.f_frsize, (v.f_blocks - v.f_bfree) * v.f_frsize))]
    if depth != "0":
        with os.scandir(full) as it:
            for e in sorted(it, key=lambda e: e.name):
                if _hidden(e.name):
                    continue
                try:
                    est = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(est.st_mode):
                    continue                            # (links aren't synced)
                d = stat.S_ISDIR(est.st_mode)
                cp = f"{path}/{e.name}"
                if d:
                    ce, cs = dir_info(root, e.path)
                    parts.append(_prop(href(cp, True), e.name, est, True, ce, cs, _perms(mode, True, False)))
                else:
                    parts.append(_prop(href(cp, False), e.name, est, False, _file_etag(est), est.st_size, _perms(mode, False, False)))
    return _multistatus(parts)


def _set_mtime(user: dict, path: str, request: Request, headers: dict) -> None:
    m = request.headers.get("x-oc-mtime")
    if m and re.fullmatch(r"\d{1,12}", m):
        _, full = files.resolve(user, path, write=True)
        fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.utime(fd, (time.time(), int(m)))
        finally:
            os.close(fd)
        headers["X-OC-MTime"] = "accepted"


def _file_headers(user: dict, path: str) -> dict:
    _, full = files.resolve(user, path)
    st = os.stat(full)
    e = f'"{_file_etag(st)}"'
    return {"ETag": e, "OC-ETag": e, "OC-FileId": f"{_fileid(st):08d}ocnas"}


# ---------- chunked uploads ("chunking NG") ----------

UPLOADS: dict[tuple[str, str], str] = {}
CHUNK_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")


def _staging_base(user: dict, dest_path: str | None) -> str:
    """Chunks are kept on the same disk as their destination when known (assembling is then a local copy)."""
    root = None
    if dest_path:
        try:
            root = os.path.realpath(files.resolve(user, dest_path.split("/")[0], write=True)[0]["path"])
        except HTTPException:
            root = None
    if not root:
        from .storage import load_pools
        pools = load_pools()
        root = pools[0]["mount"] if pools else STATE_DIR
    return os.path.join(root, ".cyn-uploads", hashlib.sha1(user["user"].lower().encode()).hexdigest()[:16])


def _dest_from_header(request: Request, user: str) -> str | None:
    d = request.headers.get("destination")
    if not d:
        return None
    p = urllib.parse.unquote(urllib.parse.urlsplit(d).path)
    for pre in (f"/remote.php/dav/files/{user}/", "/remote.php/webdav/"):
        if p.lower().startswith(pre.lower()):
            return p[len(pre):].strip("/")
    raise HTTPException(400, "Destination outside your files")


def _cleanup_staging(base: str) -> None:
    try:
        for e in os.scandir(base):
            if e.is_dir(follow_symlinks=False) and time.time() - e.stat().st_mtime > 2 * 86400:
                shutil.rmtree(e.path, ignore_errors=True)
    except OSError:
        pass


def _upload_dir(user: dict, tid: str) -> str | None:
    d = UPLOADS.get((user["user"].lower(), tid))
    return d if d and os.path.isdir(d) else None


async def uploads(request: Request, user: dict, rest: str) -> Response:
    m = request.method
    parts = [p for p in rest.split("/") if p]
    if not parts or not CHUNK_RE.fullmatch(parts[0]) or len(parts) > 2 or (len(parts) == 2 and not CHUNK_RE.fullmatch(parts[1])):
        return Response(status_code=400)
    tid = parts[0]
    if m == "MKCOL" and len(parts) == 1:
        dest = _dest_from_header(request, user["user"])
        base = await run_in_threadpool(_staging_base, user, dest)
        os.makedirs(base, mode=0o700, exist_ok=True)
        _cleanup_staging(base)
        d = os.path.join(base, tid)
        if os.path.exists(d):
            return Response(status_code=405)
        os.mkdir(d, 0o700)
        UPLOADS[(user["user"].lower(), tid)] = d
        return Response(status_code=201)
    d = _upload_dir(user, tid)
    if not d:
        return Response(status_code=404)
    if m == "PUT" and len(parts) == 2 and parts[1] != ".file":
        tmp = os.path.join(d, f".{parts[1]}.part")
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        os.replace(tmp, os.path.join(d, parts[1]))
        return Response(status_code=201)
    if m == "PROPFIND":
        st = os.stat(d)
        out = [f"<d:response><d:href>{escape(request.url.path)}</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>"
               f"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"]
        for e in sorted(os.scandir(d), key=lambda e: e.name):
            if e.name.startswith("."):
                continue
            out.append(f"<d:response><d:href>{escape(request.url.path.rstrip('/') + '/' + e.name)}</d:href><d:propstat><d:prop>"
                       f"<d:getcontentlength>{e.stat().st_size}</d:getcontentlength><d:resourcetype/></d:prop>"
                       "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
        del st
        return _multistatus(out)
    if m == "DELETE" and len(parts) == 1:
        shutil.rmtree(d, ignore_errors=True)
        UPLOADS.pop((user["user"].lower(), tid), None)
        return Response(status_code=204)
    if m == "MOVE" and len(parts) == 2 and parts[1] == ".file":
        dest = _dest_from_header(request, user["user"])
        if not dest:
            return Response(status_code=400)
        return await run_in_threadpool(_assemble, user, d, dest, request, tid)
    return Response(status_code=405)


def _assemble(user: dict, d: str, dest: str, request: Request, tid: str) -> Response:
    chunks = [e.name for e in os.scandir(d) if not e.name.startswith(".")]
    chunks.sort(key=lambda n: (int(n) if n.isdigit() else 0, n))
    total = sum(os.path.getsize(os.path.join(d, c)) for c in chunks)
    want = request.headers.get("oc-total-length")
    if want and want.isdigit() and int(want) != total:
        return Response(f"expected {want} bytes, got {total}", status_code=400)
    share, folder, name = webdav._parent_for_write(user, dest)
    target = os.path.join(folder, name)
    if os.path.isdir(target) and not os.path.islink(target):
        return Response("a folder has this name", status_code=409)
    existed = os.path.lexists(target)
    tmp = os.path.join(folder, f".{name[:64]}.{secrets.token_hex(4)}.uploading")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o664)
    try:
        with os.fdopen(fd, "wb") as out:
            for c in chunks:
                with open(os.path.join(d, c), "rb") as f:
                    shutil.copyfileobj(f, out, 4 << 20)
        files._confine(folder, os.path.realpath(share["path"]))
        os.replace(tmp, target)
    finally:
        if os.path.lexists(tmp):
            os.remove(tmp)
    files._own(target, user)
    shutil.rmtree(d, ignore_errors=True)
    UPLOADS.pop((user["user"].lower(), tid), None)
    headers = {}
    _set_mtime(user, dest, request, headers)
    headers.update(_file_headers(user, dest))
    return Response(status_code=204 if existed else 201, headers=headers)


# ---------- the DAV app (mounted at /remote.php) ----------

def _translate(request: Request, user: dict) -> tuple[str, str, str] | Response:
    """(kind, File Station path or uploads rest, URL prefix for hrefs) from /remote.php/... ."""
    raw = request.url.path
    m = re.match(r"^(/remote\.php/dav/(files|uploads)/([^/]+))(/.*)?$", raw)
    if m:
        if urllib.parse.unquote(m.group(3)).lower() != user["user"].lower():
            return Response("that's not your account", status_code=403)
        return m.group(2), urllib.parse.unquote(m.group(4) or "").strip("/"), m.group(1) + "/"
    m = re.match(r"^(/remote\.php/webdav)(/.*)?$", raw)
    if m:
        return "files", urllib.parse.unquote(m.group(2) or "").strip("/"), m.group(1) + "/"
    return Response(status_code=404)


async def dav(request: Request) -> Response:
    m = request.method
    if m == "OPTIONS":
        return Response(headers={"DAV": "1, 2", "Allow": ", ".join(webdav.METHODS)})
    user = await run_in_threadpool(authenticate, request)
    if not user:
        return _deny()
    from .privileges import allowed
    ip = request.client.host if request.client else "?"
    if not allowed("sync", user["user"], ip, user.get("admin")):
        return Response("You don't have permission to use the Nextcloud apps (Control Panel > Application Privileges).", status_code=403)
    t = _translate(request, user)
    if isinstance(t, Response):
        return t
    kind, path, base = t
    if "\x00" in path or any(x in ("..", ".") for x in path.split("/")):
        return Response(status_code=400)
    if kind == "files" and (path.split("/")[0] == "docker" or any(_hidden(x) for x in path.split("/") if x)):
        return Response(status_code=404)                # (.snapshots, #recycle... aren't synced)
    try:
        if kind == "uploads":
            return await uploads(request, user, path)
        if m == "PROPFIND":
            return await run_in_threadpool(propfind, user, path, request.headers.get("depth", "1"), base)
        if m in ("GET", "HEAD"):
            if not path or os.path.isdir(files.resolve(user, path)[1]):
                return Response(status_code=405)
            r = await run_in_threadpool(webdav.get, user, path, request)
            r.headers.update(await run_in_threadpool(_file_headers, user, path))
            return r
        if m == "PUT":
            r = await webdav.put(user, path, request)
            if r.status_code < 300:
                headers = {}
                await run_in_threadpool(_set_mtime, user, path, request, headers)
                headers.update(await run_in_threadpool(_file_headers, user, path))
                return Response(status_code=r.status_code, headers=headers)
            return r
        if m == "DELETE":
            return await run_in_threadpool(webdav.delete, user, path)
        if m == "MKCOL":
            r = await run_in_threadpool(webdav.mkcol, user, path)
            if r.status_code == 201:
                _, full = files.resolve(user, path)
                r.headers["OC-FileId"] = f"{_fileid(os.stat(full)):08d}ocnas"
            return r
        if m in ("COPY", "MOVE"):
            dest = _dest_from_header(request, user["user"])
            if dest is None:
                return Response(status_code=400)
            r = await run_in_threadpool(webdav.copy_move, user, path, request, m == "MOVE", dest)
            if r.status_code < 300:
                try:
                    _, full = files.resolve(user, dest)
                    st = os.stat(full)
                    r.headers["OC-FileId"] = f"{_fileid(st):08d}ocnas"
                    if not stat.S_ISDIR(st.st_mode):
                        r.headers["ETag"] = r.headers["OC-ETag"] = f'"{_file_etag(st)}"'
                except (HTTPException, OSError):
                    pass
            return r
        if m == "PROPPATCH":
            return await run_in_threadpool(webdav.proppatch, user, path, await request.body())
        if m == "LOCK":
            await request.body()
            return await run_in_threadpool(webdav.lock, user, path, request)
        if m == "UNLOCK":
            return Response(status_code=204)
    except HTTPException as e:
        return Response(str(e.detail), status_code=e.status_code, media_type="text/plain")
    except FileNotFoundError:
        return Response(status_code=404)
    except (PermissionError, IsADirectoryError, NotADirectoryError) as e:
        return Response(str(e.strerror or e), status_code=409 if isinstance(e, NotADirectoryError) else 403)
    return Response(status_code=405)


dav_app = Starlette(routes=[Route("/{path:path}", dav, methods=webdav.METHODS + ["REPORT", "SEARCH"])])
