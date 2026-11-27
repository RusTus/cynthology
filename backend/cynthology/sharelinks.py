"""Sharing links (DSM: File Station > Share): a link anyone can open without an account.

A link points at one file or folder and can have a password, an expiry date and a download
limit. Every request re-checks that the person who made the link still has access to the item
(their account still exists, the folder permissions still allow it), so removing someone's
access also kills their links. Paths in folder links are confined to the shared folder, and
downloads are always attachments with a sandbox policy - a shared HTML file can't run as a page.

Public URLs: /s/<id> (page), /s/<id>/info, /s/<id>/unlock, /s/<id>/dl
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import files
from .auth import current_user
from .util import DEV, STATE_DIR

router = APIRouter(prefix="/api/sharelinks", tags=["sharelinks"])
public = APIRouter(prefix="/s", tags=["sharelinks-public"])
CFG = os.path.join(STATE_DIR, "sharelinks.json")
KEY_FILE = os.path.join(STATE_DIR, "sharelinks.key")
UNLOCK_TTL = 12 * 3600
_lock = threading.Lock()
_fails: dict[str, list[float]] = {}


def load() -> list[dict]:
    try:
        return json.load(open(CFG))
    except (OSError, ValueError):
        return []


def save(items: list[dict]) -> None:
    fd = os.open(CFG + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(items, f, indent=1)
    os.replace(CFG + ".new", CFG)


def _key() -> bytes:
    try:
        return open(KEY_FILE, "rb").read()
    except OSError:
        k = secrets.token_bytes(32)
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(k)
        return k


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + ":" + hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1).hex()


def check_pw(pw: str, stored: str) -> bool:
    salt, _, _ = stored.partition(":")
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(salt)), stored)


def _unlock_token(lid: str, exp: int) -> str:
    return f"{exp}.{hmac.new(_key(), f'{lid}|{exp}'.encode(), hashlib.sha256).hexdigest()}"


def _unlocked(request: Request, link: dict) -> bool:
    if not link.get("password"):
        return True
    tok = request.cookies.get(f"cyn_s_{link['id']}", "")
    exp, _, _ = tok.partition(".")
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(tok, _unlock_token(link["id"], int(exp)))


def state(link: dict) -> str:
    if not link.get("enabled", True):
        return "disabled"
    if link.get("expires") and link["expires"] < time.time():
        return "expired"
    if link.get("max_downloads") and link.get("downloads", 0) >= link["max_downloads"]:
        return "used up"
    return "active"


def _owner(link: dict) -> dict:
    """The link's creator as File Station sees them - None if the account is gone."""
    from .auth import _is_admin
    name = link["owner"]
    if not DEV and "\\" not in name:
        import pwd
        try:
            pwd.getpwnam(name)
        except KeyError:
            raise HTTPException(404, "this link is no longer available")
    return {"user": name, "admin": _is_admin(name)}


def _get(lid: str) -> dict:
    link = next((x for x in load() if x["id"] == lid), None) if len(lid) < 64 else None
    if not link or state(link) in ("disabled", "expired"):
        raise HTTPException(404, "this link doesn't exist or has expired")
    return link


def _target(link: dict, rel: str = "") -> tuple[str, str]:
    """(real path of the shared item, real path of rel inside it). Checks the owner's access now."""
    try:
        share, root = files.resolve(_owner(link), link["path"])
    except HTTPException:
        raise HTTPException(404, "this link is no longer available")
    if not os.path.exists(root):
        raise HTTPException(404, "the shared item was moved or deleted")
    parts = [p for p in rel.strip("/").split("/") if p]
    if parts and (not os.path.isdir(root) or any(p in ("..", ".") or files._hidden(p) for p in parts)):
        raise HTTPException(404, "not found")
    full = os.path.realpath(os.path.join(root, *parts))
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(404, "not found")
    return root, full


# ---------- owner API ----------

class NewLink(BaseModel):
    path: str
    expires_days: int = 0          # 0 = never
    password: str = ""
    max_downloads: int = 0         # 0 = no limit


