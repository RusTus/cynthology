"""Web Station: websites served by nginx with PHP-FPM, from the "web" shared folder.

Installed on demand (Debian's nginx and php-fpm packages, plus common PHP extensions). Each website is
either port-based (its own port) or name-based (a host name on a shared port), and serves static
files, PHP, or proxies to another address (reverse proxy, e.g. to a container). HTTPS uses the
NAS's certificate. Config: /etc/nginx/sites-enabled/cynthology-web.conf, checked with `nginx -t`
before it replaces the working one; PHP settings in /etc/php/<ver>/fpm/conf.d/99-cynthology.ini.
"""
import glob
import json
import os
import re
import secrets
import shutil
import subprocess

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import containers as ct
from . import shares as shares_mod
from .auth import require_admin
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/webstation", tags=["webstation"])
STATE = os.path.join(STATE_DIR, "webstation.json")
NGINX_CONF = os.path.join(STATE_DIR, "nginx-web.conf") if DEV else "/etc/nginx/sites-enabled/cynthology-web.conf"
TLS_CERT, TLS_KEY = "/etc/cynthology/tls/cert.pem", "/etc/cynthology/tls/key.pem"
PACKAGES = ["nginx", "php-fpm", "php-mysql", "php-pgsql", "php-sqlite3", "php-gd", "php-curl", "php-mbstring", "php-xml",
            "php-zip", "php-intl", "php-bcmath", "php-imagick", "php-apcu", "php-redis", "php-ldap", "php-soap"]
RESERVED = {22, 139, 445, 2049, 3493, 5000, 5001}
HOST_RE = re.compile(r"^(\*\.)?[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*$")
SIZE_RE = re.compile(r"^\d{1,5}[KMG]?$")
PROXY_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+(:\d+)?(/[A-Za-z0-9._~/%-]*)?$")
PHP_DEFAULTS = {"memory_limit": "256M", "upload_max_filesize": "512M", "post_max_size": "512M",
                "max_execution_time": "120", "display_errors": False}


class Site(BaseModel):
    id: str = ""
    name: str
    enabled: bool = True
    type: str = "port"              # port | name
    hostname: str = ""              # name-based: www.example.com (several separated by spaces)
    http_port: int | None = 80
    https_port: int | None = None
    redirect_https: bool = False
    kind: str = "php"               # static | php | proxy
    folder: str = ""                # inside the web shared folder
    proxy: str = ""                 # proxy: http://127.0.0.1:8080


class Php(BaseModel):
    memory_limit: str
    upload_max_filesize: str
    post_max_size: str
    max_execution_time: str
    display_errors: bool = False


def load() -> dict:
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {"sites": [], "php": dict(PHP_DEFAULTS)}


def save(st: dict) -> None:
    with open(STATE + ".new", "w") as f:
        json.dump(st, f, indent=1)
    os.replace(STATE + ".new", STATE)


def php_version() -> str | None:
    vers = sorted((os.path.basename(os.path.dirname(os.path.dirname(p))) for p in glob.glob("/etc/php/*/fpm/php-fpm.conf")),
                  key=lambda v: tuple(int(n) for n in v.split(".") if n.isdigit()))
    return vers[-1] if vers else ("8.4" if DEV else None)


def installed() -> bool:
    return DEV or (shutil.which("nginx") is not None and php_version() is not None)


def active(unit: str) -> bool:
    return not DEV and subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0


def web_root(create: bool = False) -> str:
    share = next((s for s in shares_mod.load() if s["name"] == "web"), None)
    if share:
        return share["path"]
    if DEV:
        return "/volume1/web"
    if not create:
        return ""
    path = shares_mod.ensure_share("web", [], "Websites (Web Station)")
    run(["setfacl", "-R", "-m", "u:www-data:rwX,d:u:www-data:rwX", path])
    index = os.path.join(path, "index.html")
    if not os.listdir(path):
        with open(index, "w") as f:
            f.write("<!doctype html><meta charset=utf-8><title>It works</title>"
                    "<body style='font:16px system-ui;background:#111827;color:#e5e7eb;display:grid;place-items:center;height:90vh'>"
                    "<div><h1>Web Station is running</h1><p>Put your website in the <b>web</b> shared folder.</p></div>")
    return path


