"""Btrfs snapshots of shared folders: on demand and on a schedule, with retention.

Snapshots live in <share>/.snapshots/@GMT-YYYY.MM.DD-HH.MM.SS (UTC). That name is Samba's
shadow_copy2 default, so Windows shows them under "Previous Versions" on the SMB share.
A systemd timer runs `python3 -m cynthology.snapshots` hourly to take due snapshots and prune.
"""
import datetime
import os
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import shares as shares_mod
from .auth import current_user, require_admin
from .util import DEV, read_only, run

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])
SNAPDIR = ".snapshots"
FMT = "@GMT-%Y.%m.%d-%H.%M.%S"
NAME_RE = re.compile(r"^@GMT-\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}$")
INTERVAL_H = {"hourly": 1, "daily": 24, "weekly": 24 * 7}


def _share(name: str) -> dict:
    s = next((x for x in shares_mod.load() if x["name"] == name), None)
    if not s:
        raise HTTPException(404, "no such shared folder")
    return s


def supported(share: dict) -> bool:
    if DEV:
        return True
    return bool(read_only(["btrfs", "subvolume", "show", share["path"]]))


def list_snaps(share: dict) -> list[str]:
    d = os.path.join(share["path"], SNAPDIR)
    return sorted(n for n in os.listdir(d) if NAME_RE.match(n)) if os.path.isdir(d) else []


def snap_time(name: str) -> datetime.datetime:
    return datetime.datetime.strptime(name, FMT).replace(tzinfo=datetime.timezone.utc)


def take(share: dict) -> str:
    if not supported(share):
        raise HTTPException(400, "snapshots need a Btrfs volume")
    name = datetime.datetime.now(datetime.timezone.utc).strftime(FMT)
    d = os.path.join(share["path"], SNAPDIR)
    if DEV:
        os.makedirs(os.path.join(d, name), exist_ok=True)
        return name
    run(["mkdir", "-p", d])
    run(["btrfs", "subvolume", "snapshot", "-r", share["path"], os.path.join(d, name)])
    return name


def delete(share: dict, name: str) -> None:
    if not NAME_RE.match(name):
        raise HTTPException(400, "bad snapshot name")
    path = os.path.join(share["path"], SNAPDIR, name)
    if not os.path.isdir(path):
        raise HTTPException(404, "no such snapshot")
    if DEV:
        os.rmdir(path)
    else:
        run(["btrfs", "subvolume", "delete", path])


def prune(share: dict) -> int:
    keep = int(share.get("snap_keep") or 0)
    snaps = list_snaps(share)
    if keep <= 0 or len(snaps) <= keep:
        return 0
    for name in snaps[:-keep]:
        delete(share, name)
    return len(snaps) - keep


def run_due(now: datetime.datetime | None = None) -> list[str]:
    """Take snapshots that are due and prune. Called hourly by cynthology-snapshots.timer."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    done = []
    for s in shares_mod.load():
        hours = INTERVAL_H.get(s.get("snap_schedule") or "off")
        if not hours or not os.path.isdir(s["path"]) or not supported(s):
            continue
        snaps = list_snaps(s)
        # 5 minutes of slack so an hourly timer doesn't skip a beat
        if not snaps or now - snap_time(snaps[-1]) >= datetime.timedelta(hours=hours, minutes=-5):
            take(s)
            done.append(s["name"])
        prune(s)
    return done


@router.get("")
def overview(_=Depends(current_user)):
    out = []
    for s in shares_mod.load():
        snaps = list_snaps(s) if os.path.isdir(s["path"]) else []
        out.append({"share": s["name"], "supported": supported(s), "schedule": s.get("snap_schedule") or "off",
                    "keep": s.get("snap_keep") or 0, "count": len(snaps), "latest": snaps[-1] if snaps else None})
    return out


class Policy(BaseModel):
    schedule: str = "off"
    keep: int = 0


@router.put("/{share}")
def set_policy(share: str, body: Policy, _=Depends(require_admin)):
    if body.schedule not in ("off", *INTERVAL_H):
        raise HTTPException(400, "schedule must be off, hourly, daily or weekly")
    if not 0 <= body.keep <= 1000:
        raise HTTPException(400, "keep must be between 0 (keep all) and 1000")
    items = shares_mod.load()
    s = next((x for x in items if x["name"] == share), None)
    if not s:
        raise HTTPException(404, "no such shared folder")
    s["snap_schedule"], s["snap_keep"] = body.schedule, body.keep
    shares_mod.write_configs(items)
    prune(s)
    return {"ok": True}


@router.get("/{share}")
def get_snaps(share: str, _=Depends(current_user)):
    s = _share(share)
    return [{"name": n, "time": snap_time(n).isoformat()} for n in reversed(list_snaps(s))]


@router.post("/{share}")
def snapshot_now(share: str, _=Depends(require_admin)):
    s = _share(share)
    name = take(s)
    prune(s)
    return {"snapshot": name}


@router.delete("/{share}/{name}")
def delete_snap(share: str, name: str, _=Depends(require_admin)):
    delete(_share(share), name)
    return {"ok": True}


if __name__ == "__main__":
    taken = run_due()
    print("snapshots taken:", ", ".join(taken) or "none")
