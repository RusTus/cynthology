"""Configuration backup and restore (DSM: Control Panel > Update & Restore > Configuration Backup).

Export: one encrypted file (.cyncfg) with users and groups (password hashes for sign-in and SMB,
same user IDs so files on an imported pool still belong to the right people), 2-step sign-in,
shared folders and their permissions, sharing links, and the settings of the other services.
It is a gzip'd tar, encrypted with AES-256 (openssl enc, PBKDF2 600 000 rounds) under a password
you choose - it holds password hashes and keys, so it is never written unencrypted.

Restore: upload the file, enter its password, see what is in it, choose what to bring back.
- Users and groups that already exist are left as they are; new ones are created.
- Shared folders come back when their folder exists (import the storage pool first).
- The firewall comes back switched off, so restored rules can never lock you out.
- Network settings and iSCSI are not included (they depend on the hardware and the pools).
"""
import grp
import io
import json
import os
import pwd
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from .auth import require_admin
from .util import DEV, STATE_DIR, run, valid_name

router = APIRouter(prefix="/api/config", tags=["config"])
UPLOAD = os.path.join(STATE_DIR, "config-restore.cyncfg")
MIN_UID, MAX_UID = 1000, 60000
FORMAT = 1
# settings files (relative to the state folder) per category; directories are copied whole
SETTINGS_FILES = ["notify.json", "ddns.json", "fileservices.json", "vpn.json", "ups.json", "webstation.json",
                  "firewall.json", "settings.json", "package-sources.json", "usb.json", "tasks/tasks.json",
                  "backup", "repl", "vault"]
SHARE_FILES = ["sharelinks.json", "sharelinks.key"]
TLS_DIR = "/etc/cynthology/tls"
KEY_DIR = "/etc/cynthology/keys"
CATEGORIES = {"users": "Users and groups (with passwords and 2-step sign-in)",
              "shares": "Shared folders, permissions, home folders and sharing links",
              "settings": "Notifications, DDNS, file services, VPN, UPS, Web Station, scheduled tasks, backup and "
                          "replication tasks, firewall rules (restored switched off), certificate, general settings"}


# ---------- crypto (openssl) ----------

def _crypt(data: bytes, password: str, decrypt: bool) -> bytes:
    cmd = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "600000", "-md", "sha256", "-salt", "-pass", "env:CYN_CFG_PW"]
    if decrypt:
        cmd.append("-d")
    # (the password goes in the environment of this one root process - never on a command line)
    p = subprocess.run(cmd, input=data, capture_output=True, timeout=120,
                       env={"PATH": "/usr/bin:/bin", "CYN_CFG_PW": password})
    if p.returncode != 0:
        raise HTTPException(400, "wrong password, or not a Cynthology configuration file" if decrypt else "encryption failed")
    return p.stdout


# ---------- collect ----------

def _shadow() -> dict[str, str]:
    out = {}
    try:
        for ln in open("/etc/shadow"):
            parts = ln.split(":")
            if len(parts) > 1:
                out[parts[0]] = parts[1]
    except OSError:
        pass
    return out


def _nt_hashes() -> dict[str, str]:
    """Samba (SMB) password hashes: name -> smbpasswd line."""
    out = {}
    r = subprocess.run(["pdbedit", "-L", "-w"], capture_output=True, text=True)
    for ln in r.stdout.splitlines():
        name = ln.split(":", 1)[0]
        if name:
            out[name] = ln
    return out


