"""SNMP (net-snmp's snmpd): let monitoring tools (PRTG, LibreNMS, Zabbix, Home Assistant, ...)
read the NAS's status - read-only, never writable.

  v2c  - a community name (a shared password sent in the clear: for the local network only)
  v3   - a user name with an authentication password (SHA-256) and an encryption password (AES)
  networks - who may ask (empty = the local networks this NAS is on)
What is offered: the standard system, interface, IP, host-resources (CPU, memory, storage, processes)
and UCD (load, disks) tables - with every volume listed.
"""
import ipaddress
import os
import re
import socket
import subprocess

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import settings
from .auth import require_admin
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/snmp", tags=["snmp"])
CONF = os.path.join(STATE_DIR, "snmpd.conf") if DEV else "/etc/snmp/snmpd.conf"
PERSIST = os.path.join(STATE_DIR, "snmpd-persist.conf") if DEV else "/var/lib/snmp/snmpd.conf"
COMMUNITY_RE = re.compile(r"[A-Za-z0-9._-]{4,64}")
USER_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,31}")
SECRET_RE = re.compile(r"[!$%&()*+,./0-9:;<=>?@A-Z\[\]^_a-z{|}~-]{8,64}")     # (no spaces, quotes, # or backslash)
TEXT_RE = re.compile(r"[^\x00-\x1f\x7f\"\\]{0,100}")


def get() -> dict:
    s = settings.get("snmp") or {}
    return {"enabled": bool(s.get("enabled")), "v2c": bool(s.get("v2c", True)), "community": s.get("community", ""),
            "v3": bool(s.get("v3")), "user": s.get("user", ""), "has_v3_secrets": bool(s.get("auth_pw")),
            "networks": s.get("networks", []), "location": s.get("location", ""), "contact": s.get("contact", "")}


def installed() -> bool:
    return DEV or os.path.exists("/usr/sbin/snmpd")


def _default_networks() -> list[str]:
    try:
        from .vpn import lan_networks
        return lan_networks() or ["192.168.0.0/16"]
    except Exception:
        return ["192.168.0.0/16"]


def render(s: dict, networks: list[str]) -> str:
    """snmpd.conf (pure; unit tested). Read-only views only - nothing can be changed over SNMP."""
    out = ["# Managed by Cynthology (Control Panel > SNMP) - do not edit",
           "agentAddress udp:161,udp6:161",
           f'sysName {socket.gethostname()}',
           f'sysLocation {s.get("location") or "NAS"}',
           f'sysContact {s.get("contact") or "administrator"}',
           "sysServices 72",
           "view all included .1",
           "includeAllDisks 10%",
           "load 12 10 5",
           "dontLogTCPWrappersConnects yes"]
    if s.get("v2c") and s.get("community"):
        for n in networks + ["127.0.0.1/32"]:
            out.append(f"{'rocommunity6' if ':' in n else 'rocommunity'} {s['community']} {n} -V all")
    if s.get("v3") and s.get("user"):
        out.append(f"rouser {s['user']} priv -V all")
    return "\n".join(x for x in out if x) + "\n"


class Snmp(BaseModel):
    enabled: bool = False
    v2c: bool = True
    community: str = ""
    v3: bool = False
    user: str = ""
    auth_pw: str = ""            # empty = keep
    priv_pw: str = ""
    networks: list[str] = []
    location: str = ""
    contact: str = ""


@router.get("")
def status(_=Depends(require_admin)):
    active = DEV or subprocess.run(["systemctl", "is-active", "snmpd"], capture_output=True, text=True).stdout.strip() == "active"
    return {**get(), "installed": installed(), "running": get()["enabled"] and active, "default_networks": _default_networks()}


