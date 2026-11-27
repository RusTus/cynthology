"""Local users and groups (Linux accounts + Samba password database kept in sync)."""
import grp
import pwd

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_admin
from .util import DEV, run, valid_name

router = APIRouter(prefix="/api/users", tags=["users"])
MIN_UID = 1000
_DEV_USERS = [{"name": "admin", "uid": 1000, "full_name": "Administrator", "groups": ["cynthology-admin", "users"]},
              {"name": "alice", "uid": 1001, "full_name": "Alice", "groups": ["users"]}]


class UserCreate(BaseModel):
    name: str
    password: str
    full_name: str = ""
    admin: bool = False


class Password(BaseModel):
    password: str


def _groups_of(user: str) -> list[str]:
    return [g.gr_name for g in grp.getgrall() if user in g.gr_mem]


@router.get("")
def list_users(_=Depends(require_admin)):
    if DEV:
        return _DEV_USERS
    return [{"name": p.pw_name, "uid": p.pw_uid, "full_name": p.pw_gecos.split(",")[0],
             "groups": _groups_of(p.pw_name)}
            for p in pwd.getpwall() if MIN_UID <= p.pw_uid < 60000]


def check_password(pw: str) -> None:
    if len(pw) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    if len(pw) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in pw):
        # (chpasswd and smbpasswd read lines: a line break would start another account's entry)
        raise HTTPException(400, "password can't contain line breaks or control characters (max. 256 characters)")


def check_full_name(name: str) -> str:
    name = name.strip()
    if len(name) > 100 or any(c in name for c in ":,=\\") or any(ord(c) < 32 for c in name):
        raise HTTPException(400, "full name: up to 100 characters, without : , = or \\")
    return name


def apply_password(name: str, pw: str) -> None:
    """New password for a local account (Linux + Samba); signs the account out everywhere."""
    check_password(pw)
    run(["chpasswd"], input=f"{name}:{pw}\n")
    run(["smbpasswd", "-s", name], input=f"{pw}\n{pw}\n")
    from .auth import end_sessions
    end_sessions(name)


@router.post("")
def create_user(body: UserCreate, _=Depends(require_admin)):
    valid_name(body.name, "username")
    check_password(body.password)
    body.full_name = check_full_name(body.full_name)
    groups = "users,cynthology-admin" if body.admin else "users"
    run(["useradd", "-m", "-s", "/usr/sbin/nologin", "-G", groups, "-c", body.full_name, body.name])
    run(["chpasswd"], input=f"{body.name}:{body.password}\n")
    run(["smbpasswd", "-a", "-s", body.name], input=f"{body.password}\n{body.password}\n")
    from .shares import ensure_home
    try:
        ensure_home(body.name)
    except HTTPException:
        pass
    if DEV:
        _DEV_USERS.append({"name": body.name, "uid": 1000 + len(_DEV_USERS), "full_name": body.full_name,
                           "groups": groups.split(",")})
    return {"ok": True}


@router.put("/{name}/password")
def set_password(name: str, body: Password, _=Depends(require_admin)):
    valid_name(name, "username")
    apply_password(name, body.password)
    return {"ok": True}


@router.delete("/{name}")
def delete_user(name: str, _=Depends(require_admin)):
    valid_name(name, "username")
    if name in ("root", "admin"):
        raise HTTPException(400, "cannot delete built-in account")
    run(["smbpasswd", "-x", name], check=False)
    run(["userdel", name])
    from .auth import end_sessions
    end_sessions(name)
    from .fileservices import remove_user       # (their rsync account and sharing links go too)
    remove_user(name)
    from . import sharelinks
    sharelinks.remove_user(name)
    if DEV:
        _DEV_USERS[:] = [u for u in _DEV_USERS if u["name"] != name]
    return {"ok": True}


@router.get("/groups")
def list_groups(_=Depends(require_admin)):
    if DEV:
        return [{"name": "users", "members": ["admin", "alice"]}, {"name": "cynthology-admin", "members": ["admin"]}]
    return [{"name": g.gr_name, "members": g.gr_mem} for g in grp.getgrall()
            if g.gr_gid >= MIN_UID or g.gr_name in ("users", "cynthology-admin")]
