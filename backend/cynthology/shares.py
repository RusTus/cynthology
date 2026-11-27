"""Shared folders: Btrfs subvolumes exported over SMB (Samba) and optionally NFS.

Cynthology owns /etc/samba/cyn-shares.conf (included from smb.conf) and
/etc/exports.d/cyn.exports, regenerating both from shares.json on every change.
"""
import json
import os
import subprocess

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import current_user, require_admin
from .storage import load_pools
from .util import DEV, STATE_DIR, read_only, run, valid_name

router = APIRouter(prefix="/api/shares", tags=["shares"])
SHARES_FILE = os.path.join(STATE_DIR, "shares.json")
SHARES_META = ".cynthology-shares.json"   # copy on each volume, so importing the pool restores its shares
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
    quota_gb: int = 0                  # 0 = no limit (Btrfs pools)
    encrypted: bool = False            # gocryptfs; needs password at creation
    password: str = ""                 # only used to create an encrypted folder, never stored
    auto_unlock: bool = False          # store the key on the NAS and unlock at boot


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
    snaps = not s.get("usb") and not s.get("encrypted")
    vfs = ["catia", "fruit", streams] + (["btrfs", "shadow_copy2"] if snaps else []) + (["recycle"] if s.get("recycle_bin") else [])
    block = [f"[{s['name']}]", f"   path = {s['path']}", f"   comment = {s.get('description', '')}",
             "   browseable = yes", "   read only = yes",
             f"   valid users = {' '.join(s['read_users'] + s['write_users'])}",
             f"   write list = {' '.join(s['write_users'])}",
             "   vfs objects = " + " ".join(vfs), *FRUIT, "   hide dot files = yes"]
    if snaps:
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
        if s.get("encrypted") and not unlocked(s):
            continue                      # locked: nothing to share until it is unlocked
        if s.get("homes"):
            smb.append(homes_smb_block(s))
            continue
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
    _save_volume_copies(shares)
    if os.path.exists(AVAHI_FILE):   # older versions announced Time Machine themselves; Samba does it now
        os.remove(AVAHI_FILE)
    run(["smbcontrol", "all", "reload-config"], check=False)
    run(["exportfs", "-ra"], check=False)


def homes_smb_block(s: dict) -> str:
    """User homes: //nas/home (and //nas/<username>) is each user's own folder."""
    base = {**s, "name": "home", "path": f"{s['path']}/%U", "read_users": [], "write_users": ["%U"], "description": "Your home folder"}
    home = smb_block(base)
    homes = smb_block({**base, "name": "homes", "path": f"{s['path']}/%S", "write_users": ["%S"]}).replace(
        "   browseable = yes", "   browseable = no")
    return home + "\n\n" + homes


def _save_volume_copies(shares: list[dict]) -> None:
    if DEV:
        return
    for p in load_pools():
        if not os.path.ismount(p["mount"]):
            continue
        mine = [{k: v for k, v in s.items() if k not in ("path", "pool_id")} for s in shares
                if s.get("pool_id") == p["id"] and not s.get("usb")]
        try:
            with open(os.path.join(p["mount"], SHARES_META), "w") as f:
                json.dump(mine, f, indent=1)
        except OSError:
            pass


def adopt_pool_shares(pool: dict) -> list[str]:
    """After importing a pool: bring back its shared folders (saved settings, or every top-level folder)."""
    mount = pool["mount"] if not DEV else os.path.join(STATE_DIR, "root", pool["mount"].lstrip("/"))
    try:
        saved = json.load(open(os.path.join(mount, SHARES_META)))
    except (OSError, ValueError):
        saved = None
    if saved is None:
        saved = []
        for name in sorted(os.listdir(mount)) if os.path.isdir(mount) else []:
            if name.startswith((".", "@", "#")) or name == "lost+found" or not os.path.isdir(os.path.join(mount, name)):
                continue
            try:
                valid_name(name)
            except HTTPException:
                continue
            saved.append(Share(name=name, pool_id=pool["id"], description="Imported").model_dump())
    shares = load()
    have = {s["name"].lower() for s in shares}
    added = []
    for s in saved:
        path = os.path.join(mount, s["name"])
        if s["name"].lower() in have or not os.path.isdir(path):
            continue
        shares.append({**Share(name=s["name"], pool_id=pool["id"]).model_dump(), **s, "pool_id": pool["id"], "path": path})
        added.append(s["name"])
    write_configs(shares)
    return added


