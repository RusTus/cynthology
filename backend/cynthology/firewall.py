"""Firewall (nftables) with safe apply, plus automatic blocking of addresses that keep failing to sign in.

Everything lives in one table, `inet cynthology`, replaced atomically on every change and loaded at
boot by cynthology-firewall.service. Like the network settings, a change is applied with a rollback
armed first: if the browser can't confirm within 90 s (you locked yourself out), the previous rules
come back by themselves.

Docker publishes container ports through its own forwarding rules, so the firewall's input rules
don't cover containers (DSM's firewall has the same limitation).
"""
import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .auth import require_admin
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/firewall", tags=["firewall"])
CFG = os.path.join(STATE_DIR, "firewall.json")
NFT = os.path.join(STATE_DIR, "firewall.nft") if DEV else "/etc/cynthology/firewall.nft"
PREV = NFT + ".previous"
PENDING = os.path.join(STATE_DIR, "firewall-pending.json")
ROLLBACK_SECONDS = 90

# service -> [(protocol, port or "a-b")]
SERVICES = {
    "web": ("Cynthology web interface", [("tcp", "5000"), ("tcp", "5001")]),
    "smb": ("Windows file sharing (SMB)", [("tcp", "139"), ("tcp", "445"), ("udp", "137"), ("udp", "138")]),
    "discovery": ("Network discovery (Windows WS-Discovery, Bonjour)", [("udp", "3702"), ("tcp", "5357"), ("udp", "5353")]),
    "nfs": ("NFS", [("tcp", "111"), ("udp", "111"), ("tcp", "2049"), ("udp", "2049"), ("tcp", "20048"), ("udp", "20048")]),
    "ssh": ("SSH (also snapshot replication)", [("tcp", "22")]),
    "iscsi": ("iSCSI (SAN Manager)", [("tcp", "3260")]),
    "webstation": ("Web Station websites (HTTP/HTTPS)", [("tcp", "80"), ("tcp", "443")]),
    "ups": ("UPS server (other NAS devices)", [("tcp", "3493")]),
    "vpn": ("WireGuard VPN", [("udp", "VPN_PORT")]),
    "webdav": ("WebDAV (network drive over HTTP/HTTPS)", [("tcp", "WEBDAV_HTTP"), ("tcp", "WEBDAV_HTTPS")]),
    "rsync": ("rsync server", [("tcp", "873")]),
    "vault": ("Backup Vault (backups from other NAS boxes)", [("tcp", "VAULT_PORT")]),
}


def _service_ports(key: str) -> list[tuple[str, str]]:
    ports = SERVICES[key][1]
    if key == "vpn":            # whatever port the VPN server is set to
        try:
            from .vpn import load as vpn_load
            port = str(vpn_load().get("port", 51820))
        except Exception:
            port = "51820"
        return [(proto, port) for proto, _ in ports]
    if key == "vault":
        try:
            from .vault import load as vault_load
            return [("tcp", str(vault_load()["port"]))]
        except Exception:
            return [("tcp", "6281")]
    if key == "webdav":         # the ports WebDAV is set to
        try:
            from .fileservices import load as fs_load
            w = fs_load()["webdav"]
        except Exception:
            w = {"http_port": 5005, "https_port": 5006}
        return [("tcp", str(w["http_port"])), ("tcp", str(w["https_port"]))]
    return ports


class Rule(BaseModel):
    id: str = ""
    enabled: bool = True
    action: str = "allow"          # allow | deny
    services: list[str] = []       # keys of SERVICES; empty + no ports = all traffic
    ports: str = ""                # custom: "8080, 9000-9100"
    protocol: str = "tcp"          # for custom ports: tcp | udp | both
    source: str = ""               # empty = anyone; IP or subnet (IPv4/IPv6)
    note: str = ""


class Autoblock(BaseModel):
    enabled: bool = True
    attempts: int = 10             # failed sign-ins ...
    within_minutes: int = 5        # ... within this time ...
    block_minutes: int = 1440      # ... block the address for this long (0 = forever, until removed)
    trusted: list[str] = []        # never block these addresses / subnets


