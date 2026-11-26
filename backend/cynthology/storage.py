"""Storage manager: disks, SMART health, RAID pools (mdadm) with Btrfs volumes on top.

Every pool uses the same stack: GPT partitions -> mdadm array(s) -> LVM -> Btrfs (or ext4),
mounted at /volumeN. SHR uses several md arrays (one per size tier) - see raidplan.py. Shared folders are Btrfs subvolumes, which gives cheap snapshots.
"""
import json
import os
import subprocess
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import current_user, require_admin
from . import raidplan
from .util import DEV, STATE_DIR, read_only, run

router = APIRouter(prefix="/api/storage", tags=["storage"])
POOLS_FILE = os.path.join(STATE_DIR, "pools.json")


def load_pools() -> list[dict]:
    try:
        with open(POOLS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_pools(pools: list[dict]) -> None:
    with open(POOLS_FILE, "w") as f:
        json.dump(pools, f, indent=2)


MOCK_DISKS = [
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "size": 256060514304, "model": "Samsung 970 EVO", "serial": "S4-MOCK0", "rota": False, "in_use": True, "system": True},
    {"name": "sda", "path": "/dev/sda", "size": 4000787030016, "model": "WDC WD40EFRX", "serial": "WD-MOCK1", "rota": True, "in_use": False, "system": False},
    {"name": "sdb", "path": "/dev/sdb", "size": 4000787030016, "model": "WDC WD40EFRX", "serial": "WD-MOCK2", "rota": True, "in_use": False, "system": False},
    {"name": "sdc", "path": "/dev/sdc", "size": 8001563222016, "model": "ST8000VN004", "serial": "ZA-MOCK3", "rota": True, "in_use": False, "system": False},
    {"name": "sdd", "path": "/dev/sdd", "size": 8001563222016, "model": "ST8000VN004", "serial": "ZA-MOCK4", "rota": True, "in_use": False, "system": False},
]


def array_members(md: str) -> list[str]:
    """Disks currently in an md array, read live from sysfs (disk names change across reboots)."""
    if DEV or not md:
        return []
    mdname = os.path.basename(os.path.realpath(md))          # /dev/md/cyn1t1 -> md127
    slaves = f"/sys/block/{mdname}/slaves"
    disks = []
    for part in sorted(os.listdir(slaves)) if os.path.isdir(slaves) else []:
        try:
            with open(f"/sys/block/{mdname}/md/dev-{part}/state") as f:
                state = f.read()
            with open(f"/sys/block/{mdname}/md/dev-{part}/slot") as f:
                slot = f.read().strip()
            if "faulty" in state or (slot == "none" and "in_sync" not in state):
                continue                                   # failed drive or idle hot spare
        except OSError:
            pass
        parent = os.path.basename(os.path.dirname(os.path.realpath(f"/sys/class/block/{part}")))
        disks.append(parent)
    return disks


def member_states(md: str) -> list[tuple[str, str, str]]:
    """(disk, partition, role) for every device in an md array, read live from sysfs.
    role: 'active' (in sync), 'rebuilding', 'spare' (idle hot spare) or 'failed'."""
    if DEV or not md:
        return []
    mdname = os.path.basename(os.path.realpath(md))
    base = f"/sys/block/{mdname}/md"
    out = []
    for part in sorted(os.listdir(f"/sys/block/{mdname}/slaves")) if os.path.isdir(f"/sys/block/{mdname}/slaves") else []:
        try:
            state = open(f"{base}/dev-{part}/state").read().strip()
            slot = open(f"{base}/dev-{part}/slot").read().strip()
        except OSError:
            continue
        if "faulty" in state:
            role = "failed"
        elif "in_sync" in state:
            role = "active"
        elif slot != "none":
            role = "rebuilding"      # has a slot but not in sync yet: recovery onto this device
        else:
            role = "spare"
        disk = os.path.basename(os.path.dirname(os.path.realpath(f"/sys/class/block/{part}")))
        out.append((disk, part, role))
    return out


