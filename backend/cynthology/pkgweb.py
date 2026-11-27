"""DSM package compatibility: stand-ins for Synology's own packages, and package web services.

Stand-ins: many SynoCommunity packages depend on DSM packages by name (WebStation, PHP8.4,
Apache2.4, Perl, Node.js_v20, ContainerManager). Cynthology provides each from Debian (PHP 8.4,
Apache 2.4, Perl, Node.js 20 - the same versions or newer) or from its own features (Web Station,
Docker), and registers it under /var/packages/<name> like DSM, with the command names package
scripts call (/usr/local/bin/php84 ...).

Web services: a package's conf/resource "webservice" says what Web Station should publish:
  pkg_dir_prepare  copy the package's web folder to <web_packages>/<target> (owner, mode)
  services         static files, or PHP (an own PHP-FPM pool running as the package's account,
                   with the extensions it lists); apache_php ones go through Apache so their
                   .htaccess rules (often protecting config/data folders) still apply
  portals          alias /<name>/ on Web Station's default HTTP/HTTPS server (or own ports)
"""
import json
import os
import re
import shutil
import subprocess

from fastapi import HTTPException

from .util import DEV, STATE_DIR, run

STATE = os.path.join(STATE_DIR, "pkgweb.json")
ROOT = os.path.join(STATE_DIR, "root") if DEV else "/"
WEB_PACKAGES = os.path.join(STATE_DIR, "web_packages") if DEV else "/var/lib/cynthology/web_packages"
FPM_POOLS = os.path.join(STATE_DIR, "fpm") if DEV else "/etc/php/8.4/fpm/pool.d"
APACHE_SITE = os.path.join(STATE_DIR, "apache-cyn-packages.conf") if DEV else "/etc/apache2/sites-available/cyn-packages.conf"
APACHE_PORT = 8180                       # Apache listens on localhost only; nginx (Web Station) proxies to it
PHP_EXT = {"mysqli": "mysql", "pdo_mysql": "mysql", "mysqlnd": "mysql", "pgsql": "pgsql", "pdo_pgsql": "pgsql",
           "pdo_sqlite": "sqlite3", "sqlite3": "sqlite3", "pdo_dblib": "sybase", "curl": "curl", "gd": "gd",
           "mbstring": "mbstring", "xml": "xml", "dom": "xml", "simplexml": "xml", "zip": "zip", "intl": "intl",
           "bcmath": "bcmath", "imagick": "imagick", "ldap": "ldap", "soap": "soap", "redis": "redis", "apcu": "apcu",
           "bz2": "bz2", "gmp": "gmp", "opcache": "opcache", "memcached": "memcached", "xsl": "xsl"}
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")

STANDINS = {
    "WebStation": {"display": "Web Station", "version": "4.0.0-0001", "apt": []},
    "PHP8.4": {"display": "PHP 8.4", "version": "8.4.0-0001", "apt": ["php8.4-fpm", "php8.4-cli"],
               "links": {"usr/local/bin/php84": "/usr/bin/php8.4"}},
    "PHP8.3": {"display": "PHP 8.3 (provided by PHP 8.4)", "version": "8.3.0-0001", "apt": ["php8.4-fpm", "php8.4-cli"],
               "links": {"usr/local/bin/php83": "/usr/bin/php8.4"}},
    "PHP8.2": {"display": "PHP 8.2 (provided by PHP 8.4)", "version": "8.2.0-0001", "apt": ["php8.4-fpm", "php8.4-cli"],
               "links": {"usr/local/bin/php82": "/usr/bin/php8.4"}},
    "Apache2.4": {"display": "Apache HTTP Server 2.4", "version": "2.4.0-0001", "apt": ["apache2"]},
    "Perl": {"display": "Perl", "version": "5.40.0-0001", "apt": ["perl"], "links": {"usr/local/bin/perl": "/usr/bin/perl"}},
    "Node.js_v20": {"display": "Node.js v20", "version": "20.0.0-0001", "apt": ["nodejs"],
                    "links": {"usr/local/bin/node": "/usr/bin/node"}},
    "ContainerManager": {"display": "Container Manager (Docker)", "version": "24.0.0-0001", "apt": []},
    "Docker": {"display": "Docker", "version": "24.0.0-0001", "apt": []},
}


