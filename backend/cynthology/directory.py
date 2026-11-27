"""Domain / LDAP (Control Panel > Domain): join an Active Directory domain (or a Samba AD domain).

Samba becomes a domain member (security = ADS) with winbind: domain users and groups resolve on
this NAS (getent passwd 'CORP\\alice'), get stable IDs (idmap rid), can be given access to shared
folders (Shared Folders > Permissions: CORP\\alice, @CORP\\Domain Users) and can sign in to the web
interface (PAM via pam_winbind; administrators = members of the chosen domain group).

Settings that must be in smb.conf's [global] live in /etc/samba/cyn-global.conf, included at the
END of [global] (the last value wins); the stock workgroup/security lines are commented out with a
marker while joined and restored on leave.

Plain LDAP directories (OpenLDAP without Samba attributes) are not supported: SMB needs either AD
or LDAP with Samba schema.
"""
import json
import os
import re
import socket
import subprocess
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_admin
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/directory", tags=["directory"])
CFG = os.path.join(STATE_DIR, "directory.json")
SMB_CONF = os.path.join(STATE_DIR, "smb.conf") if DEV else "/etc/samba/smb.conf"
GLOBAL_INC = os.path.join(STATE_DIR, "cyn-global.conf") if DEV else "/etc/samba/cyn-global.conf"
KRB5 = os.path.join(STATE_DIR, "krb5.conf") if DEV else "/etc/krb5.conf"
NSSWITCH = os.path.join(STATE_DIR, "nsswitch.conf") if DEV else "/etc/nsswitch.conf"
MARK = "#cyn-domain# "
DOMAIN_RE = re.compile(r"^(?=.{3,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")


def load() -> dict:
    try:
        return json.load(open(CFG))
    except (OSError, ValueError):
        return {"joined": False}


def save(c: dict) -> None:
    with open(CFG, "w") as f:
        json.dump(c, f, indent=1)


def render_global(domain: str, workgroup: str, homes: str | None) -> str:
    """smb.conf [global] lines for an AD member (pure; unit tested)."""
    wg = workgroup.upper()
    lines = ["# Managed by Cynthology (Control Panel > Domain)",
             "   security = ADS", f"   realm = {domain.upper()}", f"   workgroup = {wg}",
             "   idmap config * : backend = tdb", "   idmap config * : range = 3000-7999",
             f"   idmap config {wg} : backend = rid", f"   idmap config {wg} : range = 100000-2099999",
             "   winbind use default domain = no", "   winbind enum users = yes", "   winbind enum groups = yes",
             "   winbind refresh tickets = yes", "   winbind offline logon = yes",
             "   template shell = /usr/sbin/nologin",
             f"   template homedir = {homes}/%U" if homes else "   template homedir = /nonexistent",
             "   kerberos method = secrets and keytab", "   dedicated keytab file = /etc/krb5.keytab"]
    return "\n".join(lines) + "\n"


def _set_include(enabled: bool) -> None:
    """Comment out the stock [global] workgroup/security lines and include cyn-global.conf at the
    end of [global] (or undo both)."""
    text = open(SMB_CONF).read()
    lines = [ln for ln in text.splitlines() if ln.strip() != f"include = {GLOBAL_INC}"]
    out, in_global = [], False
    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            if in_global and enabled:
                out.append(f"   include = {GLOBAL_INC}")
            in_global = s.lower() == "[global]"
        if enabled and in_global and re.match(r"^\s*(workgroup|security|realm)\s*=", ln):
            out.append(MARK + ln)
            continue
        if not enabled and ln.startswith(MARK):
            out.append(ln[len(MARK):])
            continue
        out.append(ln)
    with open(SMB_CONF + ".new", "w") as f:
        f.write("\n".join(out) + "\n")
    os.replace(SMB_CONF + ".new", SMB_CONF)


def _nsswitch(enabled: bool) -> None:
    out = []
    for ln in open(NSSWITCH).read().splitlines():
        m = re.match(r"^(passwd|group):(\s*)(.*)$", ln)
        if m:
            srcs = [x for x in m.group(3).split() if x != "winbind"]
            if enabled:
                srcs.append("winbind")
            ln = f"{m.group(1)}:{m.group(2) or ' '}{' '.join(srcs)}"
        out.append(ln)
    with open(NSSWITCH, "w") as f:
        f.write("\n".join(out) + "\n")