def site_root(site: dict, root: str) -> str:
    parts = [p for p in site.get("folder", "").strip("/").split("/") if p]
    if ".." in parts:
        raise HTTPException(400, "bad folder")
    return os.path.join(root, *parts)


def validate(site: Site, others: list[dict]) -> None:
    if not site.name.strip() or len(site.name) > 60:
        raise HTTPException(400, "give the website a name")
    if site.type not in ("port", "name") or site.kind not in ("static", "php", "proxy"):
        raise HTTPException(400, "bad website type")
    if not site.http_port and not site.https_port:
        raise HTTPException(400, "choose an HTTP or HTTPS port")
    for p in (site.http_port, site.https_port):
        if p is not None and not (1 <= p <= 65535) or p in RESERVED:
            raise HTTPException(400, f"port {p} can't be used (reserved for the NAS)")
    if site.http_port and site.http_port == site.https_port:
        raise HTTPException(400, "HTTP and HTTPS need different ports")
    if site.type == "name":
        names = site.hostname.split()
        if not names or not all(HOST_RE.match(n) for n in names):
            raise HTTPException(400, "enter host name(s) like www.example.com")
    if site.kind == "proxy" and not PROXY_RE.match(site.proxy):
        raise HTTPException(400, "proxy address must look like http://127.0.0.1:8080")
    if site.kind != "proxy":
        site_root(site.model_dump(), "/x")
    mine = {site.http_port, site.https_port} - {None}
    for o in others:
        if not o.get("enabled") or o["id"] == site.id:
            continue
        shared = mine & ({o.get("http_port"), o.get("https_port")} - {None})
        if not shared:
            continue
        if o.get("http_port") in shared and o.get("http_port") == site.https_port or o.get("https_port") in shared and o.get("https_port") == site.http_port:
            raise HTTPException(400, f"port {min(shared)} is HTTP in one website and HTTPS in another")
        if site.type == "port" and o.get("type") == "port":
            raise HTTPException(400, f"port {min(shared)} is already used by website '{o['name']}'. Use another port, or make one of them name-based.")
        if site.type == "name" and o.get("type") == "name" and set(site.hostname.split()) & set(o.get("hostname", "").split()):
            raise HTTPException(400, f"host name already used by website '{o['name']}'")