@router.get("")
def list_shares(_=Depends(current_user)):
    return [{**s, "locked": bool(s.get("encrypted")) and not unlocked(s)} for s in load()]


@router.post("")
def create_share(body: Share, _=Depends(require_admin)):
    valid_name(body.name, "share name")
    for u in body.read_users + body.write_users:
        valid_name(u.lstrip("@"), "user/group")
    if body.name.lower() in ("home", "homes"):
        raise HTTPException(400, "'home' and 'homes' are used by the user home service - choose another name")
    shares = load()
    if any(s["name"].lower() == body.name.lower() for s in shares):
        raise HTTPException(409, "share already exists")
    pool = next((p for p in load_pools() if p["id"] == body.pool_id), None)
    if not pool:
        raise HTTPException(400, "no such volume")
    if body.quota_gb < 0:
        raise HTTPException(400, "quota must be 0 (no limit) or more")
    if body.encrypted and len(body.password) < 8:
        raise HTTPException(400, "the encryption password needs at least 8 characters")
    path = f"{pool['mount']}/{body.name}"
    if DEV:  # real folder under the dev state dir so File Station can be tried out
        path = os.path.join(STATE_DIR, "root", pool["mount"].lstrip("/"), body.name)
        os.makedirs(path, exist_ok=True)
    rec = {**body.model_dump(exclude={"password"}), "path": path}
    recovery = None
    if body.encrypted:
        rec["cipher"] = f"{pool['mount']}/@enc/{body.name}"
        recovery = encrypt_init(rec, body.password)
    else:
        _make_dir(path)
    if body.quota_gb:
        set_quota(rec["cipher"] if body.encrypted else path, body.quota_gb)
    shares.append(rec)
    write_configs(shares)
    return {**shares[-1], "recovery_key": recovery}


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
    if share.get("encrypted"):
        lock(share)
        if delete_data:
            run(["btrfs", "subvolume", "delete", share["cipher"]], check=False)
            run(["rmdir", share["path"]], check=False)
            _forget_key(share)
    elif delete_data:
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


# ---------- quotas (Btrfs qgroups) ----------
# One qgroup per subvolume. "Simple quotas" (kernel 6.7+) are used when available: they cost
# almost nothing; classic qgroups otherwise.

def _fs_mount(path: str) -> str:
    return read_only(["findmnt", "-n", "-o", "TARGET", "-T", path]).strip()


def _fs_type(path: str) -> str:
    return read_only(["stat", "-f", "-c", "%T", path]).strip()


def set_quota(path: str, gb: int) -> None:
    if DEV:
        return
    if _fs_type(path) != "btrfs":
        raise HTTPException(400, "size limits need the shared folder to be on a Btrfs pool")
    mnt = _fs_mount(path)
    if subprocess.run(["btrfs", "qgroup", "show", mnt], capture_output=True).returncode != 0:
        if subprocess.run(["btrfs", "quota", "enable", "--simple", mnt], capture_output=True).returncode != 0:
            run(["btrfs", "quota", "enable", mnt])
    run(["btrfs", "qgroup", "limit", str(gb * 1000 ** 3) if gb else "none", path])


