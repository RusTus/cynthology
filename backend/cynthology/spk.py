"""Package Center: installs Synology-format .spk packages (e.g. SynoCommunity) on Cynthology.

Compatibility layer, written from the public SPK format (no Synology code):
  * package sources speak the DSM package-feed protocol (form POST -> {"packages": [...]})
  * DSM 7 directory layout: /var/packages/<pkg>/{INFO,conf,scripts,target,var,home,tmp,etc}
    with target/var/home/tmp/etc pointing at /volumeN/@appstore|@appdata|@apphome|@apptemp|@appconf
  * conf/privilege  -> a dedicated system user/group per package
  * conf/resource   -> usr-local-linker symlinks into /usr/local/{bin,lib,etc}
  * scripts/{preinst,postinst,preuninst,postuninst,start-stop-status} run with the SYNOPKG_* env
  * WIZARD_UIFILES/install_uifile -> wizard rendered by the web UI, answers passed as env vars
  * each package gets a systemd unit cyn-pkg-<pkg>.service wrapping start-stop-status
Packages that need DSM-only components (WebStation, Synology's PHP/Apache packages, kernel
modules built for Synology kernels) are detected via deppkgs and refused with an explanation.
"""
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from .auth import current_user, require_admin
from .storage import load_pools
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/packages", tags=["packages"])

# What we tell package servers we are. SynoCommunity's x86-64 builds list these platforms.
DSM_MAJOR, DSM_MINOR, DSM_MICRO, DSM_BUILD = 7, 2, 2, 72806
ARCH = os.environ.get("CYN_SPK_ARCH", "geminilake")
X86_ARCHES = {"x86_64", "x64", "noarch", "apollolake", "avoton", "braswell", "broadwell", "broadwellnk",
              "broadwellnkv2", "broadwellntbap", "bromolow", "cedarview", "denverton", "epyc7002",
              "epyc7003", "epyc7003ntb", "geminilake", "geminilakenk", "grantley", "icelaked", "kvmx64",
              "purley", "r1000", "r1000nk", "v1000", "v1000nk", "x86"}
# Synology-made packages that only exist on real DSM
DSM_ONLY = {"WebStation", "PHP7.4", "PHP8.0", "PHP8.1", "PHP8.2", "PHP8.3", "PHP8.4", "Apache2.2", "Apache2.4",
            "MariaDB10", "Node.js_v18", "Node.js_v20", "ContainerManager", "Docker", "PythonModule", "Perl"}

ROOT = os.path.join(STATE_DIR, "root") if DEV else "/"
from . import pkgweb                                    # noqa: E402
from .pkgweb import STANDINS                            # noqa: E402
PKG_DIR = os.path.join(ROOT, "var/packages")
STAGING = os.path.join(STATE_DIR, "spk-staging")
SOURCES_FILE = os.path.join(STATE_DIR, "package-sources.json")
SHIM_PATH = "/usr/lib/cynthology/syno-shims"
DEFAULT_SOURCES = [
    {"name": "SynoCommunity", "url": "https://packages.synocommunity.com"},
    {"name": "xpenology", "url": "https://apps.xpenology.tech"},
]
_feed_cache: dict[str, tuple[float, list]] = {}


# ---------- sources ----------

class Source(BaseModel):
    name: str
    url: str