def collect_users() -> dict:
    if DEV:
        return {"users": [{"name": "alice", "uid": 1001, "gecos": "Alice", "groups": ["users"], "shadow": "$y$dev", "smb": ""}],
                "groups": [{"name": "family", "gid": 1500, "members": ["alice"]}]}
    shadow, smb = _shadow(), _nt_hashes()
    users = [p for p in pwd.getpwall() if MIN_UID <= p.pw_uid < MAX_UID]
    names = {p.pw_name for p in users}
    private = {p.pw_gid for p in users if grp.getgrgid(p.pw_gid).gr_name == p.pw_name} if users else set()
    groups = [g for g in grp.getgrall() if (MIN_UID <= g.gr_gid < MAX_UID and g.gr_gid not in private) or g.gr_name == "cynthology-admin"]
    keep_groups = {g.gr_name for g in groups} | {"users"}
    return {
        "users": [{"name": p.pw_name, "uid": p.pw_uid, "gecos": p.pw_gecos, "shadow": shadow.get(p.pw_name, "!"),
                   "smb": smb.get(p.pw_name, ""),
                   "groups": sorted(g.gr_name for g in grp.getgrall() if p.pw_name in g.gr_mem and g.gr_name in keep_groups)}
                  for p in users],
        "groups": [{"name": g.gr_name, "gid": g.gr_gid, "members": [m for m in g.gr_mem if m in names]} for g in groups],
    }


def build(password: str) -> bytes:
    from . import shares as shares_mod
    buf = io.BytesIO()
    manifest = {"format": FORMAT, "hostname": socket.gethostname(), "created": int(time.time()),
                "version": _version(), "categories": list(CATEGORIES)}

    def add_bytes(tf, name, data: bytes):
        ti = tarfile.TarInfo(name)
        ti.size, ti.mtime, ti.mode = len(data), int(time.time()), 0o600
        tf.addfile(ti, io.BytesIO(data))

    def add_path(tf, src, arc):
        if os.path.isdir(src):
            for dp, dirs, fs in os.walk(src):
                dirs[:] = [d for d in dirs if d not in ("cache", "mnt", "tmp")]      # (restic cache etc.: not settings)
                for f in fs:
                    full = os.path.join(dp, f)
                    if not os.path.islink(full) and os.path.getsize(full) < 5_000_000 and not f.endswith((".log", ".lock")):
                        add_bytes(tf, os.path.join(arc, os.path.relpath(full, src)), open(full, "rb").read())
        elif os.path.isfile(src):
            add_bytes(tf, arc, open(src, "rb").read())

    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        add_bytes(tf, "manifest.json", json.dumps(manifest).encode())
        add_bytes(tf, "users.json", json.dumps(collect_users()).encode())
        add_path(tf, os.path.join(STATE_DIR, "2fa.json"), "state/2fa.json")
        add_bytes(tf, "shares.json", json.dumps(shares_mod.load()).encode())
        for f in SHARE_FILES + SETTINGS_FILES:
            add_path(tf, os.path.join(STATE_DIR, f), f"state/{f}")
        add_path(tf, os.path.join(STATE_DIR, "smtp.json"), "state/smtp.json")
        if not DEV:
            add_path(tf, KEY_DIR, "keys")
            add_path(tf, "/etc/rsyncd.secrets", "etc/rsyncd.secrets")
            kind = open(os.path.join(TLS_DIR, "kind")).read().strip() if os.path.exists(os.path.join(TLS_DIR, "kind")) else ""
            if kind in ("uploaded", "letsencrypt"):
                add_path(tf, TLS_DIR, "tls")
    return b"CYNCFG1\n" + _crypt(buf.getvalue(), password, decrypt=False)


def _version() -> str:
    for p in ("/opt/cynthology/VERSION", os.path.join(os.path.dirname(__file__), "..", "..", "VERSION")):
        try:
            return open(p).read().strip()
        except OSError:
            continue
    return "dev"


# ---------- read ----------

def open_backup(data: bytes, password: str) -> dict[str, bytes]:
    if not data.startswith(b"CYNCFG1\n"):
        raise HTTPException(400, "not a Cynthology configuration file")
    raw = _crypt(data[8:], password, decrypt=True)
    out = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            for m in tf.getmembers():
                if not m.isfile() or m.size > 20_000_000 or m.name.startswith("/") or ".." in m.name.split("/"):
                    continue
                out[m.name] = tf.extractfile(m).read()
    except (tarfile.TarError, OSError):
        raise HTTPException(400, "the configuration file is damaged")
    if "manifest.json" not in out:
        raise HTTPException(400, "the configuration file is damaged")
    return out


