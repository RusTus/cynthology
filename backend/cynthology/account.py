"""Personal settings of the signed-in user: name, email address, password, signed-in devices,
recent sign-ins. Everything here acts on the caller's own account only."""
import json
import os
import pwd
import re
import threading
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from . import auth
from .auth import current_user
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/account", tags=["account"])
PROFILES = os.path.join(STATE_DIR, "profiles.json")
_lock = threading.Lock()
EMAIL_RE = re.compile(r"[^@\s,;<>\"']{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,63}")


def _load() -> dict:
    try:
        with open(PROFILES) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def profile(user: str) -> dict:
    return _load().get(user.lower(), {})


def alert_recipients() -> list[str]:
    """Administrators who asked to get the NAS's alert emails at their own address."""
    out = []
    for user, p in _load().items():
        if p.get("alerts") and p.get("email") and auth._is_admin(user):
            out.append(p["email"])
    return out


def _full_name(user: str) -> str:
    if DEV:
        return profile(user).get("full_name", "")
    try:
        return pwd.getpwnam(user).pw_gecos.split(",")[0]
    except KeyError:
        return ""


def _password_changed(user: str) -> int | None:
    """Date of the last password change (from /etc/shadow, in days) as a Unix time."""
    if DEV or "\\" in user:
        return None
    try:
        with open("/etc/shadow") as f:
            for line in f:
                parts = line.split(":")
                if parts[0] == user and len(parts) > 2 and parts[2].isdigit():
                    return int(parts[2]) * 86400 or None
    except OSError:
        pass
    return None


def _token(authorization: str) -> str:
    return authorization.removeprefix("Bearer ").strip()


@router.get("")
def get(user: dict = Depends(current_user), authorization: str = Header(default="")):
    from . import health, logcenter
    name = user["user"]
    me = auth.session_id(_token(authorization))
    p = profile(name)
    smtp = health.load_smtp()
    sessions = sorted(({"id": auth.session_id(t), "current": auth.session_id(t) == me, "ip": s.get("ip", ""),
                        "agent": s.get("agent", ""), "created": s.get("created"), "seen": s.get("seen")}
                       for t, s in auth.sessions_of(name)), key=lambda x: (not x["current"], -(x["seen"] or 0)))
    history = [e for e in logcenter._auth_events() if e.get("user", "").lower() == name.lower()][-15:][::-1]
    return {"user": name, "admin": user["admin"], "local": "\\" not in name, "full_name": _full_name(name),
            "email": p.get("email", ""), "alerts": bool(p.get("alerts")), "password_changed": _password_changed(name),
            "prefs": p.get("prefs", {}), "wallpaper": os.path.exists(_wall_path(name)),
            "mail_ready": bool(smtp.get("enabled") and smtp.get("host")),
            "sessions": sessions, "history": history}


# desktop preferences, kept on the NAS so they follow the user to every browser
PREFS = {"date": ("auto", "ymd", "dmy", "mdy"), "time": ("auto", "24", "12"), "icons": ("small", "normal", "large"),
         "wall": ("default", "ocean", "dusk", "forest", "graphite", "aurora", "custom"), "resume": (True, False)}


class Profile(BaseModel):
    full_name: str | None = None
    email: str | None = None
    alerts: bool | None = None
    prefs: dict | None = None


