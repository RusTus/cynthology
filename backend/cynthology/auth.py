"""Login against the system's own users (PAM). Admin = member of the `cynthology-admin` or `sudo` group."""
import grp
import json
import os
import secrets
import time

from fastapi import APIRouter, Depends, Header, HTTPException
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


@router.post("/login")
def login(body: Login):
    if not _pam_ok(body.username, body.password):
        time.sleep(1)
        raise HTTPException(401, "Invalid username or password")
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"user": body.username, "admin": _is_admin(body.username), "exp": time.time() + SESSION_TTL}
    _save_sessions()
    return {"token": token, "user": body.username, "admin": SESSIONS[token]["admin"]}


def current_user(authorization: str = Header(default="")) -> dict:
    token = authorization.removeprefix("Bearer ").strip()
    s = SESSIONS.get(token)
    if not s or s["exp"] < time.time():
        raise HTTPException(401, "Not logged in")
    return s


def require_admin(user: dict = Depends(current_user)) -> dict:
    if not user["admin"]:
        raise HTTPException(403, "Administrator rights required")
    return user


@router.post("/logout")
def logout(authorization: str = Header(default="")):
    SESSIONS.pop(authorization.removeprefix("Bearer ").strip(), None)
    _save_sessions()
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(current_user)):
    return {"user": user["user"], "admin": user["admin"]}