class EditLink(BaseModel):
    expires_days: int | None = None   # None = keep, 0 = never
    password: str | None = None       # None = keep, "" = remove
    max_downloads: int | None = None
    enabled: bool | None = None


def _public(link: dict) -> dict:
    return {k: v for k, v in link.items() if k != "password"} | {"has_password": bool(link.get("password")),
                                                                 "state": state(link), "url": f"/s/{link['id']}"}


def _mine(user: dict, link: dict) -> bool:
    return user["admin"] or link["owner"].lower() == user["user"].lower()


@router.get("")
def list_links(path: str = "", user=Depends(current_user)):
    return [_public(x) for x in load() if _mine(user, x) and (not path or x["path"] == path)]


@router.get("/addresses")
def addresses(_=Depends(current_user)):
    """Host names worth using in links (DDNS / certificate names) besides the one in the browser."""
    out = []
    try:
        from . import ddns
        out += [e["hostname"] for e in ddns.load() if e.get("enabled", True)]
    except Exception:
        pass
    try:
        from . import tls
        ci = tls.cert_info()
        if ci.get("kind") == "letsencrypt":       # names the trusted certificate covers
            out += [x.removeprefix("DNS:") for x in ci.get("san", []) if x.startswith("DNS:") and "*" not in x]
    except Exception:
        pass
    from . import settings
    return {"names": sorted(set(out)), "https_port": int(settings.get_all()["https_port"])}


@router.post("")
def create(body: NewLink, user=Depends(current_user)):
    _, full = files.resolve(user, body.path)            # (you can only share what you can read)
    if not os.path.exists(full):
        raise HTTPException(404, "no such file or folder")
    if body.path.strip("/").split("/")[0] == "home" and len(body.path.strip("/").split("/")) == 1:
        raise HTTPException(400, "share a folder inside your home folder, not the whole home folder")
    if not 0 <= body.expires_days <= 3650 or not 0 <= body.max_downloads <= 100000:
        raise HTTPException(400, "bad expiry or download limit")
    if body.password and len(body.password) < 4:
        raise HTTPException(400, "the password needs at least 4 characters")
    link = {"id": secrets.token_urlsafe(12), "owner": user["user"], "path": "/" + body.path.strip("/"),
            "name": os.path.basename(full) or body.path, "is_dir": os.path.isdir(full), "created": int(time.time()),
            "expires": int(time.time()) + body.expires_days * 86400 if body.expires_days else None,
            "password": hash_pw(body.password) if body.password else None,
            "max_downloads": body.max_downloads, "downloads": 0, "last_access": None, "enabled": True}
    with _lock:
        items = load()
        items.append(link)
        save(items)
    return _public(link)


@router.put("/{lid}")
def edit(lid: str, body: EditLink, user=Depends(current_user)):
    with _lock:
        items = load()
        link = next((x for x in items if x["id"] == lid and _mine(user, x)), None)
        if not link:
            raise HTTPException(404, "no such link")
        if body.expires_days is not None:
            if not 0 <= body.expires_days <= 3650:
                raise HTTPException(400, "bad expiry")
            link["expires"] = int(time.time()) + body.expires_days * 86400 if body.expires_days else None
        if body.password is not None:
            if body.password and len(body.password) < 4:
                raise HTTPException(400, "the password needs at least 4 characters")
            link["password"] = hash_pw(body.password) if body.password else None
        if body.max_downloads is not None:
            link["max_downloads"] = max(0, body.max_downloads)
            link["downloads"] = 0 if body.max_downloads else link.get("downloads", 0)
        if body.enabled is not None:
            link["enabled"] = body.enabled
        save(items)
    return _public(link)


@router.delete("/{lid}")
def delete(lid: str, user=Depends(current_user)):
    with _lock:
        items = load()
        if not any(x["id"] == lid and _mine(user, x) for x in items):
            raise HTTPException(404, "no such link")
        save([x for x in items if x["id"] != lid])
    return {"ok": True}


def remove_user(user: str) -> None:
    """A deleted user's links go too."""
    with _lock:
        save([x for x in load() if x["owner"].lower() != user.lower()])


# ---------- public ----------

