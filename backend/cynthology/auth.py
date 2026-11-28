"""Login against the system's own users (PAM). Admin = member of the `cynthology-admin` or `sudo` group."""
import grp
import json
import os
import tempfile
import threading
import secrets
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .util import DEV, STATE_DIR

router = APIRouter(prefix="/api/auth", tags=["auth"])
SESSIONS_FILE = os.path.join(STATE_DIR, "sessions.json")


def _load_sessions() -> dict[str, dict]:
    try:
        with open(SESSIONS_FILE) as f:
            return {k: v for k, v in json.load(f).items() if v["exp"] > time.time()}
    except (OSError, ValueError, KeyError):
        return {}


_SAVE_LOCK = threading.Lock()


def _save_sessions() -> None:
    """Sessions survive service restarts (e.g. after a certificate change). File is root-only.
    (Requests run in parallel threads: one writer at a time, each with its own temporary file.)"""
    with _SAVE_LOCK:
        data = json.dumps(dict(SESSIONS))
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SESSIONS_FILE) or ".", prefix=".sessions-")
        try:
            with os.fdopen(fd, "w") as f:          # (mkstemp files are 0600)
                f.write(data)
            os.replace(tmp, SESSIONS_FILE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


SESSIONS: dict[str, dict] = _load_sessions()
SESSION_TTL = 8 * 3600
ADMIN_GROUPS = ("cynthology-admin", "sudo", "wheel")


class Login(BaseModel):
    username: str = Field(max_length=256)
    password: str = Field(max_length=1024)


def _pam_ok(user: str, pw: str) -> bool:
    if DEV:
        return user == "admin" and pw == "admin"
    from .pamauth import authenticate
    return authenticate(user, pw, service="login")


def _is_admin(user: str) -> bool:
    if DEV or user == "root":
        return True
    if "\\" in user:                 # domain account (CORP\\alice): the configured domain admin group
        from .directory import is_domain_admin
        return is_domain_admin(user)
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


def _log(event: str, user: str, ip: str, detail: str = "") -> None:
    try:
        from .logcenter import auth_event
        auth_event(event, user, ip, detail)
    except Exception:
        pass


ACCT_MAX = 30          # failures on one account, from addresses it never signed in from, before those are paused
OTP_MAX = 10           # wrong 2-step codes for one account (across sign-ins) before codes are paused
_AB: dict[str, list[float]] = {}     # address -> failed or refused attempts, for the firewall auto-block


def _limit(k: str) -> int:
    return ACCT_MAX if k.startswith("acct:") else OTP_MAX if k.startswith("otp:") else MAX_FAILS


def client_key(ip: str) -> str:
    """The address that limits and blocks apply to: IPv6 counts per /64 (one host can use any address of its
    /64 - billions of them - so limits per single address would mean nothing)."""
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip[:64]
    if a.version == 6:
        if a.ipv4_mapped:
            return str(a.ipv4_mapped)
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return str(a)


def _prune() -> None:
    if len(FAILS) > 20000:          # (only ever lists of recent times: drop what has expired)
        now = time.time()
        for k in [k for k, v in FAILS.items() if not v or now - v[-1] > FAIL_WINDOW]:
            FAILS.pop(k, None)
    if len(_AB) > 20000:
        _AB.clear()


def _check_lock(*keys: str) -> None:
    now = time.time()
    for k in keys:
        if k not in FAILS:              # (nothing recorded: never create entries just by looking)
            continue
        recent = [t for t in FAILS[k] if now - t < FAIL_WINDOW]
        if recent:
            FAILS[k] = recent
        else:
            del FAILS[k]
        if len(recent) >= _limit(k):
            wait = int((recent[-1] + LOCK_TIME - now) // 60) + 1
            if wait > 0:
                _autoblock(keys)          # (refused attempts count for the firewall auto-block too)
                raise HTTPException(429, f"Too many failed attempts. Try again in {wait} minute(s).")


def known_address(user: str, ck: str) -> bool:
    """Has this account signed in from here before (a session from this address)? Those addresses aren't
    affected when the account itself is under attack from elsewhere."""
    return any(s["user"].lower() == user.lower() and client_key(s.get("ip", "")) == ck for s in list(SESSIONS.values())) \
        or ck in _KNOWN.get(user.lower(), ())


_KNOWN: dict[str, set[str]] = {}


def attempt_keys(user: str, ip: str) -> tuple[str, ...]:
    """Lockout keys for a sign-in: user+address (someone elsewhere can't lock the real user out), the
    address, and - for addresses the account never signed in from - the account (address rotation)."""
    ck = client_key(ip)
    u = user.lower()[:128]
    keys = [f"u:{u}|{ck}", f"ip:{ck}"]
    if not known_address(user, ck):
        keys.append(f"acct:{u}")
    return tuple(keys)


def begin_attempt(keys: tuple[str, ...]) -> None:
    """Check the limits and count the attempt BEFORE checking the password, so parallel requests can't all
    slip past the limit."""
    with _LOCK:
        _prune()
        _check_lock(*keys)
        now = time.time()
        for k in keys:
            FAILS.setdefault(k, []).append(now)


def attempt_ok(user: str, ip: str, keys: tuple[str, ...]) -> None:
    ck = client_key(ip)
    with _LOCK:
        for k in keys:
            if not k.startswith("acct:"):       # (the account's count stays: it is about other addresses)
                FAILS.pop(k, None)
        known = _KNOWN.setdefault(user.lower(), set())
        if len(known) < 50:
            known.add(ck)


def _autoblock(keys) -> None:
    try:
        from . import firewall
        a = firewall.load()["autoblock"]
        if not a["enabled"]:
            return
        now = time.time()
        for k in keys:
            if k.startswith("ip:"):
                ab = [t for t in _AB.get(k, []) if now - t < a["within_minutes"] * 60] + [now]
                _AB[k] = ab
                if len(ab) >= a["attempts"]:
                    firewall.block_ip(k[3:].split("/")[0], "web sign-in")
                    _AB.pop(k, None)
    except Exception:
        pass


_LOCK = __import__("threading").Lock()


def _canonical(user: str) -> str:
    """The account's real name as the system knows it (domain names are case-insensitive:
    corp\\ALICE and CORP\\alice are the same account - 2FA, sessions and permissions must agree)."""
    import pwd
    try:
        return pwd.getpwnam(user).pw_name
    except KeyError:
        return user


def _fail(*keys: str, counted: bool = False) -> None:
    if not counted:
        with _LOCK:
            for k in keys:
                FAILS.setdefault(k, []).append(time.time())
    _autoblock(keys)       # firewall auto-block: an address that keeps failing is shut out completely for a while
    time.sleep(1)


def _new_session(user: str, ip: str = "", agent: str = "") -> dict:
    from . import twofa
    token = secrets.token_urlsafe(32)
    now = time.time()
    s = {"user": user, "admin": _is_admin(user), "exp": now + SESSION_TTL,
         "created": int(now), "seen": int(now), "ip": ip[:64], "agent": agent[:300]}
    if twofa.enforced_for(s) and not twofa.enabled(user):
        from . import passkeys                    # (a passkey with fingerprint/face/PIN counts too)
        if not passkeys.has(user):
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
    ip = request.client.host if request.client else "?"
    keys = attempt_keys(body.username, ip)
    try:
        begin_attempt(keys)
    except HTTPException:
        _log("locked", body.username[:128], ip, "too many failed attempts")
        raise
    if not _pam_ok(body.username, body.password):
        _fail(*keys, counted=True)
        _log("failed", body.username, ip, "wrong user name or password")
        raise HTTPException(401, "Invalid username or password")
    body.username = _canonical(body.username)
    if "\\" not in body.username:      # domain accounts authenticate against the domain
        _ensure_smb_password(body.username, body.password)
    try:
        from .shares import ensure_home
        ensure_home(body.username)
    except Exception:
        pass
    if twofa.enabled(body.username):
        ticket = secrets.token_urlsafe(24)
        PENDING[ticket] = {"user": body.username, "exp": time.time() + 300, "keys": keys, "ip": ip,
                           "agent": request.headers.get("user-agent", "")}
        return {"need_otp": True, "ticket": ticket}
    attempt_ok(body.username, ip, keys)
    _log("login", body.username, ip)
    return _new_session(body.username, ip, request.headers.get("user-agent", ""))


class Otp(BaseModel):
    ticket: str = Field(max_length=128)
    code: str = Field(max_length=64)


@router.post("/otp")
def otp(body: Otp):
    from . import twofa
    p = PENDING.get(body.ticket)
    if not p or p["exp"] < time.time():
        PENDING.pop(body.ticket, None)
        raise HTTPException(401, "Sign-in expired - enter your password again")
    keys = (*p["keys"], f"otp:{p['user'].lower()}")       # (also per account, across sign-ins)
    with _LOCK:
        _check_lock(*keys)
    if not twofa.verify(p["user"], body.code):
        _fail(*keys)
        _log("2fa-failed", p["user"], p.get("ip", "?"), "wrong verification code")
        raise HTTPException(401, "Wrong verification code")
    del PENDING[body.ticket]
    attempt_ok(p["user"], p.get("ip", ""), keys)
    FAILS.pop(f"otp:{p['user'].lower()}", None)
    _log("login", p["user"], p.get("ip", "?"), "with 2-step verification")
    return _new_session(p["user"], p.get("ip", ""), p.get("agent", ""))


def current_user(request: Request, authorization: str = Header(default="")) -> dict:
    token = authorization.removeprefix("Bearer ").strip()
    s = SESSIONS.get(token)
    if not s or s["exp"] < time.time():
        raise HTTPException(401, "Not logged in")
    # administrators must finish setting up 2-step verification before anything else
    if s.get("enroll_required") and not (request.url.path.startswith("/api/auth/") or
                                         request.url.path in ("/api/2fa", "/api/2fa/setup", "/api/2fa/enable")):
        raise HTTPException(403, "Set up 2-step verification first")
    s["seen"] = int(time.time())          # (kept in memory; written with the next save)
    # admin rights are re-checked (at most once a minute) for every request - File Station and the others
    # use the flag too: removing someone from the admin group takes effect without waiting for their
    # session to expire, and being added needs no new sign-in
    if time.time() - s.get("admin_checked", 0) > 60:
        s["admin"] = _is_admin(s["user"])
        s["admin_checked"] = time.time()
    return s


def session_id(token: str) -> str:
    """A public handle for a session (the token itself is the password of the session)."""
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def sessions_of(user: str) -> list[tuple[str, dict]]:
    now = time.time()
    return [(t, s) for t, s in list(SESSIONS.items()) if s["user"].lower() == user.lower() and s["exp"] > now]


def end_session(token: str) -> None:
    if SESSIONS.pop(token, None) is not None:
        _save_sessions()


def require_admin(user: dict = Depends(current_user)) -> dict:
    if not user["admin"]:
        raise HTTPException(403, f"Administrator rights required. The account '{user['user']}' is not an administrator - "
                                 f"on the NAS console run:  sudo usermod -aG cynthology-admin {user['user']}")
    return user


def end_sessions(user: str) -> int:
    """Sign a user out everywhere (account deleted, password changed)."""
    gone = [t for t, s in SESSIONS.items() if s["user"].lower() == user.lower()]
    for t in gone:
        del SESSIONS[t]
    if gone:
        _save_sessions()
    return len(gone)


@router.post("/logout")
def logout(authorization: str = Header(default="")):
    SESSIONS.pop(authorization.removeprefix("Bearer ").strip(), None)
    _save_sessions()
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(current_user)):
    return {"user": user["user"], "admin": user["admin"], "enroll_required": bool(user.get("enroll_required"))}
