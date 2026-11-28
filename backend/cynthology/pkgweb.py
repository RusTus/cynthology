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
APACHE_TLS_PORT = 8443                   # ... and here for https:// visitors (see render_apache)
PHP_EXT = {"mysqli": "mysql", "pdo_mysql": "mysql", "mysqlnd": "mysql", "pgsql": "pgsql", "pdo_pgsql": "pgsql",
           "pdo_sqlite": "sqlite3", "sqlite3": "sqlite3", "pdo_dblib": "sybase", "curl": "curl", "gd": "gd",
           "mbstring": "mbstring", "xml": "xml", "dom": "xml", "simplexml": "xml", "zip": "zip", "intl": "intl",
           "bcmath": "bcmath", "imagick": "imagick", "ldap": "ldap", "soap": "soap", "redis": "redis", "apcu": "apcu",
           "bz2": "bz2", "gmp": "gmp", "opcache": "opcache", "memcached": "memcached", "xsl": "xsl"}
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
# built into Synology's PHP (so packages don't list them), separate packages on Debian
PHP_BUILTIN = ["xml", "mbstring", "curl", "zip", "gd", "intl", "mysql", "bcmath"]

STANDINS = {
    "WebStation": {"display": "Web Station", "version": "4.0.0-0001", "apt": []},
    "PHP8.4": {"display": "PHP 8.4", "version": "8.4.0-0001", "apt": ["php8.4-fpm", "php8.4-cli"],
               "links": {"usr/local/bin/php84": "/usr/bin/php8.4"}},
    "PHP8.3": {"display": "PHP 8.3", "version": "8.3.0-0001", "apt": ["php8.4-fpm", "php8.4-cli"],
               "links": {"usr/local/bin/php83": "/usr/bin/php8.4"}},
    "PHP8.2": {"display": "PHP 8.2 (provided by PHP 8.4)", "version": "8.2.0-0001", "apt": ["php8.4-fpm", "php8.4-cli"],
               "links": {"usr/local/bin/php82": "/usr/bin/php8.4"}},
    "Apache2.4": {"display": "Apache HTTP Server 2.4", "version": "2.4.0-0001", "apt": ["apache2"]},
    "Perl": {"display": "Perl", "version": "5.40.0-0001", "apt": ["perl"], "links": {"usr/local/bin/perl": "/usr/bin/perl"}},
    "Node.js_v20": {"display": "Node.js v20", "version": "20.0.0-0001", "apt": ["nodejs"],
                    "links": {"usr/local/bin/node": "/usr/bin/node"}},
    "MariaDB10": {"display": "MariaDB 10", "version": "10.11.0-0001", "apt": []},
    "ContainerManager": {"display": "Container Manager (Docker)", "version": "24.0.0-0001", "apt": []},
    "Docker": {"display": "Docker", "version": "24.0.0-0001", "apt": []},
}


# tile label and gradient for each module's icon (drawn here - no third-party logos)
STANDIN_ICONS = {
    "WebStation": ("WS", "#38bdf8", "#2563eb"),
    "PHP8.4": ("PHP", "#a78bfa", "#4f46e5"),
    "PHP8.3": ("PHP", "#a78bfa", "#4f46e5"),
    "PHP8.2": ("PHP", "#a78bfa", "#4f46e5"),
    "Apache2.4": ("Ap", "#f87171", "#b91c1c"),
    "Perl": ("Pl", "#60a5fa", "#1e40af"),
    "Node.js_v20": ("JS", "#4ade80", "#15803d"),
    "MariaDB10": ("Ma", "#60a5fa", "#1d4ed8"),
    "ContainerManager": ("CM", "#38bdf8", "#0369a1"),
    "Docker": ("D", "#38bdf8", "#0369a1"),
}