def quota_usage(path: str) -> dict | None:
    """{"used": bytes, "limit": bytes|None} from the folder's qgroup, or None without quotas."""
    if DEV or not os.path.isdir(path) or _fs_type(path) != "btrfs":
        return None
    rootid = read_only(["btrfs", "inspect-internal", "rootid", path]).strip()
    out = read_only(["btrfs", "qgroup", "show", "--raw", "-re", _fs_mount(path)])
    for line in out.splitlines():
        f = line.split()
        if f and f[0] == f"0/{rootid}" and len(f) >= 4:
            return {"used": int(f[1]) if f[1].isdigit() else 0, "limit": int(f[3]) if f[3].isdigit() else None}
    return None


class Quota(BaseModel):
    quota_gb: int


@router.put("/{name}/quota")
def put_quota(name: str, body: Quota, _=Depends(require_admin)):
    shares = load()
    share = next((s for s in shares if s["name"] == name), None)
    if not share:
        raise HTTPException(404, "no such share")
    if body.quota_gb < 0:
        raise HTTPException(400, "quota must be 0 (no limit) or more")
    set_quota(share.get("cipher") or share["path"], body.quota_gb)
    share["quota_gb"] = body.quota_gb
    write_configs(shares)
    return {"ok": True}


@router.get("/usage/all")
def usage_all(_=Depends(current_user)):
    return {s["name"]: quota_usage(s.get("cipher") or s["path"]) for s in load() if not s.get("usb")}


# ---------- encrypted shared folders (gocryptfs) ----------
# The data lives encrypted in <volume>/@enc/<name> (a Btrfs subvolume: snapshots and replication
# only ever see ciphertext); while unlocked, the readable view is mounted at <volume>/<name>.

KEYDIR = os.path.join(STATE_DIR, "keys") if DEV else "/etc/cynthology/keys"
_dev_unlocked: set[str] = set()


def unlocked(s: dict) -> bool:
    if not s.get("encrypted"):
        return True
    return s["name"] in _dev_unlocked if DEV else os.path.ismount(s["path"])


def _passfile(secret: str) -> str:
    import tempfile
    fd, p = tempfile.mkstemp(prefix="cyn-", dir="/run" if os.path.isdir("/run") and not DEV else None)
    with os.fdopen(fd, "w") as f:
        f.write(secret)
    os.chmod(p, 0o600)
    return p


def _keyfile(s: dict) -> str:
    return os.path.join(KEYDIR, f"{s['name']}.key")


def _forget_key(s: dict) -> None:
    if os.path.exists(_keyfile(s)):
        os.remove(_keyfile(s))


def encrypt_init(rec: dict, password: str) -> str:
    """Create the encrypted store and unlock it; returns the recovery (master) key."""
    if DEV:
        os.makedirs(rec["path"], exist_ok=True)
        _dev_unlocked.add(rec["name"])
        return "a1b2c3d4-e5f60718-293a4b5c-6d7e8f90-a1b2c3d4-e5f60718-293a4b5c-6d7e8f90"
    import shutil
    import re as _re
    if not shutil.which("gocryptfs"):
        raise HTTPException(400, "encryption needs gocryptfs - update Cynthology (Control Panel > Update)")
    cipher = rec["cipher"]
    os.makedirs(os.path.dirname(cipher), mode=0o700, exist_ok=True)
    _make_dir(cipher)
    run(["chmod", "700", cipher])
    pf = _passfile(password)
    try:
        run(["gocryptfs", "-init", "-q", "-passfile", pf, cipher])
    finally:
        os.remove(pf)
    # gocryptfs only shows the master key on a terminal; gocryptfs-xray reads it with the password
    r = subprocess.run(["gocryptfs-xray", "-dumpmasterkey", os.path.join(cipher, "gocryptfs.conf")],
                       input=password + "\n", capture_output=True, text=True)
    hexkey = _re.sub(r"[^0-9a-f]", "", r.stdout.lower())
    m = "-".join(hexkey[i:i + 8] for i in range(0, 64, 8)) if len(hexkey) == 64 else ""
    os.makedirs(rec["path"], exist_ok=True)
    unlock(rec, password, remember=rec.get("auto_unlock", False))
    run(["chgrp", "users", rec["path"]])
    run(["chmod", "2775", rec["path"]])
    return m


