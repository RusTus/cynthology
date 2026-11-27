"""SAN Manager: iSCSI targets and LUNs with Linux's in-kernel target (LIO), via python3-rtslib-fb.

A LUN is an image file on a storage pool (<volume>/@iSCSI/<name>.img), sparse for "thin" LUNs
(space returned with UNMAP) or fully allocated for "thick" ones; on Btrfs the folder is set to
No-CoW for steady performance. VMware VAAI block primitives are native in ESXi and LIO
implements them: ATS (COMPARE AND WRITE), XCOPY (full copy), WRITE SAME (block zeroing) and
UNMAP (space reclamation) - no plugin is needed on the ESXi hosts.

LUNs are exposed through a loop device (/dev/loop<N>, fixed number per LUN, direct I/O) so they
can be expanded while hosts stay connected: LIO reads a block device's size live, but fixes the
size of a plain file at creation. (LUNs made by v0.20/0.21 use the file directly; the first
expand converts them, which needs the hosts disconnected once.)

Changes are applied to the running target incrementally (connected hosts stay connected) and
saved to /etc/rtslib-fb-target/saveconfig.json, which rtslib-fb-targetctl.service restores at
boot once the storage pools are mounted (see systemd drop-in).
"""
import json
import os
import re
import secrets
import socket
import subprocess
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import current_user, require_admin
from .storage import load_pools
from .util import DEV, STATE_DIR, read_only, run

router = APIRouter(prefix="/api/san", tags=["san"])
STATE = os.path.join(STATE_DIR, "san.json")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
IQN_RE = re.compile(r"^(iqn\.\d{4}-\d{2}\.[a-z0-9.-]+(:[^\s]+)?|eui\.[0-9a-fA-F]{16}|naa\.[0-9a-fA-F]{16,32})$")
LUN_DIR = "@iSCSI"


# ---------- state ----------

def load() -> dict:
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {"targets": [], "luns": []}


def save(st: dict) -> None:
    with open(STATE + ".new", "w") as f:
        json.dump(st, f, indent=1)
    os.chmod(STATE + ".new", 0o600)        # CHAP secrets
    os.replace(STATE + ".new", STATE)


def iqn_prefix() -> str:
    host = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().lower()) or "nas"
    return f"iqn.2000-01.org.cynthology:{host}"


def available() -> str | None:
    """None when iSCSI can be used, else why not."""
    if DEV:
        return None
    try:
        import rtslib_fb  # noqa: F401
    except ImportError:
        return "the iSCSI target software isn't installed yet - update Cynthology (Control Panel > Update)"
    if not os.path.isdir("/sys/kernel/config") or subprocess.run(["modprobe", "iscsi_target_mod"], capture_output=True).returncode:
        return "this kernel has no iSCSI target support"
    return None


def _need() -> None:
    why = available()
    if why:
        raise HTTPException(400, why)


# ---------- LIO (rtslib) ----------

def _fabric():
    from rtslib_fb import FabricModule
    return FabricModule("iscsi")


def _tpg(iqn: str, create: bool = False):
    from rtslib_fb import TPG, NetworkPortal, Target
    t = Target(_fabric(), iqn, "any" if create else "lookup")
    tpg = TPG(t, 1, "create" if create else "lookup")
    if create:
        NetworkPortal(tpg, "0.0.0.0", 3260)
        tpg.enable = True
    return tpg


def _so(name: str):
    from rtslib_fb import FileIOStorageObject
    return FileIOStorageObject(name)


