"""Passkeys and security keys (WebAuthn): sign in with Face ID / Touch ID / Windows Hello, a phone or
a USB/NFC key instead of a password - or use one instead of the 6-digit code.

Verification is done by Yubico's python-fido2 (Debian: python3-fido2). Passkeys are bound to the host
name the NAS was opened by (the WebAuthn "relying party"): a passkey made at https://nas.example.com
works there, not at an IP address. Browsers only offer WebAuthn over HTTPS with a trusted certificate
(or on http://localhost), and never for IP addresses.
Sign-in requires user verification (fingerprint, face, PIN) - so a passkey alone is two factors.
"""
import base64
import json
import os
import re
import secrets
import threading
import time

import fido2.features
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from . import auth
from .auth import current_user
from .util import STATE_DIR

fido2.features.webauthn_json_mapping.enabled = True      # (options/responses as the browser's JSON)
from fido2.server import Fido2Server  # noqa: E402
from fido2.webauthn import (AttestedCredentialData, AuthenticationResponse,  # noqa: E402
                            PublicKeyCredentialRpEntity, PublicKeyCredentialUserEntity, RegistrationResponse)

router = APIRouter(prefix="/api/passkeys", tags=["passkeys"])
public = APIRouter(prefix="/api/auth/passkey", tags=["auth"])
FILE = os.path.join(STATE_DIR, "passkeys.json")
_lock = threading.Lock()
PENDING: dict[str, dict] = {}          # challenge state, 5 minutes
IP_RE = re.compile(r"^[0-9.]+$|:")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def load() -> dict:
    try:
        with open(FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(d: dict) -> None:
    fd = os.open(FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(FILE + ".tmp", FILE)


def has(user: str) -> bool:
    return bool(load().get(user.lower(), {}).get("keys"))


def remove_user(user: str) -> None:
    with _lock:
        d = load()
        if d.pop(user.lower(), None) is not None:
            save(d)


def _server(request: Request) -> tuple[Fido2Server, str]:
    host = (request.url.hostname or "").lower()
    if not host or IP_RE.search(host):
        raise HTTPException(400, "Passkeys need the NAS opened by its name (for example your domain), not by an IP address.")
    origin = f"{request.url.scheme}://{request.headers.get('host', host)}".lower()
    if request.url.scheme != "https" and host != "localhost":
        raise HTTPException(400, "Passkeys need a secure connection (HTTPS).")
    srv = Fido2Server(PublicKeyCredentialRpEntity(id=host, name="Cynthology"), verify_origin=lambda o: o.lower() == origin)
    return srv, host


def _pending_put(data: dict) -> str:
    now = time.time()
    for k in [k for k, v in PENDING.items() if v["exp"] < now]:
        PENDING.pop(k, None)
    if len(PENDING) > 500:
        raise HTTPException(429, "too many sign-ins in progress - try again in a minute")
    tok = secrets.token_urlsafe(24)
    PENDING[tok] = {**data, "exp": now + 300}
    return tok


def _pending_pop(tok: str) -> dict:
    p = PENDING.pop(tok, None)
    if not p or p["exp"] < time.time():
        raise HTTPException(400, "this request expired - try again")
    return p


def _creds(entries: list[dict]) -> list[AttestedCredentialData]:
    return [AttestedCredentialData(_unb64(k["data"])) for k in entries]


# ---------- managing your passkeys (signed in) ----------

@router.get("")
def mine(request: Request, user: dict = Depends(current_user)):
    host = (request.url.hostname or "").lower()
    keys = load().get(user["user"].lower(), {}).get("keys", [])
    return {"host": host, "usable_here": not IP_RE.search(host) and (request.url.scheme == "https" or host == "localhost"),
            "keys": [{"id": k["id"], "name": k["name"], "rp_id": k["rp_id"], "created": k["created"], "last_used": k.get("last_used")}
                     for k in keys]}


@router.post("/register/begin")
def register_begin(request: Request, user: dict = Depends(current_user)):
    srv, host = _server(request)
    name = user["user"]
    with _lock:
        d = load()
        rec = d.setdefault(name.lower(), {"handle": _b64(secrets.token_bytes(16)), "keys": []})
        save(d)
    existing = [k for k in rec["keys"] if k["rp_id"] == host]
    options, state = srv.register_begin(
        PublicKeyCredentialUserEntity(id=_unb64(rec["handle"]), name=name, display_name=name), _creds(existing),
        user_verification="required", resident_key_requirement="required")
    tok = _pending_put({"kind": "register", "state": state, "user": name, "host": host})
    return {"token": tok, "options": dict(options)}


class RegisterDone(BaseModel):
    token: str
    credential: dict
    name: str = ""


@router.post("/register/complete")
def register_complete(body: RegisterDone, request: Request, user: dict = Depends(current_user)):
    p = _pending_pop(body.token)
    if p["kind"] != "register" or p["user"] != user["user"]:
        raise HTTPException(400, "this request isn't yours")
    srv, host = _server(request)
    if host != p["host"]:
        raise HTTPException(400, "the address changed while adding the passkey")
    try:
        auth_data = srv.register_complete(p["state"], RegistrationResponse.from_dict(body.credential))
    except Exception as e:
        raise HTTPException(400, f"the passkey couldn't be verified: {e}")
    cd = auth_data.credential_data
    name = re.sub(r"[\x00-\x1f\x7f]", "", body.name).strip()[:60] or "Passkey"
    with _lock:
        d = load()
        rec = d.setdefault(user["user"].lower(), {"handle": _b64(secrets.token_bytes(16)), "keys": []})
        rec["keys"].append({"id": _b64(cd.credential_id), "data": _b64(bytes(cd)), "name": name, "rp_id": host,
                            "created": int(time.time())})
        save(d)
    auth._log("passkey-added", user["user"], request.client.host if request.client else "?", name)
    return {"ok": True}


@router.delete("/{kid}")
def delete(kid: str, request: Request, user: dict = Depends(current_user)):
    with _lock:
        d = load()
        rec = d.get(user["user"].lower(), {})
        before = len(rec.get("keys", []))
        rec["keys"] = [k for k in rec.get("keys", []) if k["id"] != kid]
        if len(rec.get("keys", [])) == before:
            raise HTTPException(404, "no such passkey")
        save(d)
    auth._log("passkey-removed", user["user"], request.client.host if request.client else "?")
    return {"ok": True}


def _account_ok(user: str) -> bool:
    """The account still exists and isn't locked (passwd -l / usermod -L: '!' in /etc/shadow)."""
    from .util import DEV
    if DEV:
        return True
    import pwd
    try:
        pwd.getpwnam(user)
    except KeyError:
        return False
    try:
        with open("/etc/shadow") as f:
            for line in f:
                name, _, rest = line.partition(":")
                if name == user:
                    return not rest.startswith("!")
    except OSError:
        pass
    return True                                   # (domain accounts aren't in /etc/shadow)


# ---------- signing in with a passkey (public) ----------

class Begin(BaseModel):
    ticket: str = ""        # set: the password was right, the passkey replaces the 6-digit code


@public.post("/begin")
def login_begin(body: Begin, request: Request):
    srv, host = _server(request)
    allow, user = [], None
    if body.ticket:
        t = auth.PENDING.get(body.ticket)
        if not t or t["exp"] < time.time():
            raise HTTPException(401, "Sign-in expired - enter your password again")
        user = t["user"]
        allow = [k for k in load().get(user.lower(), {}).get("keys", []) if k["rp_id"] == host]
        if not allow:
            raise HTTPException(400, "You have no passkey for this address - use the code from your app.")
    options, state = srv.authenticate_begin(_creds(allow), user_verification="required")
    tok = _pending_put({"kind": "login", "state": state, "host": host, "ticket": body.ticket, "user": user})
    return {"token": tok, "options": dict(options)}


class Finish(BaseModel):
    token: str
    credential: dict


@public.post("/complete")
def login_complete(body: Finish, request: Request):
    ip = request.client.host if request.client else "?"
    p = _pending_pop(body.token)
    srv, host = _server(request)
    if p["kind"] != "login" or host != p["host"]:
        raise HTTPException(400, "this request doesn't belong here")
    auth._check_lock(f"ip:{ip}")
    cid = body.credential.get("id", "")
    owner, entry = None, None
    for name, rec in load().items():
        for k in rec.get("keys", []):
            if k["id"] == cid and k["rp_id"] == host:
                owner, entry = name, k
    handle = (body.credential.get("response") or {}).get("userHandle")
    if entry and handle and handle != load()[owner]["handle"]:
        entry = None
    if entry is None or (p["user"] and owner != p["user"].lower()):
        auth._fail(f"ip:{ip}")
        auth._log("failed", p["user"] or "?", ip, "unknown passkey")
        raise HTTPException(401, "This passkey isn't registered on this NAS (for this address).")
    try:
        srv.authenticate_complete(p["state"], _creds([entry]), AuthenticationResponse.from_dict(body.credential))
    except Exception:
        auth._fail(f"ip:{ip}")
        auth._log("failed", owner, ip, "passkey not accepted")
        raise HTTPException(401, "The passkey wasn't accepted.")
    user = auth._canonical(owner)
    if not _account_ok(user):
        auth._log("failed", user, ip, "passkey for an account that is gone or locked")
        raise HTTPException(401, "This account can't sign in.")
    if p["ticket"]:
        auth.PENDING.pop(p["ticket"], None)
    with _lock:
        d = load()
        for k in d.get(owner, {}).get("keys", []):
            if k["id"] == cid:
                k["last_used"] = int(time.time())
        save(d)
    auth._log("login", user, ip, "with a passkey")
    return auth._new_session(user, ip, request.headers.get("user-agent", ""))
