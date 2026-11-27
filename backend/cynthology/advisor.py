"""Security Advisor (DSM: Security Advisor): check the NAS's settings and say what to fix.

Each check returns {id, category, title, status, detail, fix}. status: ok | info | warning |
critical. `fix` names where it is fixed: {"cp": "<Control Panel section>"} or {"app": "<app>"}.
Checks only read the system; nothing is changed here.
"""
import json
import os
import re
import subprocess
import time

from fastapi import APIRouter, Depends

from .auth import ADMIN_GROUPS, require_admin
from .util import DEV, STATE_DIR

router = APIRouter(prefix="/api/security/advisor", tags=["advisor"])
CACHE = os.path.join(STATE_DIR, "advisor.json")


def _c(cid, category, title, status, detail, fix=None):
    return {"id": cid, "category": category, "title": title, "status": status, "detail": detail, "fix": fix}


def _out(cmd: list[str], timeout: int = 20) -> str:
    if DEV:
        return ""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _admins() -> list[str]:
    import grp
    out = set()
    for g in ADMIN_GROUPS:
        try:
            out |= set(grp.getgrnam(g).gr_mem)
        except KeyError:
            pass
    return sorted(out) if not DEV else ["admin"]


# ---------- checks ----------

def accounts() -> list[dict]:
    from . import settings, twofa
    out = []
    admins = _admins()
    no2fa = [a for a in admins if not twofa.enabled(a)]
    out.append(_c("admin-2fa", "Accounts", "Administrators use 2-step sign-in",
                  "ok" if not no2fa else "warning",
                  "All administrators sign in with a code from their phone." if not no2fa else
                  f"Without it, a guessed or leaked password is enough to take over the NAS: {', '.join(no2fa)}. "
                  "Personal settings → 2-step sign-in.", {"cp": "users"}))
    enforce = bool(settings.get_all().get("enforce_2fa_admins"))
    out.append(_c("enforce-2fa", "Accounts", "2-step sign-in is required for administrators",
                  "ok" if enforce else "info",
                  "New administrators must set it up at their first sign-in." if enforce else
                  "Turn on “require 2-step sign-in for administrators” so nobody can skip it.", {"cp": "security"}))
    empty = []
    if not DEV:
        try:
            for ln in open("/etc/shadow"):
                name, pw = ln.split(":")[:2]
                if pw == "":
                    empty.append(name)
        except OSError:
            pass
    out.append(_c("empty-passwords", "Accounts", "No account without a password",
                  "critical" if empty else "ok",
                  f"These accounts can sign in without a password: {', '.join(empty)}. Set a password or lock them "
                  "(sudo passwd -l NAME)." if empty else "Every account has a password.", {"cp": "users"}))
    out.append(_c("admin-count", "Accounts", "Few administrators", "ok" if len(admins) <= 3 else "info",
                  f"{len(admins)} administrator(s): {', '.join(admins)}." + ("" if len(admins) <= 3 else
                  " Everyday accounts don't need administrator rights - fewer administrators, fewer ways in."),
                  {"cp": "users"}))
    return out


def sshd() -> list[dict]:
    t = _out(["sshd", "-T", "-C", "user=root,host=x,addr=203.0.113.1"])
    root = re.search(r"^permitrootlogin (\S+)", t, re.M)
    root = root.group(1) if root else "prohibit-password"
    out = [_c("ssh-root", "Network", "No direct root sign-in over SSH",
              "warning" if root == "yes" else "ok",
              "root can sign in over SSH with a password - attackers try that first. Set PermitRootLogin no "
              "(or prohibit-password) in /etc/ssh/sshd_config." if root == "yes" else
              "root can't sign in over SSH with a password.", {"cp": "services"})]
    return out