def _apply_access(tpg, tgt: dict, luns: list[dict]) -> None:
    """Who may connect: everyone (dynamic ACLs) or listed initiators; optional CHAP."""
    from rtslib_fb import MappedLUN, NodeACL
    chap = tgt.get("chap") or {}
    allow_all = not tgt.get("initiators")
    tpg.set_attribute("authentication", "1" if chap.get("user") else "0")
    tpg.set_attribute("generate_node_acls", "1" if allow_all else "0")
    tpg.set_attribute("demo_mode_write_protect", "0")
    # (LIO forces cache_dynamic_acls on while generate_node_acls is on; stale cached ACLs are
    # cleared by rebuilding the TPG whenever access rules change - see _rebuild_tpg)
    if not allow_all:
        tpg.set_attribute("cache_dynamic_acls", "0")
    if allow_all:   # CHAP for dynamically created ACLs is set on the TPG
        tpg.chap_userid, tpg.chap_password = chap.get("user", ""), chap.get("password", "")
    wanted = set(tgt.get("initiators") or [])
    for acl in list(tpg.node_acls):
        if acl.node_wwn not in wanted:
            acl.delete()
    have = {a.node_wwn: a for a in tpg.node_acls}
    for wwn in wanted:
        acl = have.get(wwn) or NodeACL(tpg, wwn)
        acl.chap_userid, acl.chap_password = chap.get("user", ""), chap.get("password", "")
        mapped = {m.mapped_lun for m in acl.mapped_luns}
        for lun in tpg.luns:
            if lun.lun not in mapped:
                MappedLUN(acl, lun.lun, lun)


def _rebuild_tpg(tgt: dict) -> None:
    """Recreate a target's TPG with the same LUN numbers, then apply its access rules. Needed when
    access changes: LIO keeps cached ACLs of hosts that connected while access was open, which
    would otherwise still let them in. Connected hosts reconnect (if they are still allowed)."""
    from rtslib_fb import LUN
    try:
        old = _tpg(tgt["iqn"])
        luns = sorted(((l.lun, l.storage_object.name) for l in old.luns), key=lambda x: x[0])
        old.delete()
    except Exception:   # TPG missing (e.g. an earlier rebuild was interrupted): use Cynthology's records
        luns = list(enumerate(l["name"] for l in load()["luns"] if l.get("target") == tgt["name"]))
    tpg = _tpg(tgt["iqn"], create=True)
    for idx, name in luns:
        LUN(tpg, lun=idx, storage_object=_so(name))
    _apply_access(tpg, tgt, [])


def _persist() -> None:
    from rtslib_fb import RTSRoot
    RTSRoot().save_to_file()


def lun_path(pool: dict, name: str) -> str:
    return os.path.join(pool["mount"], LUN_DIR, f"{name}.img")


LOOP_BASE = 100


def _loop_add(n: int) -> None:
    import fcntl
    subprocess.run(["modprobe", "loop"], capture_output=True)
    fd = os.open("/dev/loop-control", os.O_RDWR)
    try:
        fcntl.ioctl(fd, 0x4C80, n)          # LOOP_CTL_ADD (EEXIST when it's already there)
    except OSError:
        pass
    finally:
        os.close(fd)
    run(["udevadm", "settle"], check=False)


def attach_loop(lun: dict, st: dict) -> str:
    """Make sure the LUN's image is attached to its loop device; returns the device path."""
    if lun.get("loop") is None:
        used = {l.get("loop") for l in st["luns"]}
        lun["loop"] = next(n for n in range(LOOP_BASE, LOOP_BASE + 4000) if n not in used)
    dev = f"/dev/loop{lun['loop']}"
    if DEV:
        return dev
    _loop_add(lun["loop"])
    back = read_only(["losetup", "-n", "-O", "BACK-FILE", dev]).strip()
    if back != lun["path"]:
        if back:
            raise HTTPException(500, f"{dev} is in use by {back}")
        run(["losetup", "--direct-io=on", dev, lun["path"]])
    return dev


def detach_loop(lun: dict) -> None:
    if lun.get("loop") is not None and not DEV:
        run(["losetup", "-d", f"/dev/loop{lun['loop']}"], check=False)


def _vaai(so) -> None:
    """Advertise the block features VMware VAAI (and Windows ODX/TRIM) use."""
    for attr, val in (("emulate_tpu", 1), ("emulate_tpws", 1), ("emulate_caw", 1), ("emulate_3pc", 1)):
        try:
            so.set_attribute(attr, str(val))
        except Exception:
            pass