@router.put("")
def put(body: Profile, user: dict = Depends(current_user)):
    from .users import check_full_name
    name = user["user"]
    with _lock:
        d = _load()
        p = d.setdefault(name.lower(), {})
        if body.email is not None:
            email = body.email.strip()
            if email and (len(email) > 254 or not EMAIL_RE.fullmatch(email)):
                raise HTTPException(400, "that doesn't look like an email address")
            p["email"] = email
        if body.alerts is not None:
            if body.alerts and not user["admin"]:
                raise HTTPException(403, "only administrators get the NAS's alerts")
            p["alerts"] = body.alerts
        if body.prefs is not None:
            prefs = p.setdefault("prefs", {})
            for k, v in body.prefs.items():
                if k not in PREFS or v not in PREFS[k]:
                    raise HTTPException(400, f"unknown setting {k}={v!r}")
                prefs[k] = v
        if body.full_name is not None:
            full = check_full_name(body.full_name)
            if "\\" in name:
                raise HTTPException(400, "domain accounts are managed by the domain")
            if DEV:
                p["full_name"] = full
            else:
                run(["usermod", "-c", full, name])
        fd = os.open(PROFILES + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(PROFILES + ".tmp", PROFILES)
    return {"ok": True}


class NewPassword(BaseModel):
    current: str
    new: str


@router.post("/password")
def change_password(body: NewPassword, request: Request, user: dict = Depends(current_user)):
    from .users import apply_password, check_password
    name = user["user"]
    ip = request.client.host if request.client else "?"
    if "\\" in name:
        raise HTTPException(400, "this is a domain account - change its password on the domain")
    key = f"pw:{name.lower()}"
    auth._check_lock(key)
    if not auth._pam_ok(name, body.current):
        auth._fail(key)
        auth._log("failed", name, ip, "password change: wrong current password")
        raise HTTPException(403, "Your current password isn't right")
    if body.new == body.current:
        raise HTTPException(400, "the new password is the same as the old one")
    check_password(body.new)
    if not DEV:
        apply_password(name, body.new)           # (signs this account out everywhere...)
    else:
        auth.end_sessions(name)
    auth._log("password", name, ip, "password changed")
    _tell(name, "Your password was changed",
          f"The password of your account '{name}' was just changed (from {ip}).\n"
          "If that wasn't you, ask an administrator to reset it right away.")
    return auth._new_session(name, ip, request.headers.get("user-agent", ""))   # (...except here)


def _tell(name: str, subject: str, text: str) -> None:
    """Best-effort email to the user's own address."""
    email = profile(name).get("email")
    if not email:
        return

    def send():
        from . import health
        cfg = health.load_smtp()
        if cfg.get("enabled") and cfg.get("host"):
            try:
                health.send_mail(subject, text, {**cfg, "to": email})
            except Exception as e:
                print("account mail failed:", e)
    threading.Thread(target=send, daemon=True).start()


# ---------- desktop background ----------
WALLS = os.path.join(STATE_DIR, "wallpapers")
WALL_MAX = 6 * 1024 * 1024


def _wall_path(user: str) -> str:
    import hashlib
    return os.path.join(WALLS, hashlib.sha256(user.lower().encode()).hexdigest()[:24] + ".img")


@router.put("/wallpaper")
async def put_wallpaper(request: Request, user: dict = Depends(current_user)):
    """The browser scales the picture down and sends a JPEG/PNG/WebP (never decoded here)."""
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > WALL_MAX:
            raise HTTPException(413, "picture too large (max. 6 MB)")
    if _image_type(data) is None:
        raise HTTPException(400, "not a JPEG, PNG or WebP picture")
    os.makedirs(WALLS, mode=0o700, exist_ok=True)
    tmp = _wall_path(user["user"]) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, _wall_path(user["user"]))
    return {"ok": True}


@router.delete("/wallpaper")
def delete_wallpaper(user: dict = Depends(current_user)):
    try:
        os.remove(_wall_path(user["user"]))
    except FileNotFoundError:
        pass
    return {"ok": True}


@router.get("/wallpaper")
def get_wallpaper(user: dict = Depends(current_user)):
    from fastapi.responses import Response
    try:
        with open(_wall_path(user["user"]), "rb") as f:
            data = f.read()
    except FileNotFoundError:
        raise HTTPException(404)
    return Response(data, media_type=_image_type(data) or "application/octet-stream",
                    headers={"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"})


def _image_type(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# ---------- storage ----------

@router.get("/storage")
def storage_use(user: dict = Depends(current_user)):
    """The shared folders this user can open, with their size limit and use (where quotas are on)."""
    import shutil
    from . import files, shares
    out = []

    def row(name, path, kind, limit_gb=0):
        q = shares.quota_usage(path) if os.path.isdir(path) else None
        try:
            du = shutil.disk_usage(path)
            free = du.free
        except OSError:
            free = None
        out.append({"name": name, "kind": kind, "used": q["used"] if q else None,
                    "limit": (q or {}).get("limit") or (limit_gb * 1024 ** 3 if limit_gb else None), "free": free})
    h = shares.homes_share()
    if h:
        try:
            home = shares.ensure_home(user["user"])
        except HTTPException:
            home = None
        if home:
            row("home", home, "home", (h.get("user_quota") or {}).get(user["user"], h.get("default_user_quota_gb", 0)))
    for sh in shares.load():
        if files.access(user, sh) and not sh.get("hidden_from_list"):
            row(sh["name"], sh.get("path", ""), "share", sh.get("quota_gb", 0))
    return out


@router.delete("/sessions/{sid}")
def end_session(sid: str, user: dict = Depends(current_user)):
    for t, _ in auth.sessions_of(user["user"]):
        if auth.session_id(t) == sid:
            auth.end_session(t)
            return {"ok": True}
    raise HTTPException(404, "no such session")


@router.post("/sessions/sign-out-others")
def sign_out_others(user: dict = Depends(current_user), authorization: str = Header(default="")):
    me = _token(authorization)
    n = 0
    for t, _ in auth.sessions_of(user["user"]):
        if t != me:
            auth.end_session(t)
            n += 1
    return {"ended": n}
