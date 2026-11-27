"""One-click container apps (catalog/*.json) and the "Synology app alternatives" list.

Each app becomes a normal Container Manager project in the "docker" shared folder
(<volume>/docker/app-<id>/compose.yaml, data next to it), so it can be edited, updated, snapshotted
and backed up like any other project. Placeholders in a catalog compose file:
  ${SECRET}        a random password generated once per install (shown in the app's notes)
  ${SHARE:name}    path of shared folder <name> (created if missing), e.g. video, music, photo
  ${HOST}          the NAS's first LAN address      ${HOSTNAME}  its host name     ${TZ}  its time zone
  ${F:key}         an answer to one of the app's install questions ("fields": key, label, default, pattern)
An app with "holds_data": true keeps its data inside the container itself, so uninstalling only stops it
unless the data is to be deleted (docker compose down would remove the container and everything in it).
"""
import json
import os
import re
import secrets
import shutil
import socket
import subprocess

import psutil
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import containers as ct
from . import shares as shares_mod
from . import spk
from .auth import current_user, require_admin
from .storage import load_pools
from .util import DEV

router = APIRouter(prefix="/api/apps", tags=["apps"])
CATALOG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "catalog")
META = ".cynthology-app.json"
_dev_installed: dict[str, dict] = {}


def catalog() -> dict[str, dict]:
    out = {}
    for fn in sorted(os.listdir(CATALOG_DIR)):
        if fn.endswith(".json") and fn != "alternatives.json":
            with open(os.path.join(CATALOG_DIR, fn)) as f:
                meta = json.load(f)
            out[meta["id"]] = meta
    return out


def project(app_id: str) -> str:
    return f"app-{app_id}"


def _app_dir(app_id: str, create: bool = False) -> str:
    root = ct.projects_root(create=create)
    return os.path.join(root, project(app_id)) if root else ""


def _install_meta(app_id: str) -> dict | None:
    if DEV:
        return _dev_installed.get(app_id)
    d = _app_dir(app_id)
    if not d or not os.path.isfile(os.path.join(d, "compose.yaml")):
        return None
    try:
        return json.load(open(os.path.join(d, META)))
    except (OSError, ValueError):
        return {}


def _compose_status() -> dict[str, str]:
    if DEV:
        return {project(k): "running(1)" for k in _dev_installed}
    try:
        return {p["Name"]: p.get("Status", "") for p in json.loads(ct.docker("compose", "ls", "-a", "--format", "json", check=False) or "[]")}
    except (HTTPException, ValueError):
        return {}


def _lan_ip() -> str:
    try:
        from .sysinfo import lan_ips
        ips = lan_ips()
        if ips:
            return ips[0]
    except Exception:
        pass
    return socket.gethostname()


def _timezone() -> str:
    try:
        return os.path.realpath("/etc/localtime").split("zoneinfo/", 1)[1]
    except (IndexError, OSError):
        return "UTC"


def fill(text: str, secret: str, share_path=None, fields: dict | None = None) -> str:
    """Substitute catalog placeholders. share_path(name) -> path is only called for ${SHARE:x}."""
    text = re.sub(r"\$\{F:([a-z0-9_]+)\}", lambda m: (fields or {}).get(m.group(1), ""), text)
    text = re.sub(r"\$\{SHARE:([a-z0-9_-]+)\}", lambda m: share_path(m.group(1)) if share_path else m.group(0), text)
    for k, v in {"SECRET": secret, "HOST": _lan_ip(), "HOSTNAME": socket.gethostname(), "TZ": _timezone()}.items():
        text = text.replace("${" + k + "}", v)
    return text


def host_ports(meta: dict) -> set[int]:
    ports: set[int] = set()
    for a, b in re.findall(r'^\s*-\s*"?(\d+)(?:-(\d+))?:\d+', meta["compose"], re.M):
        ports.update(range(int(a), int(b or a) + 1))
    if "network_mode: host" in meta["compose"] and meta.get("port"):
        ports.add(int(meta["port"]))
    return ports


