"""UPS support with NUT (Network UPS Tools).

Modes
  usb      UPS connected to this NAS by USB (usbhid-ups driver)
  snmp     network UPS with an SNMP card (snmp-ups driver)
  network  another machine runs the UPS (a Cynthology or Synology NAS, or any NUT server);
           Synology's UPS server uses NUT with user "monuser" / password "secret"
Optionally this NAS acts as a NUT server so other machines can follow its UPS (same
monuser/secret convention, so Synology NAS devices can use it too).

Shutdown policy: after N minutes on battery (upssched timer -> `upsmon -c fsd`) or only
when the UPS reports low battery (upsmon's default). Events run `python3 -m cynthology.ups event X`,
which alerts/emails immediately.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import current_user, require_admin
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/ups", tags=["ups"])
CFG_FILE = os.path.join(STATE_DIR, "ups.json")
NUT_DIR = os.path.join(STATE_DIR, "nut") if DEV else "/etc/nut"
EVENT_HOOK = "/usr/lib/cynthology/ups-event"
UPS_NAME = "ups"
SYNO_USER, SYNO_PASS = "monuser", "secret"     # Synology-compatible secondary login
DEFAULTS = {"enabled": False, "mode": "usb", "snmp_host": "", "snmp_community": "public",
            "server_host": "", "server_ups": "ups", "server_user": SYNO_USER, "server_password": SYNO_PASS,
            "shutdown": "timer", "shutdown_minutes": 5, "ups_poweroff": True,
            "serve": False, "driver": "", "port": "auto"}


def load() -> dict:
    try:
        with open(CFG_FILE) as f:
            return {**DEFAULTS, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(cfg: dict) -> None:
    fd = os.open(CFG_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(CFG_FILE + ".tmp", CFG_FILE)


# ---------- NUT configuration ----------

def render(cfg: dict) -> dict[str, str]:
    """All NUT config files for a configuration (pure function; unit tested)."""
    files = {}
    local = cfg["enabled"] and cfg["mode"] in ("usb", "snmp")
    if not cfg["enabled"]:
        nut_mode = "none"
    elif local:
        nut_mode = "netserver" if cfg["serve"] else "standalone"
    else:
        nut_mode = "netclient"
    files["nut.conf"] = f"# Managed by Cynthology\nMODE={nut_mode}\n"

    if local:
        if cfg["mode"] == "usb":
            drv = cfg.get("driver") or "usbhid-ups"
            files["ups.conf"] = (f"# Managed by Cynthology\nmaxretry = 3\n\n[{UPS_NAME}]\n  driver = {drv}\n"
                                 f"  port = {cfg.get('port') or 'auto'}\n  desc = \"UPS\"\n")
        else:
            files["ups.conf"] = (f"# Managed by Cynthology\nmaxretry = 3\n\n[{UPS_NAME}]\n  driver = snmp-ups\n"
                                 f"  port = {cfg['snmp_host']}\n  community = {cfg['snmp_community']}\n"
                                 f"  snmp_version = v1\n  mibs = auto\n  desc = \"UPS\"\n")
        listen = "LISTEN 0.0.0.0 3493\nLISTEN :: 3493\n" if cfg["serve"] else "LISTEN 127.0.0.1 3493\nLISTEN ::1 3493\n"
        files["upsd.conf"] = "# Managed by Cynthology\n" + listen
        files["upsd.users"] = ("# Managed by Cynthology\n"
                               f"[cynadmin]\n  password = {cfg['local_password']}\n  upsmon primary\n\n"
                               f"[{SYNO_USER}]\n  password = {SYNO_PASS}\n  upsmon secondary\n")
        monitor = f"MONITOR {UPS_NAME}@localhost 1 cynadmin {cfg['local_password']} primary"
    else:
        files["ups.conf"] = "# Managed by Cynthology\n"
        files["upsd.conf"] = "# Managed by Cynthology\n"
        files["upsd.users"] = "# Managed by Cynthology\n"
        monitor = (f"MONITOR {cfg['server_ups'] or 'ups'}@{cfg['server_host']} 1 {cfg['server_user']} "
                   f"{cfg['server_password']} secondary") if cfg["enabled"] else ""

    flags = "SYSLOG+EXEC"
    events = ["ONLINE", "ONBATT", "LOWBATT", "FSD", "COMMOK", "COMMBAD", "SHUTDOWN", "REPLBATT", "NOCOMM"]
    files["upsmon.conf"] = "\n".join([
        "# Managed by Cynthology",
        monitor,
        "MINSUPPLIES 1",
        'SHUTDOWNCMD "/sbin/shutdown -h +0"',
        "NOTIFYCMD /usr/sbin/upssched",
        "POLLFREQ 5", "POLLFREQALERT 5", "HOSTSYNC 15", "DEADTIME 15",
        "POWERDOWNFLAG /etc/killpower" if cfg["ups_poweroff"] and local else "# UPS power-off after shutdown: off",
        "RBWARNTIME 43200", "NOCOMMWARNTIME 300", "FINALDELAY 5",
        *[f"NOTIFYFLAG {e} {flags}" for e in events], ""])

    timer = ""
    if cfg["shutdown"] == "timer":
        secs = max(1, int(cfg["shutdown_minutes"])) * 60
        timer = f"AT ONBATT * START-TIMER shutdown-timer {secs}\nAT ONLINE * CANCEL-TIMER shutdown-timer\n"
    files["upssched.conf"] = (
        "# Managed by Cynthology\n"
        f"CMDSCRIPT {EVENT_HOOK}\n"
        "PIPEFN /run/nut/upssched.pipe\nLOCKFN /run/nut/upssched.lock\n\n"
        + timer +
        "AT ONBATT * EXECUTE onbatt\nAT ONLINE * EXECUTE online\nAT LOWBATT * EXECUTE lowbatt\n"
        "AT COMMBAD * EXECUTE commbad\nAT COMMOK * EXECUTE commok\nAT NOCOMM * EXECUTE commbad\n"
        "AT REPLBATT * EXECUTE replbatt\nAT FSD * EXECUTE fsd\n")
    return files


def apply(cfg: dict) -> None:
    if cfg["enabled"] and cfg["mode"] in ("usb", "snmp") and not cfg.get("local_password"):
        cfg["local_password"] = secrets.token_urlsafe(16)
    files = render(cfg)
    os.makedirs(NUT_DIR, exist_ok=True)
    for name, content in files.items():
        path = os.path.join(NUT_DIR, name)
        secret = name in ("upsd.users", "upsmon.conf")
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640 if secret else 0o644)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(path + ".tmp", path)
        if not DEV and secret:
            run(["chgrp", "nut", path], check=False)
    save(cfg)
    if DEV:
        return
    # restart in dependency order: drivers -> upsd -> upsmon
    local = cfg["enabled"] and cfg["mode"] in ("usb", "snmp")
    if local:
        run(["sh", "-c", "upsdrvsvcctl resync 2>/dev/null || true"], check=False)
        run(["systemctl", "restart", "nut-driver.target"], check=False)
        run(["systemctl", "enable", "--now", "nut-server"], check=False)
        run(["systemctl", "restart", "nut-server"], check=False)
    else:
        run(["systemctl", "disable", "--now", "nut-server", "nut-driver.target"], check=False)
    if cfg["enabled"]:
        run(["systemctl", "enable", "nut-monitor"], check=False)
        run(["systemctl", "restart", "nut-monitor"], check=False)
    else:
        run(["systemctl", "disable", "--now", "nut-monitor"], check=False)


# ---------- status ----------

def status(cfg: dict | None = None) -> dict:
    cfg = cfg or load()
    if not cfg["enabled"]:
        return {"enabled": False}
    target = f"{UPS_NAME}@localhost" if cfg["mode"] in ("usb", "snmp") else f"{cfg['server_ups'] or 'ups'}@{cfg['server_host']}"
    try:
        r = subprocess.run(["upsc", target], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {"enabled": True, "connected": False, "error": "upsc not available"}
    if r.returncode != 0:
        return {"enabled": True, "connected": False, "error": (r.stderr or r.stdout).strip().splitlines()[-1:] or ["no data"]}
    v = dict(line.split(": ", 1) for line in r.stdout.splitlines() if ": " in line)
    flags = v.get("ups.status", "").split()
    words = {"OL": "On mains power", "OB": "On battery", "LB": "Battery low", "RB": "Replace battery",
             "CHRG": "Charging", "DISCHRG": "Discharging", "BYPASS": "On bypass", "OVER": "Overloaded",
             "FSD": "Shutting down", "CAL": "Calibrating", "OFF": "Off"}
    num = lambda k: float(v[k]) if re.fullmatch(r"-?\d+(\.\d+)?", v.get(k, "")) else None
    return {"enabled": True, "connected": True, "target": target, "flags": flags,
            "status": ", ".join(words.get(f, f) for f in flags) or "Unknown",
            "on_battery": "OB" in flags, "low_battery": "LB" in flags, "replace_battery": "RB" in flags,
            "model": " ".join(x for x in (v.get("device.mfr") or v.get("ups.mfr"), v.get("device.model") or v.get("ups.model")) if x),
            "serial": v.get("device.serial") or v.get("ups.serial"),
            "charge": num("battery.charge"), "runtime": num("battery.runtime"), "load": num("ups.load"),
            "input_voltage": num("input.voltage"), "output_voltage": num("output.voltage")}


def health_issues() -> dict[str, dict]:
    cfg = load()
    if not cfg["enabled"]:
        return {}
    s = status(cfg)
    if not s.get("connected"):
        return {"ups-comm": {"level": "warning", "title": "UPS not reachable",
                             "detail": f"Cynthology can't talk to the UPS ({cfg['mode']}). Shutdown on power loss won't work."}}
    out = {}
    if s["on_battery"]:
        mins = f" (about {int(s['runtime'] // 60)} min left)" if s.get("runtime") else ""
        out["ups-onbatt"] = {"level": "critical", "title": "Power failure - running on UPS battery",
                             "detail": f"Battery {s.get('charge') or '?'}%{mins}. " +
                                       (f"The NAS shuts down after {cfg['shutdown_minutes']} min on battery."
                                        if cfg["shutdown"] == "timer" else "The NAS shuts down when the battery is low.")}
    if s["low_battery"]:
        out["ups-lowbatt"] = {"level": "critical", "title": "UPS battery low", "detail": "The NAS is shutting down."}
    if s["replace_battery"]:
        out["ups-replbatt"] = {"level": "warning", "title": "UPS battery needs replacing",
                               "detail": "The UPS reports that its battery should be replaced."}
    return out


# ---------- API ----------

class UpsConfig(BaseModel):
    enabled: bool = False
    mode: str = "usb"
    snmp_host: str = ""
    snmp_community: str = "public"
    server_host: str = ""
    server_ups: str = "ups"
    server_user: str = SYNO_USER
    server_password: str = SYNO_PASS
    shutdown: str = "timer"          # timer | lowbatt
    shutdown_minutes: int = 5
    ups_poweroff: bool = True
    serve: bool = False


@router.get("")
def get_ups(user=Depends(current_user)):
    cfg = load()
    cfg.pop("local_password", None)
    if not user.get("admin"):               # passwords of the UPS server / SNMP community: admins only
        for k in ("server_password", "snmp_community", "server_user"):
            cfg.pop(k, None)
    return {"config": cfg, "status": status()}


@router.put("")
def put_ups(body: UpsConfig, _=Depends(require_admin)):
    if body.mode not in ("usb", "snmp", "network"):
        raise HTTPException(400, "mode must be usb, snmp or network")
    if body.shutdown not in ("timer", "lowbatt"):
        raise HTTPException(400, "shutdown must be timer or lowbatt")
    if not 1 <= body.shutdown_minutes <= 240:
        raise HTTPException(400, "minutes must be between 1 and 240")
    host_re = r"[A-Za-z0-9.:-]+"
    if body.enabled and body.mode == "snmp" and not re.fullmatch(host_re, body.snmp_host):
        raise HTTPException(400, "enter the UPS's IP address")
    if body.enabled and body.mode == "network" and not re.fullmatch(host_re, body.server_host):
        raise HTTPException(400, "enter the UPS server's IP address")
    for v in (body.snmp_community, body.server_ups, body.server_user, body.server_password):
        if re.search(r"[\s\"'#]", v):
            raise HTTPException(400, "names and passwords can't contain spaces, quotes or #")
    cfg = {**load(), **body.model_dump()}
    apply(cfg)
    time.sleep(0 if DEV else 3)
    return {"ok": True, "status": status(cfg)}


@router.get("/detect")
def detect(_=Depends(require_admin)):
    """USB UPS models connected to this NAS (nut-scanner)."""
    try:
        r = subprocess.run(["nut-scanner", "-U", "-q"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    found, cur = [], {}
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("["):
            if cur:
                found.append(cur)
            cur = {}
        elif "=" in line:
            k, v = line.split("=", 1)
            cur[k.strip()] = v.strip().strip('"')
    if cur:
        found.append(cur)
    return found


# ---------- event hook (called by upssched) ----------

EVENT_TEXT = {
    "onbatt": ("critical", "Power failure - running on UPS battery"),
    "online": ("info", "Power restored - UPS back on mains"),
    "lowbatt": ("critical", "UPS battery low - shutting down"),
    "commbad": ("warning", "Lost contact with the UPS"),
    "commok": ("info", "Contact with the UPS restored"),
    "replbatt": ("warning", "UPS battery needs replacing"),
    "fsd": ("critical", "UPS: forced shutdown in progress"),
}


def event(name: str) -> None:
    if name == "shutdown-timer":
        # (this hook can be run by the nut account: only shut down when the UPS really is on battery)
        st = status()
        if not st.get("on_battery"):
            _notify("warning", "Ignored a shutdown request", "The UPS shutdown timer fired, but the UPS reports mains power.")
            return
        cfg = load()
        _notify("critical", "Shutting down: on UPS battery too long",
                f"The NAS was on battery for {cfg['shutdown_minutes']} minutes and is shutting down safely.")
        subprocess.run(["upsmon", "-c", "fsd"])
        return
    level, title = EVENT_TEXT.get(name, ("warning", f"UPS event: {name}"))
    s = status()
    detail = ""
    if s.get("connected"):
        detail = f"Battery {s.get('charge') or '?'}%, runtime {int((s.get('runtime') or 0) // 60)} min, load {s.get('load') or '?'}%."
    _notify(level, title, detail)


def _notify(level: str, title: str, detail: str) -> None:
    from .notify import notify
    notify(level, title, detail, event="ups")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "event":
        event(sys.argv[2])