def _own_account(pkg: str, user: str) -> bool:
    """Is user this package's own account: sc-<pkg>, or the account it was given (system range, home in the
    package's folder)? Any other account (another package's, a NAS user's, cynrepl, mysql...) is refused."""
    if user == f"sc-{pkg}":
        return True
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) or user in ("root", "www-data", "http"):
        return False
    if DEV:
        return True
    import pwd
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        return False
    return 100 <= pw.pw_uid < 1000 and pw.pw_dir == os.path.join(ROOT, "var/packages", pkg, "home")


def standin_icon(name: str) -> str | None:
    """A rounded gradient tile with a short label, in the style of the desktop's app icons."""
    if name not in STANDIN_ICONS:
        return None
    label, c1, c2 = STANDIN_ICONS[name]
    size = {1: 120, 2: 100, 3: 78}.get(len(label), 64)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">'
            f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{c1}"/>'
            f'<stop offset="1" stop-color="{c2}"/></linearGradient></defs>'
            f'<rect x="8" y="8" width="240" height="240" rx="56" fill="url(#g)"/>'
            f'<text x="128" y="128" dy=".35em" text-anchor="middle" font-family="Inter,Segoe UI,Helvetica,Arial,sans-serif" '
            f'font-weight="700" font-size="{size}" fill="#fff">{label}</text></svg>')


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
        if name == "MariaDB10":
            from . import mariadb10
            mariadb10.setup()
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
        if not _own_account(pkg, user):          # (PHP runs as the package's own account - never someone else's)
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
                # https:// visitors go to Apache's HTTPS side, so the package sees HTTPS (its redirects don't loop).
                # (a loopback hop to our own Apache with our own certificate - nothing leaves this machine)
                out.append(f"    location ^~ /{a}/ {{ set $cyn_be http://127.0.0.1:{APACHE_PORT};\n"
                           f"        if ($scheme = https) {{ set $cyn_be https://127.0.0.1:{APACHE_TLS_PORT}; }}\n"
                           "        proxy_pass $cyn_be; proxy_ssl_verify off; proxy_set_header Host $host;\n"
                           "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto $scheme;\n"
                           "        client_max_body_size 512M; proxy_read_timeout 600s; }")
    return out


def render_fpm_pool(s: dict) -> str:
    php = s["php"]
    root = os.path.join(WEB_PACKAGES, s["root"])
    lines = [f"[cyn-pkg-{s['service']}]", f"user = {php['user']}", "group = http",
             f"listen = /run/php/cyn-pkg-{s['service']}.sock", "listen.owner = www-data", "listen.group = www-data",
             "listen.mode = 0660", "pm = ondemand", "pm.max_children = 8", "pm.process_idle_timeout = 60s",
             f"php_admin_value[open_basedir] = {':'.join([root, *php.get('dirs', []), '/tmp', '/var/packages/', '/usr/share/php', '/dev/urandom'])}"]
    lines += [f"php_value[{k}] = {v}" for k, v in sorted(php["settings"].items())]
    return "\n".join(lines) + "\n"


def render_apache(st: dict, cert: str = "", key: str = "") -> str:
    """Two local-only virtual hosts with the same packages: plain HTTP (8180) for http:// visitors and
    HTTPS (8443, the NAS's certificate) for https:// ones - so a package's own "HTTPS only" rules in
    .htaccess see the truth and don't loop (nginx picks the one matching the visitor's scheme)."""
    body = []
    for pkg, ws in sorted(st.items()):
        svc = {s["service"]: s for s in ws["services"]}
        for pt in ws["portals"]:
            s = svc[pt["service"]]
            if s["type"] != "apache_php":
                continue
            root = os.path.join(WEB_PACKAGES, s["root"])
            body += [f"    Alias /{pt['alias']} {root}", f"    <Directory {root}>", "        Options -Indexes +FollowSymLinks",
                     "        AllowOverride All", "        Require all granted", "        DirectoryIndex index.php index.html",
                     "        <FilesMatch \\.php$>",
                     f'            SetHandler "proxy:unix:/run/php/cyn-pkg-{s["service"]}.sock|fcgi://localhost"',
                     "        </FilesMatch>", "    </Directory>"]
    common = ["    ServerName localhost", "    RemoteIPHeader X-Forwarded-For", "    RemoteIPInternalProxy 127.0.0.1 ::1"]
    out = ["# Managed by Cynthology - package web services that need Apache (.htaccess)", f"Listen 127.0.0.1:{APACHE_PORT}",
           f"<VirtualHost 127.0.0.1:{APACHE_PORT}>", *common, *body, "</VirtualHost>"]
    if cert and key:
        out += [f"Listen 127.0.0.1:{APACHE_TLS_PORT}", f"<VirtualHost 127.0.0.1:{APACHE_TLS_PORT}>", *common,
                "    SSLEngine on", f"    SSLCertificateFile {cert}", f"    SSLCertificateKeyFile {key}", *body, "</VirtualHost>"]
    return "\n".join(out) + "\n"