def _restart() -> None:
    for unit in ("smbd", "nmbd", "winbind"):
        run(["systemctl", "restart", unit], check=False)


def status() -> dict:
    c = load()
    if not c.get("joined"):
        return {"joined": False, "available": DEV or os.path.exists("/usr/bin/wbinfo")}
    out = {**c, "available": True}
    if not DEV:
        trust = subprocess.run(["wbinfo", "-t"], capture_output=True, text=True)
        out["healthy"] = trust.returncode == 0
        out["detail"] = (trust.stdout + trust.stderr).strip().splitlines()[-1] if (trust.stdout + trust.stderr).strip() else ""
        info = subprocess.run(["net", "ads", "info"], capture_output=True, text=True).stdout
        out["dc"] = next((ln.split(":", 1)[1].strip() for ln in info.splitlines() if ln.startswith("LDAP server name")), "")
    else:
        out["healthy"] = True
    return out


@router.get("")
def get_status(_=Depends(require_admin)):
    return status()


class Join(BaseModel):
    domain: str                        # corp.example.com
    workgroup: str = ""                # NetBIOS name (default: first part of the domain)
    username: str                      # a domain account allowed to join computers
    password: str
    admin_group: str = "Domain Admins"   # members may administer this NAS
    dns: str = ""                      # optional: the domain controller's IP, used as DNS server


@router.post("/join")
def join(body: Join, _=Depends(require_admin)):
    domain = body.domain.strip().lower()
    if not DOMAIN_RE.match(domain):
        raise HTTPException(400, "enter the domain's DNS name, e.g. corp.example.com")
    wg = (body.workgroup.strip() or domain.split(".")[0]).upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,14}", wg):
        raise HTTPException(400, "NetBIOS domain name: up to 15 letters, digits and -")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}", body.username.strip()):
        raise HTTPException(400, "enter the account name (e.g. Administrator)")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}", body.admin_group.strip()):
        raise HTTPException(400, "bad group name")
    if body.dns and not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", body.dns.strip()):
        raise HTTPException(400, "DNS server: an IPv4 address")
    if load().get("joined"):
        raise HTTPException(409, "already joined - leave the domain first")
    if DEV:
        save({"joined": True, "domain": domain, "workgroup": wg, "admin_group": body.admin_group.strip(), "since": int(time.time())})
        return status()
    if not os.path.exists("/usr/bin/wbinfo"):
        raise HTTPException(400, "the domain software isn't installed yet - update Cynthology (Control Panel > Update)")
    if body.dns.strip():
        _use_dns(body.dns.strip(), domain)
    try:
        socket.getaddrinfo(domain, 389)
    except socket.gaierror:
        raise HTTPException(400, f"{domain} can't be found in DNS. Use the domain controller as this NAS's DNS server "
                                 "(enter its address in the DNS field, or in Control Panel > Network)")
    from .shares import homes_share
    h = homes_share()
    with open(KRB5, "w") as f:
        f.write(f"[libdefaults]\n    default_realm = {domain.upper()}\n    dns_lookup_realm = false\n    dns_lookup_kdc = true\n"
                "    rdns = false\n")
    with open(GLOBAL_INC, "w") as f:
        f.write(render_global(domain, wg, h["path"] if h else None))
    _set_include(True)
    env = {**os.environ, "PASSWD": body.password}          # (not on the command line: ps would show it)
    r = subprocess.run(["net", "ads", "join", "-U", body.username.strip()], capture_output=True, text=True, env=env, timeout=120)
    if r.returncode != 0:
        _set_include(False)
        msg = (r.stderr + r.stdout).strip().splitlines()
        hint = ""
        text = " ".join(msg).lower()
        if "clock skew" in text or "time" in text and "skew" in text:
            hint = " - this NAS's clock differs from the domain controller's (more than 5 minutes)"
        elif "logon failure" in text or "invalid credentials" in text:
            hint = " - wrong user name or password"
        raise HTTPException(400, f"joining failed: {msg[-1] if msg else 'unknown error'}{hint}")
    _nsswitch(True)
    _restart()
    save({"joined": True, "domain": domain, "workgroup": wg, "admin_group": body.admin_group.strip(), "since": int(time.time()),
          "dns": body.dns.strip() or None})
    return status()


