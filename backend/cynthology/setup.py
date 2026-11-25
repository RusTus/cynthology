"""First-run setup wizard + General settings (server name, time zone, NTP)."""
import os
import re
import socket

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import settings, tls
from .auth import current_user, require_admin
from .util import DEV, read_only, run

router = APIRouter(prefix="/api/setup", tags=["setup"])


def timezones() -> list[str]:
    out = read_only(["timedatectl", "list-timezones"]).split()
    if not out:
        import zoneinfo
        out = sorted(z for z in zoneinfo.available_timezones() if "/" in z or z == "UTC")
    return out


def current_timezone() -> str:
    tz = read_only(["timedatectl", "show", "-p", "Timezone", "--value"]).strip()
    if not tz:
        try:
            tz = os.path.realpath("/etc/localtime").split("zoneinfo/", 1)[1]
        except IndexError:
            tz = "UTC"
    return tz


def ntp_enabled() -> bool:
    return read_only(["timedatectl", "show", "-p", "NTP", "--value"]).strip() == "yes"


@router.get("/status")
def status(user=Depends(current_user)):
    s = settings.get_all()
    return {"setup_done": s["setup_done"], "needs_setup": user["admin"] and not s["setup_done"]}


@router.get("/general")
def general(_=Depends(require_admin)):
    return {"hostname": socket.gethostname(), "timezone": current_timezone(), "ntp": ntp_enabled(),
            "timezones": timezones(), "ips": tls.local_ips()}


class General(BaseModel):
    hostname: str
    timezone: str
    ntp: bool = True


def apply_hostname(name: str) -> bool:
    """Returns True if the name changed."""
    if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?", name):
        raise HTTPException(400, "Server name: letters, digits and '-' only, up to 63 characters")
    if name == socket.gethostname():
        return False
    run(["hostnamectl", "set-hostname", name])
    # keep 127.0.1.1 in /etc/hosts in step so sudo & friends resolve the new name
    run(["sed", "-i", rf"s/^127\.0\.1\.1\s.*/127.0.1.1\t{name}.local {name}/", "/etc/hosts"])
    run(["systemctl", "try-restart", "avahi-daemon", "smbd", "nmbd"], check=False)
    return True


@router.put("/general")
def put_general(body: General, _=Depends(require_admin)):
    if body.timezone not in timezones():
        raise HTTPException(400, "unknown time zone")
    changed = apply_hostname(body.hostname)
    run(["timedatectl", "set-timezone", body.timezone])
    run(["timedatectl", "set-ntp", "true" if body.ntp else "false"], check=False)
    # the self-signed certificate names the host, so re-issue it after a rename
    if changed and not DEV and tls.cert_info().get("kind") == "self-signed":
        tls.generate_self_signed()
        tls.restart_soon()
    return {"ok": True, "restarting": changed}


class Finish(BaseModel):
    https_redirect: bool = True


@router.post("/finish")
def finish(body: Finish, _=Depends(require_admin)):
    settings.update(setup_done=True, https_redirect=body.https_redirect)
    return {"ok": True}
