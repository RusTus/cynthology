"""SAN Manager: iSCSI targets and LUNs with Linux's in-kernel target (LIO), via python3-rtslib-fb.

A LUN is an image file on a storage pool (<volume>/@iSCSI/<name>.img), sparse for "thin" LUNs
(space returned with UNMAP) or fully allocated for "thick" ones; on Btrfs the folder is set to
No-CoW for steady performance. VMware VAAI block primitives are native in ESXi and LIO
implements them: ATS (COMPARE AND WRITE), XCOPY (full copy), WRITE SAME (block zeroing) and
UNMAP (space reclamation) - no plugin is needed on the ESXi hosts.

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
    from .sysinfo import lan_ips
    return {**st, "available": available() is None, "why": available(), "running": DEV or os.path.isdir("/sys/kernel/config/target/iscsi"),
            "portals": [f"{ip}:3260" for ip in (lan_ips() if not DEV else ["192.168.1.20"])], "iqn_prefix": iqn_prefix()}


class TargetBody(BaseModel):
    name: str
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
    tgt = {"name": body.name, "iqn": f"{iqn_prefix()}.{body.name}", "initiators": [i.strip() for i in body.initiators],
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
                run(["cp", "--reflink=auto", "--sparse=always", src["path"], path])
            else:
                with open(path, "wb") as f:
                    f.truncate(size)
                if not lun["thin"]:
                    run(["fallocate", "-l", str(size), path])
            os.chmod(path, 0o600)
            from rtslib_fb import FileIOStorageObject
            so = FileIOStorageObject(body.name, path, size=size, write_back=False)
            _vaai(so)
            if body.target:
                _map(st, lun, body.target)
            _persist()
        except Exception:
            try:
                _so(body.name).delete()
            except Exception:
                pass
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
        if os.path.exists(lun["path"]):
            os.remove(lun["path"])
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
