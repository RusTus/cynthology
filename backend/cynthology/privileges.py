"""Application privileges: who may use which file service, and from which networks.

One rule per application:
  mode     "all" (everyone) | "allow" (only the listed users/groups) | "deny" (everyone except them)
  users    ["alice", ...]     groups ["sales", ...]
  networks ["192.168.1.0/24", ...]   - empty = from anywhere
Administrators always keep access (no lock-out) - except for an SMB network limit, which Samba
applies to every account.

Enforced by:  files / preview routers (File Station) and sharing links - checked per request here;
WebDAV - at sign-in; SMB - per share "invalid users" / "hosts allow" (shares.smb_block);
SFTP - sshd DenyUsers and a Match Address block (fileservices.render_sshd).
"""
import grp
import ipaddress
import json
import os
import pwd
import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .auth import _is_admin, current_user, require_admin
from .util import DEV, STATE_DIR, valid_name

router = APIRouter(prefix="/api/privileges", tags=["privileges"])
FILE = os.path.join(STATE_DIR, "privileges.json")
APPS = {
    "files": "File Station",
    "sharelinks": "Sharing links",
    "smb": "SMB (Windows / Mac file sharing)",
    "webdav": "WebDAV",
    "sftp": "SFTP",
    "sync": "Nextcloud apps (desktop & phone sync)",
}
MIN_UID = 1000
_lock = threading.Lock()


def load() -> dict:
    try:
        with open(FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def rule(app: str) -> dict:
    r = load().get(app) or {}
    return {"mode": r.get("mode", "all"), "users": r.get("users", []), "groups": r.get("groups", []),
            "networks": r.get("networks", [])}


def _groups_of(user: str) -> set[str]:
    if DEV:
        return {"users"}
    out = {g.gr_name for g in grp.getgrall() if user in g.gr_mem}
    try:
        out.add(grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name)
    except KeyError:
        pass
    return out


def _listed(r: dict, user: str) -> bool:
    u = user.lower()
    return u in {x.lower() for x in r["users"]} or bool({g.lower() for g in r["groups"]} & {g.lower() for g in _groups_of(user)})


def user_allowed(app: str, user: str, admin: bool | None = None) -> bool:
    """The user part of the rule (no network check)."""
    if admin if admin is not None else _is_admin(user):
        return True
    r = rule(app)
    if r["mode"] == "allow":
        return _listed(r, user)
    if r["mode"] == "deny":
        return not _listed(r, user)
    return True


def network_allowed(app: str, ip: str | None) -> bool:
    nets = rule(app)["networks"]
    if not nets or ip is None:
        return True
    try:
        a = ipaddress.ip_address(ip)
        if a.version == 6 and a.ipv4_mapped:
            a = a.ipv4_mapped
    except ValueError:
        return False
    return any(a in ipaddress.ip_network(n, strict=False) for n in nets)


def allowed(app: str, user: str, ip: str | None = None, admin: bool | None = None) -> bool:
    is_admin = admin if admin is not None else _is_admin(user)
    return user_allowed(app, user, is_admin) and (is_admin or network_allowed(app, ip))


def require_app(app: str):
    """FastAPI dependency for routers of one application."""
    def dep(request: Request, user: dict = Depends(current_user)) -> dict:
        ip = request.client.host if request.client else None
        if not allowed(app, user["user"], ip, user.get("admin")):
            where = "" if not user_allowed(app, user["user"], user.get("admin")) else " from this network"
            raise HTTPException(403, f"You don't have permission to use {APPS[app]}{where}. "
                                     "Ask an administrator (Control Panel > Application Privileges).")
        return user
    return dep


def local_people() -> list[str]:
    """Local, non-administrator accounts (for 'only these' rules, which Samba/sshd need as names)."""
    if DEV:
        return ["alice"]
    return sorted(p.pw_name for p in pwd.getpwall() if MIN_UID <= p.pw_uid < 60000 and not _is_admin(p.pw_name))


def denied_people(app: str) -> list[str]:
    """Local accounts the rule keeps out (for Samba/sshd, which can't express 'only these')."""
    if rule(app)["mode"] == "all":
        return []
    return [u for u in local_people() if not user_allowed(app, u, admin=False)]


def smb_invalid_users() -> list[str]:
    """Samba "invalid users" for every share: the local accounts the rule keeps out. (Groups are
    expanded to their members here, so an administrator in a denied group keeps access.)"""
    return denied_people("smb")


def smb_hosts_allow() -> list[str]:
    nets = rule("smb")["networks"]
    return ["127.0.0.1", "::1", *nets] if nets else []


class Rule(BaseModel):
    mode: str = "all"
    users: list[str] = []
    groups: list[str] = []
    networks: list[str] = []


def _clean(r: Rule) -> dict:
    if r.mode not in ("all", "allow", "deny"):
        raise HTTPException(400, "mode: all, allow or deny")
    users = [valid_name(u, "user name") for u in r.users]
    groups = [valid_name(g, "group name") for g in r.groups]
    nets = []
    for n in r.networks:
        n = n.strip()
        if not n:
            continue
        try:
            nets.append(str(ipaddress.ip_network(n, strict=False)))
        except ValueError:
            raise HTTPException(400, f"'{n}' isn't a network (e.g. 192.168.1.0/24) or an address")
    return {"mode": r.mode, "users": sorted(set(users)), "groups": sorted(set(groups)), "networks": nets}


@router.get("")
def get_all(_=Depends(require_admin)):
    return {"apps": APPS, "rules": {a: rule(a) for a in APPS}}


@router.put("/{app}")
def put(app: str, body: Rule, _=Depends(require_admin)):
    if app not in APPS:
        raise HTTPException(404, "unknown application")
    with _lock:
        d = load()
        d[app] = _clean(body)
        fd = os.open(FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(FILE + ".tmp", FILE)
    apply()
    return rule(app)


def apply() -> None:
    """Rewrite what Samba and sshd read (also after users or groups change)."""
    if DEV:
        return
    from . import fileservices, shares
    try:
        shares.write_configs(shares.load())
    except Exception as e:
        print("privileges: smb:", e)
    try:
        fileservices.apply_sshd(fileservices.load()["sftp"]["users"])
    except Exception as e:
        print("privileges: sshd:", e)


@router.get("/mine")
def mine(request: Request, user: dict = Depends(current_user)):
    ip = request.client.host if request.client else None
    return {a: allowed(a, user["user"], ip, user.get("admin")) for a in APPS}
