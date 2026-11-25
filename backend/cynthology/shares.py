"""Shared folders: Btrfs subvolumes exported over SMB (Samba) and optionally NFS.

Cynthology owns /etc/samba/cyn-shares.conf (included from smb.conf) and
/etc/exports.d/cyn.exports, regenerating both from shares.json on every change.
"""
import json
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import current_user, require_admin
from .storage import load_pools
from .util import DEV, STATE_DIR, read_only, run, valid_name

router = APIRouter(prefix="/api/shares", tags=["shares"])
SHARES_FILE = os.path.join(STATE_DIR, "shares.json")
SMB_CONF = os.path.join(STATE_DIR, "cyn-shares.conf") if DEV else "/etc/samba/cyn-shares.conf"
NFS_CONF = os.path.join(STATE_DIR, "cyn.exports") if DEV else "/etc/exports.d/cyn.exports"


class Share(BaseModel):
    name: str
    pool_id: int
    description: str = ""
    smb: bool = True
    nfs: bool = False
    nfs_clients: str = "192.168.0.0/16"
    recycle_bin: bool = True
    read_users: list[str] = []
    write_users: list[str] = ["@users"]


def load() -> list[dict]:
    try:
        with open(SHARES_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def write_configs(shares: list[dict]) -> None:
    smb, nfs = [], []
    for s in shares:
        if s["smb"]:
            block = [f"[{s['name']}]", f"   path = {s['path']}", f"   comment = {s['description']}",
                     "   browseable = yes", "   read only = yes",
                     f"   valid users = {' '.join(s['read_users'] + s['write_users'])}",
                     f"   write list = {' '.join(s['write_users'])}",
                     "   vfs objects = btrfs shadow_copy2" + (" recycle" if s["recycle_bin"] else ""),
                     # snapshots (see snapshots.py) appear as Windows "Previous Versions"
                     "   shadow:snapdir = .snapshots", "   shadow:sort = desc", "   shadow:format = @GMT-%Y.%m.%d-%H.%M.%S",
                     "   hide dot files = yes"]
            if s["recycle_bin"]:
                block += ["   recycle:repository = #recycle", "   recycle:keeptree = yes", "   recycle:versions = yes",
                          "   recycle:touch = yes", "   recycle:exclude_dir = .snapshots"]
            smb.append("\n".join(block))
        if s["nfs"]:
            nfs.append(f"{s['path']} {s['nfs_clients']}(rw,sync,no_subtree_check,root_squash)")
    os.makedirs(os.path.dirname(SMB_CONF), exist_ok=True)
    with open(SMB_CONF, "w") as f:
        f.write("# Managed by Cynthology - do not edit\n\n" + "\n\n".join(smb) + "\n")
    os.makedirs(os.path.dirname(NFS_CONF), exist_ok=True)
    with open(NFS_CONF, "w") as f:
        f.write("# Managed by Cynthology - do not edit\n" + "\n".join(nfs) + "\n")
    with open(SHARES_FILE, "w") as f:
        json.dump(shares, f, indent=2)
    run(["smbcontrol", "all", "reload-config"], check=False)
    run(["exportfs", "-ra"], check=False)


@router.get("")
def list_shares(_=Depends(current_user)):
    return load()


@router.post("")
def create_share(body: Share, _=Depends(require_admin)):
    valid_name(body.name, "share name")
    for u in body.read_users + body.write_users:
        valid_name(u.lstrip("@"), "user/group")
    shares = load()
    if any(s["name"].lower() == body.name.lower() for s in shares):
        raise HTTPException(409, "share already exists")
    pool = next((p for p in load_pools() if p["id"] == body.pool_id), None)
    if not pool:
        raise HTTPException(400, "no such volume")
    path = f"{pool['mount']}/{body.name}"
    if DEV:  # real folder under the dev state dir so File Station can be tried out
        path = os.path.join(STATE_DIR, "root", pool["mount"].lstrip("/"), body.name)
        os.makedirs(path, exist_ok=True)
    _make_dir(path)
    shares.append({**body.model_dump(), "path": path})
    write_configs(shares)
    return shares[-1]


def _make_dir(path: str) -> None:
    """Btrfs subvolume when the volume is Btrfs (snapshot-able), plain directory otherwise."""
    parent_fs = read_only(["stat", "-f", "-c", "%T", os.path.dirname(path)]).strip()
    if parent_fs == "btrfs":
        run(["btrfs", "subvolume", "create", path])
    else:
        run(["mkdir", "-p", path])
    run(["chgrp", "users", path])
    run(["chmod", "2775", path])


def ensure_share(name: str, rw_users: list[str]) -> str:
    """Used by packages (conf/resource data-share): create the share if missing, grant rw via ACL."""
    valid_name(name, "share name")
    shares = load()
    share = next((s for s in shares if s["name"].lower() == name.lower()), None)
    if not share:
        pools = load_pools()
        if not pools:
            raise HTTPException(400, "no volume to create shared folder on")
        share = create_share(Share(name=name, pool_id=pools[0]["id"], description="Created by a package"))
    for u in rw_users:
        valid_name(u, "user")
        run(["setfacl", "-R", "-m", f"u:{u}:rwX,d:u:{u}:rwX", share["path"]])
    return share["path"]


@router.delete("/{name}")
def delete_share(name: str, delete_data: bool = False, _=Depends(require_admin)):
    shares = load()
    share = next((s for s in shares if s["name"] == name), None)
    if not share:
        raise HTTPException(404, "no such share")
    if delete_data:
        run(["btrfs", "subvolume", "delete", share["path"]])
    write_configs([s for s in shares if s["name"] != name])
    return {"ok": True}
