"""External (USB) drives: auto-mount as shared folders, eject, format.

Plugging a drive in fires a udev rule -> cyn-usb@<kernel-name>.service -> `python3 -m cynthology.usb
add <name>`, which mounts every filesystem on it at /volumeUSB<n>/usbshare[<p>] and publishes it as a
shared folder "usbshare<n>" (SMB + File Station). A drive keeps its number (remembered by UUID).
Eject flushes, unmounts, removes the share and powers the drive down so it can be pulled safely.
"""
import json
import os
import re
import subprocess
import sys
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import shares as shares_mod
from .auth import current_user, require_admin
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/usb", tags=["usb"])
STATE = os.path.join(STATE_DIR, "usb.json")
ROOT = os.path.join(STATE_DIR, "root") if DEV else "/"
SUPPORTED = {"ext4", "ext3", "ext2", "btrfs", "xfs", "exfat", "vfat", "ntfs", "ntfs3", "hfsplus"}
FORMATS = {"exfat": "exFAT (Windows, macOS and Linux)", "ntfs": "NTFS (Windows)", "ext4": "ext4 (Linux only, fastest here)"}


def _load() -> dict:
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"numbers": {}}


def _save(s: dict) -> None:
    with open(STATE + ".tmp", "w") as f:
        json.dump(s, f, indent=2)
    os.replace(STATE + ".tmp", STATE)


def _lsblk(dev: str | None = None) -> list[dict]:
    cmd = ["lsblk", "-J", "-b", "-o", "NAME,KNAME,PATH,SIZE,TYPE,TRAN,HOTPLUG,RM,FSTYPE,LABEL,UUID,MOUNTPOINT,MODEL,VENDOR,SERIAL"]
    out = subprocess.run(cmd + ([dev] if dev else []), capture_output=True, text=True).stdout
    return json.loads(out or '{"blockdevices":[]}')["blockdevices"]


def is_external(disk: dict) -> bool:
    return disk.get("type") == "disk" and (disk.get("tran") == "usb" or (bool(disk.get("hotplug")) and disk.get("tran") not in ("sata", "nvme", "sas", None)))


def external_disks() -> list[dict]:
    out = []
    numbers = _load()["numbers"]
    share_by_path = {s["path"]: s["name"] for s in shares_mod.load()}
    for d in _lsblk():
        if not is_external(d):
            continue
        parts = d.get("children") or ([d] if d.get("fstype") else [])
        vols = []
        for p in parts:
            if p.get("type") not in ("part", "disk") or not p.get("fstype"):
                continue
            vols.append({"name": p["name"], "fstype": p["fstype"], "label": p.get("label") or "", "uuid": p.get("uuid"),
                         "size": int(p.get("size") or 0), "mountpoint": p.get("mountpoint"),
                         "share": share_by_path.get(p.get("mountpoint") or ""),
                         "supported": p["fstype"] in SUPPORTED})
        out.append({"name": d["name"], "model": " ".join(x for x in ((d.get("vendor") or "").strip(), (d.get("model") or "").strip()) if x),
                    "serial": d.get("serial"), "size": int(d.get("size") or 0), "number": numbers.get(d.get("serial") or d["name"]),
                    "volumes": vols})
    return out


def _number_for(disk: dict) -> int:
    s = _load()
    key = disk.get("serial") or disk["name"]
    if key not in s["numbers"]:
        used = set(s["numbers"].values())
        s["numbers"][key] = next(n for n in range(1, 100) if n not in used)
        _save(s)
    return s["numbers"][key]


def _mount_opts(fstype: str) -> tuple[str, str]:
    """(fstype to mount with, options). Non-Linux filesystems get users-group write access."""
    if fstype in ("vfat", "exfat"):
        return fstype, "rw,noatime,uid=0,gid=100,fmask=0002,dmask=0002,iocharset=utf8"
    if fstype in ("ntfs", "ntfs3"):
        return "ntfs3", "rw,noatime,uid=0,gid=100,fmask=0002,dmask=0002,iocharset=utf8"
    if fstype == "hfsplus":
        return "hfsplus", "ro,noatime"
    return fstype, "rw,noatime"


def mount_disk(kname: str) -> list[str]:
    """Mount every supported filesystem on an external disk and publish it. Returns share names."""
    if not re.fullmatch(r"[a-z]+[0-9]*", kname):
        raise RuntimeError("bad device name")
    devs = _lsblk(f"/dev/{kname}")
    if not devs or not is_external(devs[0]):
        raise RuntimeError(f"{kname} is not an external drive")
    disk = devs[0]
    n = _number_for(disk)
    parts = [p for p in (disk.get("children") or [disk]) if p.get("fstype") in SUPPORTED]
    shares = shares_mod.load()
    made = []
    for i, p in enumerate(parts):
        suffix = "" if len(parts) == 1 else str(i + 1)
        mp = os.path.join(ROOT, f"volumeUSB{n}", f"usbshare{suffix}")
        name = f"usbshare{n}" + (f"-{i + 1}" if suffix else "")
        if not p.get("mountpoint"):
            os.makedirs(mp, exist_ok=True)
            fstype, opts = _mount_opts(p["fstype"])
            r = subprocess.run(["mount", "-t", fstype, "-o", opts, p["path"], mp], capture_output=True, text=True)
            if r.returncode != 0 and fstype == "ntfs3":   # dirty NTFS: try the FUSE driver
                r = subprocess.run(["mount", "-t", "ntfs-3g", "-o", "rw,uid=0,gid=100,umask=0002", p["path"], mp], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"could not mount {p['path']} ({p['fstype']}): {r.stderr.strip()}")
            if p["fstype"] in ("ext4", "ext3", "ext2", "btrfs", "xfs"):
                st = os.stat(mp)
                if st.st_uid == 0 and (st.st_mode & 0o777) == 0o755:   # fresh drive: let the users group write
                    run(["chgrp", "users", mp], check=False)
                    run(["chmod", "2775", mp], check=False)
        else:
            mp = p["mountpoint"]
        shares = [s for s in shares if s["name"] != name]
        shares.append({"name": name, "pool_id": 0, "path": mp, "smb": True, "nfs": False, "nfs_clients": "",
                       "recycle_bin": False, "read_users": [], "write_users": ["@users"], "usb": True,
                       "description": f"USB: {p.get('label') or disk.get('model') or p['name']}"})
        made.append(name)
    shares_mod.write_configs(shares)
    return made