# ---------- API ----------

def _sessions(iqn: str) -> list[str]:
    base = f"/sys/kernel/config/target/iscsi/{iqn}/tpgt_1"
    out = set()
    try:
        out |= {s for s in open(f"{base}/dynamic_sessions").read().split("\x00") if s.strip()}
    except OSError:
        pass
    acls = f"{base}/acls"
    for wwn in os.listdir(acls) if os.path.isdir(acls) else []:
        try:
            if "No active" not in open(f"{acls}/{wwn}/info").read():
                out.add(wwn)
        except OSError:
            pass
    return sorted(s.strip() for s in out)


def _used(path: str) -> int:
    try:
        return os.stat(path).st_blocks * 512
    except OSError:
        return 0


@router.get("")
def overview(_=Depends(current_user)):
    st = load()
    pools = {p["id"]: p for p in load_pools()}
    for t in st["targets"]:
        t["sessions"] = [] if DEV else _sessions(t["iqn"])
        t["chap"] = {"user": (t.get("chap") or {}).get("user", "")} if t.get("chap") else None   # never send secrets back
    for lun in st["luns"]:
        p = pools.get(lun["pool_id"])
        lun["pool"] = p["label"] if p else "?"
        lun["used"] = lun["size"] // 3 if DEV else _used(lun["path"])
        lun["snapshots"] = len(list_lun_snaps(lun)) if p and snaps_supported(lun) else 0
    from .sysinfo import lan_ips
    return {**st, "available": available() is None, "why": available(), "running": DEV or os.path.isdir("/sys/kernel/config/target/iscsi"),
            "portals": [f"{ip}:3260" for ip in (lan_ips() if not DEV else ["192.168.1.20"])], "iqn_prefix": iqn_prefix()}


class TargetBody(BaseModel):
    name: str
    iqn: str = ""                       # custom target IQN (new targets only); default <prefix>.<name>
    initiators: list[str] = []          # empty = any initiator may connect
    chap_user: str = ""
    chap_password: str = ""             # 12-16 characters (iSCSI CHAP rule); empty keeps the old one when editing


def _check_target(body: TargetBody) -> None:
    if not NAME_RE.match(body.name):
        raise HTTPException(400, "target name: lowercase letters, digits and '-' (max 31)")
    for i in body.initiators:
        if not IQN_RE.match(i.strip()):
            raise HTTPException(400, f"{i} is not an iSCSI initiator name (e.g. iqn.1998-01.com.vmware:esx01-1a2b3c4d)")
    if body.chap_user and not re.match(r"^[A-Za-z0-9._@:-]{1,64}$", body.chap_user):
        raise HTTPException(400, "CHAP user: letters, digits and . _ @ : -")
    if body.chap_password and not 12 <= len(body.chap_password) <= 16:
        raise HTTPException(400, "CHAP secret must be 12-16 characters (iSCSI rule)")


@router.post("/targets")
def create_target(body: TargetBody, _=Depends(require_admin)):
    _need()
    _check_target(body)
    st = load()
    if any(t["name"] == body.name for t in st["targets"]):
        raise HTTPException(409, "a target with that name exists")
    if body.chap_user and not body.chap_password:
        raise HTTPException(400, "enter a CHAP secret")
    iqn = body.iqn.strip() or f"{iqn_prefix()}.{body.name}"
    if not re.match(r"^iqn\.\d{4}-\d{2}\.[a-z0-9.-]+(:[a-z0-9._:-]+)?$", iqn) or len(iqn) > 223:
        raise HTTPException(400, "target IQN must look like iqn.2000-01.org.example:storage.name (lowercase, max 223 characters)")
    if any(t["iqn"] == iqn for t in st["targets"]):
        raise HTTPException(409, "another target already uses that IQN")
    tgt = {"name": body.name, "iqn": iqn, "initiators": [i.strip() for i in body.initiators],
           "chap": {"user": body.chap_user, "password": body.chap_password} if body.chap_user else None}
    if not DEV:
        tpg = _tpg(tgt["iqn"], create=True)
        _apply_access(tpg, tgt, [])
        _persist()
    st["targets"].append(tgt)
    save(st)
    return {"ok": True, "iqn": tgt["iqn"]}