def summary(files: dict[str, bytes]) -> dict:
    from . import shares as shares_mod
    man = json.loads(files["manifest.json"])
    u = json.loads(files.get("users.json", b"{}"))
    existing_users = {p.pw_name for p in pwd.getpwall()} if not DEV else {"admin"}
    have_shares = {s["name"].lower() for s in shares_mod.load()}
    shares = []
    for s in json.loads(files.get("shares.json", b"[]")):
        state = "exists" if s["name"].lower() in have_shares else \
            "ready" if os.path.isdir(s.get("cipher") or s["path"]) else "missing"
        shares.append({"name": s["name"], "path": s["path"], "state": state})
    settings = sorted({k.split("/")[1] for k in files if k.startswith("state/") and k.split("/")[1] not in ("2fa.json", *SHARE_FILES)})
    return {"manifest": man, "categories": CATEGORIES,
            "users": [{"name": x["name"], "uid": x["uid"], "state": "exists" if x["name"] in existing_users else "new"} for x in u.get("users", [])],
            "groups": [{"name": g["name"], "members": g["members"]} for g in u.get("groups", [])],
            "shares": shares, "settings": settings, "has_certificate": any(k.startswith("tls/") for k in files)}


# ---------- restore ----------

def restore_users(files: dict[str, bytes], report: list[str]) -> None:
    data = json.loads(files.get("users.json", b"{}"))
    for g in data.get("groups", []):
        valid_name(g["name"], "group")
        try:
            grp.getgrnam(g["name"])
        except KeyError:
            gid_free = not any(x.gr_gid == g["gid"] for x in grp.getgrall())
            run(["groupadd", *(["-g", str(g["gid"])] if gid_free else []), g["name"]])
            report.append(f"group {g['name']} created")
    smb_lines = []
    for u in data.get("users", []):
        valid_name(u["name"], "user")
        try:
            pwd.getpwnam(u["name"])
            report.append(f"user {u['name']} already exists - left as it is")
            continue
        except KeyError:
            pass
        uid_free = not any(p.pw_uid == u["uid"] for p in pwd.getpwall())
        groups = ",".join(g for g in u.get("groups", []) if _group_exists(g)) or "users"
        gecos = "".join(c for c in u.get("gecos", "") if c not in ":\n")[:100]
        run(["useradd", "-m", "-s", "/usr/sbin/nologin", "-G", groups, "-c", gecos,
             *(["-u", str(u["uid"])] if uid_free else []), u["name"]])
        if u.get("shadow") and not u["shadow"].startswith("!"):
            run(["usermod", "-p", u["shadow"], u["name"]])
        if u.get("smb"):
            parts = u["smb"].split(":")
            parts[1] = str(pwd.getpwnam(u["name"]).pw_uid)
            smb_lines.append(":".join(parts))
        report.append(f"user {u['name']} created" + ("" if uid_free else
                      f" with a new user ID (its old ID {u['uid']} is taken) - files it owned on imported pools need new owners"))
    if smb_lines:
        fd, path = tempfile.mkstemp(prefix="cyn-smb-")
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(smb_lines) + "\n")
        try:
            run(["pdbedit", "-i", f"smbpasswd:{path}"], check=False)
        finally:
            os.remove(path)
    # 2-step sign-in for users that don't have their own already
    saved = json.loads(files.get("state/2fa.json", b"{}") or b"{}")
    if saved:
        cur_path = os.path.join(STATE_DIR, "2fa.json")
        try:
            cur = json.load(open(cur_path))
        except (OSError, ValueError):
            cur = {}
        added = {k: v for k, v in saved.items() if k not in cur} if isinstance(saved, dict) else {}
        if added:
            cur.update(added)
            _write_secret(cur_path, json.dumps(cur).encode())
            report.append(f"2-step sign-in restored for {len(added)} user(s)")


def _group_exists(g: str) -> bool:
    try:
        grp.getgrnam(g)
        return True
    except KeyError:
        return False