def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


@public.get("/{lid}")
def page(lid: str):
    from .main import FRONTEND
    return FileResponse(os.path.join(FRONTEND, "share.html"), headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@public.get("/{lid}/info")
def info(lid: str, request: Request, p: str = ""):
    link = _get(lid)
    out = {"name": link["name"], "is_dir": link["is_dir"], "owner": link["owner"].split("\\")[-1],
           "expires": link.get("expires"), "needs_password": bool(link.get("password")), "unlocked": _unlocked(request, link),
           "used_up": state(link) == "used up"}
    if not out["unlocked"]:
        return out
    root, full = _target(link, p)
    if os.path.isdir(full):
        items = []
        with os.scandir(full) as it:
            for e in it:
                if files._hidden(e.name) or e.is_symlink():
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                items.append({"name": e.name, "dir": e.is_dir(), "size": 0 if e.is_dir() else st.st_size, "mtime": int(st.st_mtime)})
        out["items"] = sorted(items, key=lambda x: (not x["dir"], x["name"].lower()))
        out["path"] = os.path.relpath(full, root) if full != root else ""
    else:
        out["size"] = os.path.getsize(full)
    return out


class Unlock(BaseModel):
    password: str


@public.post("/{lid}/unlock")
def unlock(lid: str, body: Unlock, request: Request):
    link = _get(lid)
    ip, now = _ip(request), time.time()
    with _lock:
        recent = [t for t in _fails.get(ip, []) if now - t < 900]
        if len(recent) >= 10:
            raise HTTPException(429, "too many wrong passwords - try again in 15 minutes")
        recent.append(now)                     # (counted before checking, so parallel guesses can't slip past)
        _fails[ip] = recent
    if not link.get("password") or not check_pw(body.password, link["password"]):
        try:
            from .logcenter import auth_event
            auth_event("failed", f"share link {link['name']}"[:64], ip, "wrong share-link password")
        except Exception:
            pass
        time.sleep(1)
        raise HTTPException(403, "wrong password")
    with _lock:
        _fails[ip] = [t for t in _fails.get(ip, []) if t != now]
    exp = int(now) + UNLOCK_TTL
    resp = JSONResponse({"ok": True})
    resp.set_cookie(f"cyn_s_{lid}", _unlock_token(lid, exp), max_age=UNLOCK_TTL, path=f"/s/{lid}", httponly=True,
                    samesite="lax", secure=request.url.scheme == "https")
    return resp


@public.get("/{lid}/thumb")
def share_thumb(lid: str, request: Request, p: str = "", size: int = 256):
    """Thumbnails and photo previews on the link's page (don't count as downloads)."""
    from fastapi.responses import Response
    from .preview import SANDBOX, make_thumb
    link = _get(lid)
    if not _unlocked(request, link):
        raise HTTPException(403, "enter the password first")
    root, full = _target(link, p)
    if not os.path.isfile(full):
        raise HTTPException(404, "not a file")
    if state(link) != "active":
        raise HTTPException(410, "this link has reached its download limit")
    return Response(make_thumb(full, 1600 if size > 256 else 256), media_type="image/webp",
                    headers={"Cache-Control": "private, max-age=86400", **SANDBOX})


@public.get("/{lid}/dl")
def download(lid: str, request: Request, p: str = "", inline: bool = False):
    link = _get(lid)
    if not _unlocked(request, link):
        raise HTTPException(403, "enter the password first")
    root, full = _target(link, p)
    with _lock:                                # count (and enforce the limit) atomically
        items = load()
        cur = next((x for x in items if x["id"] == lid), None)
        if not cur or state(cur) != "active":
            raise HTTPException(410, "this link has reached its download limit")
        cur["downloads"] = cur.get("downloads", 0) + 1
        cur["last_access"] = int(time.time())
        save(items)
    name = os.path.basename(full) or link["name"]
    if os.path.isdir(full):
        return StreamingResponse(files.zip_stream([(full, name)]), media_type="application/zip",
                                 headers=files._disposition(name + ".zip"))
    return FileResponse(full, headers=files._disposition(name, inline))