def unlock(s: dict, secret: str, remember: bool = False) -> None:
    """Mount the readable view. secret = the password or the recovery key."""
    if DEV:
        _dev_unlocked.add(s["name"])
        return
    if unlocked(s):
        return
    import re as _re
    os.makedirs(s["path"], exist_ok=True)
    if not secret.strip():
        raise HTTPException(400, "enter the password or the recovery key")
    keyhex = _re.sub(r"[\s-]", "", secret.strip().lower())
    is_key = bool(_re.fullmatch(r"[0-9a-f]{64}", keyhex))
    cmd = ["gocryptfs", "-q", "-allow_other"]
    if is_key:
        r = subprocess.run(cmd + ["-masterkey=stdin", s["cipher"], s["path"]], input=keyhex + "\n",
                           capture_output=True, text=True)
    else:
        pf = _passfile(secret)
        try:
            r = subprocess.run(cmd + ["-passfile", pf, s["cipher"], s["path"]], capture_output=True, text=True)
        finally:
            os.remove(pf)
    if r.returncode != 0:
        raise HTTPException(400, "wrong password or recovery key" if r.returncode == 12 else
                            "could not unlock: " + (r.stderr.strip().splitlines() or ["?"])[-1])
    if remember and not is_key:
        os.makedirs(KEYDIR, mode=0o700, exist_ok=True)
        fd = os.open(_keyfile(s), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secret)


def lock(s: dict) -> None:
    if DEV:
        _dev_unlocked.discard(s["name"])
        return
    if os.path.ismount(s["path"]):
        r = subprocess.run(["fusermount3", "-u", s["path"]], capture_output=True, text=True)
        if r.returncode != 0:
            raise HTTPException(409, "the folder is in use (open files or a connected computer) - close them and try again")


class Unlock(BaseModel):
    secret: str
    remember: bool = False


@router.post("/{name}/unlock")
def unlock_api(name: str, body: Unlock, _=Depends(require_admin)):
    shares = load()
    share = next((s for s in shares if s["name"] == name and s.get("encrypted")), None)
    if not share:
        raise HTTPException(404, "no such encrypted shared folder")
    unlock(share, body.secret, body.remember)
    share["auto_unlock"] = body.remember or share.get("auto_unlock", False)
    write_configs(shares)
    return {"ok": True}


@router.post("/{name}/lock")
def lock_api(name: str, forget_key: bool = True, _=Depends(require_admin)):
    shares = load()
    share = next((s for s in shares if s["name"] == name and s.get("encrypted")), None)
    if not share:
        raise HTTPException(404, "no such encrypted shared folder")
    lock(share)
    if forget_key:
        _forget_key(share)
        share["auto_unlock"] = False
    write_configs(shares)
    return {"ok": True}


def unlock_at_boot() -> list[str]:
    """cynthology-unlock.service: unlock encrypted folders whose key is stored on the NAS."""
    done = []
    shares = load()
    for s in shares:
        if s.get("encrypted") and s.get("auto_unlock") and os.path.exists(_keyfile(s)) and not unlocked(s):
            try:
                unlock(s, open(_keyfile(s)).read())
                done.append(s["name"])
            except HTTPException as e:
                print(f"{s['name']}: {e.detail}")
    if shares:
        write_configs(shares)
    return done


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["unlock-at-boot"]:
        print("unlocked:", ", ".join(unlock_at_boot()) or "none")


# ---------- user homes ("homes" shared folder, one private subvolume per user) ----------

def homes_share() -> dict | None:
    return next((s for s in load() if s.get("homes")), None)


def home_path(user: str) -> str | None:
    h = homes_share()
    return os.path.join(h["path"], user) if h else None