@router.put("/targets/{name}")
def edit_target(name: str, body: TargetBody, _=Depends(require_admin)):
    _need()
    st = load()
    tgt = next((t for t in st["targets"] if t["name"] == name), None)
    if not tgt:
        raise HTTPException(404, "no such target")
    body.name = name
    _check_target(body)
    old = tgt.get("chap") or {}
    if body.chap_user:
        pw = body.chap_password or old.get("password", "")
        if not pw:
            raise HTTPException(400, "enter a CHAP secret")
        tgt["chap"] = {"user": body.chap_user, "password": pw}
    else:
        tgt["chap"] = None
    tgt["initiators"] = [i.strip() for i in body.initiators]
    if not DEV:
        _rebuild_tpg(tgt)
        _persist()
    save(st)
    return {"ok": True}


@router.delete("/targets/{name}")
def delete_target(name: str, _=Depends(require_admin)):
    _need()
    st = load()
    tgt = next((t for t in st["targets"] if t["name"] == name), None)
    if not tgt:
        raise HTTPException(404, "no such target")
    if any(l.get("target") == name for l in st["luns"]):
        raise HTTPException(400, "remove or move this target's LUNs first")
    if not DEV:
        from rtslib_fb import Target
        try:
            Target(_fabric(), tgt["iqn"], "lookup").delete()
        except Exception:
            pass
        _persist()
    st["targets"] = [t for t in st["targets"] if t["name"] != name]
    save(st)
    return {"ok": True}


class LunBody(BaseModel):
    name: str
    pool_id: int
    size_gb: int
    thin: bool = True
    target: str = ""                    # map to this target ("" = not mapped yet)
    clone_of: str = ""                  # copy an existing LUN instantly (Btrfs reflink)


def _map(st: dict, lun: dict, target: str) -> None:
    """Export a LUN through a target (or stop exporting it when target is empty)."""
    from rtslib_fb import LUN
    so = _so(lun["name"])
    for l in list(so.attached_luns):     # unmap from wherever it is now
        l.delete()
    if target:
        tgt = next(t for t in st["targets"] if t["name"] == target)
        tpg = _tpg(tgt["iqn"])
        LUN(tpg, storage_object=so)
        _apply_access(tpg, tgt, [])


@router.post("/luns")
def create_lun(body: LunBody, _=Depends(require_admin)):
    _need()
    if not NAME_RE.match(body.name):
        raise HTTPException(400, "LUN name: lowercase letters, digits and '-' (max 31)")
    st = load()
    if any(l["name"] == body.name for l in st["luns"]):
        raise HTTPException(409, "a LUN with that name exists")
    if body.target and not any(t["name"] == body.target for t in st["targets"]):
        raise HTTPException(400, "no such target")
    pool = next((p for p in load_pools() if p["id"] == body.pool_id), None)
    if not pool:
        raise HTTPException(400, "no such storage pool")
    src = next((l for l in st["luns"] if l["name"] == body.clone_of), None) if body.clone_of else None
    if body.clone_of and not src:
        raise HTTPException(404, "the LUN to clone doesn't exist")
    size = src["size"] if src else body.size_gb * 1000 ** 3     # decimal GB, as the UI shows sizes
    if not src and not 1 <= body.size_gb <= 1024 * 1024:
        raise HTTPException(400, "size: 1 GB to 1 PB")
    path = lun_path(pool, body.name)
    lun = {"name": body.name, "pool_id": pool["id"], "path": path, "size": size, "thin": src["thin"] if src else body.thin,
           "target": body.target, "created": int(time.time())}
    if not DEV:
        st_fs = os.statvfs(pool["mount"])
        if not lun["thin"] and size > st_fs.f_bavail * st_fs.f_frsize:
            raise HTTPException(400, "not enough free space on the pool for a thick LUN")
        d = os.path.dirname(path)
        if not os.path.isdir(d):
            os.makedirs(d, mode=0o700)
            if pool.get("fs", "btrfs") == "btrfs":
                run(["chattr", "+C", d], check=False)     # No-CoW: steady performance for VM disks
        if os.path.exists(path):
            raise HTTPException(409, f"{path} already exists")
        try:
            if src:
                # instant copy on Btrfs; a plain copy elsewhere (for consistency, clone while the LUN isn't in use)
                run(["cp", "--reflink=auto", src["path"], path])
            else:
                with open(path, "wb") as f:
                    f.truncate(size)
                if not lun["thin"]:
                    run(["fallocate", "-l", str(size), path])
            os.chmod(path, 0o600)
            from rtslib_fb import FileIOStorageObject
            so = FileIOStorageObject(body.name, attach_loop(lun, st), write_back=False)
            _vaai(so)
            if body.target:
                _map(st, lun, body.target)
            _persist()
        except Exception:
            try:
                _so(body.name).delete()
            except Exception:
                pass
            detach_loop(lun)
            if os.path.exists(path):
                os.remove(path)
            raise
    st["luns"].append(lun)
    save(st)
    return {"ok": True}


