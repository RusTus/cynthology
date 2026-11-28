"""Stand-in for Synology's "MariaDB10" package (needed by ownCloud, Nextcloud, WordPress, ... from the
package sources): Debian's MariaDB, set up the way those packages expect it.

  port 3307 on 127.0.0.1 (Synology's MariaDB 10 port - 3306 stays free for a MariaDB container)
  socket /run/mysqld/mysqld10.sock, clients at /usr/local/mariadb10/bin/{mysql,mysqldump,mysqladmin}
  root password: generated once and kept here (root-only); packages' install questions that ask for
  "the MariaDB root password" get it filled in. Root can also still sign in over the socket as the
  system root user (unix_socket), so nothing is lost if the file is.
"""
import json
import os
import re
import secrets
import subprocess

from fastapi import HTTPException

from .util import DEV, STATE_DIR

STATE = os.path.join(STATE_DIR, "mariadb10.json")
PORT = 3307
SOCKET = "/run/mysqld/mysqld10.sock"
CNF = "/etc/mysql/mariadb.conf.d/90-cynthology-mariadb10.cnf"
LINKS = {"usr/local/mariadb10/bin/mysql": "/usr/bin/mariadb", "usr/local/mariadb10/bin/mysqldump": "/usr/bin/mariadb-dump",
         "usr/local/mariadb10/bin/mysqladmin": "/usr/bin/mariadb-admin"}
ROOT_KEY_RE = re.compile(r"(mysql|maria|db)[a-z_]*root|root[a-z_]*(mysql|maria)", re.I)


def root_password() -> str:
    try:
        pw = json.load(open(STATE))["root_password"]
        if re.fullmatch(r"[A-Za-z0-9]{16,64}", pw):
            return pw
    except (OSError, ValueError, KeyError, TypeError):
        pass
    pw = secrets.token_urlsafe(24).replace("-", "x").replace("_", "y")
    fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"root_password": pw}, f)
    return pw


def setup() -> None:
    """Install and configure (idempotent)."""
    if DEV:
        return
    os.makedirs(os.path.dirname(CNF), exist_ok=True)
    with open(CNF, "w") as f:                    # (before the package: its first start already uses these)
        f.write("# Managed by Cynthology: Synology-compatible MariaDB 10 for packages (see mariadb10.py)\n"
                f"[mysqld]\nport = {PORT}\nbind-address = 127.0.0.1\nsocket = {SOCKET}\n\n"
                f"[client]\nport = {PORT}\nsocket = {SOCKET}\n")
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    pol = "/usr/sbin/policy-rc.d"
    made = not os.path.exists(pol)
    if made:                                     # (don't let the package start it on 3306 meanwhile)
        with open(pol, "w") as f:
            f.write("#!/bin/sh\nexit 101\n")
        os.chmod(pol, 0o755)
    try:
        r = subprocess.run(["apt-get", "install", "-y", "-q", "--no-install-recommends", "mariadb-server", "mariadb-client"],
                           capture_output=True, text=True, env=env, timeout=1800)
    finally:
        if made:
            os.remove(pol)
    if r.returncode != 0:
        raise HTTPException(500, "couldn't install MariaDB: " + (r.stderr or r.stdout).strip()[-300:])
    subprocess.run(["systemctl", "enable", "mariadb"], capture_output=True)
    r = subprocess.run(["systemctl", "restart", "mariadb"], capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(500, "MariaDB didn't start: " + subprocess.run(["journalctl", "-u", "mariadb", "-n", "8", "--no-pager"],
                                                                        capture_output=True, text=True).stdout[-400:])
    for link, target in LINKS.items():
        path = "/" + link
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.lexists(path) and os.path.exists(target):
            os.symlink(target, path)
    pw = root_password()
    # root keeps socket sign-in for the system's root user, and gets the password for packages
    sql = f"ALTER USER 'root'@'localhost' IDENTIFIED VIA unix_socket OR mysql_native_password USING PASSWORD('{pw}'); FLUSH PRIVILEGES;"
    r = subprocess.run(["mariadb", "--protocol=socket", "-S", SOCKET, "-u", "root", "-e", sql], capture_output=True, text=True)
    if r.returncode != 0:
        r = subprocess.run(["mariadb", "-S", SOCKET, "-u", "root", f"-p{pw}", "-e", "SELECT 1"], capture_output=True, text=True)
        if r.returncode != 0:
            raise HTTPException(500, "couldn't set the MariaDB root password: " + r.stderr.strip()[-200:])


def fill_wizard(wizard: dict) -> dict:
    """Answer 'MariaDB root password' questions (any key like wizard_mysql_password_root)."""
    pw = root_password()
    # (SynoCommunity packages read wizard_mysql_password_root - also when their wizard didn't show it)
    return {**wizard, "wizard_mysql_password_root": pw,
            **{k: pw for k in list(wizard) if ROOT_KEY_RE.search(k) and "password" in k.lower()}}


def prefill(wizard_steps: list) -> list:
    """The same for the wizard shown in the browser (as the field's default)."""
    pw = root_password()
    for step in wizard_steps or []:
        for item in (step.get("items", []) if isinstance(step, dict) else []):
            for sub in (item.get("subitems", []) if isinstance(item, dict) else []):
                if isinstance(sub, dict) and ROOT_KEY_RE.search(str(sub.get("key", ""))) and "password" in str(sub.get("key", "")).lower():
                    sub["defaultValue"] = pw
                    sub["desc"] = "Filled in by Cynthology (its MariaDB 10)"
    return wizard_steps


def info() -> dict:
    running = DEV or subprocess.run(["systemctl", "is-active", "mariadb"], capture_output=True, text=True).stdout.strip() == "active"
    return {"port": PORT, "socket": SOCKET, "user": "root", "password": root_password(), "running": running,
            "note": "Use these in Adminer or phpMyAdmin (server 127.0.0.1:3307). Your MariaDB container, if any, is separate (port 3306)."}