def render(st: dict, root: str, php_ver: str | None, ipv6: bool | None = None) -> str:
    """nginx config for all enabled websites."""
    if ipv6 is None:   # nginx refuses to start if IPv6 is switched off and we listen on [::]
        ipv6 = os.path.exists("/proc/net/if_inet6")
    sites = [s for s in st["sites"] if s.get("enabled")]
    upload = st["php"].get("post_max_size", "512M")
    defaults: set[int] = set()   # ports that already have a default_server
    for s in sites:              # a port-based site is the default on its ports
        if s["type"] == "port":
            defaults |= {s.get("http_port"), s.get("https_port")} - {None}
    out = ["# Generated by Cynthology Web Station. Changes here are overwritten.\n"]
    claimed: set[int] = set()
    for s in sorted(sites, key=lambda s: s["type"] != "port"):
        listen = []
        for port, ssl in ((s.get("http_port"), False), (s.get("https_port"), True)):
            if not port:
                continue
            dflt = ""
            if (s["type"] == "port" or port not in defaults) and port not in claimed:
                dflt, _ = " default_server", claimed.add(port)
            listen.append(f"    listen {port}{' ssl' if ssl else ''}{dflt};")
            if ipv6:
                listen.append(f"    listen [::]:{port}{' ssl' if ssl else ''}{dflt};")
        name = s["hostname"] if s["type"] == "name" else "_"
        body = [f"server {{  # {s['name']}", *listen, f"    server_name {name};", f"    client_max_body_size {upload};"]
        if s.get("https_port"):
            body += [f"    ssl_certificate {TLS_CERT};", f"    ssl_certificate_key {TLS_KEY};",
                     "    ssl_protocols TLSv1.2 TLSv1.3;"]
        if s.get("redirect_https") and s.get("https_port") and s.get("http_port"):
            port = "" if s["https_port"] == 443 else f":{s['https_port']}"
            body += [f"    if ($scheme = http) {{ return 301 https://$host{port}$request_uri; }}"]
        if s["kind"] == "proxy":
            body += ["    location / {", f"        proxy_pass {s['proxy']};", "        proxy_http_version 1.1;",
                     "        proxy_set_header Host $host;", "        proxy_set_header X-Real-IP $remote_addr;",
                     "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
                     "        proxy_set_header X-Forwarded-Proto $scheme;", "        proxy_set_header Upgrade $http_upgrade;",
                     "        proxy_set_header Connection $connection_upgrade;", "        proxy_read_timeout 3600s;",
                     "        proxy_buffering off;", "    }"]
        else:
            body += [f"    root {site_root(s, root)};", "    index index.php index.html index.htm;",
                     "    location ~ /\\.(?!well-known) { deny all; }",
                     "    location ~ ^/(@eaDir|#recycle|\\.snapshots)(/|$) { deny all; }"]
            if s["kind"] == "php" and php_ver:
                body += ["    location / { try_files $uri $uri/ /index.php?$query_string; }",
                         "    location ~ \\.php$ {", "        include snippets/fastcgi-php.conf;",
                         f"        fastcgi_pass unix:/run/php/php{php_ver}-fpm.sock;",
                         f"        fastcgi_read_timeout {int(st['php'].get('max_execution_time', '120') or 120) + 30}s;", "    }"]
            else:
                body += ["    location / { try_files $uri $uri/ =404; }"]
        out.append("\n".join(body) + "\n}\n")
    if any(s["kind"] == "proxy" for s in sites):
        out.insert(1, "map $http_upgrade $connection_upgrade { default upgrade; '' close; }\n")
    return "\n".join(out)


def php_ini(php: dict) -> str:
    return ("; Generated by Cynthology Web Station\n" +
            "".join(f"{k} = {php[k]}\n" for k in ("memory_limit", "upload_max_filesize", "post_max_size", "max_execution_time")) +
            f"display_errors = {'On' if php.get('display_errors') else 'Off'}\n")


def apply(st: dict) -> None:
    """Write nginx + PHP config; keep the old nginx config if the new one doesn't pass `nginx -t`."""
    if not installed():
        raise HTTPException(400, "Web Station is not installed")
    root = web_root(create=True)
    ver = php_version()
    conf = render(st, root, ver)
    if DEV:
        open(NGINX_CONF, "w").write(conf)
        return
    for s in st["sites"]:
        if s.get("enabled") and s["kind"] != "proxy":
            os.makedirs(site_root(s, root), exist_ok=True)
    old = open(NGINX_CONF).read() if os.path.exists(NGINX_CONF) else None
    with open(NGINX_CONF, "w") as f:
        f.write(conf)
    for d in ("/etc/nginx/sites-enabled/default",):
        if os.path.lexists(d):
            os.remove(d)
    t = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if t.returncode != 0:
        if old is None:
            os.remove(NGINX_CONF)
        else:
            open(NGINX_CONF, "w").write(old)
        raise HTTPException(400, "web server rejected the settings: " + (t.stderr.strip().splitlines() or ["?"])[-1])
    if ver:
        ini = f"/etc/php/{ver}/fpm/conf.d/99-cynthology.ini"
        new = php_ini(st["php"])
        if not os.path.exists(ini) or open(ini).read() != new:
            open(ini, "w").write(new)
            run(["systemctl", "restart", f"php{ver}-fpm"], check=False)
    if st.get("enabled", True):
        if active("nginx"):
            run(["systemctl", "reload", "nginx"])
        else:
            r = subprocess.run(["systemctl", "enable", "--now", "nginx"], capture_output=True, text=True)
            if r.returncode != 0:
                raise HTTPException(400, "web server could not start (is another app using port 80/443?): " +
                                    subprocess.run(["journalctl", "-u", "nginx", "-n", "3", "-o", "cat"], capture_output=True, text=True).stdout.strip())


