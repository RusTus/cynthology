"""2-step verification: TOTP (RFC 6238) authenticator codes + one-time backup codes.

Works with any authenticator app (Google/Microsoft Authenticator, Authy, 1Password, Bitwarden,
Aegis...). Secrets live in 2fa.json (root-only). Lost phone: an administrator resets the user
in Users & Groups, or on the console:  sudo python3 -m cynthology.twofa reset <user>
"""
import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import socket
import struct
import sys
import time
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import settings
from .util import STATE_DIR

router = APIRouter(prefix="/api/2fa", tags=["2fa"])
FILE = os.path.join(STATE_DIR, "2fa.json")
STEP, DIGITS, WINDOW = 30, 6, 1          # 30 s codes, accept one step either side (clock drift)
BACKUP_CODES = 10


# ---------- TOTP ----------

def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10 ** DIGITS).zfill(DIGITS)


def match_step(secret: str, code: str, now: float | None = None) -> int | None:
    """The time step a code belongs to (within the window), or None."""
    code = "".join(c for c in code if c.isdigit())
    if len(code) != DIGITS:
        return None
    current = int((now or time.time()) // STEP)
    for step in range(current - WINDOW, current + WINDOW + 1):
        if hmac.compare_digest(totp(secret, step), code):
            return step
    return None


def otpauth_uri(user: str, secret: str) -> str:
    issuer = f"Cynthology ({socket.gethostname()})"
    label = urllib.parse.quote(f"{issuer}:{user}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={urllib.parse.quote(issuer)}&digits={DIGITS}&period={STEP}"


def qr_svg(text: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return ""
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


# ---------- storage ----------

def _load() -> dict:
    try:
        with open(FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    fd = os.open(FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(FILE + ".tmp", FILE)


def _hash(code: str) -> str:
    return hashlib.sha256(code.replace("-", "").strip().lower().encode()).hexdigest()


def enabled(user: str) -> bool:
    return bool(_load().get(user, {}).get("enabled"))


def enforced_for(user: dict) -> bool:
    return bool(settings.get("enforce_2fa_admins")) and user["admin"]


def verify(user: str, code: str) -> bool:
    """Check a TOTP code (no replays) or consume a backup code."""
    data = _load()
    rec = data.get(user)
    if not rec or not rec.get("enabled"):
        return False
    step = match_step(rec["secret"], code)
    if step is not None:
        if step <= rec.get("last_step", -1):
            return False                       # code already used
        rec["last_step"] = step
        _save(data)
        return True
    h = _hash(code)
    if len(code.replace("-", "").strip()) == 8 and h in rec.get("backup", []):
        rec["backup"].remove(h)
        _save(data)
        return True
    return False


def _new_backup_codes(rec: dict) -> list[str]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    codes = ["".join(secrets.choice(alphabet) for _ in range(8)) for _ in range(BACKUP_CODES)]
    rec["backup"] = [_hash(c) for c in codes]
    return [f"{c[:4]}-{c[4:]}" for c in codes]


# ---------- API (own account) ----------

def _me():
    from .auth import current_user
    return current_user


@router.get("")
def status(user=Depends(_me())):
    rec = _load().get(user["user"], {})
    return {"enabled": bool(rec.get("enabled")), "backup_codes_left": len(rec.get("backup", [])),
            "required": enforced_for(user)}


@router.post("/setup")
def setup(user=Depends(_me())):
    data = _load()
    rec = data.setdefault(user["user"], {})
    if rec.get("enabled"):
        raise HTTPException(409, "2-step verification is already on - turn it off first to re-pair")
    rec["pending"] = new_secret()
    _save(data)
    uri = otpauth_uri(user["user"], rec["pending"])
    return {"secret": rec["pending"], "uri": uri, "qr_svg": qr_svg(uri)}


class Code(BaseModel):
    code: str


@router.post("/enable")
def enable(body: Code, user=Depends(_me())):
    data = _load()
    rec = data.get(user["user"], {})
    if not rec.get("pending"):
        raise HTTPException(400, "start the setup first")
    step = match_step(rec["pending"], body.code)
    if step is None:
        raise HTTPException(400, "that code isn't right - check the time on your phone and try the next code")
    rec.update(secret=rec.pop("pending"), enabled=True, last_step=step, since=int(time.time()))
    codes = _new_backup_codes(rec)
    _save(data)
    from .auth import enrollment_done
    enrollment_done(user["user"])
    return {"ok": True, "backup_codes": codes}


@router.post("/disable")
def disable(body: Code, user=Depends(_me())):
    if enforced_for(user):
        raise HTTPException(403, "2-step verification is required for administrators on this NAS")
    if not verify(user["user"], body.code):
        raise HTTPException(400, "wrong code")
    data = _load()
    data.pop(user["user"], None)
    _save(data)
    return {"ok": True}


@router.post("/backup-codes")
def regenerate(body: Code, user=Depends(_me())):
    if not verify(user["user"], body.code):
        raise HTTPException(400, "wrong code")
    data = _load()
    codes = _new_backup_codes(data[user["user"]])
    _save(data)
    return {"backup_codes": codes}


# ---------- API (administrators) ----------

def _admin():
    from .auth import require_admin
    return require_admin


@router.get("/users")
def users(_=Depends(_admin())):
    return {u: {"enabled": bool(r.get("enabled")), "since": r.get("since")} for u, r in _load().items() if r.get("enabled")}


@router.delete("/users/{name}")
def reset(name: str, _=Depends(_admin())):
    data = _load()
    data.pop(name, None)
    _save(data)
    return {"ok": True}


class Policy(BaseModel):
    enforce_admins: bool


@router.get("/policy")
def get_policy(_=Depends(_admin())):
    return {"enforce_admins": bool(settings.get("enforce_2fa_admins"))}


@router.put("/policy")
def put_policy(body: Policy, admin=Depends(_admin())):
    if body.enforce_admins and not enabled(admin["user"]):
        raise HTTPException(400, "turn on 2-step verification for your own account first (Personal settings), "
                                 "so you don't lock yourself out")
    settings.update(enforce_2fa_admins=body.enforce_admins)
    return {"ok": True}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "reset":
        d = _load()
        print("2-step verification reset for", sys.argv[2] if d.pop(sys.argv[2], None) else f"{sys.argv[2]} (was not on)")
        _save(d)
    else:
        print("usage: python3 -m cynthology.twofa reset <user>")
