"""Storage manager: disks, SMART health, RAID pools (mdadm) with Btrfs volumes on top.

Every pool uses the same stack: GPT partitions -> mdadm array(s) -> LVM -> Btrfs (or ext4),
mounted at /volumeN. SHR uses several md arrays (one per size tier) - see raidplan.py. Shared folders are Btrfs subvolumes, which gives cheap snapshots.
"""
import json
import os
import re
import shutil
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
        if os.environ.get("CYN_MOCK_FOREIGN"):
            used |= {d for f in foreign_pools() for d in f["disks"]}
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
    """usage: system | pool (member of a pool here) | foreign (has data from elsewhere) | free"""
    pooled = _pooled_disks()
    out = []
    for d in list_disks():
        usage = "system" if d["system"] else "pool" if d["name"] in pooled else "foreign" if d["in_use"] else "free"
        out.append({**d, "usage": usage, "pool": pooled.get(d["name"])})
    return out


@router.get("/disks/{name}/smart")
def smart(name: str, _=Depends(require_admin)):
    if DEV:
        return {"supported": True, "passed": True, "temperature": 34, "power_on_hours": 12034, "reallocated": 0}
    data = json.loads(read_only(["smartctl", "-j", "-a", f"/dev/{name}"]) or "{}")
    if "smart_status" not in data:
        return {"supported": False, "passed": None}
    attrs = {a["id"]: a["raw"]["value"] for a in data.get("ata_smart_attributes", {}).get("table", [])}
    # self-tests: ATA log, or NVMe log
    tests = data.get("ata_smart_self_test_log", {}).get("standard", {}).get("table", []) or \
        data.get("nvme_self_test_log", {}).get("table", [])
    last = None
    if tests:
        t = tests[0]
        st = t.get("status", {}) if isinstance(t.get("status"), dict) else {}
        last = {"type": (t.get("type", {}) or {}).get("string") or (t.get("self_test_code", {}) or {}).get("string", "test"),
                "passed": st.get("passed", (t.get("self_test_result", {}) or {}).get("value", 0) == 0),
                "result": st.get("string") or (t.get("self_test_result", {}) or {}).get("string", ""),
                "hours": t.get("lifetime_hours") or t.get("power_on_hours")}
    run_st = data.get("ata_smart_data", {}).get("self_test", {}).get("status", {})
    running = run_st.get("remaining_percent") if run_st.get("value", 0) >> 4 == 0xF else None
    return {
        "supported": True,
        "passed": data.get("smart_status", {}).get("passed"),
        "temperature": data.get("temperature", {}).get("current"),
        "power_on_hours": data.get("power_on_time", {}).get("hours"),
        "reallocated": attrs.get(5),
        "last_test": last,
        "test_running_remaining_pct": running,
    }


class SmartTest(BaseModel):
    test: str = "short"     # short (~2 min) | long (hours)


@router.post("/disks/{name}/smart-test")
def smart_test(name: str, body: SmartTest, _=Depends(require_admin)):
    if body.test not in ("short", "long"):
        raise HTTPException(400, "test must be short or long")
    if not re.fullmatch(r"[a-z0-9]+", name) or name not in {d["name"] for d in list_disks()}:
        raise HTTPException(404, "no such drive")
    if DEV:
        return {"ok": True, "message": "Test started (dev)"}
    r = subprocess.run(["smartctl", "-t", body.test, f"/dev/{name}"], capture_output=True, text=True)
    if r.returncode & 0b11 or "not supported" in r.stdout.lower():
        raise HTTPException(400, f"{name} can't run self-tests (virtual disks and some USB enclosures don't support them)")
    wait = next((ln.strip() for ln in r.stdout.splitlines() if "Please wait" in ln), "")
    return {"ok": True, "message": f"{body.test.capitalize()} test started on {name}. {wait}"}


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
        if p.get("cache") and not DEV and status == "healthy":
            cstate = _md_info(p["cache"]["md"])["state"]
            if any(w in cstate for w in ("degraded", "FAILED", "inactive")):
                status = "SSD cache degraded"
        roles = {} if DEV else pool_roles(p)
        spares = sorted(d for d, r in roles.items() if r == "spare") if roles else p.get("dev_spares", [])
        failed = sorted(d for d, r in roles.items() if r == "failed")
        result.append({**p, **usage, "status": status, "disks": pool_disks(p), "spares": spares, "failed": failed,
                       "cache": cache_stats(p)})
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
    write_pool_meta(pool)
    job["step"] = len(CREATE_STEPS)
    return pool