def load_sources() -> list[dict]:
    try:
        with open(SOURCES_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return list(DEFAULT_SOURCES)


def save_sources(s: list[dict]) -> None:
    with open(SOURCES_FILE, "w") as f:
        json.dump(s, f, indent=2)


@router.get("/sources")
def get_sources(_=Depends(current_user)):
    return load_sources()


@router.put("/sources")
def put_sources(body: list[Source], _=Depends(require_admin)):
    for s in body:
        if not s.url.startswith("https://"):
            # packages run scripts as root and are only checked against the feed's own checksum
            raise HTTPException(400, f"{s.name}: the location must start with https:// (plain http could be tampered with)")
    save_sources([s.model_dump() for s in body])
    _feed_cache.clear()
    return {"ok": True}


def fetch_feed(src: dict) -> list[dict]:
    hit = _feed_cache.get(src["url"])
    if hit and time.time() - hit[0] < 900:
        return hit[1]
    form = urllib.parse.urlencode({
        "package_update_channel": "stable", "unique": f"cynthology_{ARCH}", "build": DSM_BUILD,
        "language": "enu", "major": DSM_MAJOR, "minor": DSM_MINOR, "micro": DSM_MICRO, "nano": 0,
        "arch": ARCH, "timezone": "UTC"}).encode()
    req = urllib.request.Request(src["url"], data=form, headers={"User-Agent": "Cynthology-PackageCenter/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    pkgs = data.get("packages", data if isinstance(data, list) else [])
    out = []
    for p in pkgs:
        deps = [d.split(">")[0].split("<")[0].strip() for d in re.split(r"[:,]", p.get("deppkgs") or "") if d.strip()]
        blocked = sorted(set(deps) & DSM_ONLY - set(STANDINS))
        provided = sorted(set(deps) & set(STANDINS))
        out.append({
            "source": src["name"], "package": p.get("package"), "dname": p.get("dname") or p.get("package"),
            "version": p.get("version"), "desc": p.get("desc", ""), "link": p.get("link"), "md5": p.get("md5"),
            "size": p.get("size"), "thumbnail": (p.get("thumbnail") or [None])[-1] if isinstance(p.get("thumbnail"), list) else p.get("thumbnail"),
            "maintainer": p.get("maintainer"), "deppkgs": deps,
            "compatible": not blocked,
            "why_not": f"needs DSM-only package(s): {', '.join(blocked)}" if blocked else None,
            "provided": provided,      # DSM packages Cynthology stands in for (installed with it)
        })
    _feed_cache[src["url"]] = (time.time(), out)
    return out


@router.get("/catalog")
def catalog(_=Depends(current_user)):
    result, errors = [], []
    for src in load_sources():
        try:
            result += fetch_feed(src)
        except Exception as e:  # one dead source must not break the others
            errors.append({"source": src["name"], "error": str(e)})
    inst = {p["package"]: p["version"] for p in installed_list()}
    for p in result:
        p["installed_version"] = inst.get(p["package"])
        p["update_available"] = newer(p["version"], inst.get(p["package"]))
    return {"packages": sorted(result, key=lambda p: (p["dname"] or "").lower()), "errors": errors}


# ---------- versions ----------

def vkey(v: str | None) -> tuple:
    """'3.1.1-3' -> (3, 1, 1, 3); good enough for SPK versions (x.y.z-build)."""
    return tuple(int(n) for n in re.findall(r"\d+", v or ""))


def newer(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and vkey(a) > vkey(b)


def find_in_sources(name: str) -> dict | None:
    """Newest compatible offer of a package across all sources."""
    best = None
    for src in load_sources():
        try:
            for p in fetch_feed(src):
                if p["package"] == name and p["compatible"] and (best is None or newer(p["version"], best["version"])):
                    best = p
        except Exception:
            continue
    return best


def updates_available() -> list[dict]:
    out = []
    for p in installed_list():
        offer = find_in_sources(p["package"])
        if offer and newer(offer["version"], p["version"]):
            out.append({"package": p["package"], "dname": p["dname"], "installed": p["version"],
                        "available": offer["version"], "source": offer["source"]})
    return out


# ---------- reading packages ----------

def parse_info(text: str) -> dict:
    info = {}
    for line in text.splitlines():
        m = re.match(r'\s*([A-Za-z0-9_]+)\s*=\s*"?(.*?)"?\s*$', line)
        if m:
            info[m.group(1)] = m.group(2)
    return info


def _pkg_path(name: str, *parts: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", name):
        raise HTTPException(400, "bad package name")
    return os.path.join(PKG_DIR, name, *parts)


def _read_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def installed_list() -> list[dict]:
    out = []
    if not os.path.isdir(PKG_DIR):
        return out
    for name in sorted(os.listdir(PKG_DIR)):
        try:
            with open(os.path.join(PKG_DIR, name, "INFO")) as f:
                info = parse_info(f.read())
        except OSError:
            continue
        standin = os.path.exists(os.path.join(PKG_DIR, name, "cynthology-standin"))
        out.append({"package": name, "dname": info.get("displayname") or name, "version": info.get("version"),
                    "desc": info.get("description", ""), "running": standin or _running(name), "url": _open_url(name, info),
                    "startable": not standin and info.get("startable", "yes") != "no", "standin": standin})
    # built-in modules: which installed packages rely on them
    names = {p["package"]: p["dname"] for p in out}
    for p in out:
        if p["standin"]:
            p["used_by"] = sorted(names[o] for o in names if o != p["package"] and p["package"] in _deps_of(o))
    return out


def _open_url(name: str, info: dict) -> dict | None:
    ws = pkgweb.load().get(name)
    if ws and ws["portals"]:                 # published by Web Station at http://<nas>/<alias>/
        return {"protocol": "http", "port": 80, "path": f"/{ws['portals'][0]['alias']}/"}
    cfg = _read_json(_pkg_path(name, "target", "app", "config")).get(".url", {})
    for v in cfg.values():
        if v.get("port") or v.get("url", "").startswith("http"):
            return {"protocol": v.get("protocol", "http"), "port": v.get("port"), "path": v.get("url", "/")}
    if info.get("adminport"):
        return {"protocol": info.get("adminprotocol", "http"), "port": info["adminport"], "path": info.get("adminurl", "/")}
    return None


def _running(name: str) -> bool:
    if DEV:
        return os.path.exists(_pkg_path(name, "enabled"))
    return run(["systemctl", "is-active", f"cyn-pkg-{name}"], check=False, privileged=False).strip() == "active"


@router.get("/installed")
def installed(_=Depends(current_user)):
    return installed_list()


@router.get("/installed/{name}/icon")
def icon(name: str):
    for fn in ("PACKAGE_ICON_256.PNG", "PACKAGE_ICON.PNG"):
        p = _pkg_path(name, fn)
        if os.path.exists(p):
            return FileResponse(p, media_type="image/png")
    svg = pkgweb.standin_icon(name)
    if svg:
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})
    raise HTTPException(404)


# ---------- install ----------

def _volume() -> str:
    pools = load_pools()
    if not pools:
        raise HTTPException(400, "Create a storage pool first - packages are installed onto a volume.")
    v = pools[0]["mount"]
    return os.path.join(ROOT, v.lstrip("/")) if DEV else v


def _safe_extract(tar_path: str, dest: str) -> None:
    """Extract refusing absolute paths, '..' and links escaping dest (CVE-2007-4559)."""
    with tarfile.open(tar_path) as t:
        try:
            t.extractall(dest, filter="tar")
            return
        except TypeError:  # Python without extraction filters
            pass
        root = os.path.realpath(dest)
        for m in t.getmembers():
            target = os.path.realpath(os.path.join(dest, m.name))
            if not (target == root or target.startswith(root + os.sep)) or m.isdev():
                raise tarfile.TarError(f"unsafe path in archive: {m.name}")
            if m.issym() or m.islnk():
                lt = os.path.realpath(os.path.join(os.path.dirname(target), m.linkname))
                if not lt.startswith(root + os.sep):
                    raise tarfile.TarError(f"unsafe link in archive: {m.name}")
        t.extractall(dest)


def _stage(spk_path: str) -> dict:
    """Unpack an .spk into a staging dir and report what installing it would need."""
    sid = uuid.uuid4().hex
    d = os.path.join(STAGING, sid)
    os.makedirs(d)
    try:
        _safe_extract(spk_path, d)
    except (tarfile.TarError, OSError) as e:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, f"not a valid .spk package: {e}")
    os.remove(spk_path)
    if not os.path.exists(os.path.join(d, "INFO")):
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, "not a valid .spk package (no INFO file)")
    with open(os.path.join(d, "INFO")) as f:
        info = parse_info(f.read())
    problems = []
    arches = set(info.get("arch", "noarch").split())
    if not arches & X86_ARCHES:
        problems.append(f"built for {info.get('arch')}, not x86-64")
    deps = [x.split(">")[0].strip() for x in re.split(r"[:,]", info.get("install_dep_packages", "")) if x.strip()]
    if set(deps) & DSM_ONLY - set(STANDINS):
        problems.append(f"needs DSM-only package(s): {', '.join(sorted(set(deps) & DSM_ONLY - set(STANDINS)))}")
    missing = [x for x in deps if x not in DSM_ONLY and x not in STANDINS and not os.path.isdir(_pkg_path(x))]
    auto_deps = []
    for x in missing:
        offer = find_in_sources(x)
        if offer:
            auto_deps.append({"package": x, "version": offer["version"], "source": offer["source"]})
        else:
            problems.append(f"needs the package '{x}', which none of your package sources offers")
    name = info.get("package", "")
    installed = None
    if name and os.path.exists(_pkg_path(name, "INFO")):
        with open(_pkg_path(name, "INFO")) as f:
            installed = parse_info(f.read()).get("version")
        if installed and not newer(info.get("version"), installed):
            problems.append(f"version {installed} is already installed (this is {info.get('version')})")
    kind = "upgrade" if installed else "install"
    wizard = []
    wf = os.path.join(d, "WIZARD_UIFILES", f"{kind}_uifile")
    if os.path.exists(wf):
        wizard = _read_json(wf) or []
    elif os.path.exists(wf + ".sh"):
        wizard = _dynamic_wizard(wf + ".sh", info, "UPGRADE" if installed else "INSTALL")
    return {"staging": sid, "info": info, "wizard": wizard, "problems": problems, "mode": kind,
            "installed_version": installed, "dependencies": auto_deps}


def _dynamic_wizard(script: str, info: dict, status: str = "INSTALL") -> list:
    """DSM runs install_uifile.sh, which writes the wizard JSON to $SYNOPKG_TEMP_LOGFILE.
    We run it unprivileged (nobody) since the package is not trusted yet."""
    if DEV:
        return []
    import subprocess
    out = tempfile.NamedTemporaryFile(prefix="cyn-wiz-", suffix=".json", delete=False)
    out.close()
    os.chmod(out.name, 0o666)
    os.chmod(script, 0o755)
    env = {"PATH": f"{SHIM_PATH}:/usr/bin:/bin", "SYNOPKG_TEMP_LOGFILE": out.name,
           "SYNOPKG_PKGNAME": info.get("package", ""), "SYNOPKG_PKGVER": info.get("version", ""),
           "SYNOPKG_DSM_VERSION_MAJOR": str(DSM_MAJOR), "SYNOPKG_DSM_VERSION_MINOR": str(DSM_MINOR),
           "SYNOPKG_PKG_STATUS": status}
    try:
        subprocess.run(["setpriv", "--reuid=nobody", "--regid=nogroup", "--clear-groups", "--", "/bin/bash", script],
                       env=env, cwd="/tmp", capture_output=True, timeout=30)
        with open(out.name) as f:
            return json.load(f)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    finally:
        os.remove(out.name)


def _data_shares(base: str, wizard: dict) -> None:
    """conf/resource data-share: create the shared folders a package asks for and grant access."""
    from .shares import ensure_share
    res = _read_json(os.path.join(base, "conf", "resource")).get("data-share", {})
    for sh in res.get("shares", []):
        name = re.sub(r"\{\{(\w+)\}\}", lambda m: str(wizard.get(m.group(1), "")), sh.get("name", "")).strip("/")
        if not name:
            continue
        ensure_share(name, sh.get("permission", {}).get("rw", []))


class Prepare(BaseModel):
    source: str
    package: str


@router.post("/prepare")
def prepare(body: Prepare, _=Depends(require_admin)):
    return prepare_from_source(body.source, body.package)


def prepare_from_source(source: str, package: str) -> dict:
    src = next((s for s in load_sources() if s["name"] == source), None)
    if not src:
        raise HTTPException(404, "unknown source")
    pkg = next((p for p in fetch_feed(src) if p["package"] == package), None)
    if not pkg or not pkg["link"]:
        raise HTTPException(404, "package not in source")
    os.makedirs(STAGING, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".spk", dir=STAGING)
    md5 = hashlib.md5()
    req = urllib.request.Request(pkg["link"], headers={"User-Agent": "Cynthology-PackageCenter/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r, os.fdopen(fd, "wb") as f:
            while chunk := r.read(1 << 20):
                md5.update(chunk)
                f.write(chunk)
    except OSError as e:
        os.path.exists(tmp) and os.remove(tmp)
        raise HTTPException(502, f"download failed: {e}")
    if pkg.get("md5") and md5.hexdigest() != pkg["md5"]:
        os.remove(tmp)
        raise HTTPException(502, "download corrupted (md5 mismatch)")
    return _stage(tmp)


@router.post("/upload")
async def upload(request: Request, _=Depends(require_admin)):
    """Manual Install: raw .spk bytes in the request body."""
    os.makedirs(STAGING, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".spk", dir=STAGING)
    with os.fdopen(fd, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    return _stage(tmp)


class Install(BaseModel):
    staging: str
    wizard: dict[str, str | bool | int] = {}


_ENV_DENY = {"ENV", "IFS", "SHELLOPTS", "BASHOPTS", "PS4", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LOCPATH",
             "NLSPATH", "HOSTALIASES", "RESOLV_HOST_CONF", "LANGUAGE", "CDPATH", "GLOBIGNORE", "PROMPT_COMMAND"}
_PRIV_GROUPS = {"root", "sudo", "adm", "shadow", "disk", "docker", "cynthology-admin", "users", "staff", "wheel", "sys",
                "kmem", "tty", "lpadmin", "systemd-journal", "netdev", "kvm", "libvirt", "nut", "www-data", "operator"}


def _env(name: str, info: dict, status: str, extra: dict | None = None) -> dict:
    base = _pkg_path(name)
    env = {
        "PATH": f"{SHIM_PATH}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "SYNOPKG_PKGNAME": name, "SYNOPKG_PKGVER": info.get("version", ""),
        "SYNOPKG_PKGDEST": f"{base}/target", "SYNOPKG_PKGVAR": f"{base}/var", "SYNOPKG_PKGHOME": f"{base}/home",
        "SYNOPKG_PKGTMP": f"{base}/tmp", "SYNOPKG_PKGETC": f"{base}/etc",
        "SYNOPKG_PKGDEST_VOL": _volume() if not DEV else "/volume1",
        "SYNOPKG_PKG_STATUS": status, "SYNOPKG_DSM_VERSION_MAJOR": str(DSM_MAJOR),
        "SYNOPKG_DSM_VERSION_MINOR": str(DSM_MINOR), "SYNOPKG_DSM_VERSION_BUILD": str(DSM_BUILD),
        "SYNOPKG_DSM_ARCH": ARCH, "SYNOPKG_DSM_LANGUAGE": "enu",
        "SYNOPKG_TEMP_LOGFILE": f"/run/cyn-pkg/{name}.log", "SYNOPKG_TEMP_UPGRADE_FOLDER": os.path.join(STATE_DIR, "spk-upgrade", name),
    }
    for k, v in (extra or {}).items():
        # wizard answers become variables - but never ones that change how programs run (LD_PRELOAD, PATH, ...)
        if k in env or k in _ENV_DENY or k.startswith(("LD_", "BASH_", "PYTHON", "PERL", "RUBY", "NODE_", "GCONV", "MALLOC")):
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            env[k] = "true" if v is True else "false" if v is False else str(v)
    return env


def _privilege(stage_or_pkg: str, name: str) -> tuple[str, str, bool]:
    priv = _read_json(os.path.join(stage_or_pkg, "conf", "privilege"))
    user = priv.get("username") or f"sc-{name}"
    group = priv.get("groupname") or user
    as_root = priv.get("defaults", {}).get("run-as") == "root"
    # The package's own account: a plain new name - never root, a real user or a privileged group
    import grp
    import pwd
    for n in (user, group):
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", n or "") or n in _PRIV_GROUPS:
            raise HTTPException(400, f"the package asks for the account {n!r}, which is not allowed")
    try:
        pw = pwd.getpwnam(user)
        if pw.pw_uid < 100 or pw.pw_uid >= 1000 or pw.pw_dir != _pkg_path(name, "home"):
            raise HTTPException(400, f"the package wants to run as the existing account {user!r}, which is not allowed")
    except KeyError:
        pass
    try:
        if grp.getgrnam(group).gr_gid < 100:
            raise HTTPException(400, f"the package wants the system group {group!r}, which is not allowed")
    except KeyError:
        pass
    return user, group, as_root


def _script(name: str, script: str, env: dict, user: str | None, scripts_dir: str | None = None) -> None:
    path = os.path.join(scripts_dir or _pkg_path(name, "scripts"), script)
    if not os.path.exists(path):
        return
    cmd = ["/bin/bash", path]  # DSM's /bin/sh is bash; SynoCommunity scripts rely on it
    if DEV:
        print("[dev] would run", script, "for", name)
        return
    if user and scripts_dir:
        os.chmod(scripts_dir, 0o755)   # staged scripts must be readable by the package user
        os.chmod(os.path.dirname(scripts_dir), 0o755)
    log = env["SYNOPKG_TEMP_LOGFILE"]
    os.makedirs(os.path.dirname(log), exist_ok=True)
    open(log, "w").close()
    if user:  # "user:group"
        u, _, g = user.partition(":")
        shutil.chown(log, u, g or None)
        cmd = ["setpriv", f"--reuid={u}", f"--regid={g or u}", "--init-groups", "--"] + cmd
    import subprocess
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=1800, cwd="/")
    with open(log, "a") as f:
        f.write(f"--- {script} rc={r.returncode}\n{r.stdout}\n{r.stderr}\n")
    if r.returncode != 0:
        msg = (open(log).read()[-800:] if os.path.exists(log) else r.stderr)
        raise HTTPException(500, f"{name}: {script} failed (rc {r.returncode}). {msg.strip()}")


@router.post("/install")
def install(body: Install, _=Depends(require_admin)):
    d = os.path.join(STAGING, re.sub(r"[^a-f0-9]", "", body.staging))
    if not os.path.isdir(d):
        raise HTTPException(404, "staged package expired - start again")
    return install_staged(d, body.wizard)


def install_staged(d: str, wizard: dict, depth: int = 0) -> dict:
    """Install or upgrade the package unpacked in staging dir d (installing dependencies first)."""
    try:
        with open(os.path.join(d, "INFO")) as f:
            info = parse_info(f.read())
        name = info.get("package")
        wizard = {**_static_wizard_defaults(d, os.path.exists(_pkg_path(name, "INFO"))), **(wizard or {})}
        _install_dependencies(info, depth)
        if os.path.exists(_pkg_path(name, "INFO")):
            return _upgrade(d, info, wizard)
        return _fresh_install(d, info, wizard)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _install_dependencies(info: dict, depth: int) -> None:
    deps = [x.split(">")[0].split("<")[0].split("=")[0].strip()
            for x in re.split(r"[:,]", info.get("install_dep_packages", "")) if x.strip()]
    for dep in deps:
        if dep in STANDINS:                       # Synology's PHP/Apache/WebStation...: provided by Cynthology
            if not os.path.exists(_pkg_path(dep, "cynthology-standin")):
                pkgweb.ensure_standin(dep)
            continue
        if dep in DSM_ONLY or os.path.isdir(_pkg_path(dep)):
            continue
        if depth >= 3:
            raise HTTPException(400, f"dependency chain too deep at '{dep}'")
        offer = find_in_sources(dep)
        if not offer:
            raise HTTPException(400, f"{info.get('package')} needs '{dep}', which none of your package sources offers")
        staged = prepare_from_source(offer["source"], dep)
        if staged["problems"]:
            raise HTTPException(400, f"can't install dependency '{dep}': {'; '.join(staged['problems'])}")
        install_staged(os.path.join(STAGING, staged["staging"]), _wizard_defaults(staged["wizard"]), depth + 1)


def _static_wizard_defaults(d: str, upgrade: bool) -> dict:
    """Questions the caller didn't answer get the package's own default (an API install without the wizard)."""
    for f in (("upgrade_uifile",) if upgrade else ("install_uifile",)):
        w = _read_json(os.path.join(d, "WIZARD_UIFILES", f))
        if isinstance(w, list):
            return _wizard_defaults(w)
    return {}


def _wizard_defaults(wizard: list) -> dict:
    out = {}
    for step in wizard or []:
        for item in (step.get("items", []) if isinstance(step, dict) else []):
            for sub in (item.get("subitems", []) if isinstance(item, dict) else []):
                if isinstance(sub, dict) and "key" in sub:
                    out[sub["key"]] = sub.get("defaultValue", "")
    return out


def _move_in(d: str, base: str) -> None:
    for item in ("INFO", "conf", "scripts", "WIZARD_UIFILES", "PACKAGE_ICON.PNG", "PACKAGE_ICON_256.PNG"):
        src = os.path.join(d, item)
        if os.path.exists(src):
            shutil.move(src, os.path.join(base, item))


def _owner(name: str, user: str, group: str) -> None:
    if not DEV:
        base = _pkg_path(name)
        run(["chown", "-R", f"{user}:{group}", *(os.path.realpath(os.path.join(base, x))
                                                 for x in ("target", "var", "home", "tmp", "etc"))])


def _fresh_install(d: str, info: dict, wizard: dict) -> dict:
    name = info["package"]
    base = _pkg_path(name)
    vol = _volume()
    user, group, as_root = _privilege(d, name)

    # 1. package user/group (conf/privilege)
    run(["sh", "-c", f"getent group {group} >/dev/null || groupadd -r {group}"])
    run(["sh", "-c", f"id -u {user} >/dev/null 2>&1 || useradd -r -g {group} -d {base}/home -s /usr/sbin/nologin {user}"])

    # 2. DSM 7 layout
    os.makedirs(base)
    _move_in(d, base)
    for link, store in (("target", "@appstore"), ("var", "@appdata"), ("home", "@apphome"),
                        ("tmp", "@apptemp"), ("etc", "@appconf")):
        real = os.path.join(vol, store, name)
        os.makedirs(real, exist_ok=True)
        os.symlink(real, os.path.join(base, link))
    env = _env(name, info, "INSTALL", wizard)
    run_user = None if as_root else f"{user}:{group}"

    try:
        _data_shares(base, wizard)
        _script(name, "preinst", env, None if DEV else run_user)
        _safe_extract(os.path.join(d, "package.tgz"), os.path.realpath(os.path.join(base, "target")))
        _owner(name, user, group)
        _link_usr_local(name, create=True)
        _script(name, "postinst", env, run_user)
        _write_unit(name, info, run_user)
        pkgweb.apply_package(name, base)
    except Exception:
        pkgweb.remove_package(name)
        _remove(name, keep_data=False)
        raise
    if info.get("startable", "yes") != "no":
        _service(name, "start")
    return {"ok": True, "package": name, "version": info.get("version")}


def _upgrade(d: str, new_info: dict, wizard: dict) -> dict:
    """DSM 7 order: stop, new preupgrade, old preuninst+postuninst, new preinst, files, new postinst,
    new postupgrade, start. Data (var/home/etc) is kept. On failure the old version is put back."""
    name = new_info["package"]
    base = _pkg_path(name)
    with open(os.path.join(base, "INFO")) as f:
        old_info = parse_info(f.read())
    old_ver = old_info.get("version", "")
    if not newer(new_info.get("version"), old_ver):
        raise HTTPException(409, f"{name} {old_ver} is already installed")
    user, group, as_root = _privilege(d, name)
    run_user = None if as_root else f"{user}:{group}"
    old_user, old_group, old_root = _privilege(base, name)
    old_run_user = None if old_root else f"{old_user}:{old_group}"
    extra = {**wizard, "SYNOPKG_OLD_PKGVER": old_ver}
    env_new = _env(name, new_info, "UPGRADE", extra)
    env_old = _env(name, old_info, "UPGRADE", extra)
    upg = env_new["SYNOPKG_TEMP_UPGRADE_FOLDER"]
    shutil.rmtree(upg, ignore_errors=True)
    os.makedirs(upg)
    if not DEV and run_user:
        shutil.chown(upg, user, group)
    was_running = _running(name)

    # keep the old version aside so we can roll back
    target = os.path.realpath(os.path.join(base, "target"))
    saved_meta = base + ".cyn-old"
    saved_target = target + ".cyn-old"
    shutil.rmtree(saved_meta, ignore_errors=True)
    shutil.rmtree(saved_target, ignore_errors=True)

    _service(name, "stop")
    try:
        _script(name, "preupgrade", env_new, run_user, scripts_dir=os.path.join(d, "scripts"))
        _script(name, "preuninst", env_old, old_run_user)
        _script(name, "postuninst", env_old, old_run_user)
        _link_usr_local(name, create=False)
        os.makedirs(saved_meta)
        for item in ("INFO", "conf", "scripts", "WIZARD_UIFILES", "PACKAGE_ICON.PNG", "PACKAGE_ICON_256.PNG"):
            if os.path.exists(os.path.join(base, item)):
                shutil.move(os.path.join(base, item), os.path.join(saved_meta, item))
        os.rename(target, saved_target)
        os.makedirs(target)
        _move_in(d, base)
        _script(name, "preinst", env_new, None if DEV else run_user)
        _safe_extract(os.path.join(d, "package.tgz"), target)
        _owner(name, user, group)
        _link_usr_local(name, create=True)
        _script(name, "postinst", env_new, run_user)
        _script(name, "postupgrade", env_new, run_user)
        _write_unit(name, new_info, run_user)
    except Exception as e:
        # roll back to the old version
        if os.path.isdir(saved_target):
            shutil.rmtree(target, ignore_errors=True)
            os.rename(saved_target, target)
        if os.path.isdir(saved_meta):
            for item in os.listdir(saved_meta):
                dst = os.path.join(base, item)
                if os.path.isdir(dst) and not os.path.islink(dst):
                    shutil.rmtree(dst)
                elif os.path.exists(dst):
                    os.remove(dst)
                shutil.move(os.path.join(saved_meta, item), dst)
            shutil.rmtree(saved_meta, ignore_errors=True)
        _link_usr_local(name, create=True)
        _write_unit(name, old_info, old_run_user)
        if was_running:
            try:
                _service(name, "start")
            except Exception:
                pass
        msg = e.detail if isinstance(e, HTTPException) else str(e)
        raise HTTPException(500, f"upgrade of {name} failed and was rolled back to {old_ver}: {msg}")
    shutil.rmtree(saved_meta, ignore_errors=True)
    shutil.rmtree(saved_target, ignore_errors=True)
    shutil.rmtree(upg, ignore_errors=True)
    try:
        pkgweb.apply_package(name, base)
    except Exception as e:
        print(f"{name}: web service:", e)
    if new_info.get("startable", "yes") != "no":
        _service(name, "start")
    return {"ok": True, "package": name, "version": new_info.get("version"), "upgraded_from": old_ver}


@router.get("/updates")
def updates(_=Depends(current_user)):
    return updates_available()


def update_all() -> list[dict]:
    results = []
    for u in updates_available():
        try:
            staged = prepare_from_source(u["source"], u["package"])
            if staged["problems"]:
                raise HTTPException(400, "; ".join(staged["problems"]))
            r = install_staged(os.path.join(STAGING, staged["staging"]), _wizard_defaults(staged["wizard"]))
            results.append({"package": u["package"], "ok": True, "version": r.get("version")})
        except HTTPException as e:
            results.append({"package": u["package"], "ok": False, "error": e.detail})
        except Exception as e:
            results.append({"package": u["package"], "ok": False, "error": str(e)})
    return results


@router.post("/update-all")
def update_all_api(_=Depends(require_admin)):
    return update_all()


class AutoUpdate(BaseModel):
    enabled: bool


@router.get("/auto-update")
def get_auto(_=Depends(require_admin)):
    from . import settings
    return {"enabled": bool(settings.get("pkg_auto_update"))}


@router.put("/auto-update")
def put_auto(body: AutoUpdate, _=Depends(require_admin)):
    from . import settings
    settings.update(pkg_auto_update=body.enabled)
    return {"ok": True}


def _link_usr_local(name: str, create: bool) -> None:
    res = _read_json(_pkg_path(name, "conf", "resource")).get("usr-local-linker", {})
    for kind, items in res.items():
        if kind not in ("bin", "lib", "etc", "sbin", "share", "include") or not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, str) or os.path.basename(item) in ("", ".", ".."):
                continue
            dst = os.path.join(ROOT, "usr/local", kind, os.path.basename(item))
            if create:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if not os.path.lexists(dst):
                    os.symlink(_pkg_path(name, "target", item), dst)
            elif os.path.islink(dst) and os.readlink(dst).startswith(_pkg_path(name)):
                os.remove(dst)


def _write_unit(name: str, info: dict, user: str | None) -> None:
    if DEV or info.get("startable", "yes") == "no":
        return
    env = _env(name, info, "START")
    envfile = _pkg_path(name, "cyn.env")
    with open(envfile, "w") as f:
        f.writelines(f'{k}="{v}"\n' for k, v in env.items())
    script = _pkg_path(name, "scripts", "start-stop-status")
    unit = f"""[Unit]
Description=Cynthology package {info.get('displayname', name)}
After=network-online.target local-fs.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
EnvironmentFile={envfile}
{'User=' + user.split(':')[0] + chr(10) + 'Group=' + user.split(':')[-1] if user else ''}
ExecStart=/bin/bash {script} start
ExecStop=/bin/bash {script} stop
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
"""
    with open(f"/etc/systemd/system/cyn-pkg-{name}.service", "w") as f:
        f.write(unit)
    run(["systemctl", "daemon-reload"])


def _service(name: str, action: str) -> None:
    if DEV:
        flag = _pkg_path(name, "enabled")
        open(flag, "w").close() if action == "start" else (os.path.exists(flag) and os.remove(flag))
        return
    if action == "start":
        run(["systemctl", "enable", "--now", f"cyn-pkg-{name}"])
    else:
        run(["systemctl", "disable", "--now", f"cyn-pkg-{name}"], check=False)


@router.post("/installed/{name}/{action}")
def control(name: str, action: str, _=Depends(require_admin)):
    if not os.path.isdir(_pkg_path(name)) or action not in ("start", "stop"):
        raise HTTPException(400, "bad request")
    _service(name, action)
    return {"ok": True}


def _remove(name: str, keep_data: bool) -> None:
    base = _pkg_path(name)
    if not os.path.isdir(base):
        return
    for link in ("target", "tmp") + (() if keep_data else ("var", "home", "etc")):
        p = os.path.join(base, link)
        if os.path.islink(p):
            shutil.rmtree(os.path.realpath(p), ignore_errors=True)
    _link_usr_local(name, create=False)
    shutil.rmtree(base, ignore_errors=True)
    if not DEV:
        unit = f"/etc/systemd/system/cyn-pkg-{name}.service"
        if os.path.exists(unit):
            os.remove(unit)
            run(["systemctl", "daemon-reload"])


@router.delete("/installed/{name}")
def uninstall(name: str, keep_data: bool = True, _=Depends(require_admin)):
    base = _pkg_path(name)
    if not os.path.isdir(base):
        raise HTTPException(404, "not installed")
    if os.path.exists(os.path.join(base, "cynthology-standin")):
        users = [p for p in os.listdir(PKG_DIR) if name in _deps_of(p)]
        if users:
            raise HTTPException(409, f"{', '.join(users)} need{'s' if len(users) == 1 else ''} {name} - uninstall {'it' if len(users) == 1 else 'them'} first")
        shutil.rmtree(base)
        return {"ok": True}
    with open(os.path.join(base, "INFO")) as f:
        info = parse_info(f.read())
    pkgweb.remove_package(name)
    _service(name, "stop")
    user, group, as_root = _privilege(base, name)
    ru = None if as_root else f"{user}:{group}"
    env = _env(name, info, "UNINSTALL", {"wizard_keep_data": keep_data, "wizard_delete_data": not keep_data})
    try:
        _script(name, "preuninst", env, ru)
        _script(name, "postuninst", env, ru)
    finally:
        _remove(name, keep_data)
    return {"ok": True}


if __name__ == "__main__":
    import sys
    from . import settings
    if sys.argv[1:] == ["auto-update"]:
        if not settings.get("pkg_auto_update"):
            print("automatic package updates are off")
            sys.exit(0)
        for r in update_all():
            print(r)
            if not r["ok"]:
                from .backup import _alert
                _alert({"name": r["package"]}, "warning", f"Package update failed: {r['package']}", r["error"])


def _deps_of(pkg: str) -> set[str]:
    try:
        with open(_pkg_path(pkg, "INFO")) as f:
            info = parse_info(f.read())
    except (OSError, HTTPException):
        return set()
    return {x.split(">")[0].split("<")[0].split("=")[0].strip()
            for x in re.split(r"[:,]", info.get("install_dep_packages", "")) if x.strip()}