def setup_apache() -> None:
    if DEV:
        return
    # (Debian's default site and port 80 belong to Web Station's nginx)
    run(["a2dissite", "-q", "000-default"], check=False)
    ports = "/etc/apache2/ports.conf"
    with open(ports, "w") as f:
        f.write("# Managed by Cynthology: Apache only serves package web services, on localhost (see cyn-packages.conf)\n")
    run(["a2enmod", "-q", "proxy_fcgi", "setenvif", "rewrite", "headers", "remoteip", "alias", "ssl"], check=False)


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


def apply_package(pkg: str, base: str, copy: bool = True, publish: bool = True) -> dict | None:
    """Web services of a package. Like DSM: the web folder is copied (copy) after the files are unpacked
    and BEFORE postinst - packages finish setting themselves up there (ownCloud writes its config,
    Adminer its .htaccess) - and published (nginx/PHP-FPM/Apache, publish) after postinst."""
    try:
        res = json.load(open(os.path.join(base, "conf", "resource")))
    except (OSError, ValueError):
        return None
    ws = parse_webservice(pkg, res)
    if not ws["services"]:
        return None
    _set_php_version(ws, base)
    _ensure_http_group()
    if copy:
        ensure_php(ws["services"])                 # (before postinst: package installers run PHP tools)
    os.makedirs(WEB_PACKAGES, mode=0o755, exist_ok=True)
    _services_link()
    for pp in (ws["prepare"] if copy else []):
        src = os.path.join(ROOT, pp["source"].lstrip("/"))
        dst = os.path.join(WEB_PACKAGES, pp["target"])
        if not os.path.isdir(src):
            continue
        if DEV:
            shutil.copytree(src, dst, dirs_exist_ok=True)
            continue
        user = next((s["php"]["user"] for s in ws["services"] if s["root"] == pp["target"] and s["php"]), f"sc-{pkg}")
        ids = subprocess.run(["id", "-g", user], capture_output=True, text=True)
        # the copy is made as the package's account (the source is in its own folders, which it can change -
        # e.g. into a link to /etc - so root must not be the one reading it)
        drop = (["setpriv", f"--reuid={user}", f"--regid={ids.stdout.strip()}", "--init-groups", "--"]
                if ids.returncode == 0 and user != "root" else [])
        os.makedirs(dst, mode=0o750, exist_ok=True)
        if drop:
            run(["chown", "-h", f"{user}:http", dst])
        # (no --delete: an upgrade must not wipe what the app wrote into its own folder)
        run(drop + ["rsync", "-a", "--no-owner", "--no-group", src.rstrip("/") + "/", dst + "/"])
        run(["chown", "-R", "-h", f"{user if drop else 'root'}:http", dst])
        run(["chmod", "-R", "u+rwX,g+rX,o-rwx", dst])
        os.chmod(dst, 0o750)                     # (package account + web server group only)
    if not publish:
        return ws
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
        for v in ("8.4", "8.3"):
            p = os.path.join(_pool_dir(v), f"cyn-pkg-{s['service']}.conf")
            if os.path.exists(p):
                os.remove(p)
    for pp in ws["prepare"]:
        shutil.rmtree(os.path.join(WEB_PACKAGES, pp["target"]), ignore_errors=True)
    reload_all(st)