def _grow_when_reshaped(pool: dict) -> None:
    """md only exposes new capacity after the reshape completes (hours on big drives), so a
    background systemd job waits for it, then grows PV -> LV -> filesystem."""
    mds = " ".join(a["md"] for a in pool["arrays"])
    fs_grow = (f"btrfs filesystem resize max {pool['mount']}" if pool.get("fs", "btrfs") == "btrfs"
               else f"resize2fs {pool['lv']}")
    vg, c = pool.get("vg"), pool.get("cache")
    # a cached volume is detached from its SSD cache while it grows, then re-attached
    uncache = f"lvconvert -y --splitcache {vg}/data; " if c else ""
    recache = f"; lvconvert -y --type cache --cachevol {vg}/cache --cachemode {c['mode']} {vg}/data" if c else ""
    script = (f"for md in {mds}; do mdadm --wait $md; done; "
              f"for md in {mds}; do pvresize $md; done; {uncache}"
              f"lvextend -l +100%FREE {pool['lv']} {' '.join(a['md'] for a in pool['arrays'])} || true; {fs_grow}{recache}")
    run(["systemd-run", f"--unit=cyn-grow-pool{pool['id']}-{int(time.time())}", "--collect",
         "--description", f"Cynthology: grow {pool['label']} after reshape", "sh", "-c", script])


def _refuse_if_in_use(dev: str) -> None:
    """Last line of defence before wiping: the drive and its partitions must not be held by an
    active md array, LVM or dm device, and nothing on it may be mounted. (Drive names change
    between boots, so a stale name must never be able to wipe a working pool member.)"""
    name = os.path.basename(os.path.realpath(dev))
    parts = [name] + [p for p in os.listdir(f"/sys/class/block/{name}") if p.startswith(name)] if os.path.isdir(f"/sys/class/block/{name}") else [name]
    for p in parts:
        holders = os.listdir(f"/sys/class/block/{p}/holders") if os.path.isdir(f"/sys/class/block/{p}/holders") else []
        if holders:
            raise HTTPException(409, f"refusing to erase {name}: {p} is in use by {', '.join(holders)}")
    mounted = read_only(["lsblk", "-ln", "-o", "MOUNTPOINT", dev]).split()
    if mounted:
        raise HTTPException(409, f"refusing to erase {name}: mounted at {mounted[0]}")


def _scrub_disk(dev: str) -> None:
    """Erase RAID/filesystem signatures on every partition, then the partition table.
    Order matters: if only the table is wiped, re-creating partitions at the same offsets
    brings the old md superblocks back and udev re-adds them to arrays on its own."""
    if not DEV:
        _refuse_if_in_use(dev)
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


# ---------- pools and drives from an earlier installation ----------
# A reinstall (or moving the drives to a new box) leaves the md arrays, LVM and file system on the
# drives intact; Linux even assembles the arrays at boot. Such a pool can be imported (DSM calls it
# "Online Assemble"); any drive with data that isn't part of a pool here can be erased instead.

POOL_META = ".cynthology-pool.json"      # on the volume itself, so an import restores name and RAID type
VG_RE = re.compile(r"^vg_cyn(\d+)\Z")
_scanned: list = []
MOCK_FOREIGN = [{"vg": "vg_cyn1", "label": "Volume 1", "raid": "shr", "fs": "btrfs", "size": 17_100_000_000,
                 "disks": ["sda", "sdb", "sdc", "sdd"], "status": "healthy", "tolerance": 1, "importable": True, "why_not": None,
                 "arrays": []}]