def unmount_disk(kname: str, power_off: bool = False) -> None:
    """Remove the shares, flush and unmount every filesystem of a disk, optionally power it down."""
    if not re.fullmatch(r"[a-z]+[0-9]*", kname):
        raise RuntimeError("bad device name")
    devs = _lsblk(f"/dev/{kname}") if os.path.exists(f"/dev/{kname}") else []
    mps = []
    if devs:
        for p in (devs[0].get("children") or [devs[0]]):
            if p.get("mountpoint"):
                mps.append(p["mountpoint"])
    shares = shares_mod.load()
    keep = [s for s in shares if not (s.get("usb") and (s["path"] in mps or not os.path.ismount(s["path"])))]
    if len(keep) != len(shares):
        shares_mod.write_configs(keep)
    subprocess.run(["sync"])
    busy = []
    for mp in mps:
        r = subprocess.run(["umount", mp], capture_output=True, text=True)
        if r.returncode != 0:
            busy.append(mp)
        else:
            try:
                os.rmdir(mp)
                os.rmdir(os.path.dirname(mp))
            except OSError:
                pass
    if busy:
        shares_mod.write_configs(shares)   # still mounted: keep sharing it
        users = subprocess.run(["fuser", "-vm", *busy], capture_output=True, text=True).stderr.strip()
        raise RuntimeError(f"the drive is in use ({', '.join(busy)}). Close files and stop apps/backups using it." +
                           (f"\n{users}" if users else ""))
    if power_off and os.path.exists(f"/sys/block/{kname}/device/delete"):
        with open(f"/sys/block/{kname}/device/delete", "w") as f:
            f.write("1")


def cleanup_removed() -> None:
    """A drive was pulled without ejecting: drop its stale mounts and shares."""
    shares = shares_mod.load()
    gone = [s for s in shares if s.get("usb") and not os.path.ismount(s["path"])]
    for line in open("/proc/mounts").read().splitlines():
        dev, mp = line.split()[:2]
        if "/volumeUSB" in mp and not os.path.exists(dev):
            subprocess.run(["umount", "-l", mp], capture_output=True)
            gone += [s for s in shares if s["path"] == mp]
    if gone:
        names = {s["name"] for s in gone}
        shares_mod.write_configs([s for s in shares if s["name"] not in names])


# ---------- API ----------

@router.get("")
def devices(_=Depends(current_user)):
    return external_disks()


def _disk(name: str) -> dict:
    d = next((x for x in external_disks() if x["name"] == name), None)
    if not d:
        raise HTTPException(404, "no such external drive")
    return d


@router.post("/{name}/mount")
def api_mount(name: str, _=Depends(require_admin)):
    _disk(name)
    try:
        return {"shares": mount_disk(name)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.post("/{name}/eject")
def api_eject(name: str, _=Depends(require_admin)):
    _disk(name)
    try:
        unmount_disk(name, power_off=True)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "message": "You can unplug the drive now."}


class Format(BaseModel):
    fs: str = "exfat"
    label: str = "USB"


@router.post("/{name}/format")
def api_format(name: str, body: Format, _=Depends(require_admin)):
    _disk(name)
    if body.fs not in FORMATS:
        raise HTTPException(400, "choose exfat, ntfs or ext4")
    label = re.sub(r"[^A-Za-z0-9_-]", "", body.label)[:11] or "USB"
    try:
        unmount_disk(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    dev = f"/dev/{name}"
    part = f"{dev}p1" if name[-1].isdigit() else f"{dev}1"
    for p in (_lsblk(dev)[0].get("children") or []):
        run(["wipefs", "-a", p["path"]], check=False)
    run(["wipefs", "-a", dev])
    run(["sgdisk", "--zap-all", dev])
    code = {"exfat": "0700", "ntfs": "0700", "ext4": "8300"}[body.fs]
    run(["sgdisk", "-n", "1:0:0", "-t", f"1:{code}", "-c", f"1:{label}", dev])
    run(["partprobe", dev], check=False)
    run(["udevadm", "settle"], check=False)
    time.sleep(1)
    mkfs = {"exfat": ["mkfs.exfat", "-L", label, part], "ntfs": ["mkfs.ntfs", "-f", "-L", label, part],
            "ext4": ["mkfs.ext4", "-F", "-L", label, part]}[body.fs]
    run(mkfs)
    run(["udevadm", "settle"], check=False)
    try:
        return {"ok": True, "shares": mount_disk(name)}
    except RuntimeError as e:
        raise HTTPException(500, f"formatted, but mounting failed: {e}")


if __name__ == "__main__":
    # called by cyn-usb@.service (udev): add <kname> | remove <kname>
    action, kname = (sys.argv[1:3] + ["", ""])[:2]
    try:
        if action == "add":
            time.sleep(1)   # let partitions settle
            print("mounted", mount_disk(kname))
        elif action == "remove":
            cleanup_removed()
    except Exception as e:
        print("usb:", e, file=sys.stderr)
        sys.exit(1)
