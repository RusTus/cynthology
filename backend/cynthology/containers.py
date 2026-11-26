"""Container Manager: Docker containers, images, Compose projects and registry search.

Drives the `docker` CLI (JSON output) - no extra Python dependencies. Long operations (image pulls,
`compose up`) run as background jobs whose output the UI polls. Compose projects live in the
"docker" shared folder (<volume>/docker/<project>/compose.yaml), so snapshots and backups cover them.
Folder mappings made with the Run wizard are limited to shared folders.
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import shares as shares_mod
from .auth import require_admin
from .storage import load_pools
from .util import DEV, run

router = APIRouter(prefix="/api/containers", tags=["containers"])
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,254}$")
RESERVED_PORTS = {22: "SSH", 139: "SMB", 445: "SMB", 2049: "NFS", 3493: "UPS server", 5000: "Cynthology", 5001: "Cynthology"}


def docker(*args: str, timeout: int = 60, check: bool = True) -> str:
    try:
        r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise HTTPException(503, "Docker is not installed")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Docker did not answer in time")
    if check and r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        if msg and "Cannot connect to the Docker daemon" in msg[-1]:
            raise HTTPException(503, "The container engine is not running (Control Panel → File Services → Container engine)")
        raise HTTPException(400, msg[-1] if msg else f"docker {args[0]} failed")
    return r.stdout


def jlines(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip().startswith("{")]


# ---------- background jobs (pull, compose up, ...) ----------

JOBS: dict[str, dict] = {}


def start_job(title: str, cmd: list[str], cwd: str | None = None) -> str:
    jid = secrets.token_hex(4)
    job = {"id": jid, "title": title, "state": "running", "log": [], "started": time.time()}
    JOBS[jid] = job

    def work():
        try:
            p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in p.stdout:
                job["log"].append(line.rstrip())
                del job["log"][:-400]
            job["state"] = "done" if p.wait() == 0 else "failed"
        except Exception as e:
            job["log"].append(str(e))
            job["state"] = "failed"
        job["finished"] = time.time()

    threading.Thread(target=work, daemon=True).start()
    for k in [k for k, j in JOBS.items() if j.get("finished", time.time()) < time.time() - 3600]:
        del JOBS[k]
    return jid


@router.get("/jobs/{jid}")
def job(jid: str, _=Depends(require_admin)):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "no such job")
    return j


# ---------- overview / containers ----------

@router.get("/overview")
def overview(_=Depends(require_admin)):
    info = json.loads(docker("info", "--format", "{{json .}}"))
    return {"running": info.get("ContainersRunning", 0), "stopped": info.get("ContainersStopped", 0) + info.get("ContainersPaused", 0),
            "images": info.get("Images", 0), "version": info.get("ServerVersion"), "data_root": info.get("DockerRootDir"),
            "cpus": info.get("NCPU"), "memory": info.get("MemTotal")}


def _ports(raw: str) -> list[dict]:
    """'0.0.0.0:8080->80/tcp, [::]:8080->80/tcp' -> [{host: 8080, container: 80, proto: tcp}]"""
    out, seen = [], set()
    for m in re.finditer(r"(?:[\d.:\[\]]+:)?(\d+)->(\d+)/(\w+)", raw or ""):
        key = m.groups()
        if key not in seen:
            seen.add(key)
            out.append({"host": int(m.group(1)), "container": int(m.group(2)), "proto": m.group(3)})
    return out


def _labels(raw: str) -> dict:
    return dict(kv.split("=", 1) for kv in (raw or "").split(",") if "=" in kv)


@router.get("")
def containers(_=Depends(require_admin)):
    rows = jlines(docker("ps", "-a", "--no-trunc", "--format", "{{json .}}"))
    stats = {s["Name"]: s for s in jlines(docker("stats", "--no-stream", "--format", "{{json .}}", timeout=30, check=False))}
    out = []
    for c in rows:
        name = c["Names"].split(",")[0]
        lab = _labels(c.get("Labels", ""))
        st = stats.get(name, {})
        out.append({"id": c["ID"][:12], "name": name, "image": c["Image"], "state": c["State"], "status": c["Status"],
                    "ports": _ports(c.get("Ports", "")), "project": lab.get("com.docker.compose.project"),
                    "cpu": st.get("CPUPerc"), "mem": st.get("MemUsage"), "created": c.get("CreatedAt")})
    return sorted(out, key=lambda c: (c["state"] != "running", c["name"]))


def _cid(ref: str) -> str:
    if not NAME_RE.match(ref):
        raise HTTPException(400, "bad container name")
    return ref


@router.post("/c/{ref}/{action}")
def container_action(ref: str, action: str, _=Depends(require_admin)):
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "unknown action")
    docker(action, _cid(ref), timeout=120)
    return {"ok": True}


@router.delete("/c/{ref}")
def remove(ref: str, _=Depends(require_admin)):
    docker("rm", "-f", _cid(ref), timeout=120)
    return {"ok": True}


@router.get("/c/{ref}/logs")
def logs(ref: str, tail: int = 300, _=Depends(require_admin)):
    r = subprocess.run(["docker", "logs", "--tail", str(min(max(tail, 10), 5000)), "--timestamps", _cid(ref)],
                       capture_output=True, text=True, timeout=30)
    return {"log": (r.stdout + r.stderr)[-200000:]}


def _secret(env: str) -> bool:
    """Hide values of variables whose NAME looks secret (DB_PASSWORD, API_KEY, GITHUB_TOKEN...)."""
    return bool(re.search(r"PASS|SECRET|TOKEN|KEY|CREDENTIAL|PRIVATE", env.split("=", 1)[0], re.I))


@router.get("/c/{ref}/details")
def details(ref: str, _=Depends(require_admin)):
    d = json.loads(docker("inspect", _cid(ref)))[0]
    cfg, hc = d.get("Config", {}), d.get("HostConfig", {})
    return {"name": d["Name"].lstrip("/"), "image": cfg.get("Image"), "created": d.get("Created"),
            "state": d.get("State", {}).get("Status"), "started": d.get("State", {}).get("StartedAt"),
            "restart": hc.get("RestartPolicy", {}).get("Name"), "network": hc.get("NetworkMode"),
            "env": [e if not _secret(e) else e.split("=", 1)[0] + "=••••••" for e in cfg.get("Env", [])],
            "mounts": [{"source": m.get("Source"), "target": m.get("Destination"), "rw": m.get("RW")} for m in d.get("Mounts", [])],
            "ports": [{"container": k, "host": [b.get("HostPort") for b in (v or [])]} for k, v in (d.get("NetworkSettings", {}).get("Ports") or {}).items()],
            "command": " ".join(cfg.get("Cmd") or [])}


# ---------- run a new container ----------

class PortMap(BaseModel):
    host: int
    container: int
    proto: str = "tcp"


class VolMap(BaseModel):
    share_path: str     # "<share>/<sub/folder>" inside a shared folder
    container: str
    readonly: bool = False


class RunSpec(BaseModel):
    image: str
    name: str
    ports: list[PortMap] = []
    volumes: list[VolMap] = []
    env: dict[str, str] = {}
    restart: str = "unless-stopped"
    network: str = "bridge"           # bridge | host
    command: str = ""


def used_host_ports() -> dict[int, str]:
    used = dict(RESERVED_PORTS)
    for c in jlines(docker("ps", "-a", "--format", "{{json .}}")):
        for p in _ports(c.get("Ports", "")):
            used[p["host"]] = f"container {c['Names']}"
    return used


def share_dir(share_path: str) -> str:
    parts = [p for p in share_path.strip("/").split("/") if p]
    if not parts or ".." in parts:
        raise HTTPException(400, f"bad folder: {share_path}")
    share = next((s for s in shares_mod.load() if s["name"] == parts[0]), None)
    if not share:
        raise HTTPException(400, f"folder mappings must be inside a shared folder ({parts[0]} isn't one)")
    path = os.path.join(share["path"], *parts[1:])
    os.makedirs(path, exist_ok=True)
    return path


@router.post("/run")
def run_container(body: RunSpec, _=Depends(require_admin)):
    if not IMAGE_RE.match(body.image):
        raise HTTPException(400, "bad image name")
    _cid(body.name)
    if body.restart not in ("no", "always", "unless-stopped", "on-failure"):
        raise HTTPException(400, "bad restart policy")
    if body.network not in ("bridge", "host"):
        raise HTTPException(400, "network must be bridge or host")
    args = ["run", "-d", "--name", body.name, "--restart", body.restart]
    if body.network == "host":
        args += ["--network", "host"]
    used = used_host_ports()
    for p in body.ports:
        if not (1 <= p.host <= 65535 and 1 <= p.container <= 65535) or p.proto not in ("tcp", "udp"):
            raise HTTPException(400, "ports must be 1-65535, tcp or udp")
        if p.host in used:
            raise HTTPException(409, f"port {p.host} is already used by {used[p.host]}")
        if body.network == "bridge":
            args += ["-p", f"{p.host}:{p.container}/{p.proto}"]
    for v in body.volumes:
        if not v.container.startswith("/") or ":" in v.container:
            raise HTTPException(400, f"container path must be absolute: {v.container}")
        args += ["-v", f"{share_dir(v.share_path)}:{v.container}{':ro' if v.readonly else ''}"]
    for k, val in body.env.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            raise HTTPException(400, f"bad variable name {k}")
        args += ["-e", f"{k}={val}"]
    args.append(body.image)
    if body.command.strip():
        import shlex
        args += shlex.split(body.command)
    cid = docker(*args, timeout=600).strip()
    return {"ok": True, "id": cid[:12]}


# ---------- images ----------

@router.get("/images/list")
def images(_=Depends(require_admin)):
    in_use = {c["Image"] for c in jlines(docker("ps", "-a", "--format", "{{json .}}"))}
    out = []
    for i in jlines(docker("images", "--format", "{{json .}}")):
        ref = f"{i['Repository']}:{i['Tag']}" if i["Tag"] != "<none>" else i["ID"]
        out.append({"id": i["ID"], "repository": i["Repository"], "tag": i["Tag"], "ref": ref, "size": i["Size"],
                    "created": i.get("CreatedSince"), "in_use": ref in in_use or i["Repository"] in in_use})
    return out


class Pull(BaseModel):
    image: str


@router.post("/images/pull")
def pull(body: Pull, _=Depends(require_admin)):
    img = body.image.strip()
    if not IMAGE_RE.match(img):
        raise HTTPException(400, "bad image name (e.g. nginx:latest or ghcr.io/owner/app:1.2)")
    return {"job": start_job(f"Pull {img}", ["docker", "pull", img])}


@router.delete("/images/{ref:path}")
def remove_image(ref: str, _=Depends(require_admin)):
    if not IMAGE_RE.match(ref):
        raise HTTPException(400, "bad image")
    docker("rmi", ref, timeout=120)
    return {"ok": True}


@router.get("/registry/search")
def search(q: str, _=Depends(require_admin)):
    url = "https://hub.docker.com/v2/search/repositories/?" + urllib.parse.urlencode({"query": q, "page_size": 25})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Cynthology"}), timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        raise HTTPException(502, f"Docker Hub search failed: {e}")
    return [{"name": x["repo_name"], "description": x.get("short_description", ""), "stars": x.get("star_count", 0),
             "official": x.get("is_official", False), "pulls": x.get("pull_count", 0)} for x in data.get("results", [])]


# ---------- compose projects ----------

def projects_root(create: bool = False) -> str:
    share = next((s for s in shares_mod.load() if s["name"] == "docker"), None)
    if not share:
        if not create:
            return ""
        share = {"path": shares_mod.ensure_share("docker", [], "Container Manager projects (compose.yaml + data)")}
    return share["path"]


def _project_dir(name: str) -> str:
    if not PROJECT_RE.match(name):
        raise HTTPException(400, "project name: lowercase letters, digits, '-' and '_'")
    root = projects_root()
    if not root:
        raise HTTPException(404, "no projects yet")
    return os.path.join(root, name)


@router.get("/projects/list")
def projects(_=Depends(require_admin)):
    root = projects_root()
    status = {p["Name"]: p for p in json.loads(docker("compose", "ls", "-a", "--format", "json") or "[]")}
    out = []
    for name in sorted(os.listdir(root)) if root and os.path.isdir(root) else []:
        f = os.path.join(root, name, "compose.yaml")
        if PROJECT_RE.match(name) and os.path.isfile(f):
            st = status.get(name, {})
            out.append({"name": name, "path": os.path.join(root, name), "status": st.get("Status", "not started")})
    return out


@router.get("/projects/{name}")
def get_project(name: str, _=Depends(require_admin)):
    d = _project_dir(name)
    try:
        return {"name": name, "yaml": open(os.path.join(d, "compose.yaml")).read()}
    except OSError:
        raise HTTPException(404, "no such project")


class Project(BaseModel):
    name: str
    yaml: str
    start: bool = True


@router.put("/projects/save")
def save_project(body: Project, _=Depends(require_admin)):
    if not PROJECT_RE.match(body.name):
        raise HTTPException(400, "project name: lowercase letters, digits, '-' and '_'")
    d = os.path.join(projects_root(create=True), body.name)
    if _holds_data(d):
        raise HTTPException(400, f"{body.name} keeps its data inside its container; changing its compose.yaml would recreate it and erase everything.")
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, "compose.yaml")
    with open(f + ".new", "w") as fh:
        fh.write(body.yaml)
    # let compose validate before replacing the working file
    r = subprocess.run(["docker", "compose", "-f", f + ".new", "-p", body.name, "config", "-q"], capture_output=True, text=True, cwd=d)
    if r.returncode != 0:
        os.remove(f + ".new")
        raise HTTPException(400, "compose.yaml is not valid: " + (r.stderr.strip().splitlines() or ["?"])[-1])
    os.replace(f + ".new", f)
    job_id = start_job(f"Build {body.name}", ["docker", "compose", "-p", body.name, "up", "-d", "--remove-orphans"], cwd=d) if body.start else None
    return {"ok": True, "path": d, "job": job_id}


def _holds_data(d: str) -> bool:
    try:
        return bool(json.load(open(os.path.join(d, ".cynthology-app.json"))).get("holds_data"))
    except (OSError, ValueError):
        return False


@router.post("/projects/{name}/{action}")
def project_action(name: str, action: str, _=Depends(require_admin)):
    d = _project_dir(name)
    cmds = {"up": ["up", "-d", "--remove-orphans"], "down": ["down"], "restart": ["restart"],
            "update": None}
    if action not in cmds:
        raise HTTPException(400, "unknown action")
    if _holds_data(d):   # never remove/recreate a container that holds the app's data
        if action == "update":
            raise HTTPException(400, f"{name} keeps its data inside its container; recreating it would erase everything. "
                                     "Update it from inside the app instead.")
        cmds.update(down=["stop"], up=["up", "-d", "--no-recreate"])
    base = ["docker", "compose", "-p", name]
    if action == "update":   # pull newer images, then recreate what changed
        cmd = ["sh", "-c", f"{' '.join(base)} pull && {' '.join(base)} up -d --remove-orphans"]
    else:
        cmd = base + cmds[action]
    return {"job": start_job(f"{action} {name}", cmd, cwd=d)}


@router.get("/projects/{name}/logs")
def project_logs(name: str, _=Depends(require_admin)):
    d = _project_dir(name)
    r = subprocess.run(["docker", "compose", "-p", name, "logs", "--tail", "300", "--timestamps", "--no-color"],
                       capture_output=True, text=True, timeout=30, cwd=d)
    return {"log": (r.stdout + r.stderr)[-200000:]}


@router.delete("/projects/{name}")
def delete_project(name: str, delete_files: bool = False, _=Depends(require_admin)):
    d = _project_dir(name)
    subprocess.run(["docker", "compose", "-p", name, "down"], capture_output=True, cwd=d, timeout=300)
    if delete_files:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


# ---------- settings: where Docker keeps its data ----------

class DataRoot(BaseModel):
    pool_id: int


@router.post("/settings/data-root")
def set_data_root(body: DataRoot, _=Depends(require_admin)):
    """Move Docker's storage from the boot disk to a pool. Only while Docker is still empty."""
    info = json.loads(docker("info", "--format", "{{json .}}"))
    if info.get("Containers") or info.get("Images"):
        raise HTTPException(409, "remove all containers and images first - existing ones are not moved")
    pool = next((p for p in load_pools() if p["id"] == body.pool_id), None)
    if not pool:
        raise HTTPException(404, "no such pool")
    root = os.path.join(pool["mount"], "@docker")
    path = "/etc/docker/daemon.json"
    cfg = {}
    if os.path.exists(path):
        try:
            cfg = json.load(open(path))
        except ValueError:
            pass
    cfg["data-root"] = root
    if not DEV:
        backup = open(path).read() if os.path.exists(path) else None
        os.makedirs(root, mode=0o710, exist_ok=True)
        os.makedirs("/etc/docker", exist_ok=True)
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2)
        if not _restart_docker():
            # put the old configuration back so Docker keeps working
            if backup is None:
                os.remove(path)
            else:
                with open(path, "w") as f:
                    f.write(backup)
            _restart_docker()
            raise HTTPException(500, "Docker did not start with the new location - the change was undone")
    return {"ok": True, "data_root": root}


def _restart_docker() -> bool:
    # a few quick restarts in a row trip systemd's start limit; clear it first
    subprocess.run(["systemctl", "reset-failed", "docker.service", "docker.socket"], capture_output=True)
    subprocess.run(["systemctl", "restart", "docker"], capture_output=True, timeout=120)
    for _ in range(20):
        if subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0:
            return True
        time.sleep(1)
    return False