RESOLVED_DROPIN = "/etc/systemd/resolved.conf.d/cynthology-domain.conf"


def _use_dns(ip: str, domain: str) -> None:
    """Send the domain's DNS queries to the domain controller."""
    path = "/etc/resolv.conf"
    if os.path.islink(path) and "systemd/resolve" in os.path.realpath(path):
        # systemd-resolved: a routing domain - only *.domain goes to the DC, the rest is unchanged
        os.makedirs(os.path.dirname(RESOLVED_DROPIN), exist_ok=True)
        with open(RESOLVED_DROPIN, "w") as f:
            f.write(f"# Cynthology: Active Directory domain {domain}\n[Resolve]\nDNS={ip}\nDomains=~{domain}\n")
        run(["systemctl", "restart", "systemd-resolved"])
        time.sleep(2)
        return
    if os.path.islink(path):
        raise HTTPException(400, "DNS on this system is managed by another service - set the DNS server in Control Panel > Network")
    old = open(path).read() if os.path.exists(path) else ""
    rest = [ln for ln in old.splitlines() if not ln.startswith(("nameserver " + ip, "search ", "# Cynthology domain"))]
    with open(path + ".cyn-before-domain", "w") as f:
        f.write(old)
    with open(path, "w") as f:
        f.write(f"# Cynthology domain DNS\nsearch {domain}\nnameserver {ip}\n" + "\n".join(rest) + "\n")


def _undo_dns() -> None:
    if os.path.exists(RESOLVED_DROPIN):
        os.remove(RESOLVED_DROPIN)
        run(["systemctl", "restart", "systemd-resolved"], check=False)
    elif os.path.exists("/etc/resolv.conf.cyn-before-domain") and not os.path.islink("/etc/resolv.conf"):
        os.replace("/etc/resolv.conf.cyn-before-domain", "/etc/resolv.conf")


class Leave(BaseModel):
    username: str = ""
    password: str = ""


@router.post("/leave")
def leave(body: Leave, _=Depends(require_admin)):
    c = load()
    if not c.get("joined"):
        raise HTTPException(400, "not joined")
    if not DEV:
        if body.username:      # also remove the computer account from the domain
            env = {**os.environ, "PASSWD": body.password}
            subprocess.run(["net", "ads", "leave", "-U", body.username], capture_output=True, text=True, env=env, timeout=60)
        _nsswitch(False)
        _set_include(False)
        if os.path.exists(GLOBAL_INC):
            os.remove(GLOBAL_INC)
        if load().get("dns"):
            _undo_dns()
        _restart()
    save({"joined": False})
    return {"ok": True}


@router.get("/lookup")
def lookup(q: str, _=Depends(require_admin)):
    """Domain users and groups whose name contains q (for the permission editor)."""
    c = load()
    if not c.get("joined") or len(q) < 2:
        return {"users": [], "groups": []}
    if DEV:
        return {"users": [f"{c['workgroup']}\\alice"], "groups": [f"{c['workgroup']}\\Domain Users"]}
    ql = q.lower()
    users = [u for u in subprocess.run(["wbinfo", "-u"], capture_output=True, text=True, timeout=30).stdout.splitlines() if ql in u.lower()]
    groups = [g for g in subprocess.run(["wbinfo", "-g"], capture_output=True, text=True, timeout=30).stdout.splitlines() if ql in g.lower()]
    return {"users": users[:50], "groups": groups[:50]}


def is_domain_admin(user: str) -> bool:
    """Web sign-in: domain users in the configured admin group administer the NAS."""
    c = load()
    if not c.get("joined") or "\\" not in user:
        return False
    group = f"{c['workgroup']}\\{c.get('admin_group', 'Domain Admins')}".lower()
    r = subprocess.run(["id", "-Gn", user], capture_output=True, text=True)
    # id prints groups separated by spaces, but AD group names contain spaces: compare via getent
    try:
        import grp
        import pwd
        gid = grp.getgrnam(f"{c['workgroup']}\\{c.get('admin_group', 'Domain Admins')}").gr_gid
        return gid in os.getgrouplist(user, pwd.getpwnam(user).pw_gid)
    except (KeyError, OSError):
        return group in r.stdout.lower()
