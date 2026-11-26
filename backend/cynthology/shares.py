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
AVAHI_FILE = os.path.join(STATE_DIR, "avahi-timemachine.service") if DEV else "/etc/avahi/services/cynthology-timemachine.service"
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


NO_XATTR_FS = ("exfat", "vfat")       # USB filesystems without extended attributes
FRUIT = ["   fruit:metadata = stream", "   fruit:model = MacSamba", "   fruit:posix_rename = yes",
         "   fruit:veto_appledouble = no", "   fruit:nfs_aces = no", "   fruit:wipe_intentionally_left_blank_rfork = yes",
         "   fruit:delete_empty_adfiles = yes"]


def smb_block(s: dict) -> str:
    """One share's smb.conf section (pure; unit tested)."""
    # Apple's SMB extensions (fruit) must be loaded on EVERY share once any share uses them (Time Machine).
    streams = "streams_depot" if s.get("fstype") in NO_XATTR_FS else "streams_xattr"
    vfs = ["catia", "fruit", streams] + ([] if s.get("usb") else ["btrfs", "shadow_copy2"]) + (["recycle"] if s.get("recycle_bin") else [])
    block = [f"[{s['name']}]", f"   path = {s['path']}", f"   comment = {s.get('description', '')}",
             "   browseable = yes", "   read only = yes",
             f"   valid users = {' '.join(s['read_users'] + s['write_users'])}",
             f"   write list = {' '.join(s['write_users'])}",
             "   vfs objects = " + " ".join(vfs), *FRUIT, "   hide dot files = yes"]
    if not s.get("usb"):
        # snapshots (see snapshots.py) appear as Windows "Previous Versions"
        block += ["   shadow:snapdir = .snapshots", "   shadow:sort = desc", "   shadow:format = @GMT-%Y.%m.%d-%H.%M.%S"]
    if s.get("recycle_bin"):
        block += ["   recycle:repository = #recycle", "   recycle:keeptree = yes", "   recycle:versions = yes",
                  "   recycle:touch = yes", "   recycle:exclude_dir = .snapshots"]
    if s.get("time_machine"):
        block += ["   fruit:time machine = yes", "   durable handles = yes", "   kernel oplocks = no",
                  "   kernel share modes = no", "   posix locking = no"]
        if int(s.get("tm_max_gb") or 0) > 0:
            block.append(f"   fruit:time machine max size = {int(s['tm_max_gb'])}G")
    return "\n".join(block)


def write_configs(shares: list[dict]) -> None:
    smb, nfs = [], []
    for s in shares:
        if s["smb"]:
            smb.append(smb_block(s))
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
    if os.path.exists(AVAHI_FILE):   # older versions announced Time Machine themselves; Samba does it now
        os.remove(AVAHI_FILE)
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


def ensure_share(name: str, rw_users: list[str], description: str = "Created by a package") -> str:
    """Used by packages (conf/resource data-share): create the share if missing, grant rw via ACL."""
    valid_name(name, "share name")
    shares = load()
    share = next((s for s in shares if s["name"].lower() == name.lower()), None)
    if not share:
        pools = load_pools()
        if not pools:
            raise HTTPException(400, "no volume to create shared folder on")
        share = create_share(Share(name=name, pool_id=pools[0]["id"], description=description))
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


# ---------- Time Machine ----------

class TimeMachine(BaseModel):
    enabled: bool
    share: str = "TimeMachine"
    max_gb: int = 0           # 0 = no limit
    pool_id: int | None = None


@router.get("/timemachine/status")
def tm_status(_=Depends(current_user)):
    s = next((x for x in load() if x.get("time_machine")), None)
    return {"enabled": bool(s), "share": s["name"] if s else None, "max_gb": int(s.get("tm_max_gb") or 0) if s else 0}


@router.put("/timemachine/config")
def tm_config(body: TimeMachine, _=Depends(require_admin)):
    import uuid
    items = load()
    for x in items:                 # one Time Machine share at a time
        x.pop("time_machine", None)
    if body.enabled:
        valid_name(body.share, "share name")
        if not 0 <= body.max_gb <= 1_000_000:
            raise HTTPException(400, "size limit out of range")
        target = next((x for x in items if x["name"].lower() == body.share.lower()), None)
        if target and target.get("usb"):
            raise HTTPException(400, "pick a shared folder on a storage pool, not a USB drive")
        if not target:
            write_configs(items)
            pools = load_pools()
            if not pools:
                raise HTTPException(400, "create a storage pool first")
            create_share(Share(name=body.share, pool_id=body.pool_id or pools[0]["id"], description="Time Machine backups for Macs",
                               recycle_bin=False, write_users=["@users"]))
            items = load()
            target = next(x for x in items if x["name"] == body.share)
        target["time_machine"] = True
        target["tm_max_gb"] = body.max_gb
        target.setdefault("tm_uuid", str(uuid.uuid4()).upper())
        target["smb"] = True
    write_configs(items)
    # Samba announces Time Machine shares on Bonjour (_adisk._tcp) itself, but only when it starts
    run(["systemctl", "try-restart", "smbd"], check=False)
    return tm_status(None)