def network() -> list[dict]:
    from . import firewall, settings, tls
    out = []
    fw = firewall.load()
    out.append(_c("firewall", "Network", "Firewall is on", "ok" if fw.get("enabled") else "warning",
                  "Only the services you allow can be reached." if fw.get("enabled") else
                  "Every service on the NAS can be reached by anything that can reach the NAS. Allow what you use "
                  "(and your home network) and block the rest.", {"cp": "firewall"}))
    ab = fw.get("autoblock", {}).get("enabled")
    out.append(_c("autoblock", "Network", "Auto-block stops password guessing", "ok" if ab else "warning",
                  "Addresses that keep failing to sign in are blocked." if ab else
                  "Turn on auto-block: an address with repeated failed sign-ins is shut out for a while.", {"cp": "firewall"}))
    s = settings.get_all()
    out.append(_c("https-redirect", "Network", "The web interface always uses HTTPS",
                  "ok" if s.get("https_redirect") else "warning",
                  "Plain HTTP is redirected to HTTPS." if s.get("https_redirect") else
                  f"Passwords typed at http://…:{s['http_port']} cross the network unencrypted. Turn on “redirect HTTP "
                  "to HTTPS”.", {"cp": "security"}))
    ci = tls.cert_info() if not DEV else {"present": True, "kind": "self-signed", "days_left": 3000}
    try:
        from . import ddns
        public = [e["hostname"] for e in ddns.load() if e.get("enabled", True)]
    except Exception:
        public = []
    if ci.get("present"):
        days = ci.get("days_left", 999)
        if days < 0:
            out.append(_c("cert-expiry", "Network", "The certificate is valid", "critical",
                          "The HTTPS certificate has expired - browsers and apps will refuse to connect.", {"cp": "security"}))
        elif days < 14:
            out.append(_c("cert-expiry", "Network", "The certificate is valid", "warning",
                          f"The HTTPS certificate expires in {days} day(s).", {"cp": "security"}))
        if public and ci.get("kind") == "self-signed":
            out.append(_c("cert-public", "Network", "A trusted certificate for your public name", "warning",
                          f"You reach the NAS as {public[0]} but use a self-signed certificate: visitors can't tell "
                          "your NAS from an impostor. Get a free Let's Encrypt certificate.", {"cp": "security"}))
        else:
            out.append(_c("cert-public", "Network", "A trusted certificate for your public name", "ok",
                          "Let's Encrypt or your own certificate is in use." if ci.get("kind") != "self-signed" else
                          "No public name configured - the self-signed certificate is fine for your home network.", {"cp": "security"}))
    smb = _out(["testparm", "-s", "--parameter-name=server min protocol"]).strip().upper()
    out.append(_c("smb1", "Network", "Old SMB1 is off", "critical" if smb in ("NT1", "CORE", "COREPLUS", "LANMAN1", "LANMAN2") else "ok",
                  "SMB1 is switched on - it is insecure (WannaCry). Remove “server min protocol” from smb.conf." if smb in ("NT1", "LANMAN1", "LANMAN2", "CORE", "COREPLUS")
                  else "Windows file sharing uses SMB2/SMB3 only.", {"cp": "services"}))
    from .fileservices import load as fs_load
    fs = fs_load()
    if fs["webdav"]["enabled"] and fs["webdav"]["http"]:
        out.append(_c("webdav-http", "Network", "WebDAV is encrypted", "warning",
                      "WebDAV over plain HTTP sends passwords and files unencrypted. Use HTTPS only.", {"cp": "services"}))
    if fs["rsync"]["enabled"]:
        out.append(_c("rsync", "Network", "rsync server", "info",
                      "The rsync protocol is not encrypted - use it only inside your home network (or over the VPN).", {"cp": "services"}))
    from . import shares as shares_mod
    wide = [s["name"] for s in shares_mod.load() if s.get("nfs") and (not s.get("nfs_clients") or "/8" in s.get("nfs_clients", "") or "/16" in s.get("nfs_clients", ""))]
    if wide:
        out.append(_c("nfs-wide", "Network", "NFS is limited to the computers that need it", "info",
                      f"NFS trusts the user IDs a computer claims. {', '.join(wide)} can be reached from a whole network range - "
                      "list only the computers that use it.", {"cp": "shares"}))
    return out


def system() -> list[dict]:
    from . import health, update
    out = []
    try:
        st = update.status(refresh=False, _=None)
        out.append(_c("update", "System", "Cynthology is up to date", "warning" if st.get("update_available") else "ok",
                      f"Version {st['latest']['version']} is available - updates contain security fixes." if st.get("update_available")
                      else f"Running {st.get('current')}.", {"cp": "update"}))
    except Exception:
        pass
    sec = _out(["apt", "list", "--upgradable"], timeout=60)
    sec_pk = [ln.split("/")[0] for ln in sec.splitlines() if "-security" in ln]
    out.append(_c("debian-security", "System", "Debian security updates are installed",
                  "warning" if sec_pk else "ok",
                  f"{len(sec_pk)} security update(s) waiting: {', '.join(sec_pk[:8])}{' …' if len(sec_pk) > 8 else ''}." if sec_pk
                  else "No security updates waiting (as of the last package list refresh).", {"cp": "update"}))
    ntp = _out(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).strip()
    if ntp:
        out.append(_c("time", "System", "The clock is synchronised", "ok" if ntp == "yes" else "warning",
                      "Time comes from the internet." if ntp == "yes" else
                      "The clock isn't synchronised - 2-step codes, certificates and schedules depend on the right time.", {"cp": "general"}))
    smtp = health.load_smtp()
    try:
        from .notify import load as notify_load
        push = [c for c in notify_load() if c.get("enabled", True)]
    except Exception:
        push = []
    told = (smtp.get("enabled") and smtp.get("host")) or push
    out.append(_c("notifications", "System", "You will hear about problems", "ok" if told else "warning",
                  "Alerts go to " + ", ".join((["email"] if smtp.get("enabled") else []) + [c["type"] for c in push]) + "." if told else
                  "No email or phone notifications are set up - a failing drive would go unnoticed.", {"cp": "notify"}))
    issues = health.collect() if not DEV else {}
    crit = [v["title"] for v in issues.values() if v["level"] == "critical"]
    out.append(_c("health", "System", "Drives and pools are healthy", "critical" if crit else "ok",
                  "; ".join(crit) if crit else "No failed drives or degraded pools.", {"app": "storage"}))
    return out