# ---------- API ----------

@router.get("")
def status(_=Depends(require_admin)):
    st = load()
    ver = php_version() if installed() else None
    exts = []
    if ver and not DEV:
        exts = [line.strip() for line in subprocess.run(["php", "-m"], capture_output=True, text=True).stdout.splitlines()
                if line.strip() and not line.startswith("[")]
    return {"installed": installed(), "installing": not DEV and active("cynthology-webstation-install"),
            "running": DEV or active("nginx"), "php_running": DEV or bool(ver and active(f"php{ver}-fpm")),
            "enabled": st.get("enabled", True), "php_version": ver, "extensions": sorted(set(exts), key=str.lower),
            "web_root": web_root(), "sites": st["sites"], "php": {**PHP_DEFAULTS, **st.get("php", {})}}


@router.post("/install")
def install(_=Depends(require_admin)):
    if DEV:
        return {"job": None}
    # policy-rc.d keeps nginx from starting with Debian's default site (port 80 may belong to an app)
    script = ("set -e; export DEBIAN_FRONTEND=noninteractive; apt-get update -q; P=/usr/sbin/policy-rc.d; "
              "if [ ! -e $P ]; then printf '#!/bin/sh\\nexit 101\\n' > $P; chmod 755 $P; trap 'rm -f $P' EXIT; fi; "
              f"apt-get install -y -q --no-install-recommends {' '.join(PACKAGES)}; "
              "rm -f /etc/nginx/sites-enabled/default; echo 'Web Station installed.'")
    return {"job": ct.start_job("Install Web Station", ["sh", "-c", script])}


@router.post("/setup")
def setup(_=Depends(require_admin)):
    """After installing: create the web shared folder and a default website on ports 80/443."""
    from .apps import listening_ports
    st = load()
    if not st["sites"]:
        busy = listening_ports() | set(ct.used_host_ports()) if not DEV else set()
        http, https = (80, 443) if not {80, 443} & busy else (8880, 8443)   # e.g. a hosting panel already has 80/443
        st["sites"] = [{"id": secrets.token_hex(4), "name": "Default website", "enabled": True, "type": "port", "hostname": "",
                        "http_port": http, "https_port": https, "redirect_https": False, "kind": "php", "folder": "", "proxy": ""}]
    st["enabled"] = True
    apply(st)
    save(st)
    return status()


class Enabled(BaseModel):
    enabled: bool


@router.put("/enabled")
def set_enabled(body: Enabled, _=Depends(require_admin)):
    st = load()
    st["enabled"] = body.enabled
    if body.enabled:
        apply(st)
    else:
        run(["systemctl", "disable", "--now", "nginx"], check=False)
    save(st)
    return {"ok": True}


@router.put("/sites")
def put_site(site: Site, _=Depends(require_admin)):
    st = load()
    validate(site, st["sites"])
    if not site.id:
        site.id = secrets.token_hex(4)
    site.hostname = " ".join(site.hostname.split()) if site.type == "name" else ""
    site.folder = "/".join(p for p in site.folder.strip("/").split("/") if p)
    new = dict(st, sites=[s for s in st["sites"] if s["id"] != site.id] + [site.model_dump()])
    new["sites"].sort(key=lambda s: s["name"].lower())
    apply(new)
    save(new)
    return {"ok": True, "id": site.id}


@router.delete("/sites/{sid}")
def delete_site(sid: str, _=Depends(require_admin)):
    st = load()
    new = dict(st, sites=[s for s in st["sites"] if s["id"] != sid])
    apply(new)
    save(new)
    return {"ok": True}


@router.put("/php")
def put_php(body: Php, _=Depends(require_admin)):
    for k in ("memory_limit", "upload_max_filesize", "post_max_size"):
        if not SIZE_RE.match(getattr(body, k)):
            raise HTTPException(400, f"{k}: a size like 256M or 2G")
    if not body.max_execution_time.isdigit():
        raise HTTPException(400, "max_execution_time: seconds")
    st = load()
    st["php"] = body.model_dump()
    apply(st)
    save(st)
    return {"ok": True}
