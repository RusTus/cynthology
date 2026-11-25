"""App Center: one-click apps run as Docker Compose projects from catalog/*.yml."""
import json
import os

from fastapi import APIRouter, Depends, HTTPException

from .auth import current_user, require_admin
from .storage import load_pools
from .util import DEV, STATE_DIR, read_only, run

router = APIRouter(prefix="/api/apps", tags=["apps"])
CATALOG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "catalog")
APPS_DIR = os.path.join(STATE_DIR, "apps")
_dev_installed: set[str] = set()


def catalog() -> dict[str, dict]:
    out = {}
    for fn in sorted(os.listdir(CATALOG_DIR)):
        if fn.endswith(".json"):
            with open(os.path.join(CATALOG_DIR, fn)) as f:
                meta = json.load(f)
            out[meta["id"]] = meta
    return out


def _installed(app_id: str) -> bool:
    if DEV:
        return app_id in _dev_installed
    return os.path.exists(os.path.join(APPS_DIR, app_id, "compose.yml"))


def _running(app_id: str) -> bool:
    if DEV:
        return app_id in _dev_installed
    out = read_only(["docker", "compose", "-p", f"cyn-{app_id}", "ps", "-q"])
    return bool(out.strip())


@router.get("")
def list_apps(_=Depends(current_user)):
    return [{**m, "installed": _installed(k), "running": _installed(k) and _running(k)}
            for k, m in catalog().items()]


@router.post("/{app_id}/install")
def install(app_id: str, _=Depends(require_admin)):
    meta = catalog().get(app_id)
    if not meta:
        raise HTTPException(404, "unknown app")
    pools = load_pools()
    if not pools:
        raise HTTPException(400, "create a storage volume first")
    data = f"{pools[0]['mount']}/apps/{app_id}"
    compose = meta["compose"].replace("${DATA}", data)
    if DEV:
        _dev_installed.add(app_id)
        print(f"[dev] would write compose for {app_id}:\n{compose}")
        return {"ok": True}
    d = os.path.join(APPS_DIR, app_id)
    os.makedirs(d, exist_ok=True)
    os.makedirs(data, exist_ok=True)
    with open(os.path.join(d, "compose.yml"), "w") as f:
        f.write(compose)
    run(["docker", "compose", "-p", f"cyn-{app_id}", "-f", os.path.join(d, "compose.yml"), "up", "-d"])
    return {"ok": True}


@router.post("/{app_id}/uninstall")
def uninstall(app_id: str, _=Depends(require_admin)):
    if DEV:
        _dev_installed.discard(app_id)
        return {"ok": True}
    d = os.path.join(APPS_DIR, app_id)
    run(["docker", "compose", "-p", f"cyn-{app_id}", "-f", os.path.join(d, "compose.yml"), "down"], check=False)
    run(["rm", "-rf", d])
    return {"ok": True}
