"""Cloud Sync: keep a folder on the NAS in sync with a cloud service (rclone).

Connections (stored here, secrets 0600) become sections of a generated rclone.conf:
  drive / onedrive / dropbox / box  - signed in through the provider's own page (OAuth, see below)
  s3     - Amazon S3 and compatibles (Wasabi, Cloudflare R2, MinIO, ...)
  b2     - Backblaze B2
  webdav - Nextcloud, ownCloud or any WebDAV server
  sftp   - any SSH server
Tasks pair a folder in a shared folder with a folder of a connection:
  upload   - NAS -> cloud      download - cloud -> NAS      both - two-way (rclone bisync)
"mirror" makes a one-way task delete what was deleted on the source; otherwise it only adds/updates.
Runs happen in transient systemd units (`python3 -m cynthology.cloudsync run <id>`); the
cynthology-cloudsync timer starts scheduled ones (`... due`, every 5 minutes).

Signing in (OAuth) without a browser on the NAS: `rclone authorize <type>` listens on
127.0.0.1:53682 on the NAS. We fetch its start page to get the provider's sign-in address and give
that to the user; after they approve, the provider sends their browser to http://localhost:53682/?code=...
- which doesn't load (it's their own computer). They paste that address back, and we pass the code
and state on to rclone on the NAS, which trades them for a token. The token never reaches the browser.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import shares as shares_mod
from .auth import require_admin
from .util import DEV, STATE_DIR, one_line, run

router = APIRouter(prefix="/api/cloudsync", tags=["cloudsync"])
DIR = os.path.join(STATE_DIR, "cloudsync")
CONNS = os.path.join(DIR, "connections.json")
TASKS = os.path.join(DIR, "tasks.json")
CONF = os.path.join(DIR, "rclone.conf")
OAUTH_TYPES = {"drive": "Google Drive", "onedrive": "Microsoft OneDrive", "dropbox": "Dropbox", "box": "Box"}
TYPES = {**OAUTH_TYPES, "s3": "Amazon S3 / S3-compatible", "b2": "Backblaze B2", "webdav": "WebDAV (Nextcloud, ownCloud…)",
         "sftp": "SFTP (SSH server)"}
FIELDS = {        # (name, secret?) per type - everything else is refused
    "drive": [("root_folder_id", False)],
    "onedrive": [],
    "dropbox": [],
    "box": [],
    "s3": [("provider", False), ("endpoint", False), ("region", False), ("access_key_id", False), ("secret_access_key", True)],
    "b2": [("account", False), ("key", True)],
    "webdav": [("url", False), ("vendor", False), ("user", False), ("pass", True)],
    "sftp": [("host", False), ("port", False), ("user", False), ("pass", True)],
}
OBSCURED = {("webdav", "pass"), ("sftp", "pass")}       # rclone wants these "obscured"
EXCLUDES = ["#recycle/**", ".snapshots/**", "@eaDir/**", ".DS_Store", "Thumbs.db", "desktop.ini"]
_lock = threading.Lock()
os.makedirs(DIR, mode=0o700, exist_ok=True)


# ---------- storage ----------

def _load(path: str) -> list[dict]:
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _save(path: str, data: list[dict]) -> None:
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(path + ".tmp", path)


def load_conns() -> list[dict]:
    return _load(CONNS)


def load_tasks() -> list[dict]:
    return _load(TASKS)


def _get(items: list[dict], iid: str, what: str) -> dict:
    x = next((i for i in items if i["id"] == iid), None)
    if not x:
        raise HTTPException(404, f"no such {what}")
    return x


def _status_file(tid: str) -> str:
    return os.path.join(DIR, f"{tid}.status.json")


def _log_file(tid: str) -> str:
    return os.path.join(DIR, f"{tid}.log")


def read_status(tid: str) -> dict:
    try:
        with open(_status_file(tid)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"state": "never"}


def write_status(tid: str, **values) -> dict:
    s = read_status(tid)
    s.update(values)
    with open(_status_file(tid), "w") as f:
        json.dump(s, f)
    return s


def _remote(cid: str) -> str:
    return f"c{cid}"


def write_conf(path: str | None = None) -> str:
    """rclone.conf from the connections (rewritten before every use; root-only)."""
    path = path or CONF
    out = []
    for c in load_conns():
        out.append(f"[{_remote(c['id'])}]")
        out.append(f"type = {c['type']}")
        for k, v in sorted(c.get("cfg", {}).items()):
            if v not in ("", None):
                out.append(f"{k} = {v}")
        if c["type"] == "drive":
            out.append("scope = drive")
        if c["type"] == "s3" and not c.get("cfg", {}).get("provider"):
            out.append("provider = Other")
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(out) + "\n")
    os.replace(path + ".tmp", path)
    return path


def rclone(*args: str, timeout: int | None = 60, input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["rclone", "--config", write_conf(), *args], capture_output=True, text=True, timeout=timeout, input=input)


def _obscure(pw: str) -> str:
    r = subprocess.run(["rclone", "obscure", "-"], input=pw, capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise HTTPException(500, "rclone obscure failed: " + r.stderr.strip()[-200:])
    return r.stdout.strip()


# ---------- OAuth (sign in with the provider) ----------

OAUTH: dict[str, dict] = {}


def _oauth_cleanup() -> None:
    for sid, o in list(OAUTH.items()):
        if time.time() - o["started"] > 900:
            if o["proc"].poll() is None:
                o["proc"].kill()
            OAUTH.pop(sid, None)


class OAuthStart(BaseModel):
    type: str


@router.post("/oauth/start")
def oauth_start(body: OAuthStart, _=Depends(require_admin)):
    if body.type not in OAUTH_TYPES:
        raise HTTPException(400, "this service doesn't use a sign-in page")
    with _lock:
        _oauth_cleanup()
        for o in OAUTH.values():                   # (rclone's helper needs port 53682: one sign-in at a time)
            if o["proc"].poll() is None:
                o["proc"].kill()
        OAUTH.clear()
        p = subprocess.Popen(["rclone", "authorize", body.type, "--auth-no-open-browser", "--config", "/dev/null"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        o = {"proc": p, "out": [], "started": time.time(), "type": body.type}

        def reader():
            for line in p.stdout:
                o["out"].append(line)
        threading.Thread(target=reader, daemon=True).start()
        local = None
        for _ in range(100):
            m = re.search(r"http://127\.0\.0\.1:53682/auth\?state=[A-Za-z0-9_-]+", "".join(o["out"]))
            if m:
                local = m.group(0)
                break
            if p.poll() is not None:
                break
            time.sleep(0.1)
        if not local:
            p.kill()
            raise HTTPException(500, "couldn't start the sign-in helper: " + "".join(o["out"])[-300:])

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            urllib.request.build_opener(NoRedirect).open(local, timeout=10)
            raise HTTPException(500, "the sign-in helper didn't send us to the provider")
        except urllib.error.HTTPError as e:
            target = e.headers.get("Location", "")
        if not target.startswith("https://"):
            p.kill()
            raise HTTPException(500, "the sign-in helper didn't send us to the provider")
        o["state"] = urllib.parse.parse_qs(urllib.parse.urlparse(target).query).get("state", [""])[0]
        sid = secrets.token_urlsafe(16)
        OAUTH[sid] = o
    return {"session": sid, "url": target}


class OAuthFinish(BaseModel):
    session: str
    url: str


@router.post("/oauth/finish")
def oauth_finish(body: OAuthFinish, _=Depends(require_admin)):
    o = OAUTH.get(body.session)
    if not o or o["proc"].poll() is not None and "token" not in o:
        raise HTTPException(400, "this sign-in expired - start again")
    if "token" in o:
        return {"ok": True}
    q = urllib.parse.parse_qs(urllib.parse.urlparse(body.url.strip()).query)
    if q.get("error"):
        raise HTTPException(400, "the provider said: " + one_line(" ".join(q.get("error_description", q["error"])), 200))
    code, state = q.get("code", [""])[0], q.get("state", [""])[0]
    if not code or state != o.get("state"):
        raise HTTPException(400, "that isn't the address from this sign-in: paste the whole address shown after you approved")
    try:
        urllib.request.urlopen("http://127.0.0.1:53682/?" + urllib.parse.urlencode({"state": state, "code": code}), timeout=30).read()
    except Exception:
        pass                                       # (rclone may close the page before answering)
    try:
        o["proc"].wait(timeout=40)
    except subprocess.TimeoutExpired:
        o["proc"].kill()
    text = "".join(o["out"])
    m = re.search(r"--->\s*(\{.*?\})\s*<---", text, re.S)
    if not m:
        raise HTTPException(400, "the provider didn't accept the sign-in: " + one_line(text[-300:], 300))
    o["token"] = m.group(1)
    return {"ok": True}


def _onedrive_drive(token: str) -> dict:
    """rclone needs the drive's id and type after signing in to OneDrive."""
    access = json.loads(token)["access_token"]
    req = urllib.request.Request("https://graph.microsoft.com/v1.0/me/drive", headers={"Authorization": f"Bearer {access}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    return {"drive_id": d["id"], "drive_type": d.get("driveType", "personal")}


# ---------- connections ----------

class Connection(BaseModel):
    name: str
    type: str
    cfg: dict[str, str] = {}
    oauth_session: str = ""


def _clean_cfg(c: Connection, old: dict | None) -> dict:
    if c.type not in TYPES:
        raise HTTPException(400, "unknown service")
    allowed = dict(FIELDS[c.type])
    cfg = {}
    for k, v in c.cfg.items():
        if k not in allowed:
            raise HTTPException(400, f"unknown setting '{k}'")
        v = str(v)
        if v != v.strip() or any(ord(ch) < 32 for ch in v) or len(v) > 2000:
            raise HTTPException(400, f"{k}: one line, no spaces at the ends")
        if allowed[k] and v == "" and old:          # secret left empty while editing: keep the old one
            if k in old.get("cfg", {}):
                cfg[k] = old["cfg"][k]
            continue
        cfg[k] = _obscure(v) if (c.type, k) in OBSCURED and v else v
    if c.type == "sftp":
        if cfg.get("port") and not cfg["port"].isdigit():
            raise HTTPException(400, "port: a number")
        if not re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", cfg.get("host", "")):
            raise HTTPException(400, "server: a host name or IP address")
    if c.type == "webdav" and cfg.get("url") and not re.match(r"https?://", cfg["url"]):
        raise HTTPException(400, "WebDAV address: starts with https:// (or http://)")
    if c.type == "webdav" and cfg.get("vendor", "other") not in ("nextcloud", "owncloud", "sharepoint", "other"):
        raise HTTPException(400, "unknown WebDAV server type")
    if c.type in OAUTH_TYPES:
        o = OAUTH.get(c.oauth_session) if c.oauth_session else None
        if o and o.get("type") == c.type and o.get("token"):
            cfg["token"] = o["token"]
            if c.type == "onedrive":
                try:
                    cfg.update(_onedrive_drive(o["token"]))
                except Exception as e:
                    raise HTTPException(502, f"signed in, but couldn't read your OneDrive: {e}")
            OAUTH.pop(c.oauth_session, None)
        elif old and old.get("type") == c.type and old.get("cfg", {}).get("token"):
            cfg = {**{k: v for k, v in old["cfg"].items() if k in ("token", "drive_id", "drive_type")}, **cfg}
        else:
            raise HTTPException(400, f"sign in to {OAUTH_TYPES[c.type]} first")
    return cfg


def _public_conn(c: dict) -> dict:
    secret = {k for k, s in FIELDS.get(c["type"], []) if s} | {"token"}
    return {"id": c["id"], "name": c["name"], "type": c["type"], "type_name": TYPES.get(c["type"], c["type"]),
            "cfg": {k: v for k, v in c.get("cfg", {}).items() if k not in secret | {"drive_id", "drive_type", "known_hosts_file"}},
            "signed_in": bool(c.get("cfg", {}).get("token"))}


def _pin_host_key(c: dict, old: dict | None) -> None:
    """SFTP: remember the server's key the first time (or when the server changes), check it afterwards."""
    if c["type"] != "sftp":
        return
    path = os.path.join(DIR, f"known_hosts-{c['id']}")
    same = old and old.get("type") == "sftp" and (old["cfg"].get("host"), old["cfg"].get("port")) == (c["cfg"].get("host"), c["cfg"].get("port"))
    if not (same and os.path.exists(path)) and not DEV:
        r = subprocess.run(["ssh-keyscan", "-T", "10", "-p", c["cfg"].get("port") or "22", "--", c["cfg"]["host"]],
                           capture_output=True, text=True, timeout=30)
        if not r.stdout.strip():
            raise HTTPException(400, f"couldn't reach the SSH server {c['cfg']['host']}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(r.stdout)
    c["cfg"]["known_hosts_file"] = path


@router.post("/connections")
def create_conn(body: Connection, _=Depends(require_admin)):
    with _lock:
        conns = load_conns()
        c = {"id": secrets.token_hex(4), "name": one_line(body.name, 60) or TYPES.get(body.type, "cloud"), "type": body.type,
             "cfg": _clean_cfg(body, None)}
        _pin_host_key(c, None)
        conns.append(c)
        _save(CONNS, conns)
    return _public_conn(c)


@router.put("/connections/{cid}")
def update_conn(cid: str, body: Connection, _=Depends(require_admin)):
    with _lock:
        conns = load_conns()
        c = _get(conns, cid, "connection")
        old = json.loads(json.dumps(c))
        c.update(name=one_line(body.name, 60) or c["name"], type=body.type, cfg=_clean_cfg(body, c))
        _pin_host_key(c, old)
        _save(CONNS, conns)
    return _public_conn(c)


@router.delete("/connections/{cid}")
def delete_conn(cid: str, _=Depends(require_admin)):
    with _lock:
        users = [t["name"] for t in load_tasks() if t["connection"] == cid]
        if users:
            raise HTTPException(409, "still used by: " + ", ".join(users))
        _save(CONNS, [c for c in load_conns() if c["id"] != cid])
    return {"ok": True}


def _remote_path(p: str) -> str:
    # (a leading / matters for SFTP: absolute instead of the user's home folder)
    p = (p or "").strip()
    p = ("/" if p.startswith("/") else "") + p.strip("/")
    if any(part in ("..", ".") for part in p.split("/")) or any(ord(ch) < 32 for ch in p) or len(p) > 1000:
        raise HTTPException(400, "cloud folder: a plain path like Backups/NAS")
    return p


@router.get("/connections/{cid}/browse")
def browse(cid: str, path: str = "", _=Depends(require_admin)):
    """Folders at a path of the connection (for picking the cloud folder) - also tests the connection."""
    _get(load_conns(), cid, "connection")
    p = _remote_path(path)
    if DEV:
        return {"path": p, "folders": ["Documents", "Photos"] if not p else []}
    try:
        r = rclone("lsjson", "--dirs-only", "--no-modtime", "--no-mimetype", f"{_remote(cid)}:{p}", timeout=60)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "the service didn't answer within a minute")
    if r.returncode != 0:
        raise HTTPException(400, "couldn't open it: " + _err(r.stderr))
    return {"path": p, "folders": sorted(d["Name"] for d in json.loads(r.stdout or "[]"))}


def _err(text: str) -> str:
    lines = [ln for ln in text.splitlines() if "ERROR" in ln or "Failed" in ln or "error" in ln.lower()]
    return one_line((lines[-1] if lines else text.strip()[-300:]).split(": ", 1)[-1] if lines else text.strip()[-300:], 300)


# ---------- tasks ----------

class Task(BaseModel):
    name: str
    connection: str
    share: str
    local_path: str = ""
    remote_path: str = ""
    direction: str = "upload"            # upload | download | both
    mirror: bool = False                 # one-way: also delete what was deleted on the source
    every: int = 60                      # minutes between runs, 0 = only by hand
    bwlimit: str = ""                    # e.g. 2M (rclone syntax), empty = unlimited
    enabled: bool = True


def local_dir(t: dict) -> str:
    share = next((s for s in shares_mod.load() if s["name"] == t["share"]), None)
    if not share:
        raise HTTPException(400, f"shared folder '{t['share']}' doesn't exist")
    if share.get("encrypted") and not shares_mod.unlocked(share):
        raise HTTPException(400, f"shared folder '{t['share']}' is locked (encrypted)")
    root = os.path.realpath(share["path"])
    full = os.path.realpath(os.path.join(root, t.get("local_path", "").strip("/")))
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(400, "folder must be inside the shared folder")
    return full


def _validate(body: Task) -> dict:
    _get(load_conns(), body.connection, "connection")
    if body.direction not in ("upload", "download", "both"):
        raise HTTPException(400, "direction: upload, download or both")
    if body.every not in (0, 5, 15, 30, 60, 180, 360, 720, 1440):
        raise HTTPException(400, "unsupported schedule")
    if body.bwlimit and not re.fullmatch(r"\d{1,6}[KMG]?", body.bwlimit):
        raise HTTPException(400, "speed limit: e.g. 500K or 2M")
    lp = body.local_path.strip().strip("/")
    if any(part in ("..", ".") for part in lp.split("/")) or any(ord(ch) < 32 for ch in lp):
        raise HTTPException(400, "NAS folder: a plain path inside the shared folder")
    d = {"name": one_line(body.name, 80) or "Cloud sync", "connection": body.connection, "share": body.share,
         "local_path": lp, "remote_path": _remote_path(body.remote_path), "direction": body.direction,
         "mirror": body.mirror, "every": body.every, "bwlimit": body.bwlimit, "enabled": body.enabled}
    local_dir(d)
    return d


def _public_task(t: dict) -> dict:
    conn = next((c for c in load_conns() if c["id"] == t["connection"]), {})
    return {**t, "connection_name": conn.get("name", "?"), "connection_type": conn.get("type", ""),
            "status": read_status(t["id"]), "running": _running(t["id"])}


@router.get("")
def overview(_=Depends(require_admin)):
    return {"types": TYPES, "oauth_types": list(OAUTH_TYPES), "connections": [_public_conn(c) for c in load_conns()],
            "tasks": [_public_task(t) for t in load_tasks()]}


@router.post("/tasks")
def create_task(body: Task, _=Depends(require_admin)):
    with _lock:
        tasks = load_tasks()
        t = {"id": secrets.token_hex(4), **_validate(body)}
        tasks.append(t)
        _save(TASKS, tasks)
    return _public_task(t)


@router.put("/tasks/{tid}")
def update_task(tid: str, body: Task, _=Depends(require_admin)):
    with _lock:
        tasks = load_tasks()
        t = _get(tasks, tid, "task")
        new = _validate(body)
        if (new["direction"], new["share"], new["local_path"], new["connection"], new["remote_path"]) != \
           (t["direction"], t["share"], t["local_path"], t["connection"], t["remote_path"]):
            write_status(tid, resynced=False)       # (two-way: the next run compares both sides afresh)
        t.update(new)
        _save(TASKS, tasks)
    return _public_task(t)


@router.delete("/tasks/{tid}")
def delete_task(tid: str, _=Depends(require_admin)):
    stop(tid, _)
    with _lock:
        _save(TASKS, [t for t in load_tasks() if t["id"] != tid])
    for f in (_status_file(tid), _log_file(tid)):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass
    run(["rm", "-rf", os.path.join(DIR, f"bisync-{tid}")], check=False)
    return {"ok": True}


def _unit(tid: str) -> str:
    return f"cyn-cloudsync-{tid}"


def _running(tid: str) -> bool:
    if DEV:
        return False
    r = subprocess.run(["systemctl", "is-active", _unit(tid)], capture_output=True, text=True)
    return r.stdout.strip() in ("active", "activating")


def start(tid: str) -> None:
    if DEV:
        return
    if _running(tid):
        raise HTTPException(409, "already running")
    run(["systemd-run", f"--unit={_unit(tid)}", "--collect", "--property=Nice=10", "--property=IOSchedulingClass=idle",
         f"--setenv=CYN_STATE={STATE_DIR}", "--working-directory=/opt/cynthology/backend",
         "/usr/bin/python3", "-m", "cynthology.cloudsync", "run", tid])


@router.post("/tasks/{tid}/run")
def run_now(tid: str, _=Depends(require_admin)):
    _get(load_tasks(), tid, "task")
    start(tid)
    return {"ok": True}


@router.post("/tasks/{tid}/stop")
def stop(tid: str, _=Depends(require_admin)):
    if not DEV and _running(tid):
        run(["systemctl", "stop", _unit(tid)], check=False)
    return {"ok": True}


@router.get("/tasks/{tid}/log")
def log(tid: str, _=Depends(require_admin)):
    _get(load_tasks(), tid, "task")
    try:
        with open(_log_file(tid), errors="replace") as f:
            return {"log": f.read()[-60000:]}
    except FileNotFoundError:
        return {"log": ""}


def command(t: dict, resync: bool = False) -> list[str]:
    """The rclone command line for a task (without the --config part)."""
    local = local_dir(t)
    remote = f"{_remote(t['connection'])}:{t['remote_path']}"
    common = ["--log-file", _log_file(t["id"]), "--log-level", "INFO", "--stats", "0", "--retries", "3",
              "--low-level-retries", "10", "--transfers", "4", "--checkers", "8"]
    for x in EXCLUDES:
        common += ["--exclude", x]
    if t.get("bwlimit"):
        common += ["--bwlimit", t["bwlimit"]]
    if t["direction"] == "both":
        cmd = ["bisync", local, remote, "--workdir", os.path.join(DIR, f"bisync-{t['id']}")]
        if resync:
            cmd.append("--resync")
        return cmd + common
    verb = "sync" if t.get("mirror") else "copy"
    src, dst = (local, remote) if t["direction"] == "upload" else (remote, local)
    return [verb, src, dst, "--create-empty-src-dirs"] + common


def sandbox(t: dict) -> list[str]:
    """rclone runs as root but may only write the task's folder (and its own log, state and config): a link
    a user puts into the shared folder - say to /etc - must never let a download write there."""
    if DEV:
        return []
    work = os.path.join(DIR, f"bisync-{t['id']}")
    os.makedirs(work, mode=0o700, exist_ok=True)
    rw = [local_dir(t), work, _log_file(t["id"])]      # (its own copy of rclone.conf is in work)
    return ["systemd-run", "--quiet", "--pipe", "--wait", "--collect", "--service-type=exec",
            "--property=ProtectSystem=strict", "--property=ProtectHome=tmpfs", "--property=PrivateTmp=yes",
            "--property=NoNewPrivileges=yes", "--property=Nice=10", "--property=IOSchedulingClass=idle",
            "--property=UMask=0002", *[f'--property=ReadWritePaths="-{_sd_quote(p)}"' for p in rw], "--"]


def _sd_quote(p: str) -> str:
    return p.replace("\\", "\\\\").replace('"', '\\"')


def do_run(tid: str) -> None:
    t = _get(load_tasks(), tid, "task")
    with open(_log_file(tid), "w"):
        pass
    resync = t["direction"] == "both" and not read_status(tid).get("resynced")
    write_status(tid, state="running", started=int(time.time()), error="")
    os.umask(0o002)                              # (new files: group-writable, like files from SMB)
    try:
        if t["direction"] != "upload":
            os.makedirs(local_dir(t), exist_ok=True)
        os.makedirs(os.path.join(DIR, f"bisync-{tid}"), mode=0o700, exist_ok=True)
        conf = write_conf(os.path.join(DIR, f"bisync-{tid}", "rclone.conf")) if not DEV else write_conf()
        r = subprocess.run(sandbox(t) + ["rclone", "--config", conf, *command(t, resync)], capture_output=True, text=True)
        ok = r.returncode == 0
        logtext = open(_log_file(tid), errors="replace").read()
        counts = {"copied": len(re.findall(r": Copied \(", logtext)), "deleted": len(re.findall(r": Deleted", logtext))}
        err = "" if ok else _err(logtext + "\n" + r.stderr)
        if not ok and "Must run --resync" in logtext:
            # bisync refuses to go on when one side suddenly looks empty or very different (a protection
            # against wiping the other side). The next run compares both sides afresh, which only adds files.
            resync_next = True
            err = ("To protect your files nothing was synced: one side changed too much at once (for example "
                   "it's empty). The next sync copies everything that's on either side; nothing is deleted.")
        else:
            resync_next = False
    except HTTPException as e:
        ok, counts, err, resync_next = False, {}, str(e.detail), False
    write_status(tid, state="ok" if ok else "failed", finished=int(time.time()), error=err, **counts,
                 **({"resynced": True} if ok and resync else {}), **({"resynced": False} if resync_next else {}))
    if not ok:
        from .notify import notify
        notify("warning", f"Cloud sync '{t['name']}' failed", err, event="cloudsync")


def due(now: float | None = None) -> list[str]:
    now = now or time.time()
    out = []
    for t in load_tasks():
        if not t.get("enabled") or not t.get("every"):
            continue
        last = read_status(t["id"]).get("started") or 0
        if now - last >= t["every"] * 60 - 30:
            out.append(t["id"])
    return out


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"]:
        do_run(sys.argv[2])
    elif sys.argv[1:2] == ["due"]:
        for tid in due():
            try:
                start(tid)
            except HTTPException:
                pass                                 # (still running from last time)