class MapBody(BaseModel):
    target: str


@router.post("/luns/{name}/map")
def map_lun(name: str, body: MapBody, _=Depends(require_admin)):
    _need()
    st = load()
    lun = next((l for l in st["luns"] if l["name"] == name), None)
    if not lun:
        raise HTTPException(404, "no such LUN")
    if body.target and not any(t["name"] == body.target for t in st["targets"]):
        raise HTTPException(400, "no such target")
    if not DEV:
        _map(st, lun, body.target)
        _persist()
    lun["target"] = body.target
    save(st)
    return {"ok": True}


@router.delete("/luns/{name}")
def delete_lun(name: str, _=Depends(require_admin)):
    _need()
    st = load()
    lun = next((l for l in st["luns"] if l["name"] == name), None)
    if not lun:
        raise HTTPException(404, "no such LUN")
    if not DEV:
        tgt = next((t for t in st["targets"] if t["name"] == lun.get("target")), None)
        if tgt and _sessions(tgt["iqn"]):
            raise HTTPException(409, f"hosts are connected to target {tgt['name']} - unmount the datastore and disconnect them first")
        try:
            so = _so(name)
            for l in list(so.attached_luns):
                l.delete()
            so.delete()
        except Exception:
            pass
        _persist()
        detach_loop(lun)
        if os.path.exists(lun["path"]):
            os.remove(lun["path"])
        for n in list_lun_snaps(lun):
            run(["btrfs", "subvolume", "delete", os.path.join(snap_dir(lun), n)], check=False)
        if os.path.isdir(snap_dir(lun)):
            os.rmdir(snap_dir(lun))
    st["luns"] = [l for l in st["luns"] if l["name"] != name]
    save(st)
    return {"ok": True}


@router.get("/vaai/{name}")
def vaai_status(name: str, _=Depends(require_admin)):
    """What the LUN advertises (as ESXi will see it)."""
    if DEV:
        return {"ats": True, "xcopy": True, "write_same": True, "unmap": True}
    so = _so(name)
    a = lambda k: so.get_attribute(k) == "1"
    return {"ats": a("emulate_caw"), "xcopy": a("emulate_3pc"), "write_same": a("emulate_tpws"), "unmap": a("emulate_tpu")}


# ---------- LUN snapshots ----------
# Each snapshot is a read-only Btrfs subvolume <pool>/@iSCSI/.snapshots/<lun>/@GMT-... holding a
# reflink copy of the LUN image: instant, shares unchanged blocks with the live LUN, and point in
# time (the clone locks the file, so concurrent writes land either before or after it). Being
# subvolumes, they can be replicated with btrfs send like shared-folder snapshots.

