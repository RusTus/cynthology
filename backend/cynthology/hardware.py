"""Hardware & Power (DSM: Control Panel > Hardware & Power).

- Drive hibernation: hard drives that have been idle for N minutes spin down (hd-idle watches
  the I/O counters; it works for drives that ignore their own standby timer). SSDs and the
  system drive are left alone. Background checks (health/SMART) don't wake sleeping drives.
- Power schedule: shut down at set times, and start up again at set times where the board's
  real-time clock can wake the machine (most PCs; the alarm is set before each shutdown).
- Wake-on-LAN: let the NAS be switched on from the network; and wake other computers from here.
- Temperatures and fans as the hardware reports them.
"""
import datetime
import json
import os
import shutil
import re
import socket
import subprocess

import psutil
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_admin
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/hardware", tags=["hardware"])
CFG = os.path.join(STATE_DIR, "hardware.json")
HDIDLE_DEFAULT = os.path.join(STATE_DIR, "hd-idle") if DEV else "/etc/default/hd-idle"
UNIT_DIR = os.path.join(STATE_DIR, "units") if DEV else "/etc/systemd/system"
WAKEALARM = "/sys/class/rtc/rtc0/wakealarm"
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DEFAULTS = {"hibernate_minutes": 0, "schedule": [], "wol": []}