def listening_ports() -> set[int]:
    try:
        return {c.laddr.port for c in psutil.net_connections(kind="inet") if c.status in ("LISTEN", "NONE") and c.laddr}
    except (psutil.AccessDenied, OSError):
        return set()


_docker_ver: list = []


def docker_version() -> str:
    if DEV:
        return "26.1.5"
    if not _docker_ver:
        try:
            _docker_ver.append(ct.docker("version", "--format", "{{.Server.Version}}", timeout=10).strip())
        except HTTPException:
            return ""
    return _docker_ver[0]


def blocked(meta: dict) -> str | None:
    """Why an app can't be installed on this NAS (e.g. it needs a newer Docker than Debian ships)."""
    need = meta.get("min_docker")
    have = docker_version()
    if need and have and spk.vkey(have) < spk.vkey(need):
        return (f"Needs Docker {need} or newer; this NAS has Docker {have} (Debian's version). "
                "It will work once Cynthology can use a newer Docker.")
    return None


def _app_view(meta: dict, status: dict) -> dict:
    inst = _install_meta(meta["id"])
    st = status.get(project(meta["id"]), "")
    out = {k: meta.get(k) for k in ("id", "name", "description", "port", "path", "scheme", "holds_data")}
    out["blocked"] = None if inst is not None else blocked(meta)
    out["fields"] = [{**f, "default": fill(f.get("default", ""), "")} for f in meta.get("fields", [])]
    out.update(installed=inst is not None, running="running" in st, status=st or ("not started" if inst is not None else ""),
               project=project(meta["id"]), notes=(inst or {}).get("notes"))
    return out


@router.get("")
def list_apps(user=Depends(current_user)):
    status = _compose_status()
    views = [_app_view(m, status) for m in catalog().values()]
    if not user.get("admin"):            # notes hold generated passwords (database root etc.)
        for v in views:
            v["notes"] = None
    return views


# ---------- Synology app alternatives ----------

def _spk_index() -> dict[str, dict]:
    """Best offer per package across the package sources: newest compatible, else newest (with why_not)."""
    best: dict[str, dict] = {}
    for src in spk.load_sources():
        try:
            feed = spk.fetch_feed(src)
        except Exception:
            continue
        for p in feed:
            cur = best.get(p["package"])
            if cur is None or (p["compatible"], spk.vkey(p["version"])) > (cur["compatible"], spk.vkey(cur["version"])):
                best[p["package"]] = p
    return best


@router.get("/alternatives")
def alternatives(user=Depends(current_user)):
    alts = json.load(open(os.path.join(CATALOG_DIR, "alternatives.json")))
    apps = catalog()
    status = _compose_status()
    offers = _spk_index()
    installed = {p["package"]: p for p in spk.installed_list()}
    for a in alts:
        for o in a["options"]:
            if o["kind"] == "spk":
                p, i = offers.get(o["package"]), installed.get(o["package"])
                o.update(name=(p or {}).get("dname") or (i or {}).get("dname") or o["package"],
                         description=(p or {}).get("desc") or (i or {}).get("desc", ""), thumbnail=(p or {}).get("thumbnail"),
                         version=(p or {}).get("version"), source=(p or {}).get("source"),
                         available=bool(p and p["compatible"]), why_not=(p or {}).get("why_not"), installed=i is not None, running=bool(i and i.get("running")),
                         url=(i or {}).get("url"))
            elif o["kind"] == "container":
                o.update(_app_view(apps[o["app"]], status), available=True)
                if not user.get("admin"):
                    o["notes"] = None
            else:
                o.update(available=True, installed=True)
    return alts


class InstallBody(BaseModel):
    fields: dict[str, str] = {}