SNAP_FMT = "@GMT-%Y.%m.%d-%H.%M.%S"
SNAP_RE = re.compile(r"^@GMT-\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}$")
INTERVAL_H = {"hourly": 1, "daily": 24, "weekly": 24 * 7}
_dev_snaps: dict[str, list[str]] = {}


def _lun(st: dict, name: str) -> dict:
    lun = next((l for l in st["luns"] if l["name"] == name), None)
    if not lun:
        raise HTTPException(404, "no such LUN")
    return lun


def _pool_of(lun: dict) -> dict:
    pool = next((p for p in load_pools() if p["id"] == lun["pool_id"]), None)
    if not pool:
        raise HTTPException(400, "the LUN's storage pool is missing")
    return pool


def snap_dir(lun: dict) -> str:
    return os.path.join(os.path.dirname(lun["path"]), ".snapshots", lun["name"])


def snaps_supported(lun: dict) -> bool:
    return DEV or _pool_of(lun).get("fs", "btrfs") == "btrfs"


def list_lun_snaps(lun: dict) -> list[str]:
    if DEV:
        return sorted(_dev_snaps.get(lun["name"], []))
    d = snap_dir(lun)
    return sorted(n for n in os.listdir(d) if SNAP_RE.match(n)) if os.path.isdir(d) else []


def take_lun_snapshot(lun: dict) -> str:
    import datetime
    name = datetime.datetime.now(datetime.timezone.utc).strftime(SNAP_FMT)
    if DEV:
        _dev_snaps.setdefault(lun["name"], []).append(name)
        return name
    if not snaps_supported(lun):
        raise HTTPException(400, "LUN snapshots need a Btrfs storage pool")
    d = snap_dir(lun)
    os.makedirs(d, mode=0o700, exist_ok=True)
    target = os.path.join(d, name)
    if os.path.exists(target):
        raise HTTPException(409, "a snapshot was taken this second - try again")
    run(["btrfs", "subvolume", "create", target])
    try:
        run(["chattr", "+C", target], check=False)      # same No-CoW flag as the LUN, or the reflink is refused
        run(["cp", "--reflink=always", lun["path"], os.path.join(target, "lun.img")])
        run(["btrfs", "property", "set", "-ts", target, "ro", "true"])
    except HTTPException:
        run(["btrfs", "subvolume", "delete", target], check=False)
        raise
    return name


def delete_lun_snapshot(lun: dict, name: str) -> None:
    if not SNAP_RE.match(name):
        raise HTTPException(400, "bad snapshot name")
    if DEV:
        _dev_snaps[lun["name"]] = [n for n in _dev_snaps.get(lun["name"], []) if n != name]
        return
    target = os.path.join(snap_dir(lun), name)
    if not os.path.isdir(target):
        raise HTTPException(404, "no such snapshot")
    run(["btrfs", "subvolume", "delete", target])


def prune_lun(lun: dict) -> int:
    keep = int((lun.get("snap") or {}).get("keep") or 0)
    snaps = list_lun_snaps(lun)
    gone = snaps[:-keep] if keep > 0 else []
    for n in gone:
        delete_lun_snapshot(lun, n)
    return len(gone)