def pool_roles(pool: dict) -> dict[str, str]:
    """disk -> worst role over all tiers (failed > rebuilding > active > spare)."""
    rank = {"failed": 3, "rebuilding": 2, "active": 1, "spare": 0}
    roles: dict[str, str] = {}
    for a in pool.get("arrays", []):
        for disk, _part, role in member_states(a["md"]):
            if disk not in roles or rank[role] > rank[roles[disk]]:
                roles[disk] = role
    return roles


def pool_disks(pool: dict) -> list[str]:
    if DEV:
        return pool["disks"]
    live = sorted({d for a in pool.get("arrays", []) for d in array_members(a["md"])})
    return live or pool["disks"]


def _mounts(dev: dict) -> list[str]:
    """All mount points on a device and everything stacked on it (partitions, LVM, md...)."""
    out = [dev["mountpoint"]] if dev.get("mountpoint") else []
    for c in dev.get("children") or []:
        out += _mounts(c)
    return out


def list_disks() -> list[dict]:
    if DEV:
        used = {d for p in load_pools() for d in p["disks"]}
        return [{**d, "in_use": d["in_use"] or d["name"] in used} for d in MOCK_DISKS]
    out = read_only(["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,MODEL,SERIAL,ROTA,TYPE,MOUNTPOINT,FSTYPE,TRAN,HOTPLUG"])
    disks = []
    for d in json.loads(out or '{"blockdevices":[]}')["blockdevices"]:
        test_loop = os.environ.get("CYN_TEST_LOOP") == "1" and d.get("type") == "loop"
        if not test_loop and (d.get("type") != "disk" or d["name"].startswith(("loop", "zram", "sr"))):
            continue
        if d.get("tran") == "usb":
            continue   # external drives are handled by External Devices, never offered for pools
        children = d.get("children") or []
        mounts = _mounts(d)
        disks.append({
            "name": d["name"], "path": d["path"], "size": int(d["size"]),
            "model": (d.get("model") or "").strip(), "serial": d.get("serial"),
            "rota": bool(d.get("rota")),
            "in_use": bool(children) or bool(d.get("fstype")),
            "system": any(m in ("/", "/boot", "/boot/efi") for m in mounts if m),
        })
    return disks


@router.get("/disks")
def disks(_=Depends(current_user)):
    return list_disks()


@router.get("/disks/{name}/smart")
def smart(name: str, _=Depends(require_admin)):
    if DEV:
        return {"supported": True, "passed": True, "temperature": 34, "power_on_hours": 12034, "reallocated": 0}
    data = json.loads(read_only(["smartctl", "-j", "-a", f"/dev/{name}"]) or "{}")
    if "smart_status" not in data:
        return {"supported": False, "passed": None}
    attrs = {a["id"]: a["raw"]["value"] for a in data.get("ata_smart_attributes", {}).get("table", [])}
    return {
        "supported": True,
        "passed": data.get("smart_status", {}).get("passed"),
        "temperature": data.get("temperature", {}).get("current"),
        "power_on_hours": data.get("power_on_time", {}).get("hours"),
        "reallocated": attrs.get(5),
    }