@router.put("")
def put(body: Snmp, _=Depends(require_admin)):
    old = settings.get("snmp") or {}
    if body.v2c and body.enabled and not COMMUNITY_RE.fullmatch(body.community):
        raise HTTPException(400, "community: 4-64 letters, digits, . _ or -")
    if body.community.lower() in ("public", "private") and body.v2c and body.enabled:
        raise HTTPException(400, "choose your own community name - 'public' and 'private' are the first ones anyone tries")
    nets = []
    for n in body.networks:
        n = n.strip()
        if n:
            try:
                nets.append(str(ipaddress.ip_network(n, strict=False)))
            except ValueError:
                raise HTTPException(400, f"'{n}' isn't a network (e.g. 192.168.1.0/24)")
    for k in ("location", "contact"):
        if not TEXT_RE.fullmatch(getattr(body, k)):
            raise HTTPException(400, f"{k}: up to 100 characters, no quotes or backslashes")
    auth_pw, priv_pw = body.auth_pw or old.get("auth_pw", ""), body.priv_pw or old.get("priv_pw", "")
    if body.v3 and body.enabled:
        if not USER_RE.fullmatch(body.user):
            raise HTTPException(400, "v3 user name: letters, digits, . _ -")
        if not (SECRET_RE.fullmatch(auth_pw) and SECRET_RE.fullmatch(priv_pw)):
            raise HTTPException(400, "v3 passwords: 8-64 characters, no spaces, quotes, # or backslash")
    if body.enabled and not body.v2c and not body.v3:
        raise HTTPException(400, "turn on v2c or v3")
    new = {"enabled": body.enabled, "v2c": body.v2c, "community": body.community, "v3": body.v3, "user": body.user,
           "auth_pw": auth_pw, "priv_pw": priv_pw, "networks": nets, "location": body.location.strip(), "contact": body.contact.strip()}
    settings.update(snmp=new)
    apply(new, old)
    return status(None)


def apply(s: dict, old: dict | None = None) -> None:
    if DEV:
        with open(CONF, "w") as f:
            f.write(render(s, s["networks"] or _default_networks()))
        return
    if not s["enabled"]:
        run(["systemctl", "disable", "--now", "snmpd"], check=False)
        return
    if not installed():
        env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        r = subprocess.run(["apt-get", "install", "-y", "-q", "--no-install-recommends", "snmpd"], capture_output=True, text=True, env=env, timeout=900)
        if r.returncode != 0:
            raise HTTPException(500, "couldn't install snmpd: " + (r.stderr or r.stdout).strip()[-300:])
    run(["systemctl", "stop", "snmpd"], check=False)
    fd = os.open(CONF + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # (holds the community name)
    with os.fdopen(fd, "w") as f:
        f.write(render(s, s["networks"] or _default_networks()))
    os.replace(CONF + ".new", CONF)
    _v3_user(s, old or {})
    run(["systemctl", "enable", "--now", "snmpd"])


def _v3_user(s: dict, old: dict) -> None:
    """v3 users live in snmpd's own persistent file: snmpd turns a createUser line into keys on start
    (it has to be stopped while we edit it). Old users of ours are removed first."""
    try:
        lines = open(PERSIST).read().splitlines()
    except FileNotFoundError:
        lines = []
    drop = {u for u in (old.get("user"), s.get("user")) if u}
    keep = [ln for ln in lines if not (ln.startswith("createUser ") and ln.split()[1:2] and ln.split()[1] in drop)
            and not (ln.startswith("usmUser ") and any(_usm_name(ln) == u for u in drop))]
    if s.get("v3") and s.get("user"):
        keep.append(f"createUser {s['user']} SHA-256 {s['auth_pw']} AES {s['priv_pw']}")
    os.makedirs(os.path.dirname(PERSIST), exist_ok=True)
    fd = os.open(PERSIST + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(keep) + "\n")
    os.replace(PERSIST + ".new", PERSIST)


def _usm_name(line: str) -> str:
    """The user name in a stored 'usmUser 1 3 <engine> "name" "name" ...' line (or 0x<hex>)."""
    parts = line.split()
    if len(parts) < 5:
        return ""
    v = parts[4]
    try:
        return bytes.fromhex(v[2:]).decode() if v.startswith("0x") else v.strip('"')
    except ValueError:
        return ""