def snap_due() -> list[str]:
    """Scheduled LUN snapshots (called by the hourly snapshot timer)."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    done = []
    for lun in load()["luns"]:
        hours = INTERVAL_H.get((lun.get("snap") or {}).get("schedule") or "off")
        if not hours or not snaps_supported(lun) or not os.path.exists(lun["path"]):
            continue
        snaps = list_lun_snaps(lun)
        last = datetime.datetime.strptime(snaps[-1], SNAP_FMT).replace(tzinfo=datetime.timezone.utc) if snaps else None
        if not last or now - last >= datetime.timedelta(hours=hours, minutes=-5):
            take_lun_snapshot(lun)
            done.append(f"LUN {lun['name']}")
        prune_lun(lun)
    return done


def _snap_info(lun: dict, name: str) -> dict:
    import datetime
    t = datetime.datetime.strptime(name, SNAP_FMT).replace(tzinfo=datetime.timezone.utc)
    return {"name": name, "time": t.isoformat()}


@router.get("/luns/{name}/snapshots")
def get_lun_snaps(name: str, _=Depends(current_user)):
    lun = _lun(load(), name)
    return {"supported": snaps_supported(lun), "policy": lun.get("snap") or {"schedule": "off", "keep": 24},
            "snapshots": [_snap_info(lun, n) for n in reversed(list_lun_snaps(lun))]}


@router.post("/luns/{name}/snapshots")
def lun_snapshot_now(name: str, _=Depends(require_admin)):
    lun = _lun(load(), name)
    return {"ok": True, "name": take_lun_snapshot(lun)}


class SnapPolicy(BaseModel):
    schedule: str = "off"       # off | hourly | daily | weekly
    keep: int = 24


@router.put("/luns/{name}/snapshots/policy")
def set_lun_policy(name: str, body: SnapPolicy, _=Depends(require_admin)):
    if body.schedule not in ("off", *INTERVAL_H) or not 0 <= body.keep <= 1024:
        raise HTTPException(400, "bad schedule or count")
    st = load()
    lun = _lun(st, name)
    if body.schedule != "off" and not snaps_supported(lun):
        raise HTTPException(400, "LUN snapshots need a Btrfs storage pool")
    lun["snap"] = body.model_dump()
    save(st)
    return {"ok": True}


@router.delete("/luns/{name}/snapshots/{snap}")
def delete_lun_snap(name: str, snap: str, _=Depends(require_admin)):
    delete_lun_snapshot(_lun(load(), name), snap)
    return {"ok": True}


@router.post("/luns/{name}/snapshots/{snap}/revert")
def revert_lun(name: str, snap: str, _=Depends(require_admin)):
    """Put the whole LUN back to the snapshot. Hosts must be disconnected (their caches would be stale)."""
    st = load()
    lun = _lun(st, name)
    if not SNAP_RE.match(snap) or snap not in list_lun_snaps(lun):
        raise HTTPException(404, "no such snapshot")
    if DEV:
        return {"ok": True, "safety": None}
    tgt = next((t for t in st["targets"] if t["name"] == lun.get("target")), None)
    if tgt and _sessions(tgt["iqn"]):
        raise HTTPException(409, f"hosts are connected to target {tgt['name']} - unmount the datastore and disconnect "
                                 "them first, or use “Clone as new LUN” to get at the data while it stays in use")
    safety = take_lun_snapshot(lun)      # so the revert can be undone
    # cloning onto the open image swaps its contents in place; the target keeps using the same file
    run(["cp", "--reflink=always", os.path.join(snap_dir(lun), snap, "lun.img"), lun["path"]])
    return {"ok": True, "safety": safety}


class SnapClone(BaseModel):
    new_name: str


@router.post("/luns/{name}/snapshots/{snap}/clone")
def clone_lun_snapshot(name: str, snap: str, body: SnapClone, _=Depends(require_admin)):
    """New, writable LUN from a snapshot (instant); safe while the original is in use."""
    st = load()
    lun = _lun(st, name)
    if not SNAP_RE.match(snap) or snap not in list_lun_snaps(lun):
        raise HTTPException(404, "no such snapshot")
    return _new_lun_from_image(st, body.new_name, _pool_of(lun) if not DEV else {"id": lun["pool_id"], "mount": "/volume1"},
                               os.path.join(snap_dir(lun), snap, "lun.img"), lun["size"], lun["thin"])


def _new_lun_from_image(st: dict, new_name: str, pool: dict, image: str, size: int, thin: bool) -> dict:
    if not NAME_RE.match(new_name):
        raise HTTPException(400, "LUN name: lowercase letters, digits and '-' (max 31)")
    if any(l["name"] == new_name for l in st["luns"]):
        raise HTTPException(409, "a LUN with that name exists")
    path = lun_path(pool, new_name)
    lun = {"name": new_name, "pool_id": pool["id"], "path": path, "size": size, "thin": thin, "target": "",
           "created": int(time.time())}
    if not DEV:
        d = os.path.dirname(path)
        if not os.path.isdir(d):
            os.makedirs(d, mode=0o700)
            if pool.get("fs", "btrfs") == "btrfs":
                run(["chattr", "+C", d], check=False)
        run(["cp", "--reflink=auto", image, path])
        os.chmod(path, 0o600)
        from rtslib_fb import FileIOStorageObject
        so = FileIOStorageObject(new_name, attach_loop(lun, st), write_back=False)
        _vaai(so)
        _persist()
    st["luns"].append(lun)
    save(st)
    return {"ok": True, "name": new_name}


class FromReplica(BaseModel):
    source: str
    lun: str                    # replicated LUN name (stored as "lun.<name>" on this NAS)
    snapshot: str
    new_name: str


@router.post("/luns/from-replica")
def lun_from_replica(body: FromReplica, _=Depends(require_admin)):
    """Failover: a writable LUN from a replica received from another NAS."""
    _need()
    from . import repl
    try:
        d = repl._rdir(body.source, f"lun.{body.lun}")
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    image = os.path.join(d, body.snapshot, "lun.img")
    if not SNAP_RE.match(body.snapshot) or (not DEV and not os.path.isfile(image)):
        raise HTTPException(404, "no such replica")
    pool = load_pools()[0]           # replicas are received on the first pool
    size = os.path.getsize(image) if not DEV else 10 ** 10
    return _new_lun_from_image(load(), body.new_name, pool, image, size, True)


# ---------- expand ----------

class Expand(BaseModel):
    size_gb: int


@router.post("/luns/{name}/expand")
def expand_lun(name: str, body: Expand, _=Depends(require_admin)):
    """Grow a LUN. Loop-backed LUNs grow while in use; older file-backed ones are converted first
    (same serial/WWN, so hosts recognise the disk), which needs the hosts disconnected."""
    _need()
    st = load()
    lun = _lun(st, name)
    new = body.size_gb * 1000 ** 3
    if new <= lun["size"]:
        raise HTTPException(400, f"the new size must be larger than {lun['size'] // 1000 ** 3} GB (LUNs can't shrink)")
    if new > 1000 ** 5:
        raise HTTPException(400, "at most 1 PB")
    online = lun.get("loop") is not None
    if DEV:
        lun["size"] = new
        save(st)
        return {"ok": True, "online": online}
    pool = _pool_of(lun)
    if not lun["thin"]:
        f = os.statvfs(pool["mount"])
        if new - lun["size"] > f.f_bavail * f.f_frsize:
            raise HTTPException(400, "not enough free space on the pool to grow this thick LUN")
    tgt = next((t for t in st["targets"] if t["name"] == lun.get("target")), None)
    if not online and tgt and _sessions(tgt["iqn"]):
        raise HTTPException(409, "this LUN was made by an older version and has to be converted once to grow: "
                                 "unmount the datastore and disconnect the hosts, then expand. Later expansions work while in use.")
    # grow the image
    if lun["thin"]:
        with open(lun["path"], "r+b") as f:
            f.truncate(new)
    else:
        run(["fallocate", "-l", str(new), lun["path"]])
    if online:
        run(["losetup", "-c", f"/dev/loop{lun['loop']}"])     # the loop device (and so the LUN) picks up the new size
    else:
        from rtslib_fb import LUN, FileIOStorageObject
        old = _so(name)
        wwn = old.wwn
        places = [(l.parent_tpg, l.lun) for l in old.attached_luns]
        for l in list(old.attached_luns):
            l.delete()
        old.delete()
        so = FileIOStorageObject(name, attach_loop(lun, st), wwn=wwn, write_back=False)
        _vaai(so)
        for tpg, idx in places:
            LUN(tpg, lun=idx, storage_object=so)
            if tgt:
                _apply_access(tpg, tgt, [])
        _persist()
    lun["size"] = new
    save(st)
    return {"ok": True, "online": online}