def write_pool_meta(pool: dict) -> None:
    if DEV or not os.path.ismount(pool["mount"]):
        return
    try:
        with open(os.path.join(pool["mount"], POOL_META), "w") as f:
            json.dump({k: pool.get(k) for k in ("label", "raid", "fs", "tolerance")}, f)
    except OSError:
        pass


def _slaves(md: str) -> list[str]:
    d = f"/sys/block/{os.path.basename(os.path.realpath(md))}/slaves"
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def _part_info(part: str) -> dict:
    disk = os.path.basename(os.path.dirname(os.path.realpath(f"/sys/class/block/{part}")))
    try:
        num = int(open(f"/sys/class/block/{part}/partition").read())
    except (OSError, ValueError):
        num = 0
    return {"part": part, "path": f"/dev/{part}", "disk": disk, "num": num}


def _md_info(md: str) -> dict:
    detail = read_only(["mdadm", "--detail", md])
    field = lambda k: next((ln.split(":", 1)[1].strip() for ln in detail.splitlines() if ln.strip().startswith(k + " :")), "")
    level = field("Raid Level").replace("raid", "")
    parts = [_part_info(p) for p in _slaves(md)]
    name = field("Name").split()[0] if field("Name") else ""
    return {"dev": md, "level": level, "state": field("State"), "devices": int(field("Raid Devices") or 0),
            "name": name, "parts": parts, "tier": parts[0]["num"] if parts else 0,
            "cache": bool(re.search(r"cyn\d+c$", name))}


def _tolerance(a: dict) -> int:
    return {"0": 0, "1": max(a["devices"] - 1, 0), "4": 1, "5": 1, "6": 2, "10": 1}.get(a["level"], 0)


def _guess_raid(arrays: list[dict]) -> str:
    if len(arrays) > 1:
        return "shr2" if any(a["level"] == "6" for a in arrays) else "shr"
    a = arrays[0] if arrays else {"level": "", "devices": 0}
    if a["level"] == "1" and a["devices"] == 1:
        return "basic"
    return f"raid{a['level']}" if a["level"] else "unknown"


def _fstab_set(uuid: str, mount: str, line: str) -> None:
    lines = open("/etc/fstab").read().splitlines()
    keep = [ln for ln in lines if not (ln.split() and (ln.split()[0] == f"UUID={uuid}" or (len(ln.split()) > 1 and ln.split()[1] == mount)))]
    with open("/etc/fstab.new", "w") as f:
        f.write("\n".join(keep + [line]) + "\n")
    os.replace("/etc/fstab.new", "/etc/fstab")


