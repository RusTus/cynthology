"""Login against the system's own users (PAM). Admin = member of the `cynthology-admin` or `sudo` group."""
import grp
import json
import os
import secrets
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from .util import DEV, STATE_DIR

router = APIRouter(prefix="/api/auth", tags=["auth"])
SESSIONS_FILE = os.path.join(STATE_DIR, "sessions.json")


def _load_sessions() -> dict[str, dict]:
    try:
        with open(SESSIONS_FILE) as f:
            return {k: v for k, v in json.load(f).items() if v["exp"] > time.time()}
    except (OSError, ValueError, KeyError):
        return {}


def _save_sessions() -> None:
    """Sessions survive service restarts (e.g. after a certificate change). File is root-only."""
    tmp = SESSIONS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(SESSIONS, f)
    os.replace(tmp, SESSIONS_FILE)


SESSIONS: dict[str, dict] = _load_sessions()
SESSION_TTL = 8 * 3600
ADMIN_GROUPS = ("cynthology-admin", "sudo", "wheel")


class Login(BaseModel):
    username: str
    password: str


def _pam_ok(user: str, pw: str) -> bool:
    if DEV:
        return user == "admin" and pw == "admin"
    from .pamauth import authenticate
    return authenticate(user, pw, service="login")


def _is_admin(user: str) -> bool:
    if DEV or user == "root":
        return True
    for g in ADMIN_GROUPS:
        try:
            if user in grp.getgrnam(g).gr_mem:
                return True
        except KeyError:
            pass
    return False


def _ensure_smb_password(user: str, pw: str) -> None:
    """Accounts made outside Cynthology (e.g. the user from the Debian installer) have no Samba
    password, so SMB logins fail. After a successful PAM login we know the password: add it."""
    if DEV or user == "root":
        return
    import subprocess
    try:
        known = subprocess.run(["pdbedit", "-L", "-u", user], capture_output=True, timeout=10).returncode == 0
        if not known:
            subprocess.run(["smbpasswd", "-a", "-s", user], input=f"{pw}\n{pw}\n", text=True,
                           capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


# ---------- brute-force protection ----------
MAX_FAILS, FAIL_WINDOW, LOCK_TIME = 5, 15 * 60, 15 * 60
FAILS: dict[str, list[float]] = {}
PENDING: dict[str, dict] = {}      # password OK, waiting for the 2-step code


def _check_lock(*keys: str) -> None:
    now = time.time()
    for k in keys:
        recent = [t for t in FAILS.get(k, []) if now - t < FAIL_WINDOW]
        FAILS[k] = recent
        if len(recent) >= MAX_FAILS:
            wait = int((recent[-1] + LOCK_TIME - now) // 60) + 1
            if wait > 0:
                raise HTTPException(429, f"Too many failed attempts. Try again in {wait} minute(s).")


def _fail(*keys: str) -> None:
    for k in keys:
        FAILS.setdefault(k, []).append(time.time())
    # firewall auto-block: an address that keeps failing is shut out completely for a while
    try:
        from . import firewall
        a = firewall.load()["autoblock"]
        for k in keys:
            if k.startswith("ip:") and a["enabled"]:
                recent = [t for t in FAILS[k] if time.time() - t < a["within_minutes"] * 60]
                if len(recent) >= a["attempts"]:
                    firewall.block_ip(k[3:], "web sign-in")
                    FAILS[k] = []
    except Exception:
        pass
    time.sleep(1)


def _new_session(user: str) -> dict:
    from . import twofa
    token = secrets.token_urlsafe(32)
    s = {"user": user, "admin": _is_admin(user), "exp": time.time() + SESSION_TTL}
    if twofa.enforced_for(s) and not twofa.enabled(user):
        s["enroll_required"] = True
    SESSIONS[token] = s
    _save_sessions()
    return {"token": token, "user": user, "admin": s["admin"], "enroll_required": bool(s.get("enroll_required"))}


def enrollment_done(user: str) -> None:
    for s in SESSIONS.values():
        if s["user"] == user:
            s.pop("enroll_required", None)
    _save_sessions()


@router.post("/login")
def login(body: Login, request: Request):
    from . import twofa
    keys = (f"u:{body.username.lower()}", f"ip:{request.client.host if request.client else '?'}")
    _check_lock(*keys)
    if not _pam_ok(body.username, body.password):
        _fail(*keys)
        raise HTTPException(401, "Invalid username or password")
    _ensure_smb_password(body.username, body.password)
    try:
        from .shares import ensure_home
        ensure_home(body.username)
    except Exception:
        pass
    if twofa.enabled(body.username):
        ticket = secrets.token_urlsafe(24)
        PENDING[ticket] = {"user": body.username, "exp": time.time() + 300, "keys": keys}
        return {"need_otp": True, "ticket": ticket}
    for k in keys:
        FAILS.pop(k, None)
    return _new_session(body.username)


class Otp(BaseModel):
    ticket: str
    code: str


@router.post("/otp")
def otp(body: Otp):
    from . import twofa
    p = PENDING.get(body.ticket)
    if not p or p["exp"] < time.time():
        PENDING.pop(body.ticket, None)
        raise HTTPException(401, "Sign-in expired - enter your password again")
    _check_lock(*p["keys"])
    if not twofa.verify(p["user"], body.code):
        _fail(*p["keys"])
        raise HTTPException(401, "Wrong verification code")
    del PENDING[body.ticket]
    for k in p["keys"]:
        FAILS.pop(k, None)
    return _new_session(p["user"])


def current_user(request: Request, authorization: str = Header(default="")) -> dict:
    token = authorization.removeprefix("Bearer ").strip()
    s = SESSIONS.get(token)
    if not s or s["exp"] < time.time():
        raise HTTPException(401, "Not logged in")
    # administrators must finish setting up 2-step verification before anything else
    if s.get("enroll_required") and not request.url.path.startswith(("/api/auth/", "/api/2fa")):
        raise HTTPException(403, "Set up 2-step verification first")
    return s


def require_admin(user: dict = Depends(current_user)) -> dict:
    if not user["admin"] and _is_admin(user["user"]):
        user["admin"] = True            # made admin since signing in: no need to sign in again
        _save_sessions()
    if not user["admin"]:
        raise HTTPException(403, f"Administrator rights required. The account '{user['user']}' is not an administrator - "
                                 f"on the NAS console run:  sudo usermod -aG cynthology-admin {user['user']}")
    return user


@router.post("/logout")
def logout(authorization: str = Header(default="")):
    SESSIONS.pop(authorization.removeprefix("Bearer ").strip(), None)
    _save_sessions()
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(current_user)):
    return {"user": user["user"], "admin": user["admin"], "enroll_required": bool(user.get("enroll_required"))}
