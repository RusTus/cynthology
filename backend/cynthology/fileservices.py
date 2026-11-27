"""More file services (Control Panel > File Services): WebDAV, SFTP for users, rsync server.

- WebDAV: cynthology-webdav.service (webdav.py), HTTP 5005 / HTTPS 5006 like DSM.
- SFTP: administrators always have SSH. Other users get SFTP only - no shell, no port forwarding
  (sshd: ForceCommand internal-sftp for the users group minus administrators). Off = no SSH at all
  for them (their login shell is nologin).
- rsync server (port 873, e.g. for another NAS or rsync clients): one module per shared folder,
  with the folder's read/write lists. Each transfer runs as the signed-in user (rsync's own uid
  switch) inside the folder (chroot), so the normal file permissions apply. rsync has its own
  passwords (its protocol needs them in a file), set per user here - like DSM's rsync accounts.
"""
import json
import os
import re
import subprocess

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_admin
from .util import DEV, STATE_DIR, one_line, run

router = APIRouter(prefix="/api/fileservices", tags=["fileservices"])
CFG = os.path.join(STATE_DIR, "fileservices.json")
SSHD_DROPIN = os.path.join(STATE_DIR, "sshd-sftp.conf") if DEV else "/etc/ssh/sshd_config.d/cynthology-sftp.conf"
RSYNCD_CONF = os.path.join(STATE_DIR, "rsyncd.conf") if DEV else "/etc/rsyncd.conf"
RSYNCD_SECRETS = os.path.join(STATE_DIR, "rsyncd.secrets") if DEV else "/etc/rsyncd.secrets"
DEFAULTS = {"webdav": {"enabled": False, "http": False, "https": True, "http_port": 5005, "https_port": 5006},
            "sftp": {"users": False},
            "rsync": {"enabled": False}}
USER_RE = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")


def load() -> dict:
    try:
        saved = json.load(open(CFG))
    except (OSError, ValueError):
        saved = {}
    return {k: {**v, **saved.get(k, {})} for k, v in DEFAULTS.items()}