def foreign_pools() -> list[dict]:
    """Cynthology pools on the drives that this installation doesn't know (yet)."""
    if DEV:
        return [p for p in MOCK_FOREIGN if not any(x.get("vg") == p["vg"] for x in load_pools())] if os.environ.get("CYN_MOCK_FOREIGN") else []
    if not _scanned:   # arrays may not have been assembled (e.g. no mdadm.conf yet)
        run(["mdadm", "--assemble", "--scan"], check=False)
        _scanned.append(True)
    known = {p.get("vg") for p in load_pools()}
    out = read_only(["pvs", "--reportformat", "json", "--units", "b", "--nosuffix", "-o", "pv_name,vg_name,vg_size"])
    try:
        rows = json.loads(out)["report"][0]["pv"] if out else []
    except (ValueError, KeyError, IndexError):
        rows = []
    vgs: dict[str, dict] = {}
    for r in rows:
        if VG_RE.match(r.get("vg_name", "")) and r["vg_name"] not in known:
            v = vgs.setdefault(r["vg_name"], {"size": int(float(r.get("vg_size") or 0)), "pvs": []})
            v["pvs"].append(r["pv_name"])
    result = []
    for vg, info in sorted(vgs.items()):
        everything = [_md_info(pv) for pv in info["pvs"]]
        arrays = [a for a in everything if not a["cache"]]
        cache = next((a for a in everything if a["cache"]), None)
        lv = f"/dev/{vg}/data"
        run(["vgchange", "-ay", vg], check=False)
        fs = read_only(["blkid", "-s", "TYPE", "-o", "value", lv]).strip()
        label = read_only(["blkid", "-s", "LABEL", "-o", "value", lv]).strip()
        mounted = read_only(["findmnt", "-n", "-o", "TARGET", "--source", lv]).strip()
        states = " ".join(a["state"] for a in arrays)
        status = "degraded" if any(w in states for w in ("degraded", "FAILED", "inactive")) else "healthy"
        why = None
        if not all(a["dev"].startswith("/dev/md") for a in arrays):
            why = "not on RAID arrays this version understands"
        elif fs not in ("btrfs", "ext4"):
            why = "the file system can't be read (the pool may be missing too many drives)"
        elif mounted:
            why = f"already mounted at {mounted}"
        cache_info = None
        if cache:
            mode = read_only(["lvs", "--noheadings", "-o", "cache_mode", f"{vg}/data"]).strip() or "writethrough"
            cache_info = {"dev": cache["dev"], "parts": cache["parts"], "level": cache["level"], "mode": mode,
                          "disks": sorted({p["disk"] for p in cache["parts"]})}
        result.append({"vg": vg, "label": label or vg, "raid": _guess_raid(arrays), "fs": fs, "size": info["size"],
                       "disks": sorted({p["disk"] for a in everything for p in a["parts"]}), "status": status,
                       "tolerance": min((_tolerance(a) for a in arrays), default=0), "importable": why is None, "why_not": why,
                       "arrays": arrays, "cache": cache_info})
    return result


@router.get("/foreign")
def get_foreign(_=Depends(require_admin)):
    return foreign_pools()


@router.post("/foreign/{vg}/import")
def import_pool(vg: str, _=Depends(require_admin)):
    f = next((x for x in foreign_pools() if x["vg"] == vg), None)
    if not f:
        raise HTTPException(404, "no such storage pool on the drives")
    if not f["importable"]:
        raise HTTPException(400, f"can't import: {f['why_not']}")
    pools = load_pools()
    ids = {p["id"] for p in pools}
    n = int(VG_RE.match(vg).group(1))
    if n in ids or (not DEV and os.path.ismount(f"/volume{n}")):
        n = max(ids | {n}) + 1
    new_vg, mount = f"vg_cyn{n}", f"/volume{n}"
    arrays = []
    if not DEV:
        # Re-assemble each array under this installation's naming (cyn<n>t<tier>, local host name)
        run(["vgchange", "-an", vg])
        try:
            for a in f["arrays"]:
                name = f"cyn{n}t{a['tier']}"
                run(["mdadm", "--stop", a["dev"]])
                run(["mdadm", "--assemble", f"/dev/md/{name}", "--run", "--update=name", f"--name={name}",
                     *[p["path"] for p in a["parts"]]])
                arrays.append({"md": f"/dev/md/{name}", "level": a["level"], "part": a["tier"],
                               "members": sorted({p["disk"] for p in a["parts"]})})
            if f.get("cache"):
                c = f["cache"]
                run(["mdadm", "--stop", c["dev"]])
                run(["mdadm", "--assemble", f"/dev/md/cyn{n}c", "--run", "--update=name", f"--name=cyn{n}c", *[p["path"] for p in c["parts"]]])
        except HTTPException:
            run(["mdadm", "--assemble", "--scan"], check=False)    # put back whatever we stopped
            run(["vgchange", "-ay", vg], check=False)
            raise
        run(["udevadm", "settle"], check=False)
        if new_vg != vg:
            run(["vgrename", vg, new_vg])
        run(["vgchange", "-ay", new_vg])
        lv = f"/dev/{new_vg}/data"
        uuid = run(["blkid", "-s", "UUID", "-o", "value", lv]).strip()
        opts = "defaults,noatime,compress=zstd:3,space_cache=v2" if f["fs"] == "btrfs" else "defaults,noatime"
        run(["mkdir", "-p", mount])
        _fstab_set(uuid, mount, f"UUID={uuid} {mount} {f['fs']} {opts},nofail 0 0")
        run(["systemctl", "daemon-reload"], check=False)
        run(["mount", mount])
    else:
        lv, uuid = f"/dev/{new_vg}/data", "mock"
    meta = {}
    try:
        meta = json.load(open(os.path.join(mount, POOL_META)))
    except (OSError, ValueError):
        pass
    cache_disks = set(f["cache"]["disks"]) if f.get("cache") else set()
    pool = {"id": n, "label": meta.get("label") or (f["label"] if f["label"] != vg else f"Volume {n}"),
            "raid": meta.get("raid") or f["raid"], "fs": f["fs"], "disks": [d for d in f["disks"] if d not in cache_disks], "arrays": arrays,
            "vg": new_vg, "lv": lv, "mount": mount, "uuid": uuid, "tolerance": f["tolerance"],
            "mock_total": f["size"], "imported": True}
    if f.get("cache"):
        c = f["cache"]
        pool["cache"] = {"md": f"/dev/md/cyn{n}c", "disks": c["disks"], "mode": c["mode"], "level": c["level"],
                         "size": int(read_only(["blockdev", "--getsize64", f"/dev/md/cyn{n}c"]).strip() or 0) if not DEV else 0}
    pools = load_pools()
    pools.append(pool)
    save_pools(pools)
    if not DEV:
        _save_mdadm_conf(initramfs=True)
    write_pool_meta(pool)
    from . import shares as shares_mod
    restored = shares_mod.adopt_pool_shares(pool)
    return {"ok": True, "pool": pool, "shares": restored}