def php_version_for(base: str) -> str:
    """8.3 for packages that ask for PHP 8.3 when the real PHP 8.3 is installed (php83.py), else 8.4."""
    from . import php83
    try:
        info = open(os.path.join(base, "INFO")).read()
    except OSError:
        info = ""
    return "8.3" if re.search(r"install_dep_packages=.*PHP8\.3", info) and php83.installed() else "8.4"


def _set_php_version(ws: dict, base: str) -> None:
    v = php_version_for(base)
    # the package's own shared folders (DSM 7: /var/packages/<pkg>/shares/<name>) may be used by its PHP
    dirs = []
    sd = os.path.join(base, "shares")
    if os.path.isdir(sd):
        for n in sorted(os.listdir(sd)):
            real = os.path.realpath(os.path.join(sd, n))
            if os.path.isdir(real) and re.fullmatch(r"/[A-Za-z0-9._/@-]+", real) and real.count("/") >= 2:
                dirs.append(real)
    for s in ws["services"]:
        if s["php"]:
            s["php"]["version"] = v
            s["php"]["dirs"] = dirs


def ensure_php(services: list[dict]) -> None:
    """The PHP extensions (and Synology's built-in ones) for these services, per PHP version."""
    if DEV:
        return
    for v in ("8.4", "8.3"):
        mine = [s for s in services if s.get("php") and s["php"].get("version", "8.4") == v]
        if not mine:
            continue
        exts = sorted(set(PHP_BUILTIN) | {PHP_EXT[e] for s in mine for e in s["php"]["extensions"]})
        subprocess.run(["apt-get", "install", "-y", "-q", f"php{v}-fpm", f"php{v}-cli", *[f"php{v}-{e}" for e in exts]],
                       capture_output=True, env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"}, timeout=1800)


def refresh_php_versions() -> None:
    """After PHP 8.3 was installed or removed: move the packages that want it."""
    st = load()
    for pkg, ws in st.items():
        _set_php_version(ws, os.path.join(ROOT, "var/packages", pkg))
    save(st)
    reload_all(st)


def _pool_dir(v: str) -> str:
    return os.path.join(STATE_DIR, f"fpm{v}") if DEV else f"/etc/php/{v}/fpm/pool.d"


def reload_all(st: dict | None = None) -> None:
    st = load() if st is None else st
    php = [s for ws in st.values() for s in ws["services"] if s["php"]]
    for v in ("8.4", "8.3"):
        mine = [s for s in php if s["php"].get("version", "8.4") == v]
        d = _pool_dir(v)
        if os.path.isdir(d):                      # (pools that moved to the other version go)
            for f in os.listdir(d):
                if f.startswith("cyn-pkg-") and f[len("cyn-pkg-"):-len(".conf")] not in {s["service"] for s in mine}:
                    os.remove(os.path.join(d, f))
        if not mine:
            if not DEV and os.path.isdir(d) and v == "8.3":
                run(["systemctl", "reload-or-restart", "php8.3-fpm"], check=False)
            continue
        os.makedirs(d, exist_ok=True)
        ensure_php(mine)
        for s in mine:
            with open(os.path.join(d, f"cyn-pkg-{s['service']}.conf"), "w") as f:
                f.write(render_fpm_pool(s))
        if not DEV:
            run(["systemctl", "reload-or-restart", f"php{v}-fpm"], check=False)
    if any(s["type"] == "apache_php" for ws in st.values() for s in ws["services"]):
        from . import tls
        with open(APACHE_SITE, "w") as f:
            f.write(render_apache(st, *((tls.CERT, tls.KEY) if not DEV and os.path.exists(tls.CERT) else ("", ""))))
        if not DEV:
            run(["a2ensite", "-q", "cyn-packages"], check=False)
            run(["systemctl", "enable", "apache2"], check=False)
            run(["systemctl", "reload-or-restart", "apache2"], check=False)
    from . import webstation
    try:
        webstation.apply(webstation.load())
    except HTTPException as e:
        print("web station:", e.detail)