def ensure_home(user: str) -> str | None:
    """Create the user's home folder if the home service is on (idempotent)."""
    h = homes_share()
    if not h:
        return None
    valid_name(user, "user")
    path = os.path.join(h["path"], user)
    if not os.path.isdir(path):
        if DEV:
            os.makedirs(path, exist_ok=True)
        else:
            _make_dir(path)           # a subvolume of its own: per-user size limit possible
            run(["chown", f"{user}:users", path])
            run(["chmod", "700", path])
            run(["mkdir", "-p", os.path.join(path, ".snapshots")])   # now, while there's room (quota)
        gb = (h.get("user_quota") or {}).get(user, h.get("default_user_quota_gb", 0))
        if gb:
            set_quota(path, gb)
    return path


class HomesBody(BaseModel):
    enabled: bool
    pool_id: int | None = None
    default_user_quota_gb: int = 0


@router.get("/homes/status")
def homes_status(_=Depends(require_admin)):
    h = homes_share()
    if not h:
        return {"enabled": False}
    users = {}
    for u in sorted(os.listdir(h["path"])) if os.path.isdir(h["path"]) else []:
        if not u.startswith((".", "#", "@")):
            users[u] = {"quota_gb": (h.get("user_quota") or {}).get(u, h.get("default_user_quota_gb", 0)),
                        "usage": quota_usage(os.path.join(h["path"], u))}
    return {"enabled": True, "path": h["path"], "pool_id": h["pool_id"], "default_user_quota_gb": h.get("default_user_quota_gb", 0),
            "users": users}


@router.put("/homes")
def set_homes(body: HomesBody, _=Depends(require_admin)):
    shares = load()
    h = next((s for s in shares if s.get("homes")), None)
    if not body.enabled:
        if h:
            write_configs([s for s in shares if not s.get("homes")])   # folders are kept
        return {"ok": True}
    if h:
        h["default_user_quota_gb"] = max(0, body.default_user_quota_gb)
        write_configs(shares)
    else:
        if any(s["name"].lower() == "homes" for s in shares):
            raise HTTPException(409, "a shared folder called 'homes' already exists")
        pool = next((p for p in load_pools() if p["id"] == (body.pool_id or load_pools()[0]["id"] if load_pools() else None)), None)
        if not pool:
            raise HTTPException(400, "create a storage pool first")
        path = os.path.join(pool["mount"], "homes") if not DEV else os.path.join(STATE_DIR, "root", pool["mount"].lstrip("/"), "homes")
        if not os.path.isdir(path):
            if DEV:
                os.makedirs(path, exist_ok=True)
            else:
                _make_dir(path)
        run(["chown", "root:root", path])
        run(["chmod", "711", path])            # users can reach their own folder but not list the others
        shares.append({**Share(name="homes", pool_id=pool["id"], description="User home folders").model_dump(exclude={"password"}),
                       "path": path, "homes": True, "smb": True, "read_users": [], "write_users": ["@cynthology-admin"],
                       "default_user_quota_gb": max(0, body.default_user_quota_gb), "user_quota": {}})
        write_configs(shares)
    import pwd
    users = [p.pw_name for p in pwd.getpwall() if 1000 <= p.pw_uid < 60000] if not DEV else ["admin", "alice"]
    for u in users:
        ensure_home(u)
    return {"ok": True}


class UserQuota(BaseModel):
    quota_gb: int


@router.put("/homes/quota/{user}")
def set_user_quota(user: str, body: UserQuota, _=Depends(require_admin)):
    valid_name(user, "user")
    shares = load()
    h = next((s for s in shares if s.get("homes")), None)
    if not h:
        raise HTTPException(400, "turn on the user home service first")
    path = ensure_home(user)
    set_quota(path, max(0, body.quota_gb))
    h.setdefault("user_quota", {})[user] = max(0, body.quota_gb)
    write_configs(shares)
    return {"ok": True}