class Erase(BaseModel):
    disks: list[str]
    confirm: str


def _pooled_disks() -> dict[str, str]:
    """Every drive that belongs to a pool of this installation (members, spares, failed)."""
    out = {}
    for p in load_pools():
        names = set(p["disks"]) if DEV else {_part_info(s)["disk"] for a in p.get("arrays", []) for s in _slaves(a["md"])} | set(pool_disks(p))
        if p.get("cache"):
            names |= set(cache_disks(p["cache"]))
        for d in names:
            out[d] = p["label"]
    return out


def _dismantle(dev: str) -> None:
    """Unmount and stop everything stacked on a drive (LVM, md arrays), then wipe it."""
    tree = json.loads(read_only(["lsblk", "-J", "-o", "PATH,TYPE,MOUNTPOINT", dev]) or '{"blockdevices":[]}')["blockdevices"]
    mounts, lvs, mds = [], [], []

    def walk(n):
        for c in n.get("children") or []:
            walk(c)
        if n.get("mountpoint"):
            mounts.append(n["mountpoint"])
        if n.get("type") == "lvm":
            lvs.append(n["path"])
        elif (n.get("type") or "").startswith("raid") or n.get("type") == "linear":
            mds.append(n["path"])
    for n in tree:
        walk(n)
    for m in mounts:
        if m in ("/", "/boot", "/boot/efi") or m.startswith("[SWAP]"):
            raise HTTPException(400, f"{dev} is used by the system ({m})")
        run(["umount", m])
    for lv in lvs:
        vg = read_only(["lvs", "--noheadings", "-o", "vg_name", lv]).strip()
        if vg:
            run(["vgchange", "-an", vg], check=False)
        run(["dmsetup", "remove", "--retry", lv], check=False)
    for md in dict.fromkeys(mds):
        run(["mdadm", "--stop", md], check=False)
    _scrub_disk(dev)


@router.post("/disks/erase")
def erase_disks(body: Erase, _=Depends(require_admin)):
    if body.confirm != "ERASE":
        raise HTTPException(400, "type ERASE to confirm")
    known = {d["name"]: d for d in list_disks()}
    pooled = _pooled_disks()
    for name in body.disks:
        if name not in known:
            raise HTTPException(400, f"unknown drive {name}")
        if known[name]["system"]:
            raise HTTPException(400, f"{name} is the system drive")
        if name in pooled:
            raise HTTPException(400, f"{name} belongs to storage pool '{pooled[name]}'")
    for name in body.disks:
        _dismantle(f"/dev/{name}")
    _scanned.clear()
    return {"ok": True}