class Config(BaseModel):
    enabled: bool = False
    default: str = "allow"         # what happens to traffic no rule matches: allow | deny
    rules: list[Rule] = []
    autoblock: Autoblock = Autoblock()


def load() -> dict:
    try:
        return Config(**json.load(open(CFG))).model_dump()
    except (OSError, ValueError):
        return Config().model_dump()


def save(cfg: dict) -> None:
    with open(CFG + ".new", "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(CFG + ".new", CFG)


def _net(src: str):
    try:
        return ipaddress.ip_network(src.strip(), strict=False)
    except ValueError:
        raise HTTPException(400, f"{src} is not an IP address or subnet (e.g. 192.168.1.0/24)")


def _ports(spec: str) -> list[str]:
    out = []
    for part in [p.strip() for p in spec.replace(" ", ",").split(",") if p.strip()]:
        a, _, b = part.partition("-")
        if not a.isdigit() or (b and not b.isdigit()) or not 1 <= int(a) <= 65535 or (b and not int(a) < int(b) <= 65535):
            raise HTTPException(400, f"bad port {part!r} (use e.g. 8080 or 9000-9100)")
        out.append(f"{int(a)}-{int(b)}" if b else str(int(a)))
    return out


def validate(cfg: Config) -> None:
    if cfg.default not in ("allow", "deny"):
        raise HTTPException(400, "default must be allow or deny")
    for r in cfg.rules:
        if r.action not in ("allow", "deny") or r.protocol not in ("tcp", "udp", "both"):
            raise HTTPException(400, "bad rule")
        for s in r.services:
            if s not in SERVICES:
                raise HTTPException(400, f"unknown service {s}")
        _ports(r.ports)
        if r.source:
            _net(r.source)
    for t in cfg.autoblock.trusted:
        _net(t)
    a = cfg.autoblock
    if not (2 <= a.attempts <= 100 and 1 <= a.within_minutes <= 1440 and 0 <= a.block_minutes <= 525600):
        raise HTTPException(400, "auto-block: attempts 2-100, within 1-1440 minutes, block 0-525600 minutes")


def render(cfg: dict) -> str:
    """The nftables ruleset for a configuration (pure; unit tested)."""
    lines = ["#!/usr/sbin/nft -f", "# Generated by Cynthology (Control Panel > Firewall). Changes here are overwritten.",
             "table inet cynthology", "delete table inet cynthology", "table inet cynthology {",
             "  set autoblock4 { type ipv4_addr; flags timeout; }",
             "  set autoblock6 { type ipv6_addr; flags timeout; }",
             "  chain input {", "    type filter hook input priority filter; policy accept;",
             '    iif "lo" accept',
             "    ip saddr @autoblock4 drop", "    ip6 saddr @autoblock6 drop",     # before "established": cuts off open connections too
             "    ct state established,related accept"]
    if cfg.get("enabled"):
        lines += ["    ct state invalid drop",
                  "    meta l4proto { icmp, ipv6-icmp } accept",          # ping, IPv6 neighbour discovery
                  "    udp dport { 68, 546 } accept"]                    # DHCP / DHCPv6 replies
        for r in cfg["rules"]:
            if not r.get("enabled", True):
                continue
            verdict = "accept" if r["action"] == "allow" else "drop"
            src = ""
            if r.get("source"):
                net = ipaddress.ip_network(r["source"], strict=False)
                src = f"{'ip' if net.version == 4 else 'ip6'} saddr {net} "
            by_proto: dict[str, list[str]] = {}
            for s in r.get("services", []):
                for proto, port in _service_ports(s):
                    by_proto.setdefault(proto, []).append(port)
            protos = ["tcp", "udp"] if r.get("protocol") == "both" else [r.get("protocol", "tcp")]
            for port in _ports(r.get("ports", "")):
                for proto in protos:
                    by_proto.setdefault(proto, []).append(port)
            comment = f' comment "{r.get("note", "")[:60].replace(chr(34), "")}"' if r.get("note") else ""
            if not by_proto:
                lines.append(f"    {src}{verdict}{comment}")
            for proto, ports in by_proto.items():
                uniq = list(dict.fromkeys(ports))
                lines.append(f"    {src}{proto} dport {{ {', '.join(uniq)} }} {verdict}{comment}")
        if cfg.get("default") == "deny":
            lines.append("    counter drop")
    lines += ["  }", "}"]
    return "\n".join(lines) + "\n"


def needed(cfg: dict) -> bool:
    return bool(cfg.get("enabled") or cfg.get("autoblock", {}).get("enabled"))


def _load_rules(text: str | None) -> None:
    if DEV:
        return
    if text is None:
        subprocess.run(["nft", "delete", "table", "inet", "cynthology"], capture_output=True)
        return
    r = subprocess.run(["nft", "-f", "-"], input=text, capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(400, "the firewall rejected the rules: " + (r.stderr.strip().splitlines() or ["?"])[-1])


def _allowed(cfg: dict, ip: str, port: int) -> bool:
    """Would a TCP connection from ip to port be accepted? (for the don't-lock-yourself-out check)"""
    if not cfg.get("enabled"):
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if addr.is_loopback:
        return True
    for r in cfg["rules"]:
        if not r.get("enabled", True):
            continue
        if r.get("source") and addr not in ipaddress.ip_network(r["source"], strict=False):
            continue
        ports = [p for s in r.get("services", []) for proto, p in _service_ports(s) if proto == "tcp"]
        if r.get("protocol") in ("tcp", "both"):
            ports += _ports(r.get("ports", ""))
        match = not r.get("services") and not r.get("ports")
        for p in ports:
            a, _, b = p.partition("-")
            if int(a) <= port <= int(b or a):
                match = True
        if match:
            return r["action"] == "allow"
    return cfg.get("default") != "deny"


# ---------- API ----------

def pending() -> dict | None:
    try:
        p = json.load(open(PENDING))
        return p if p["deadline"] > time.time() else None
    except (OSError, ValueError, KeyError):
        return None


@router.get("")
def get_firewall(request: Request, _=Depends(require_admin)):
    return {**load(), "services": {k: v[0] for k, v in SERVICES.items()}, "pending": pending(),
            "your_ip": request.client.host if request.client else "", "blocked": blocked(),
            "available": DEV or shutil.which("nft") is not None}


@router.put("")
def apply(body: Config, request: Request, _=Depends(require_admin)):
    if not DEV and not shutil.which("nft"):
        raise HTTPException(400, "nftables isn't installed - update Cynthology (Control Panel > Update)")
    validate(body)
    cfg = body.model_dump()
    for r in cfg["rules"]:
        r["id"] = r["id"] or secrets.token_hex(3)
    me = request.client.host if request.client else ""
    # the port this NAS really received the request on (the browser's may differ behind a port forward)
    port = (request.scope.get("server") or (None, None))[1] or request.url.port or 5001
    if me and not _allowed(cfg, me, port):
        raise HTTPException(400, f"these rules would block you ({me}) from this page - add a rule that allows "
                                 f"the web interface from your address or network first")
    if pending():
        raise HTTPException(409, "a previous change is still waiting for confirmation")
    old = open(NFT).read() if os.path.exists(NFT) else None
    if old is not None:
        with open(PREV, "w") as f:
            f.write(old)
    elif os.path.exists(PREV):
        os.remove(PREV)
    text = render(cfg) if needed(cfg) else None
    if not DEV:   # arm the rollback BEFORE the new rules take effect
        run(["systemd-run", f"--on-active={ROLLBACK_SECONDS}", "--timer-property=AccuracySec=1s", "--unit=cyn-fw-rollback", "--collect",
             "/usr/lib/cynthology/fw-rollback"])
    with open(PENDING, "w") as f:
        json.dump({"deadline": time.time() + ROLLBACK_SECONDS, "config": cfg}, f)
    try:
        _load_rules(text)
    except HTTPException:
        _cancel_rollback()
        raise
    os.makedirs(os.path.dirname(NFT), exist_ok=True)
    if text is None:
        if os.path.exists(NFT):
            os.remove(NFT)
    else:
        with open(NFT, "w") as f:
            f.write(text)
    if os.path.exists(CFG):
        shutil.copy(CFG, CFG + ".previous")
    elif os.path.exists(CFG + ".previous"):
        os.remove(CFG + ".previous")
    save(cfg)
    return {"ok": True, "confirm_within": ROLLBACK_SECONDS}


def _cancel_rollback() -> None:
    if not DEV:
        subprocess.run(["systemctl", "stop", "cyn-fw-rollback.timer"], capture_output=True)
    if os.path.exists(PENDING):
        os.remove(PENDING)


@router.post("/confirm")
def confirm(_=Depends(require_admin)):
    if not pending():
        raise HTTPException(409, "nothing to confirm (the change may already have been rolled back)")
    _cancel_rollback()
    return {"ok": True}


def rollback() -> None:
    """Run by the cyn-fw-rollback timer: restore the previous rules and settings."""
    p = None
    try:
        p = json.load(open(PENDING))
    except (OSError, ValueError):
        pass
    prev = open(PREV).read() if os.path.exists(PREV) else None
    _load_rules(prev)
    if prev is None:
        if os.path.exists(NFT):
            os.remove(NFT)
    else:
        shutil.copy(PREV, NFT)
    if p:
        if os.path.exists(CFG + ".previous"):
            shutil.copy(CFG + ".previous", CFG)
        elif os.path.exists(CFG):
            os.remove(CFG)
    if os.path.exists(PENDING):
        os.remove(PENDING)
    try:
        from .backup import _alert
        _alert({"name": "firewall"}, "warning", "Firewall change rolled back",
               "The new firewall rules were not confirmed within 90 seconds, so the previous rules were restored.")
    except Exception:
        pass


# ---------- auto-block ----------

def blocked() -> list[dict]:
    if DEV:
        return []
    out = []
    for fam in ("autoblock4", "autoblock6"):
        r = subprocess.run(["nft", "-j", "list", "set", "inet", "cynthology", fam], capture_output=True, text=True)
        try:
            for item in json.loads(r.stdout)["nftables"]:
                for e in (item.get("set") or {}).get("elem", []):
                    el = e.get("elem", e) if isinstance(e, dict) else {"val": e}
                    out.append({"ip": el.get("val"), "expires_in": el.get("expires")})
        except (ValueError, KeyError, TypeError):
            pass
    return out


def block_ip(ip: str, reason: str = "") -> bool:
    """Called by sign-in when an address keeps failing. Returns True when it was blocked."""
    cfg = load()
    a = cfg["autoblock"]
    if not a["enabled"] or DEV:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback or any(addr in ipaddress.ip_network(t, strict=False) for t in a["trusted"]):
        return False
    timeout = f" timeout {a['block_minutes']}m" if a["block_minutes"] else ""
    s = "autoblock4" if addr.version == 4 else "autoblock6"
    r = subprocess.run(["nft", "add", "element", "inet", "cynthology", s, f"{{ {addr}{timeout} }}"], capture_output=True)
    if r.returncode == 0:
        try:
            from .logcenter import auth_event
            auth_event("blocked", "", str(addr), f"auto-blocked after {a['attempts']} failed sign-ins ({reason})")
        except Exception:
            pass
        try:
            from .backup import _alert
            _alert({"name": "auto-block"}, "warning", f"Blocked {addr}",
                   f"{addr} was blocked after {a['attempts']} failed sign-ins{(': ' + reason) if reason else ''}.")
        except Exception:
            pass
    return r.returncode == 0


class Unblock(BaseModel):
    ip: str


@router.post("/unblock")
def unblock(body: Unblock, _=Depends(require_admin)):
    addr = ipaddress.ip_address(body.ip)
    if not DEV:
        s = "autoblock4" if addr.version == 4 else "autoblock6"
        subprocess.run(["nft", "delete", "element", "inet", "cynthology", s, f"{{ {addr} }}"], capture_output=True)
    return {"ok": True}


def boot() -> None:
    """At boot (cynthology-firewall.service): load the saved rules."""
    if os.path.exists(NFT):
        _load_rules(open(NFT).read())


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["rollback"]:
        rollback()
    else:
        boot()