def _write_secret(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(path + ".new", path)


def restore_shares(files: dict[str, bytes], report: list[str]) -> None:
    from . import shares as shares_mod, sharelinks
    from .storage import load_pools
    from .util import valid_principal
    pools = load_pools()
    cur = shares_mod.load()
    have = {s["name"].lower() for s in cur}
    added = []
    for s in json.loads(files.get("shares.json", b"[]")):
        try:
            valid_name(s["name"], "share")
            for e in s.get("read_users", []) + s.get("write_users", []):
                if not s.get("homes"):
                    valid_principal(e)
        except HTTPException:
            report.append(f"shared folder {s.get('name')!r} skipped (invalid entry)")
            continue
        if s["name"].lower() in have or s.get("usb"):
            continue
        pool = next((p for p in pools if s["path"].startswith(p["mount"].rstrip("/") + "/")), None)
        if not pool or not os.path.isdir(s.get("cipher") or s["path"]):
            report.append(f"shared folder {s['name']} not restored - {s['path']} is not there (import its storage pool first)")
            continue
        rec = {**s, "pool_id": pool["id"], "description": shares_mod.one_line(s.get("description", ""))}
        cur.append(rec)
        added.append(rec)
        if s.get("encrypted") and f"keys/{s['name']}.key" in files:
            _write_secret(os.path.join(KEY_DIR, f"{s['name']}.key"), files[f"keys/{s['name']}.key"])
        report.append(f"shared folder {s['name']} restored")
    if added:
        shares_mod.write_configs(cur)
        if any(r.get("encrypted") for r in added):          # encrypted folders with a stored key: unlock now
            opened = shares_mod.unlock_at_boot()
            if opened:
                report.append("unlocked: " + ", ".join(opened))
        for rec in added:
            if not rec.get("homes") and not (rec.get("encrypted") and not shares_mod.unlocked(rec)):
                shares_mod.sync_group_perms(rec["path"], rec)
    # sharing links: only for folders that exist here now
    links = json.loads(files.get("state/sharelinks.json", b"[]") or b"[]")
    names = {s["name"].lower() for s in shares_mod.load()}
    mine = sharelinks.load()
    ids = {x["id"] for x in mine}
    new = [x for x in links if x.get("id") not in ids and x.get("path", "/").strip("/").split("/")[0].lower() in names]
    if new:
        if not os.path.exists(sharelinks.KEY_FILE) and "state/sharelinks.key" in files:
            _write_secret(sharelinks.KEY_FILE, files["state/sharelinks.key"])
        sharelinks.save(mine + new)
        report.append(f"{len(new)} sharing link(s) restored")


def restore_settings(files: dict[str, bytes], report: list[str]) -> None:
    from . import firewall, settings
    for name, data in files.items():
        if not name.startswith("state/"):
            continue
        rel = name[len("state/"):]
        top = rel.split("/")[0]
        if rel in ("2fa.json", *SHARE_FILES) or not (rel in SETTINGS_FILES or top in ("backup", "repl") or rel == "smtp.json"):
            continue
        dest = os.path.join(STATE_DIR, rel)
        if rel == "settings.json":                       # merge: keep this install's "setup done" etc.
            saved = json.loads(data)
            settings.update(**{k: v for k, v in saved.items() if k in ("https_redirect", "http_port", "https_port")})
            continue
        if rel == "firewall.json":
            cfg = json.loads(data)
            cfg["enabled"] = False                       # (never lock anyone out: review and switch on)
            data = json.dumps(cfg).encode()
        _write_secret(dest, data)
    report.append("settings files restored")
    if "etc/rsyncd.secrets" in files and not DEV:
        _write_secret("/etc/rsyncd.secrets", files["etc/rsyncd.secrets"])
    # firewall: rules restored but switched off - apply what that means now
    try:
        cfg = firewall.load()
        text = firewall.render(cfg) if firewall.needed(cfg) else None
        firewall._load_rules(text)
        if text is None and os.path.exists(firewall.NFT):
            os.remove(firewall.NFT)
        elif text:
            open(firewall.NFT, "w").write(text)
        report.append("firewall rules restored - switched off; review them and switch the firewall on")
    except Exception as e:
        report.append(f"firewall: {e}")
    if any(k.startswith("tls/") for k in files) and not DEV:
        for k, v in files.items():
            if k.startswith("tls/") and "/" not in k[4:]:
                _write_secret(os.path.join(TLS_DIR, k[4:]), v)
        report.append("certificate restored (takes effect after the web interface restarts)")
    _reapply(report)


def _reapply(report: list[str]) -> None:
    """Make the services follow the restored settings."""
    steps = []

    def step(label, fn):
        try:
            fn()
            steps.append(label)
        except Exception as e:
            report.append(f"{label}: {getattr(e, 'detail', e)}")
    from . import fileservices, tasks, ups, vpn, webstation
    fs = fileservices.load()
    step("SFTP", lambda: fileservices.apply_sshd(fs["sftp"]["users"]))
    if fs["rsync"]["enabled"]:
        step("rsync server", lambda: (fileservices.write_rsyncd(), None if DEV else run(["systemctl", "enable", "--now", "rsync"], check=False)))
    if fs["webdav"]["enabled"] and not DEV:
        step("WebDAV", lambda: run(["systemctl", "enable", "--now", "cynthology-webdav"], check=False))
    if not DEV:
        step("scheduled tasks", lambda: [tasks.install_units(t) for t in tasks.load()])
    if vpn.load().get("enabled"):
        step("VPN", lambda: vpn.apply(vpn.load()))
    if ups.load().get("enabled"):
        step("UPS", lambda: ups.apply(ups.load()))
    from . import vault
    if vault.load().get("enabled"):
        step("Backup Vault", lambda: vault.apply(vault.load()))
    if webstation.installed() and webstation.load().get("sites"):
        step("Web Station", lambda: webstation.apply(webstation.load()))
    if steps:
        report.append("applied: " + ", ".join(steps))


# ---------- API ----------

class Password(BaseModel):
    password: str


class Apply(BaseModel):
    password: str
    categories: list[str]


@router.post("/backup")
def backup(body: Password, _=Depends(require_admin)):
    if len(body.password) < 8:
        raise HTTPException(400, "choose a password of at least 8 characters - the file holds password hashes and keys")
    data = build(body.password)
    name = f"cynthology-{socket.gethostname()}-{time.strftime('%Y-%m-%d')}.cyncfg"
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.post("/restore/upload")
async def upload(request: Request, _=Depends(require_admin)):
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > 50_000_000:
            raise HTTPException(413, "this file is too big to be a configuration backup")
    if not data.startswith(b"CYNCFG1\n"):
        raise HTTPException(400, "not a Cynthology configuration file (.cyncfg)")
    _write_secret(UPLOAD, data)
    return {"ok": True}


def _uploaded(password: str) -> dict[str, bytes]:
    try:
        data = open(UPLOAD, "rb").read()
    except OSError:
        raise HTTPException(400, "upload the configuration file first")
    return open_backup(data, password)


@router.post("/restore/inspect")
def inspect(body: Password, _=Depends(require_admin)):
    return summary(_uploaded(body.password))


@router.post("/restore/apply")
def apply(body: Apply, _=Depends(require_admin)):
    files = _uploaded(body.password)
    if not body.categories or any(c not in CATEGORIES for c in body.categories):
        raise HTTPException(400, "choose what to restore")
    report: list[str] = []
    if "users" in body.categories:                 # (users first: shares and links refer to them)
        restore_users(files, report)
    if "shares" in body.categories:
        restore_shares(files, report)
    if "settings" in body.categories:
        restore_settings(files, report)
    try:
        os.remove(UPLOAD)
    except OSError:
        pass
    try:
        from .notify import notify
        notify("info", "Configuration restored", "\n".join(report), event="config")
    except Exception:
        pass
    return {"report": report}