# ---------- SSD cache (LVM dm-cache) ----------
# Read-only = writethrough (every write also goes to the drives at once: losing the SSD loses
# nothing); with 2+ SSDs they are mirrored by default so the cache survives a failing SSD, or
# striped for more space. Read-write = writeback (needs two mirrored SSDs, like DSM). The SSDs form an md
# array (/dev/md/cyn<n>c) that joins the pool's volume group as a "cachevol" for vg/data.

class CacheCreate(BaseModel):
    disks: list[str]
    mode: str = "readonly"     # readonly | readwrite
    mirror: bool = True        # 2+ drives: mirror them (redundancy) or stripe them (more space); read-write is always mirrored


def _pool(pool_id: int) -> dict:
    p = next((x for x in load_pools() if x["id"] == pool_id), None)
    if not p:
        raise HTTPException(404, "no such storage pool")
    return p


def cache_disks(c: dict) -> list[str]:
    """Drives in the cache array right now (names change between boots; never trust saved ones)."""
    if DEV:
        return c["disks"]
    return sorted({_part_info(x)["disk"] for x in _slaves(c["md"])})


def cache_stats(pool: dict) -> dict | None:
    c = pool.get("cache")
    if not c:
        return None
    out = {**c, "size": c.get("size", 0), "disks": cache_disks(c)}
    if DEV:
        return {**out, "hit_rate": 87, "used_pct": 41, "dirty": 0}
    raw = read_only(["lvs", "--reportformat", "json", "--units", "b", "--nosuffix", "-o",
                     "lv_size,cache_read_hits,cache_read_misses,cache_write_hits,cache_write_misses,cache_used_blocks,cache_total_blocks,cache_dirty_blocks",
                     f"{pool['vg']}/data"])
    try:
        r = json.loads(raw)["report"][0]["lv"][0]
        num = lambda k: int(r.get(k) or 0)
        reads = num("cache_read_hits") + num("cache_read_misses")
        out.update(hit_rate=round(100 * num("cache_read_hits") / reads) if reads else None,
                   used_pct=round(100 * num("cache_used_blocks") / num("cache_total_blocks")) if num("cache_total_blocks") else 0,
                   dirty=num("cache_dirty_blocks"))
    except (ValueError, KeyError, IndexError):
        out["error"] = "cache statistics unavailable"
    return out


@router.get("/pools/{pool_id}/cache")
def get_cache(pool_id: int, _=Depends(current_user)):
    return cache_stats(_pool(pool_id))