def answers(meta: dict, given: dict[str, str]) -> dict[str, str]:
    out = {}
    for f in meta.get("fields", []):
        v = (given.get(f["key"]) or fill(f.get("default", ""), "")).strip()
        if not v or not re.match(f.get("pattern", r"^[^\n\r$`\\\"']{1,200}$"), v) or any(c in v for c in "\n\r$`\"'\\"):
            raise HTTPException(400, f"please check: {f['label']}")
        out[f["key"]] = v
    return out


@router.post("/{app_id}/install")
def install(app_id: str, body: InstallBody | None = None, _=Depends(require_admin)):
    meta = catalog().get(app_id)
    if not meta:
        raise HTTPException(404, "unknown app")
    if not load_pools():
        raise HTTPException(400, "create a storage pool first")
    if _install_meta(app_id) is not None:
        raise HTTPException(409, f"{meta['name']} is already installed (Container Manager → Projects → {project(app_id)})")
    if blocked(meta):
        raise HTTPException(400, blocked(meta))
    fields = answers(meta, (body or InstallBody()).fields)
    secret = secrets.token_urlsafe(15).replace("-", "x").replace("_", "y")
    if not DEV:   # reinstall over kept data: reuse the passwords its databases were created with
        try:
            kept = json.load(open(os.path.join(_app_dir(app_id), META)))["secret"]
            if isinstance(kept, str) and re.fullmatch(r"[A-Za-z0-9]{8,64}", kept):   # (it goes into YAML)
                secret = kept
        except (OSError, ValueError, KeyError, TypeError):
            pass
    notes = fill(meta.get("notes", ""), secret, fields=fields)
    if DEV:
        _dev_installed[app_id] = {"notes": notes}
        print(f"[dev] would install {app_id}:\n{fill(meta['compose'], secret, lambda n: '/volume1/' + n, fields)}")
        return {"ok": True, "job": None, "notes": notes}
    busy = host_ports(meta) & (listening_ports() | set(ct.used_host_ports()))
    if busy:
        raise HTTPException(409, f"port(s) {', '.join(map(str, sorted(busy)))} already in use on this NAS "
                                 "(another app, Web Station or a system service). Stop it first, or install the app "
                                 "from Container Manager with different ports.")
    compose = fill(meta["compose"], secret, lambda n: shares_mod.ensure_share(n, [], f"Created for {meta['name']}"), fields)
    d = _app_dir(app_id, create=True)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "compose.yaml"), "w") as f:
        f.write(compose)
    with open(os.path.join(d, META), "w") as f:
        json.dump({"id": app_id, "notes": notes, "secret": secret, "holds_data": bool(meta.get("holds_data"))}, f)
    os.chmod(os.path.join(d, META), 0o600)
    jid = ct.start_job(f"Install {meta['name']}", ["docker", "compose", "-p", project(app_id), "up", "-d", "--no-recreate"], cwd=d)
    return {"ok": True, "job": jid, "notes": notes}


@router.post("/{app_id}/uninstall")
def uninstall(app_id: str, delete_data: bool = False, _=Depends(require_admin)):
    meta = catalog().get(app_id)
    if not meta:
        raise HTTPException(404, "unknown app")
    if DEV:
        _dev_installed.pop(app_id, None)
        return {"ok": True}
    d = _app_dir(app_id)
    if not d or not os.path.isdir(d):
        raise HTTPException(404, "not installed")
    if meta.get("holds_data") and not delete_data:
        # its data is inside the container: stop it and keep it (Container Manager can start it again)
        subprocess.run(["docker", "compose", "-p", project(app_id), "stop"], cwd=d, capture_output=True, timeout=300)
        return {"ok": True, "kept": True}
    subprocess.run(["docker", "compose", "-p", project(app_id), "down"], cwd=d, capture_output=True, timeout=300)
    if delete_data:
        shutil.rmtree(d, ignore_errors=True)
    else:   # keep the data for a later reinstall
        os.replace(os.path.join(d, "compose.yaml"), os.path.join(d, "compose.yaml.removed"))
    return {"ok": True}
