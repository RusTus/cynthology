"""Backup Vault (DSM: Hyper Backup Vault): let other Cynthology boxes back up to this one.

Runs restic's REST server (Debian package restic-rest-server, its own unprivileged account and
hardened systemd unit) over HTTPS with this NAS's certificate. Each sending NAS gets an account;
with --private-repos an account can only reach its own folder. Append-only (on by default) means
a sender can add backups but never delete or overwrite them - so a compromised or ransomware-hit
sender can't destroy its own backup history. Old versions are then cleaned up here, by you.

Storage: <pool>/@vault/<account>/<repository>, not a shared folder (nothing to see over SMB).
"""
import hashlib
import json
import os
import re
import ssl
import subprocess

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_admin
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/vault", tags=["vault"])
DIR = os.path.join(STATE_DIR, "vault")
CFG = os.path.join(DIR, "vault.json")
HTPASSWD = os.path.join(DIR, "htpasswd")
ENV_FILE = os.path.join(DIR, "env")
DROPIN = os.path.join(STATE_DIR, "vault-dropin.conf") if DEV else "/etc/systemd/system/restic-rest-server.service.d/cynthology.conf"
UNIT = "restic-rest-server"
SVC_USER = "restic-rest-server"
TLS_CERT, TLS_KEY = "/etc/cynthology/tls/cert.pem", "/etc/cynthology/tls/key.pem"
ACCOUNT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
DEFAULTS = {"enabled": False, "port": 6281, "pool_id": None, "append_only": True}


def load() -> dict:
    try:
        return {**DEFAULTS, **json.load(open(CFG))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(cfg: dict) -> None:
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    with open(CFG + ".new", "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(CFG + ".new", CFG)


def accounts() -> dict[str, str]:
    out = {}
    try:
        for ln in open(HTPASSWD):
            name, sep, h = ln.strip().partition(":")
            if sep and ACCOUNT_RE.fullmatch(name):
                out[name] = h
    except OSError:
        pass
    return out


def _write_htpasswd(acc: dict[str, str]) -> None:
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    fd = os.open(HTPASSWD + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.writelines(f"{n}:{h}\n" for n, h in sorted(acc.items()))
    os.replace(HTPASSWD + ".new", HTPASSWD)
    if not DEV:
        run(["chown", SVC_USER, HTPASSWD], check=False)


def _hash(pw: str) -> str:
    import bcrypt
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=10)).decode()


def data_dir(cfg: dict) -> str | None:
    from .storage import load_pools
    pool = next((p for p in load_pools() if p["id"] == cfg.get("pool_id")), None)
    return os.path.join(pool["mount"], "@vault") if pool else None


def fingerprint(pem_path: str = TLS_CERT) -> str:
    """SHA-256 of the certificate, shown on both NAS boxes so you can compare them."""
    try:
        der = ssl.PEM_cert_to_DER_cert(open(pem_path).read())
    except (OSError, ValueError):
        return ""
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def render_env(cfg: dict, path: str) -> str:
    """Environment for the packaged unit (pure; unit tested)."""
    args = ["--tls", "--tls-cert", f"{DIR}/tls/cert.pem", "--tls-key", f"{DIR}/tls/key.pem",
            "--private-repos", "--htpasswd-file", HTPASSWD]
    if cfg.get("append_only", True):
        args.append("--append-only")
    return f"BACKUP_DIR={path}\nLISTEN=:{int(cfg['port'])}\nARGS=\"{' '.join(args)}\"\n"


def apply(cfg: dict) -> None:
    if DEV:
        return
    if not cfg["enabled"]:
        run(["systemctl", "disable", "--now", UNIT], check=False)
        return
    path = data_dir(cfg)
    if not path:
        raise HTTPException(400, "choose the storage pool for the vault")
    if subprocess.run(["id", SVC_USER], capture_output=True).returncode != 0:
        raise HTTPException(500, "restic-rest-server is not installed - update Cynthology")
    os.makedirs(path, exist_ok=True)
    run(["chown", f"{SVC_USER}:{SVC_USER}", path])
    os.chmod(path, 0o700)
    os.makedirs(os.path.join(DIR, "tls"), exist_ok=True)
    run(["chown", SVC_USER, DIR, os.path.join(DIR, "tls")])
    os.chmod(DIR, 0o700)
    if not os.path.exists(HTPASSWD):
        _write_htpasswd({})
    with open(ENV_FILE, "w") as f:
        f.write(render_env(cfg, path))
    os.makedirs(os.path.dirname(DROPIN), exist_ok=True)
    with open(DROPIN, "w") as f:
        f.write("# Managed by Cynthology (Control Panel > File Services > Backup Vault)\n[Service]\n"
                f"EnvironmentFile={ENV_FILE}\n"
                # the NAS certificate, copied for the service account each start (so renewals apply)
                f"ExecStartPre=+/usr/bin/install -o {SVC_USER} -m 0600 {TLS_CERT} {TLS_KEY} {DIR}/tls/\n"
                f"ReadWritePaths={path}\n")
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", UNIT], check=False)
    run(["systemctl", "restart", UNIT])


def repo_sizes(cfg: dict) -> dict[str, int]:
    path = data_dir(cfg)
    out = {}
    if not path or not os.path.isdir(path):
        return out
    for name in accounts():
        p = os.path.join(path, name)
        if os.path.isdir(p):
            r = subprocess.run(["du", "-sb", p], capture_output=True, text=True)
            out[name] = int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout else 0
    return out


# ---------- API ----------

class VaultCfg(BaseModel):
    enabled: bool
    port: int = 6281
    pool_id: int | None = None
    append_only: bool = True


class Account(BaseModel):
    name: str
    password: str = ""            # empty = remove the account (its backups stay)


@router.get("")
def get_vault(_=Depends(require_admin)):
    cfg = load()
    active = not DEV and subprocess.run(["systemctl", "is-active", "--quiet", UNIT]).returncode == 0
    return {**cfg, "running": active, "accounts": sorted(accounts()), "sizes": repo_sizes(cfg),
            "fingerprint": fingerprint(), "path": data_dir(cfg)}


@router.put("")
def put_vault(body: VaultCfg, _=Depends(require_admin)):
    from . import settings
    ui = settings.get_all()
    if not 1024 <= body.port <= 65535 or body.port in (int(ui["http_port"]), int(ui["https_port"]), 5005, 5006):
        raise HTTPException(400, "choose a free port between 1024 and 65535")
    cfg = {**load(), **body.model_dump()}
    if cfg["enabled"] and not data_dir(cfg):
        raise HTTPException(400, "choose the storage pool for the vault")
    apply(cfg)
    save(cfg)
    return {"ok": True}


@router.put("/account")
def put_account(body: Account, _=Depends(require_admin)):
    if not ACCOUNT_RE.fullmatch(body.name):
        raise HTTPException(400, "account name: lowercase letters, digits, - and _ (e.g. the sending NAS's name)")
    acc = accounts()
    if body.password:
        if len(body.password) < 12:
            raise HTTPException(400, "use a password of at least 12 characters")
        acc[body.name] = _hash(body.password)
    else:
        acc.pop(body.name, None)
    _write_htpasswd(acc)
    if not DEV and load()["enabled"]:
        run(["systemctl", "reload-or-restart", UNIT], check=False)
    return {"ok": True}