def load() -> dict:
    try:
        return {**DEFAULTS, **json.load(open(CFG))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(cfg: dict) -> None:
    with open(CFG + ".new", "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(CFG + ".new", CFG)


# ---------- drive hibernation ----------

def hard_drives() -> list[dict]:
    """Spinning data drives (not SSDs, not the system drive)."""
    from .storage import list_disks
    out = []
    for d in list_disks():
        if d.get("rota") and not d.get("system"):
            out.append(d)
    return out


def render_hdidle(minutes: int, disks: list[str]) -> str:
    """/etc/default/hd-idle (pure; unit tested). -i 0 = default off; each listed drive gets the timeout."""
    if minutes <= 0 or not disks:
        return 'START_HD_IDLE=false\nHD_IDLE_OPTS="-i 0"\n'
    opts = ["-i", "0"] + [x for d in disks for x in ("-a", d, "-i", str(minutes * 60))]
    return f'START_HD_IDLE=true\nHD_IDLE_OPTS="{" ".join(opts)}"\n'


def apply_hibernation(cfg: dict) -> None:
    disks = [d["name"] for d in hard_drives()] if not DEV else ["sdb"]
    with open(HDIDLE_DEFAULT, "w") as f:
        f.write(render_hdidle(int(cfg["hibernate_minutes"]), disks))
    if DEV:
        return
    if int(cfg["hibernate_minutes"]) > 0 and disks:
        run(["systemctl", "enable", "hd-idle"], check=False)
        run(["systemctl", "restart", "hd-idle"], check=False)
    else:
        run(["systemctl", "disable", "--now", "hd-idle"], check=False)


def drive_states() -> list[dict]:
    out = []
    for d in hard_drives() if not DEV else [{"name": "sdb", "model": "WD Red", "serial": "X"}]:
        state = "unknown"
        if not DEV:
            r = subprocess.run(["hdparm", "-C", f"/dev/{d['name']}"], capture_output=True, text=True)
            m = re.search(r"drive state is:\s+(.+)", r.stdout)
            state = "asleep" if m and "standby" in m.group(1) else "spinning" if m else "unknown"
        out.append({"name": d["name"], "model": d.get("model", ""), "serial": d.get("serial", ""), "state": state})
    return out


# ---------- power schedule ----------

def _on_days(e: dict) -> list[int]:
    return sorted({int(x) for x in e.get("days", []) if 0 <= int(x) <= 6})


def next_time(entries: list[dict], action: str, after: datetime.datetime) -> datetime.datetime | None:
    """The next time any enabled entry of this action fires (pure; unit tested)."""
    best = None
    for e in entries:
        if e.get("action") != action or not e.get("enabled", True):
            continue
        hh, mm = (int(x) for x in e["time"].split(":"))
        for add in range(0, 8):
            day = after.date() + datetime.timedelta(days=add)
            if day.weekday() not in _on_days(e):
                continue
            t = datetime.datetime.combine(day, datetime.time(hh, mm), after.tzinfo)
            if t > after:
                best = t if best is None or t < best else best
                break
    return best


def rtc_supported() -> bool:
    return os.path.exists(WAKEALARM)


def set_wake_alarm(cfg: dict) -> datetime.datetime | None:
    """Program the real-time clock for the next scheduled start-up (or clear it)."""
    now = datetime.datetime.now().astimezone()
    nxt = next_time(cfg["schedule"], "startup", now + datetime.timedelta(minutes=2))
    if DEV or not rtc_supported():
        return nxt
    try:
        with open(WAKEALARM, "w") as f:
            f.write("0")
        if nxt:
            with open(WAKEALARM, "w") as f:
                f.write(str(int(nxt.timestamp())))
    except OSError:
        return None          # (many PC clocks can only wake the machine up to 24 hours ahead)
    return nxt


def wake_alarm() -> int | None:
    """The start-up time actually programmed into the clock."""
    try:
        v = open(WAKEALARM).read().strip()
        return int(v) if v else None
    except (OSError, ValueError):
        return None


def render_shutdown_units(entries: list[dict]) -> dict[str, str]:
    """systemd timer + service that shut the NAS down on the schedule (pure; unit tested)."""
    cal = []
    for e in entries:
        if e.get("action") == "shutdown" and e.get("enabled", True) and _on_days(e):
            cal.append(f"OnCalendar={','.join(DAYS[d] for d in _on_days(e))} *-*-* {e['time']}:00")
    if not cal:
        return {}
    return {
        "cyn-power-off.timer": "[Unit]\nDescription=Cynthology scheduled shutdown\n\n[Timer]\n" + "\n".join(cal) +
                               "\n\n[Install]\nWantedBy=timers.target\n",
        "cyn-power-off.service": "[Unit]\nDescription=Cynthology scheduled shutdown\n\n[Service]\nType=oneshot\n"
                                 "WorkingDirectory=/opt/cynthology/backend\nEnvironment=CYN_STATE=/var/lib/cynthology\n"
                                 "ExecStart=/usr/bin/python3 -m cynthology.hardware scheduled-shutdown\n",
    }


def apply_schedule(cfg: dict) -> None:
    units = render_shutdown_units(cfg["schedule"])
    for name in ("cyn-power-off.timer", "cyn-power-off.service"):
        path = os.path.join(UNIT_DIR, name)
        if name in units:
            os.makedirs(UNIT_DIR, exist_ok=True)
            with open(path, "w") as f:
                f.write(units[name])
        elif os.path.exists(path):
            if not DEV:
                run(["systemctl", "disable", "--now", "cyn-power-off.timer"], check=False)
            os.remove(path)
    if not DEV:
        run(["systemctl", "daemon-reload"])
        if units:
            run(["systemctl", "enable", "--now", "cyn-power-off.timer"])
    set_wake_alarm(cfg)


def scheduled_shutdown() -> None:
    """Runs from the timer. Never shut down in the middle of a backup, a RAID rebuild or a scrub."""
    from . import backup, storage
    busy = []
    for t in backup.load_tasks():
        if backup.read_status(t["id"]).get("state") == "running":
            busy.append(f"backup '{t['name']}'")
    for p in storage.pools():
        if p["status"].startswith(("rebuilding", "resync", "scrub", "reshap")):
            busy.append(f"{p['label']} ({p['status']})")
    if busy:
        from .notify import notify
        notify("warning", "Scheduled shutdown skipped", "The NAS stays on because it is busy: " + ", ".join(busy) + ".", event="power")
        return
    set_wake_alarm(load())
    subprocess.run(["systemctl", "poweroff"])


# ---------- Wake-on-LAN ----------

# ---------- video acceleration (hardware transcoding for media servers) ----------

VENDORS = {"0x8086": "intel", "0x1002": "amd", "0x10de": "nvidia"}
JELLYFIN_SETTING = {"intel": "Intel QuickSync (QSV)", "amd": "Video Acceleration API (VAAPI)",
                    "nvidia": "Nvidia NVENC", "other": "Video Acceleration API (VAAPI)"}


def video_accel() -> dict:
    """{"kind": intel|amd|nvidia|other|None, "name", "dri": [render nodes], "nvidia_runtime": bool}."""
    dri = sorted(f"/dev/dri/{n}" for n in (os.listdir("/dev/dri") if os.path.isdir("/dev/dri") else []) if n.startswith("renderD"))
    kind, name = None, ""
    for node in dri:
        base = f"/sys/class/drm/{os.path.basename(node)}/device"
        try:
            vendor = open(f"{base}/vendor").read().strip()
        except OSError:
            continue
        kind = VENDORS.get(vendor, "other")
        try:
            name = subprocess.run(["lspci", "-s", os.path.basename(os.path.realpath(base)), "-mm"], capture_output=True,
                                  text=True, timeout=5).stdout.split('"')[5]
        except (OSError, IndexError, subprocess.SubprocessError):
            name = kind
        if kind in ("intel", "amd"):
            break                                  # (prefer the integrated GPU for transcoding)
    nvidia_rt = shutil.which("nvidia-container-runtime") is not None and os.path.exists("/dev/nvidia0")
    if nvidia_rt and kind in (None, "nvidia", "other"):
        kind = "nvidia"
    if kind == "nvidia" and not nvidia_rt:
        kind = None                                # (an NVIDIA card needs its driver + container toolkit first)
    return {"kind": kind, "name": name, "dri": dri, "nvidia_runtime": nvidia_rt,
            "jellyfin_setting": JELLYFIN_SETTING.get(kind or "", "")}


def gpu_compose(indent: str) -> str:
    """The compose lines that hand the GPU to a container (empty without one)."""
    v = video_accel()
    if v["kind"] == "nvidia":
        lines = ["deploy:", "  resources:", "    reservations:", "      devices:", "        - driver: nvidia",
                 "          count: all", "          capabilities: [gpu, video, compute, utility]"]
    elif v["dri"]:
        import grp
        gids = []
        for g in ("render", "video"):
            try:
                gids.append(str(grp.getgrnam(g).gr_gid))
            except KeyError:
                pass
        lines = ["devices:", "  - /dev/dri:/dev/dri"] + (["group_add:"] + [f'  - "{x}"' for x in gids] if gids else [])
    else:
        return ""
    return "".join(f"{indent}{ln}\n" for ln in lines)


def nics() -> list[dict]:
    from .network import physical_nics
    out = []
    for n in physical_nics() if not DEV else ["eth0"]:
        txt = "" if DEV else subprocess.run(["ethtool", n], capture_output=True, text=True).stdout
        sup = re.search(r"Supports Wake-on:\s*(\S+)", txt)
        cur = re.search(r"^\s*Wake-on:\s*(\S+)", txt, re.M)
        mac = psutil.net_if_addrs().get(n, [])
        mac = next((a.address for a in mac if a.family == psutil.AF_LINK), "")
        out.append({"name": n, "mac": mac, "supported": bool(sup and "g" in sup.group(1)) or DEV,
                    "enabled": bool(cur and "g" in cur.group(1))})
    return out


def apply_wol(cfg: dict) -> None:
    if DEV:
        return
    for n in nics():
        if n["supported"]:
            run(["ethtool", "-s", n["name"], "wol", "g" if n["name"] in cfg["wol"] else "d"], check=False)


def magic_packet(mac: str) -> bytes:
    hexs = re.sub(r"[:.\-\s]", "", mac)
    raw = bytes.fromhex(hexs) if re.fullmatch(r"[0-9a-fA-F]{12}", hexs) else b""
    if len(raw) != 6:
        raise HTTPException(400, "a MAC address looks like 00:11:22:33:44:55")
    return b"\xff" * 6 + raw * 16


def boot() -> None:
    """cynthology-power.service at every start: re-apply what the hardware forgets."""
    cfg = load()
    apply_wol(cfg)
    apply_hibernation(cfg)        # (drive names can change between boots)
    set_wake_alarm(cfg)


# ---------- API ----------

class Entry(BaseModel):
    action: str                    # shutdown | startup
    time: str                      # HH:MM
    days: list[int]                # 0 = Monday
    enabled: bool = True


class Settings(BaseModel):
    hibernate_minutes: int = 0
    schedule: list[Entry] = []
    wol: list[str] = []


class Wake(BaseModel):
    mac: str


@router.get("")
def get_hw(_=Depends(require_admin)):
    cfg = load()
    temps = {}
    try:
        for name, entries in (psutil.sensors_temperatures() or {}).items():
            for e in entries:
                if e.current:
                    temps[f"{name} {e.label}".strip()] = e.current
    except (AttributeError, OSError):
        pass
    fans = {}
    try:
        for name, entries in (psutil.sensors_fans() or {}).items():
            for e in entries:
                fans[f"{name} {e.label}".strip()] = e.current
    except (AttributeError, OSError):
        pass
    now = datetime.datetime.now().astimezone()
    nxt_on = next_time(cfg["schedule"], "startup", now)
    nxt_off = next_time(cfg["schedule"], "shutdown", now)
    return {**cfg, "drives": drive_states(), "nics": nics(), "rtc": rtc_supported() or DEV,
            "hdidle_installed": DEV or os.path.exists("/usr/sbin/hd-idle"),
            "next_startup": int(nxt_on.timestamp()) if nxt_on else None,
            "alarm_set": wake_alarm() if not DEV else (int(nxt_on.timestamp()) if nxt_on else None),
            "next_shutdown": int(nxt_off.timestamp()) if nxt_off else None,
            "temps": temps, "fans": fans}


@router.put("")
def put_hw(body: Settings, _=Depends(require_admin)):
    if not 0 <= body.hibernate_minutes <= 600:
        raise HTTPException(400, "hibernation: 0 (never) to 600 minutes")
    if 0 < body.hibernate_minutes < 10:
        raise HTTPException(400, "spinning drives up and down too often wears them out - use 10 minutes or more")
    for e in body.schedule:
        if e.action not in ("shutdown", "startup") or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", e.time) \
                or not e.days or any(not 0 <= d <= 6 for d in e.days):
            raise HTTPException(400, "each schedule entry needs shutdown/startup, a time (HH:MM) and at least one day")
    names = {n["name"] for n in nics()}
    if any(w not in names for w in body.wol):
        raise HTTPException(400, "unknown network port")
    cfg = body.model_dump()
    save(cfg)
    apply_hibernation(cfg)
    apply_schedule(cfg)
    apply_wol(cfg)
    return get_hw(_=None)


@router.post("/wake")
def wake(body: Wake, _=Depends(require_admin)):
    """Send a Wake-on-LAN packet to another computer on the network."""
    pkt = magic_packet(body.mac)
    if not DEV:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(pkt, ("255.255.255.255", 9))
    return {"ok": True}


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["scheduled-shutdown"]:
        scheduled_shutdown()
    else:
        boot()