@router.post("/pools/{pool_id}/cache")
def create_cache(pool_id: int, body: CacheCreate, _=Depends(require_admin)):
    pool = _pool(pool_id)
    if pool.get("cache"):
        raise HTTPException(409, "this storage pool already has an SSD cache")
    if not DEV and not shutil.which("cache_check"):
        raise HTTPException(400, "SSD cache needs the thin-provisioning-tools package (without it the volume can't start at boot). "
                                 "Update Cynthology (Control Panel > Update) to install it.")
    if body.mode not in ("readonly", "readwrite"):
        raise HTTPException(400, "mode must be readonly or readwrite")
    if not body.disks:
        raise HTTPException(400, "choose at least one drive")
    if body.mode == "readwrite" and len(body.disks) != 2:
        raise HTTPException(400, "a read-write cache needs exactly 2 drives (they are mirrored, so a failing SSD loses no data)")
    if len(body.disks) > 4:
        raise HTTPException(400, "at most 4 drives")
    disks = _free_disks(body.disks)
    n = pool["id"]
    md, vg = f"/dev/md/cyn{n}c", pool["vg"]
    # one drive or mirrored -> RAID 1 (every SSD holds the whole cache); striped read-only -> RAID 0
    level = "0" if body.mode == "readonly" and len(disks) > 1 and not body.mirror else "1"
    size = sum(d["size"] for d in disks.values()) if level == "0" else min(d["size"] for d in disks.values())
    mode = "writethrough" if body.mode == "readonly" else "writeback"
    if not DEV:
        parts = []
        for name in body.disks:
            dev = f"/dev/{name}"
            _scrub_disk(dev)
            run(["sgdisk", "-n", "1:1M:0", "-t", "1:FD00", "-c", f"1:cyn{n}-cache", dev])
            run(["partprobe", dev], check=False)
            parts.append(_part(dev, 1))
        run(["udevadm", "settle"], check=False)
        run(["mdadm", "--create", md, "--run", "--force", "--metadata=1.2", f"--level={level}",
             f"--raid-devices={len(parts)}", f"--name=cyn{n}c", *parts])
        try:
            run(["pvcreate", "-ff", "-y", md])
            run(["vgextend", vg, md])
            run(["lvcreate", "-y", "-n", "cache", "-l", "100%PVS", vg, md])
            run(["lvconvert", "-y", "--type", "cache", "--cachevol", f"{vg}/cache", "--cachemode", mode, f"{vg}/data"])
        except HTTPException:   # undo: the pool must never be left half-converted
            run(["lvremove", "-y", f"{vg}/cache"], check=False)
            run(["vgreduce", vg, md], check=False)
            run(["pvremove", "-y", md], check=False)
            run(["mdadm", "--stop", md], check=False)
            for name in body.disks:
                _scrub_disk(f"/dev/{name}")
            raise
        _save_mdadm_conf(initramfs=True)
    pools = load_pools()
    for p in pools:
        if p["id"] == n:
            p["cache"] = {"md": md, "disks": body.disks, "mode": mode, "level": level, "size": size}
    save_pools(pools)
    return {"ok": True}


@router.delete("/pools/{pool_id}/cache")
def remove_cache(pool_id: int, _=Depends(require_admin)):
    """Detach the cache (a read-write cache first writes its data back to the drives) and free the SSDs."""
    pool = _pool(pool_id)
    c = pool.get("cache")
    if not c:
        raise HTTPException(404, "no SSD cache")
    if not DEV:
        vg = pool["vg"]
        members = cache_disks(c)            # read live, before the array is stopped
        cached = read_only(["lvs", "--noheadings", "-o", "segtype", f"{vg}/data"]).strip() == "cache"
        if cached:
            r = subprocess.run(["lvconvert", "-y", "--uncache", f"{vg}/data"], capture_output=True, text=True)   # may take a while (flush)
            if r.returncode != 0 and c["mode"] == "writethrough":
                # a dead read-only cache holds nothing that isn't on the drives: drop it without flushing
                r = subprocess.run(["lvconvert", "-y", "--uncache", "--force", f"{vg}/data"], capture_output=True, text=True)
            if r.returncode != 0:
                raise HTTPException(500, "could not detach the cache: " + (r.stderr.strip() or r.stdout.strip()))
        run(["lvremove", "-y", f"{vg}/cache"], check=False)
        run(["vgreduce", vg, c["md"]], check=False)   # (already gone if an earlier removal stopped half-way)
        run(["pvremove", "-y", c["md"]], check=False)
        run(["mdadm", "--stop", c["md"]], check=False)
    pools = load_pools()                    # the pool no longer uses the cache: record that first
    for p in pools:
        if p["id"] == pool_id:
            p.pop("cache", None)
    save_pools(pools)
    if not DEV:
        _save_mdadm_conf(initramfs=True)
        run(["udevadm", "settle"], check=False)
        failed = []
        for name in members:
            try:
                _scrub_disk(f"/dev/{name}")
            except HTTPException as e:
                failed.append(f"{name}: {e.detail}")
        if failed:
            return {"ok": True, "warning": "cache removed, but some SSDs could not be wiped (erase them under Drives): " + "; ".join(failed)}
    return {"ok": True}