def data() -> list[dict]:
    from . import backup, sharelinks, shares as shares_mod
    out = []
    tasks = backup.load_tasks()
    if not tasks:
        out.append(_c("backup", "Data protection", "Your data is backed up", "warning",
                      "There is no backup task. RAID protects against a failed drive - not against deleting, "
                      "ransomware, theft or fire. Back up to a USB drive, another NAS or the cloud.", {"app": "backup"}))
    else:
        bad = []
        for t in tasks:
            st = backup.read_status(t["id"])
            age = time.time() - (st.get("finished") or 0)
            if st.get("state") == "failed" or (t.get("schedule", {}).get("enabled") and age > 8 * 86400):
                bad.append(t["name"])
        out.append(_c("backup", "Data protection", "Your data is backed up", "warning" if bad else "ok",
                      f"These backups failed or haven't run for over a week: {', '.join(bad)}." if bad else
                      f"{len(tasks)} backup task(s), all recent.", {"app": "backup"}))
    snapshotable = [s for s in shares_mod.load() if not s.get("usb") and not s.get("encrypted")]
    scheduled = [s for s in snapshotable if (s.get("snap_schedule") or "off") != "off"]
    out.append(_c("snapshots", "Data protection", "Snapshots on a schedule",
                  "ok" if scheduled or not snapshotable else "info",
                  f"{len(scheduled)} of {len(snapshotable)} shared folder(s) take snapshots - a ransomware attack or a "
                  "mistake can be undone in seconds." if snapshotable else "No shared folders yet.", {"app": "snapshots"}))
    if scheduled:
        locked = [s for s in scheduled if int(s.get("snap_lock_days") or 0) > 0]
        out.append(_c("immutable", "Data protection", "Snapshots are locked against deletion", "ok" if locked else "info",
                      "Locked snapshots can't be deleted even with an administrator's password." if locked else
                      "Lock new snapshots for a few days: then even someone with an administrator's password can't "
                      "delete them (Snapshots → Lock).", {"app": "snapshots"}))
    open_links = [x["name"] for x in sharelinks.load() if sharelinks.state(x) == "active" and not x.get("password") and not x.get("expires")]
    if open_links:
        out.append(_c("links", "Data protection", "Sharing links expire or have a password", "info",
                      f"{len(open_links)} link(s) work forever for anyone who has them: {', '.join(open_links[:5])}. "
                      "Add an expiry date or delete the ones you don't need (File Station → Shared links).", {"app": "files"}))
    return out


def run_all() -> dict:
    checks = []
    for part in (accounts, network, sshd, system, data):
        try:
            checks += part()
        except Exception as e:
            checks.append(_c(f"{part.__name__}-error", "System", f"Couldn't check {part.__name__}", "info", str(e)))
    order = {"critical": 0, "warning": 1, "info": 2, "ok": 3}
    checks.sort(key=lambda c: order[c["status"]])
    counts = {k: sum(c["status"] == k for c in checks) for k in order}
    result = {"time": int(time.time()), "checks": checks, "counts": counts}
    try:
        with open(CACHE, "w") as f:
            json.dump(result, f)
    except OSError:
        pass
    return result


def daily() -> None:
    """From the health timer: re-check once a day and tell the admin about problems that are new."""
    try:
        prev = json.load(open(CACHE))
    except (OSError, ValueError):
        prev = None
    if prev and time.time() - prev.get("time", 0) < 23 * 3600:
        return
    before = {c["id"] for c in (prev or {}).get("checks", []) if c["status"] in ("critical", "warning")}
    now = run_all()
    new = [c for c in now["checks"] if c["status"] in ("critical", "warning") and c["id"] not in before]
    if new and prev is not None:            # (not on the very first scan: that's what the page is for)
        from .notify import notify
        notify("critical" if any(c["status"] == "critical" for c in new) else "warning",
               f"Security Advisor: {len(new)} new finding(s)",
               "\n\n".join(f"{c['title']}\n{c['detail']}" for c in new) + "\n\nControl Panel → Security Advisor", event="advisor")


@router.get("")
def last(_=Depends(require_admin)):
    try:
        return json.load(open(CACHE))
    except (OSError, ValueError):
        return run_all()


@router.post("")
def scan(_=Depends(require_admin)):
    return run_all()
