"""File services and system services: SMB, NFS, SSH, etc. backed by systemd units."""
from fastapi import APIRouter, Depends, HTTPException

from .auth import current_user, require_admin
from .util import DEV, read_only, run

router = APIRouter(prefix="/api/services", tags=["services"])

SERVICES = {
    "smb": {"label": "SMB / Windows file sharing", "unit": "smbd"},
    "nfs": {"label": "NFS", "unit": "nfs-server"},
    "ssh": {"label": "SSH terminal", "unit": "ssh"},
    "wsdd": {"label": "Windows network discovery (WS-Discovery)", "unit": "wsdd2"},
    "avahi": {"label": "Bonjour / mDNS discovery", "unit": "avahi-daemon"},
    "docker": {"label": "Container engine", "unit": "docker"},
    "ups": {"label": "UPS monitoring (NUT)", "unit": "nut-monitor"},
}
_dev_state = {"smb": True, "ssh": True, "avahi": True, "docker": True}


def _active(unit: str, key: str) -> bool:
    if DEV:
        return _dev_state.get(key, False)
    return read_only(["systemctl", "is-active", unit]).strip() == "active"


@router.get("")
def list_services(_=Depends(current_user)):
    return [{"id": k, **v, "running": _active(v["unit"], k)} for k, v in SERVICES.items()]


@router.post("/{sid}/{action}")
def control(sid: str, action: str, _=Depends(require_admin)):
    svc = SERVICES.get(sid)
    if not svc or action not in ("enable", "disable"):
        raise HTTPException(400, "bad request")
    if action == "enable":
        run(["systemctl", "enable", "--now", svc["unit"]])
    else:
        run(["systemctl", "disable", "--now", svc["unit"]])
    if DEV:
        _dev_state[sid] = action == "enable"
    return {"ok": True}