@router.get("/pools")
def pools(_=Depends(current_user)):
    result = []
    for p in load_pools():
        usage = {"total": 0, "used": 0}
        if not DEV and os.path.ismount(p["mount"]):
            st = os.statvfs(p["mount"])
            usage = {"total": st.f_blocks * st.f_frsize, "used": (st.f_blocks - st.f_bfree) * st.f_frsize}
        elif DEV:
            usage = {"total": p.get("mock_total", 0), "used": p.get("mock_total", 0) // 3}
        status = "healthy"
        for a in ([] if DEV else p.get("arrays", [])):
            md = read_only(["mdadm", "--detail", a["md"]])
            state = next((ln.split(":", 1)[1] for ln in md.splitlines() if ln.strip().startswith("State :")), "")
            if any(w in state for w in ("recovering", "resyncing", "reshaping")):
                # resync = first sync after creation (pool usable) | recovery = drive replaced | reshape = growing
                word = "rebuilding" if "recovering" in state else "expanding" if "reshaping" in state else "optimizing"
                progress = next((ln.split(":", 1)[1].strip() for ln in md.splitlines()
                                 if "Rebuild Status" in ln or "Reshape Status" in ln or "Resync Status" in ln), "")
                status = f"{word} ({progress.split()[0]})" if progress else word
                if word == "rebuilding":
                    break
            elif "degraded" in state or "FAILED" in state or "inactive" in state:
                status = "degraded"
                break
        roles = {} if DEV else pool_roles(p)
        spares = sorted(d for d, r in roles.items() if r == "spare") if roles else p.get("dev_spares", [])
        failed = sorted(d for d, r in roles.items() if r == "failed")
        result.append({**p, **usage, "status": status, "disks": pool_disks(p), "spares": spares, "failed": failed})
    return result


class PoolCreate(BaseModel):
    raid: str                  # basic | raid0 | raid1 | raid5 | raid6 | raid10 | shr | shr2
    disks: list[str]
    label: str = ""
    fs: str = "btrfs"          # btrfs (snapshots, checksums, compression) | ext4


def _save_mdadm_conf(initramfs: bool = False) -> None:
    """Rewrite the ARRAY lines of mdadm.conf (keeping the rest) so arrays assemble at boot."""
    cmd = ("grep -v '^ARRAY' /etc/mdadm/mdadm.conf > /etc/mdadm/mdadm.conf.new 2>/dev/null || true; "
           "mdadm --detail --scan >> /etc/mdadm/mdadm.conf.new && mv /etc/mdadm/mdadm.conf.new /etc/mdadm/mdadm.conf")
    if initramfs:
        cmd += " && update-initramfs -u"
    run(["sh", "-c", cmd])


def _part(dev: str, n: int) -> str:
    return f"{dev}p{n}" if dev[-1].isdigit() else f"{dev}{n}"


def _free_disks(names: list[str]) -> dict[str, dict]:
    known = {d["name"]: d for d in list_disks()}
    for name in names:
        if name not in known:
            raise HTTPException(400, f"unknown disk {name}")
        if known[name]["system"] or known[name]["in_use"]:
            raise HTTPException(400, f"disk {name} is in use")
    return {n: known[n] for n in names}


@router.post("/plan")
def plan_preview(body: PoolCreate, _=Depends(current_user)):
    """Capacity preview for a layout, plus a comparison of every layout for the selected disks."""
    known = {d["name"]: d["size"] for d in list_disks()}
    sizes = {d: known[d] for d in body.disks if d in known}
    result = {"compare": raidplan.compare(sizes) if sizes else []}
    try:
        p = raidplan.plan(body.raid, sizes)
        result["plan"] = {"usable": p.usable_bytes, "raw": p.raw_bytes, "tolerance": p.tolerance,
                          "warnings": p.warnings,
                          "tiers": [{"level": t.level, "members": t.members, "usable": t.usable_mib * raidplan.MiB}
                                    for t in p.tiers]}
    except ValueError as e:
        result["error"] = str(e)
    return result


POOL_JOBS: dict[str, dict] = {}
CREATE_STEPS = ["Preparing drives", "Building RAID arrays", "Updating boot configuration",
                "Creating the volume", "Creating the file system", "Mounting"]


@router.post("/pools")
def create_pool(body: PoolCreate, _=Depends(require_admin)):
    """Validates, then builds the pool in the background (minutes); poll /api/storage/jobs/<id>."""
    if body.fs not in ("btrfs", "ext4"):
        raise HTTPException(400, "filesystem must be btrfs or ext4")
    disks = _free_disks(body.disks)
    try:
        p = raidplan.plan(body.raid, {n: d["size"] for n, d in disks.items()})
    except ValueError as e:
        raise HTTPException(400, str(e))
    if any(j["state"] == "running" for j in POOL_JOBS.values()):
        raise HTTPException(409, "another storage pool is being created - wait for it to finish")
    import secrets
    import threading
    jid = secrets.token_hex(4)
    job = {"id": jid, "state": "running", "steps": CREATE_STEPS, "step": 0, "error": None, "pool": None,
           "started": time.time()}
    POOL_JOBS[jid] = job

    def work():
        try:
            job["pool"] = _build_pool(body, p, job)
            job["state"] = "done"
        except HTTPException as e:
            job.update(state="failed", error=e.detail)
        except Exception as e:
            job.update(state="failed", error=str(e))
        job["finished"] = time.time()

    threading.Thread(target=work, daemon=True).start()
    return {"job": jid}


@router.get("/jobs/{jid}")
def pool_job(jid: str, _=Depends(current_user)):
    j = POOL_JOBS.get(jid)
    if not j:
        raise HTTPException(404, "no such job")
    return j


def _build_pool(body: "PoolCreate", p, job: dict) -> dict:
    def step(i):
        job["step"] = i
        if DEV:
            time.sleep(0.7)   # let the progress view be seen in dev mode

    pools = load_pools()
    n = max([x["id"] for x in pools], default=0) + 1
    mount, vg = f"/volume{n}", f"vg_cyn{n}"

    # 1. partition every disk: one GPT partition per tier it belongs to (type FD00 = Linux RAID)
    step(0)
    for name in body.disks:
        dev = f"/dev/{name}"
        _scrub_disk(dev)
        for i, t in enumerate(p.tiers, start=1):
            if name in t.members:
                end = "0" if t.size_mib == 0 else f"{t.start_mib + t.size_mib - 1}M"
                run(["sgdisk", "-n", f"{i}:{t.start_mib}M:{end}", "-t", f"{i}:FD00",
                     "-c", f"{i}:cyn{n}-t{i}", dev])
        run(["partprobe", dev], check=False)
    run(["udevadm", "settle"], check=False)

    # 2. one md array per tier
    step(1)
    arrays = []
    for i, t in enumerate(p.tiers, start=1):
        md = f"/dev/md/cyn{n}t{i}"
        parts = [_part(f"/dev/{m}", i) for m in t.members]
        run(["mdadm", "--create", md, "--run", "--force", "--metadata=1.2", f"--level={t.level}",
             f"--raid-devices={len(parts)}", *(["--bitmap=internal"] if t.level in ("1", "5", "6", "10") else []),
             f"--name=cyn{n}t{i}", *parts])
        arrays.append({"md": md, "level": t.level, "members": t.members, "part": i})
    step(2)
    _save_mdadm_conf(initramfs=True)

    # 3. LVM joins the tier arrays into one volume (lets SHR volumes grow later)
    step(3)
    mds = [a["md"] for a in arrays]
    run(["pvcreate", "-ff", "-y", *mds])
    run(["vgcreate", vg, *mds])
    run(["lvcreate", "-y", "-l", "100%FREE", "-n", "data", vg])
    lv = f"/dev/{vg}/data"

    # 4. filesystem
    step(4)
    label = (body.label or f"volume{n}")[:15]
    if body.fs == "btrfs":
        run(["mkfs.btrfs", "-f", "-L", label, lv])
        opts = "defaults,noatime,compress=zstd:3,space_cache=v2"
    else:
        run(["mkfs.ext4", "-F", "-L", label, lv])
        opts = "defaults,noatime"
    step(5)
    run(["mkdir", "-p", mount])
    uuid = run(["blkid", "-s", "UUID", "-o", "value", lv]).strip()
    run(["sh", "-c", f"echo 'UUID={uuid} {mount} {body.fs} {opts},nofail 0 0' >> /etc/fstab"])
    run(["systemctl", "daemon-reload"], check=False)
    run(["mount", mount])

    pool = {"id": n, "label": body.label or f"Volume {n}", "raid": body.raid, "fs": body.fs,
            "disks": body.disks, "arrays": arrays, "vg": vg, "lv": lv, "mount": mount, "uuid": uuid,
            "tolerance": p.tolerance, "mock_total": p.usable_bytes}
    pools = load_pools()
    pools.append(pool)
    save_pools(pools)
    job["step"] = len(CREATE_STEPS)
    return pool


def _grow_when_reshaped(pool: dict) -> None:
    """md only exposes new capacity after the reshape completes (hours on big drives), so a
    background systemd job waits for it, then grows PV -> LV -> filesystem."""
    mds = " ".join(a["md"] for a in pool["arrays"])
    fs_grow = (f"btrfs filesystem resize max {pool['mount']}" if pool.get("fs", "btrfs") == "btrfs"
               else f"resize2fs {pool['lv']}")
    script = (f"for md in {mds}; do mdadm --wait $md; done; "
              f"for md in {mds}; do pvresize $md; done; "
              f"lvextend -l +100%FREE {pool['lv']} || true; {fs_grow}")
    run(["systemd-run", f"--unit=cyn-grow-pool{pool['id']}-{int(time.time())}", "--collect",
         "--description", f"Cynthology: grow {pool['label']} after reshape", "sh", "-c", script])


def _scrub_disk(dev: str) -> None:
    """Erase RAID/filesystem signatures on every partition, then the partition table.
    Order matters: if only the table is wiped, re-creating partitions at the same offsets
    brings the old md superblocks back and udev re-adds them to arrays on its own."""
    out = subprocess.run(["lsblk", "-ln", "-o", "PATH,TYPE", dev], capture_output=True, text=True).stdout if not DEV else ""
    for line in out.splitlines():
        path, typ = (line.split() + [""])[:2]
        if typ == "part":
            run(["mdadm", "--zero-superblock", "--force", path], check=False)
            run(["wipefs", "-a", path], check=False)
    run(["wipefs", "-a", dev])
    run(["sgdisk", "--zap-all", dev])
    run(["partprobe", dev], check=False)
    run(["udevadm", "settle"], check=False)


def _clone_layout(pool: dict, disk: str) -> str:
    """Partition a free disk exactly like the pool's largest working member, so it has a
    partition for every tier. Returns /dev/<disk>."""
    new = _free_disks([disk])[disk]
    sizes = {d["name"]: d["size"] for d in list_disks()}
    members = pool_disks(pool)
    if not members:
        raise HTTPException(400, "can't find any working drive of this pool")
    ref_name = max(members, key=lambda d: sizes.get(d, 0))
    if new["size"] < sizes.get(ref_name, 0):
        raise HTTPException(400, f"the drive must be at least as large as the pool's largest drive "
                                 f"({ref_name}, {sizes.get(ref_name, 0) / 1e12:.2f} TB)")
    dev, ref = f"/dev/{disk}", f"/dev/{ref_name}"
    _scrub_disk(dev)
    run(["sh", "-c", f"sgdisk --backup=/run/cyn-pt {ref} && sgdisk --load-backup=/run/cyn-pt {dev} && sgdisk -e -G {dev}"])
    run(["partprobe", dev], check=False)
    run(["udevadm", "settle"], check=False)
    return dev


class AddDisk(BaseModel):
    disk: str


@router.post("/pools/{pool_id}/add-disk")
def add_disk(pool_id: int, body: AddDisk, _=Depends(require_admin)):
    """Repair a degraded pool or expand it with a new disk.

    Degraded array  -> the new disk's partitions are added and md rebuilds onto them.
    Healthy array   -> the arrays are grown onto the new disk (RAID1->RAID5 for SHR
                       when the third disk arrives), then LVM and the filesystem are grown.
    """
    pools = load_pools()
    pool = next((x for x in pools if x["id"] == pool_id), None)
    if not pool:
        raise HTTPException(404, "no such pool")
    if pool["raid"] in ("raid0", "raid10", "basic"):
        raise HTTPException(400, f"{pool['raid']} pools cannot be expanded in place")
    for a in pool["arrays"]:
        a["members"] = array_members(a["md"]) or a["members"]
    dev = _clone_layout(pool, body.disk)

    for a in pool["arrays"]:
        part = _part(dev, a["part"])
        detail = read_only(["mdadm", "--detail", a["md"]]) if not DEV else ""
        run(["mdadm", "--add", a["md"], part])
        if "degraded" in detail:
            continue  # rebuild starts automatically
        count = len(a["members"]) + 1
        if pool["raid"] == "shr" and a["level"] == "1" and count >= 3:
            # mdadm converts level and grows in two steps: 2-disk RAID1 -> 2-disk RAID5 -> N disks
            run(["mdadm", "--grow", a["md"], "--level=5"])
            run(["mdadm", "--grow", a["md"], f"--raid-devices={count}"])
            a["level"] = "5"
        elif pool["raid"] == "shr2" and a["level"] == "1" and count >= 4:
            run(["mdadm", "--grow", a["md"], "--level=6"])
            run(["mdadm", "--grow", a["md"], f"--raid-devices={count}"])
            a["level"] = "6"
        elif a["level"] == "1" and pool["raid"] in ("shr", "shr2"):
            run(["mdadm", "--grow", a["md"], f"--raid-devices={count}"])
        elif a["level"] in ("5", "6"):
            run(["mdadm", "--grow", a["md"], f"--raid-devices={count}"])
        a["members"].append(body.disk)

    _grow_when_reshaped(pool)
    pool["disks"] = pool_disks(pool) if not DEV else pool["disks"] + [body.disk]
    _save_mdadm_conf()
    save_pools(pools)
    return {"ok": True, "note": "Reshape runs in the background; the volume stays usable meanwhile."}


@router.post("/pools/{pool_id}/spare")
def add_spare(pool_id: int, body: AddDisk, _=Depends(require_admin)):
    """Hot spare: the drive waits in every tier's array as an md spare. If a drive fails, the kernel
    starts rebuilding onto it immediately - no user action, nothing in Cynthology has to be running.
    If the pool is already degraded, the rebuild starts right away (same as Repair)."""
    pools_ = load_pools()
    pool = next((x for x in pools_ if x["id"] == pool_id), None)
    if not pool:
        raise HTTPException(404, "no such pool")
    if pool["raid"] in ("raid0", "basic"):
        raise HTTPException(400, "a hot spare needs a pool with redundancy (not Basic or RAID 0)")
    dev = _clone_layout(pool, body.disk)
    for a in pool["arrays"]:
        run(["mdadm", "--add", a["md"], _part(dev, a["part"])])
    if DEV:
        pool.setdefault("dev_spares", []).append(body.disk)
    _save_mdadm_conf()
    save_pools(pools_)
    return {"ok": True}


class RemoveDisk(BaseModel):
    disk: str


@router.post("/pools/{pool_id}/spare/remove")
def remove_spare(pool_id: int, body: RemoveDisk, _=Depends(require_admin)):
    """Take an idle hot spare out of the pool (only allowed while it is not rebuilding)."""
    pools_ = load_pools()
    pool = next((x for x in pools_ if x["id"] == pool_id), None)
    if not pool:
        raise HTTPException(404, "no such pool")
    if DEV:
        pool["dev_spares"] = [d for d in pool.get("dev_spares", []) if d != body.disk]
        save_pools(pools_)
        return {"ok": True}
    removed = 0
    for a in pool["arrays"]:
        for disk, part, role in member_states(a["md"]):
            if disk != body.disk:
                continue
            if role != "spare":
                raise HTTPException(409, f"{disk} is no longer an idle spare (it is {role}) and can't be removed")
            run(["mdadm", a["md"], "--remove", f"/dev/{part}"])
            removed += 1
    if not removed:
        raise HTTPException(404, f"{body.disk} is not a hot spare of this pool")
    _scrub_disk(f"/dev/{body.disk}")
    _save_mdadm_conf()
    return {"ok": True}


@router.post("/pools/{pool_id}/remove-failed")
def remove_failed(pool_id: int, _=Depends(require_admin)):
    """Detach drives md has marked failed, so they can be pulled and the slot reused."""
    pool = next((x for x in load_pools() if x["id"] == pool_id), None)
    if not pool:
        raise HTTPException(404, "no such pool")
    for a in pool["arrays"]:
        run(["mdadm", a["md"], "--remove", "failed"], check=False)
    _save_mdadm_conf()
    return {"ok": True}


def scrub_status(pool: dict) -> dict:
    """Parsed `btrfs scrub status` (Btrfs pools only)."""
    if pool.get("fs", "btrfs") != "btrfs":
        return {"supported": False}
    if DEV:
        return {"supported": True, "state": "never"}
    out = read_only(["btrfs", "scrub", "status", pool["mount"]])
    info = {"supported": True, "state": "never"}
    for line in out.splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k == "status":
            info["state"] = {"finished": "finished", "running": "running", "aborted": "aborted", "interrupted": "aborted"}.get(v, v)
        elif k == "scrub started":
            info["started"] = v
        elif k == "duration":
            info["duration"] = v
        elif k == "error summary":
            info["errors"] = v
        elif k == "total to scrub":
            info["total"] = v
        elif k == "rate":
            info["rate"] = v
    if "no stats available" in out:
        info["state"] = "never"
    return info


@router.get("/pools/{pool_id}/scrub")
def get_scrub(pool_id: int, _=Depends(current_user)):
    pool = next((p for p in load_pools() if p["id"] == pool_id), None)
    if not pool:
        raise HTTPException(404, "no such pool")
    from . import settings
    return {**scrub_status(pool), "schedule": settings.get("scrub_schedule") or "off"}


class ScrubSchedule(BaseModel):
    schedule: str      # off | monthly | quarterly


@router.put("/scrub-schedule")
def put_scrub_schedule(body: ScrubSchedule, _=Depends(require_admin)):
    if body.schedule not in ("off", "monthly", "quarterly"):
        raise HTTPException(400, "schedule must be off, monthly or quarterly")
    from . import settings
    settings.update(scrub_schedule=body.schedule)
    return {"ok": True}


def scrub_due() -> list[int]:
    """Start scrubs that are due (cynthology-scrub.timer, daily)."""
    from . import settings
    days = {"monthly": 30, "quarterly": 91}.get(settings.get("scrub_schedule") or "off")
    started = []
    if not days:
        return started
    for p in load_pools():
        st = scrub_status(p)
        if not st.get("supported") or st.get("state") == "running":
            continue
        last = 0
        if st.get("started"):
            import datetime
            try:
                last = datetime.datetime.strptime(st["started"], "%a %b %d %H:%M:%S %Y").timestamp()
            except ValueError:
                pass
        if time.time() - last > days * 86400:
            run(["btrfs", "scrub", "start", p["mount"]], check=False)
            started.append(p["id"])
    return started


@router.post("/pools/{pool_id}/scrub")
def scrub(pool_id: int, _=Depends(require_admin)):
    pool = next((p for p in load_pools() if p["id"] == pool_id), None)
    if not pool:
        raise HTTPException(404, "no such pool")
    run(["btrfs", "scrub", "start", pool["mount"]])
    return {"ok": True}


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["scrub-due"]:
        print("scrubs started for pools:", scrub_due())