def save(cfg: dict) -> None:
    with open(CFG + ".new", "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(CFG + ".new", CFG)


def _active(unit: str) -> bool:
    if DEV:
        return False
    return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0


# ---------- rsync ----------

def _read_secrets() -> dict[str, str]:
    out = {}
    try:
        for ln in open(RSYNCD_SECRETS):
            u, sep, pw = ln.rstrip("\n").partition(":")
            if sep and USER_RE.fullmatch(u):
                out[u] = pw
    except OSError:
        pass
    return out


def _write_secrets(sec: dict[str, str]) -> None:
    fd = os.open(RSYNCD_SECRETS + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.writelines(f"{u}:{pw}\n" for u, pw in sorted(sec.items()))
    os.replace(RSYNCD_SECRETS + ".new", RSYNCD_SECRETS)


def _auth_entry(e: str, mode: str) -> str | None:
    """'alice' -> 'alice:rw', '@family' -> '@family:ro'. (Domain accounts have no rsync password.)"""
    if not re.fullmatch(r"@?[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", e):
        return None
    return f"{e}:{mode}"


def render_rsyncd(shares: list[dict], homes_path: str | None) -> str:
    """rsyncd.conf for the shared folders (pure; unit tested)."""
    out = ["# Managed by Cynthology - do not edit (Control Panel > File Services > rsync)",
           f"secrets file = {RSYNCD_SECRETS}", "strict modes = yes", "use chroot = yes", "munge symlinks = yes",
           "numeric ids = yes", "reverse lookup = no", "max connections = 20", "timeout = 900",
           "uid = %RSYNC_USER_NAME%", "gid = *",              # each transfer runs as the signed-in user
           "read only = no", "list = yes", "transfer logging = yes",
           "exclude = @eaDir/ .snapshots/ #recycle/", "dont compress = *.gz *.zip *.7z *.mp4 *.mkv *.jpg *.mov *.rar *.xz *.zst", ""]
    for s in shares:
        if s.get("usb") or s.get("homes") or (s.get("encrypted") and not s.get("_unlocked")):
            continue
        auth = ["@cynthology-admin:rw"]
        auth += [a for a in (_auth_entry(e, "rw") for e in s.get("write_users", [])) if a]
        auth += [a for a in (_auth_entry(e, "ro") for e in s.get("read_users", [])) if a]
        out += [f"[{s['name']}]", f"    path = {s['path']}", f"    comment = {one_line(s.get('description', ''), 80)}",
                f"    auth users = {', '.join(dict.fromkeys(auth))}"]
        if len(auth) == 1 or set(auth[1:]) <= {"@cynthology-admin:rw"}:
            out.append("    list = no")            # (admin-only folders aren't shown in the module list)
        out.append("")
    if homes_path:
        out += ["[home]", f"    path = {homes_path}/%RSYNC_USER_NAME%", "    comment = Your home folder",
                "    auth users = @users:rw, @cynthology-admin:rw", ""]
    return "\n".join(out)


def write_rsyncd() -> None:
    """Called whenever shared folders change (shares.write_configs)."""
    from . import shares as shares_mod
    if not load()["rsync"]["enabled"]:
        return
    items = [{**s, "_unlocked": not s.get("encrypted") or shares_mod.unlocked(s)} for s in shares_mod.load()]
    homes = next((s for s in items if s.get("homes")), None)
    with open(RSYNCD_CONF + ".new", "w") as f:
        f.write(render_rsyncd(items, homes["path"] if homes else None))
    os.replace(RSYNCD_CONF + ".new", RSYNCD_CONF)
    if not os.path.exists(RSYNCD_SECRETS):
        _write_secrets({})


# ---------- SFTP ----------

def render_sshd(users_sftp: bool, start_dir: str, deny: list[str] | None = None, networks: list[str] | None = None) -> str:
    lines = ["# Managed by Cynthology (Control Panel > File Services > SFTP)"]
    if users_sftp and deny:              # Control Panel > Application Privileges (never administrators)
        lines.append("DenyUsers " + " ".join(deny))
    if users_sftp and networks:
        # from anywhere else, users (not administrators) get a session that ends at once
        lines += ["Match Group users,!cynthology-admin Address *,!" + ",!".join(networks),
                  "    ForceCommand /bin/false", "    AllowTcpForwarding no", "    PermitTTY no", "    X11Forwarding no"]
    if users_sftp:
        # (internal-sftp runs inside sshd, so it works although users' login shell is nologin)
        lines += ["Match Group users,!cynthology-admin",
                  f"    ForceCommand internal-sftp -d {start_dir}",
                  "    AllowTcpForwarding no", "    PermitTunnel no", "    X11Forwarding no", "    PermitTTY no"]
    return "\n".join(lines) + "\n"


def apply_sshd(users_sftp: bool) -> None:
    from .storage import load_pools
    pools = load_pools()
    start = pools[0]["mount"] if pools else "/"
    if not re.fullmatch(r"/[A-Za-z0-9/_-]*", start):
        start = "/"
    old = open(SSHD_DROPIN).read() if os.path.exists(SSHD_DROPIN) else None
    with open(SSHD_DROPIN, "w") as f:
        from .privileges import denied_people, rule
        f.write(render_sshd(users_sftp, start, denied_people("sftp"), rule("sftp")["networks"]))
    if DEV:
        return
    r = subprocess.run(["sshd", "-t"], capture_output=True, text=True)
    if r.returncode != 0:                      # never leave sshd with a config it refuses
        if old is None:
            os.remove(SSHD_DROPIN)
        else:
            open(SSHD_DROPIN, "w").write(old)
        raise HTTPException(500, f"sshd refused the configuration: {r.stderr.strip()[:300]}")
    run(["systemctl", "reload-or-restart", "ssh"], check=False)


# ---------- API ----------

class WebDav(BaseModel):
    enabled: bool
    http: bool = False
    https: bool = True
    http_port: int = 5005
    https_port: int = 5006


class Sftp(BaseModel):
    users: bool


class Rsync(BaseModel):
    enabled: bool


class RsyncAccount(BaseModel):
    user: str
    password: str = ""            # empty = remove the account


@router.get("")
def get_all(_=Depends(require_admin)):
    cfg = load()
    return {**cfg, "rsync": {**cfg["rsync"], "accounts": sorted(_read_secrets())},
            "running": {"webdav": _active("cynthology-webdav"), "rsync": _active("rsync"), "ssh": _active("ssh")}}


@router.put("/webdav")
def put_webdav(body: WebDav, _=Depends(require_admin)):
    from . import settings
    ui = settings.get_all()
    taken = {int(ui["http_port"]), int(ui["https_port"]), 22, 80, 443, 139, 445, 873, 2049, 3260}
    for on, port in ((body.http, body.http_port), (body.https, body.https_port)):
        if on and (not 1024 <= port <= 65535 or port in taken):
            raise HTTPException(400, f"port {port} can't be used (1024-65535, not used by another service)")
    if body.http and body.https and body.http_port == body.https_port:
        raise HTTPException(400, "HTTP and HTTPS need different ports")
    if body.enabled and not (body.http or body.https):
        raise HTTPException(400, "switch on HTTP, HTTPS or both")
    cfg = load()
    cfg["webdav"] = body.model_dump()
    save(cfg)
    if not DEV:
        if body.enabled:
            run(["systemctl", "enable", "cynthology-webdav"], check=False)
            run(["systemctl", "restart", "cynthology-webdav"])
        else:
            run(["systemctl", "disable", "--now", "cynthology-webdav"], check=False)
    return {"ok": True}


@router.put("/sftp")
def put_sftp(body: Sftp, _=Depends(require_admin)):
    apply_sshd(body.users)
    cfg = load()
    cfg["sftp"] = body.model_dump()
    save(cfg)
    return {"ok": True}


@router.put("/rsync")
def put_rsync(body: Rsync, _=Depends(require_admin)):
    cfg = load()
    cfg["rsync"] = {"enabled": body.enabled}
    save(cfg)
    if body.enabled:
        write_rsyncd()
        if not DEV:
            run(["systemctl", "enable", "rsync"], check=False)
            run(["systemctl", "restart", "rsync"])
    elif not DEV:
        run(["systemctl", "disable", "--now", "rsync"], check=False)
    return {"ok": True}


@router.put("/rsync/account")
def put_rsync_account(body: RsyncAccount, _=Depends(require_admin)):
    if not USER_RE.fullmatch(body.user):
        raise HTTPException(400, "rsync accounts are for local NAS users")
    if not DEV:
        import pwd
        try:
            pwd.getpwnam(body.user)
        except KeyError:
            raise HTTPException(404, "no such user")
    sec = _read_secrets()
    if body.password:
        if len(body.password) < 8 or any(c in body.password for c in "\n\r:"):
            raise HTTPException(400, "the rsync password needs at least 8 characters (no colons)")
        sec[body.user] = body.password
    else:
        sec.pop(body.user, None)
    _write_secrets(sec)
    return {"ok": True}


def remove_user(user: str) -> None:
    """Called when a user is deleted."""
    sec = _read_secrets()
    if sec.pop(user, None) is not None:
        _write_secrets(sec)