def is_standin(name: str) -> bool:
    return name in STANDINS


def _pkgdir(name: str) -> str:
    return os.path.join(ROOT, "var/packages", name)


def ensure_standin(name: str) -> None:
    """Install what a DSM package name stands for and register it under /var/packages."""
    s = STANDINS[name]
    if not DEV:
        if s["apt"]:
            r = subprocess.run(["apt-get", "install", "-y", "-q", *s["apt"]], capture_output=True, text=True,
                               env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"}, timeout=1800)
            if r.returncode != 0:
                raise HTTPException(500, f"couldn't install {name}: {(r.stderr or r.stdout).strip()[-300:]}")
        if name == "WebStation":
            from . import webstation
            if not webstation.installed():
                webstation.install_now()
        if name == "Apache2.4":
            setup_apache()
        if name in ("ContainerManager", "Docker") and not shutil.which("docker"):
            raise HTTPException(500, "the container engine is not installed")
    for link, target in s.get("links", {}).items():
        path = os.path.join(ROOT, link)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.lexists(path):
            os.symlink(target, path)
    d = _pkgdir(name)
    os.makedirs(os.path.join(d, "target"), exist_ok=True)
    with open(os.path.join(d, "INFO"), "w") as f:
        f.write(f'package="{name}"\nversion="{s["version"]}"\ndisplayname="{s["display"]}"\n'
                f'maintainer="Cynthology (stand-in for the DSM package)"\narch="noarch"\n'
                f'description="Provided by Cynthology from Debian / built-in features so that packages needing it work."\n')
    open(os.path.join(d, "cynthology-standin"), "w").close()
    open(os.path.join(d, "enabled"), "w").close()


# ---------- web services ----------

def load() -> dict:
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def save(st: dict) -> None:
    with open(STATE + ".new", "w") as f:
        json.dump(st, f, indent=1)
    os.replace(STATE + ".new", STATE)


def parse_webservice(pkg: str, res: dict) -> dict:
    """Validate a package's "webservice" resource (pure; unit tested). Untrusted input from the .spk."""
    ws = res.get("webservice") or {}
    services, portals, prepare = [], [], []
    for sv in ws.get("services", []):
        sid, root = str(sv.get("service", "")), str(sv.get("root", "")).strip("/")
        if not NAME_RE.fullmatch(sid) or not NAME_RE.fullmatch(root):
            continue
        typ = sv.get("type", "static")
        php = sv.get("php") or {}
        if typ not in ("static", "apache_php", "nginx_php"):
            continue
        user = str(php.get("user") or f"sc-{pkg}")
        if not re.fullmatch(r"sc-[a-z0-9_-]{1,30}|[a-z_][a-z0-9_-]{0,31}", user) or user in ("root", "www-data"):
            user = f"sc-{pkg}"
        services.append({"service": sid, "type": typ, "root": root, "display": str(sv.get("display_name", sid))[:60],
                         "php": {"user": user, "extensions": [e for e in php.get("extensions", []) if e in PHP_EXT],
                                 "settings": {k: str(v) for k, v in (php.get("php_settings") or {}).items()
                                              if re.fullmatch(r"[a-z_.]{1,40}", k) and re.fullmatch(r"[A-Za-z0-9 ._/:-]{0,80}", str(v))}}
                         if typ != "static" else None})
    known = {s["service"] for s in services}
    for pt in ws.get("portals", []):
        if pt.get("service") in known and pt.get("type") == "alias" and NAME_RE.fullmatch(str(pt.get("alias", ""))):
            portals.append({"service": pt["service"], "alias": pt["alias"], "name": str(pt.get("name", pt["alias"]))[:60]})
    for pp in ws.get("pkg_dir_prepare", []):
        src, tgt = str(pp.get("source", "")), str(pp.get("target", "")).strip("/")
        if not NAME_RE.fullmatch(tgt) or not src.startswith(f"/var/packages/{pkg}/") or ".." in src.split("/"):
            continue
        mode = str(pp.get("mode", "0755"))
        prepare.append({"source": src, "target": tgt, "mode": mode if re.fullmatch(r"0?[0-7]{3}", mode) else "0755"})
    return {"services": services, "portals": portals, "prepare": prepare}


def render_nginx(st: dict) -> list[str]:
    """nginx location blocks for every package portal (pure; unit tested)."""
    out = []
    for pkg, ws in sorted(st.items()):
        svc = {s["service"]: s for s in ws["services"]}
        for pt in ws["portals"]:
            s, a = svc[pt["service"]], pt["alias"]
            root = os.path.join(WEB_PACKAGES, s["root"])
            out.append(f"    location = /{a} {{ return 301 /{a}/; }}   # package {pkg}")
            if s["type"] == "static":
                out.append(f"    location ^~ /{a}/ {{ alias {root}/; index index.html index.htm; }}")
            elif s["type"] == "nginx_php":
                out.append(f"    location ^~ /{a}/ {{ alias {root}/; index index.php index.html;\n"
                           f"        location ~ \\.php$ {{ include fastcgi_params; fastcgi_param SCRIPT_FILENAME $request_filename;\n"
                           f"            fastcgi_pass unix:/run/php/cyn-pkg-{s['service']}.sock; }} }}")
            else:   # apache_php: Apache applies the package's .htaccess rules
                out.append(f"    location ^~ /{a}/ {{ proxy_pass http://127.0.0.1:{APACHE_PORT}/{a}/; proxy_set_header Host $host;\n"
                           "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto $scheme;\n"
                           "        client_max_body_size 512M; proxy_read_timeout 600s; }")
    return out


def render_fpm_pool(s: dict) -> str:
    php = s["php"]
    root = os.path.join(WEB_PACKAGES, s["root"])
    lines = [f"[cyn-pkg-{s['service']}]", f"user = {php['user']}", "group = http",
             f"listen = /run/php/cyn-pkg-{s['service']}.sock", "listen.owner = www-data", "listen.group = www-data",
             "listen.mode = 0660", "pm = ondemand", "pm.max_children = 8", "pm.process_idle_timeout = 60s",
             f"php_admin_value[open_basedir] = {root}:/tmp:/var/packages/:/usr/share/php"]
    lines += [f"php_value[{k}] = {v}" for k, v in sorted(php["settings"].items())]
    return "\n".join(lines) + "\n"


def render_apache(st: dict) -> str:
    out = [f"# Managed by Cynthology - package web services that need Apache (.htaccess)", f"Listen 127.0.0.1:{APACHE_PORT}",
           f"<VirtualHost 127.0.0.1:{APACHE_PORT}>", "    ServerName localhost", "    RemoteIPHeader X-Forwarded-For",
           "    RemoteIPInternalProxy 127.0.0.1 ::1"]
    for pkg, ws in sorted(st.items()):
        svc = {s["service"]: s for s in ws["services"]}
        for pt in ws["portals"]:
            s = svc[pt["service"]]
            if s["type"] != "apache_php":
                continue
            root = os.path.join(WEB_PACKAGES, s["root"])
            out += [f"    Alias /{pt['alias']} {root}", f"    <Directory {root}>", "        Options -Indexes +FollowSymLinks",
                    "        AllowOverride All", "        Require all granted", "        DirectoryIndex index.php index.html",
                    "        <FilesMatch \\.php$>",
                    f'            SetHandler "proxy:unix:/run/php/cyn-pkg-{s["service"]}.sock|fcgi://localhost"',
                    "        </FilesMatch>", "    </Directory>"]
    out.append("</VirtualHost>")
    return "\n".join(out) + "\n"


def setup_apache() -> None:
    if DEV:
        return
    # (Debian's default site and port 80 belong to Web Station's nginx)
    run(["a2dissite", "-q", "000-default"], check=False)
    ports = "/etc/apache2/ports.conf"
    with open(ports, "w") as f:
        f.write("# Managed by Cynthology: Apache only serves package web services, on localhost (see cyn-packages.conf)\n")
    run(["a2enmod", "-q", "proxy_fcgi", "setenvif", "rewrite", "headers", "remoteip", "alias"], check=False)


def _ensure_http_group() -> None:
    if DEV:
        return
    if subprocess.run(["getent", "group", "http"], capture_output=True).returncode != 0:
        run(["groupadd", "-r", "http"])
    run(["usermod", "-aG", "http", "www-data"], check=False)


def _services_link() -> None:
    """DSM 7 scripts edit /var/services/web_packages/<target>/... (e.g. Adminer's .htaccess)."""
    if DEV:
        return
    link = "/var/services/web_packages"
    os.makedirs("/var/services", mode=0o755, exist_ok=True)
    if not os.path.lexists(link):
        os.symlink(WEB_PACKAGES, link)


def apply_package(pkg: str, base: str) -> dict | None:
    """After a package is installed/upgraded: publish its web services."""
    try:
        res = json.load(open(os.path.join(base, "conf", "resource")))
    except (OSError, ValueError):
        return None
    ws = parse_webservice(pkg, res)
    if not ws["services"]:
        return None
    _ensure_http_group()
    os.makedirs(WEB_PACKAGES, mode=0o755, exist_ok=True)
    _services_link()
    for pp in ws["prepare"]:
        src = os.path.join(ROOT, pp["source"].lstrip("/"))
        dst = os.path.join(WEB_PACKAGES, pp["target"])
        if not os.path.isdir(src):
            continue
        if DEV:
            shutil.copytree(src, dst, dirs_exist_ok=True)
            continue
        user = next((s["php"]["user"] for s in ws["services"] if s["root"] == pp["target"] and s["php"]), f"sc-{pkg}")
        if subprocess.run(["id", user], capture_output=True).returncode != 0:
            user = "root"
        # (no --delete: an upgrade must not wipe what the app wrote into its own folder)
        run(["rsync", "-a", "--no-owner", "--no-group", src.rstrip("/") + "/", dst + "/"])
        run(["chown", "-R", f"{user}:http", dst])
        run(["chmod", "-R", "u+rwX,g+rX,o-rwx", dst])
        os.chmod(dst, 0o750)                     # (package account + web server group only)
    st = load()
    st[pkg] = ws
    save(st)
    reload_all(st)
    return ws


def remove_package(pkg: str) -> None:
    st = load()
    ws = st.pop(pkg, None)
    if not ws:
        return
    save(st)
    for s in ws["services"]:
        p = os.path.join(FPM_POOLS, f"cyn-pkg-{s['service']}.conf")
        if os.path.exists(p):
            os.remove(p)
    for pp in ws["prepare"]:
        shutil.rmtree(os.path.join(WEB_PACKAGES, pp["target"]), ignore_errors=True)
    reload_all(st)


def reload_all(st: dict | None = None) -> None:
    st = load() if st is None else st
    php = [s for ws in st.values() for s in ws["services"] if s["php"]]
    if php:
        os.makedirs(FPM_POOLS, exist_ok=True)
        exts = sorted({PHP_EXT[e] for s in php for e in s["php"]["extensions"]})
        if exts and not DEV:
            subprocess.run(["apt-get", "install", "-y", "-q", *[f"php8.4-{e}" for e in exts]], capture_output=True,
                           env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"}, timeout=1800)
        for s in php:
            with open(os.path.join(FPM_POOLS, f"cyn-pkg-{s['service']}.conf"), "w") as f:
                f.write(render_fpm_pool(s))
        if not DEV:
            run(["systemctl", "reload-or-restart", "php8.4-fpm"], check=False)
    if any(s["type"] == "apache_php" for ws in st.values() for s in ws["services"]):
        with open(APACHE_SITE, "w") as f:
            f.write(render_apache(st))
        if not DEV:
            run(["a2ensite", "-q", "cyn-packages"], check=False)
            run(["systemctl", "enable", "apache2"], check=False)
            run(["systemctl", "reload-or-restart", "apache2"], check=False)
    from . import webstation
    try:
        webstation.apply(webstation.load())
    except HTTPException as e:
        print("web station:", e.detail)
