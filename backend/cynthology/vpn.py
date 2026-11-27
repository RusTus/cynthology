"""VPN Server (WireGuard): reach the NAS and your home network from anywhere without opening
any other port.

wg0 is managed by wg-quick (/etc/wireguard/wg0.conf, wg-quick@wg0.service). Each device ("peer")
gets a key pair + pre-shared key generated here; its config is shown as a QR code / .conf file.
Devices can reach the NAS and the home network (NAT), or send all their traffic through home.
PostUp/PostDown (/usr/lib/cynthology/vpn-route) enable forwarding + NAT and open Docker's
DOCKER-USER chain for wg0 (Docker sets the forward policy to drop).
"""
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_admin
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/vpn", tags=["vpn"])
CFG = os.path.join(STATE_DIR, "vpn.json")
WG_CONF = os.path.join(STATE_DIR, "wg0.conf") if DEV else "/etc/wireguard/wg0.conf"
UNIT = "wg-quick@wg0"


def load() -> dict:
    try:
        return json.load(open(CFG))
    except (OSError, ValueError):
        return {"enabled": False, "subnet": "10.8.0.0/24", "port": 51820, "endpoint": "", "dns": "", "peers": []}


def save(c: dict) -> None:
    fd = os.open(CFG + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # secrets: never readable by others
    with os.fdopen(fd, "w") as f:
        json.dump(c, f, indent=1)
    os.replace(CFG + ".new", CFG)


def _wg(*args: str, input: str | None = None) -> str:
    if DEV:
        return secrets.token_urlsafe(32)[:43] + "="
    return subprocess.run(["wg", *args], input=input, capture_output=True, text=True, check=True).stdout.strip()


def keypair() -> tuple[str, str]:
    priv = _wg("genkey")
    return priv, _wg("pubkey", input=priv + "\n")


def lan_networks() -> list[str]:
    """The NAS's own LAN subnets (what VPN devices should be able to reach)."""
    import psutil
    import socket
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        if name.startswith(("lo", "docker", "br-", "veth", "wg", "virbr")):
            continue
        for a in addrs:
            if a.family == socket.AF_INET and a.netmask:
                net = ipaddress.IPv4Interface(f"{a.address}/{a.netmask}").network
                if net.is_private and str(net) not in out:
                    out.append(str(net))
    return out


def render_server(c: dict) -> str:
    """wg0.conf (pure; unit tested)."""
    net = ipaddress.ip_network(c["subnet"])
    lines = ["# Managed by Cynthology (Control Panel > VPN Server)", "[Interface]",
             f"Address = {next(net.hosts())}/{net.prefixlen}", f"ListenPort = {c['port']}",
             f"PrivateKey = {c['server_private']}",
             f"PostUp = /usr/lib/cynthology/vpn-route up %i {net}",
             f"PostDown = /usr/lib/cynthology/vpn-route down %i {net}", ""]
    for p in c["peers"]:
        if not p.get("enabled", True):
            continue
        lines += [f"# {p['name']}", "[Peer]", f"PublicKey = {p['public']}", f"PresharedKey = {p['psk']}",
                  f"AllowedIPs = {p['address']}/32", ""]
    return "\n".join(lines)


def render_client(c: dict, p: dict) -> str:
    net = ipaddress.ip_network(c["subnet"])
    allowed = "0.0.0.0/0, ::/0" if p.get("full_tunnel") else ", ".join([str(net)] + lan_networks())
    lines = ["[Interface]", f"PrivateKey = {p['private']}", f"Address = {p['address']}/32"]
    if c.get("dns") or p.get("full_tunnel"):
        lines.append(f"DNS = {c.get('dns') or upstream_dns()}")
    lines += ["", "[Peer]", f"PublicKey = {c['server_public']}", f"PresharedKey = {p['psk']}",
              f"Endpoint = {c['endpoint']}:{c['port']}", f"AllowedIPs = {allowed}", "PersistentKeepalive = 25"]
    return "\n".join(lines) + "\n"


def upstream_dns() -> str:
    """The DNS server this NAS uses (usually the router) - reachable for VPN devices through NAT.
    (The NAS itself runs no DNS server, so its VPN address can't be used.)"""
    servers = []
    try:
        servers = re.findall(r"^nameserver\s+(\S+)", open("/etc/resolv.conf").read(), re.M)
        if all(s.startswith("127.") for s in servers):      # systemd-resolved stub: ask it for the real ones
            out = subprocess.run(["resolvectl", "dns"], capture_output=True, text=True).stdout
            servers = re.findall(r"(\d{1,3}(?:\.\d{1,3}){3})", out)
    except OSError:
        pass
    return next((s for s in servers if not s.startswith("127.") and ":" not in s), "1.1.1.1")


def default_endpoint() -> str:
    try:
        from .ddns import load as ddns_load
        e = next((x for x in ddns_load() if x.get("enabled", True)), None)
        if e:
            return e["hostname"]
    except Exception:
        pass
    return ""


def apply(c: dict) -> None:
    if DEV:
        open(WG_CONF, "w").write(render_server(c))
        return
    os.makedirs(os.path.dirname(WG_CONF), mode=0o700, exist_ok=True)
    fd = os.open(WG_CONF + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(render_server(c))
    os.replace(WG_CONF + ".new", WG_CONF)
    if c["enabled"]:
        active = subprocess.run(["systemctl", "is-active", "--quiet", UNIT]).returncode == 0
        if active:   # add/remove peers without dropping connected devices
            r = subprocess.run(["bash", "-c", f"wg syncconf wg0 <(wg-quick strip {WG_CONF})"], capture_output=True, text=True)
            if r.returncode != 0:
                run(["systemctl", "restart", UNIT])
        else:
            r = subprocess.run(["systemctl", "enable", "--now", UNIT], capture_output=True, text=True)
            if r.returncode != 0:
                log = subprocess.run(["journalctl", "-u", UNIT, "-n", "5", "-o", "cat"], capture_output=True, text=True).stdout
                raise HTTPException(500, "the VPN could not start: " + (log.strip().splitlines() or ["?"])[-1])
    else:
        run(["systemctl", "disable", "--now", UNIT], check=False)


def status() -> dict:
    """Per peer: last handshake and traffic (wg show dump)."""
    if DEV:
        return {}
    r = subprocess.run(["wg", "show", "wg0", "dump"], capture_output=True, text=True)
    out = {}
    for line in r.stdout.splitlines()[1:]:
        f = line.split("\t")
        if len(f) >= 7:
            out[f[0]] = {"endpoint": None if f[2] == "(none)" else f[2], "handshake": int(f[4] or 0),
                         "rx": int(f[5] or 0), "tx": int(f[6] or 0)}
    return out


def _view(c: dict) -> dict:
    st = status()
    peers = []
    for p in c["peers"]:
        s = st.get(p["public"], {})
        peers.append({k: v for k, v in p.items() if k not in ("private", "psk")} |
                     {"handshake": s.get("handshake", 0), "rx": s.get("rx", 0), "tx": s.get("tx", 0), "endpoint": s.get("endpoint"),
                      "online": bool(s.get("handshake")) and time.time() - s["handshake"] < 180})
    running = DEV and c["enabled"] or (not DEV and subprocess.run(["systemctl", "is-active", "--quiet", UNIT]).returncode == 0)
    fw_warning = None
    try:
        from . import firewall
        fc = firewall.load()
        if c["enabled"] and fc.get("enabled") and fc.get("default") == "deny" and not any(
                r.get("enabled", True) and r["action"] == "allow" and ("vpn" in r.get("services", []) or not (r.get("services") or r.get("ports")))
                and not r.get("source") for r in fc["rules"]):
            fw_warning = "The firewall denies traffic by default: add a rule that allows “WireGuard VPN” from anyone, or devices can't connect."
        elif c["enabled"] and fc.get("enabled") and not firewall._allowed(fc, str(list(ipaddress.ip_network(c["subnet"]).hosts())[1]), 5001):
            fw_warning = (f"Devices connect, but the firewall blocks them from this NAS: VPN devices use addresses in {c['subnet']}. "
                          "Add a firewall rule that allows the services you need from that subnet.")
    except Exception:
        pass
    return {k: v for k, v in c.items() if k not in ("server_private", "peers")} | {
        "peers": peers, "running": running, "available": DEV or shutil.which("wg") is not None,
        "suggested_endpoint": default_endpoint(), "lan": lan_networks(), "firewall_warning": fw_warning}


@router.get("")
def get_vpn(_=Depends(require_admin)):
    return _view(load())


class Settings(BaseModel):
    enabled: bool
    subnet: str = "10.8.0.0/24"
    port: int = 51820
    endpoint: str = ""          # public name/IP devices connect to (DDNS name)
    dns: str = ""               # DNS server for devices (optional)


@router.put("")
def put_settings(body: Settings, _=Depends(require_admin)):
    if not DEV and not shutil.which("wg"):
        raise HTTPException(400, "WireGuard isn't installed yet - update Cynthology (Control Panel > Update)")
    try:
        net = ipaddress.ip_network(body.subnet, strict=True)
    except ValueError:
        raise HTTPException(400, "subnet must look like 10.8.0.0/24")
    if net.version != 4 or not net.is_private or not 16 <= net.prefixlen <= 29:
        raise HTTPException(400, "use a private IPv4 subnet between /16 and /29, e.g. 10.8.0.0/24")
    if any(ipaddress.ip_network(l).overlaps(net) for l in lan_networks()):
        raise HTTPException(400, "the VPN subnet overlaps your home network - choose another one")
    if not 1024 <= body.port <= 65535:
        raise HTTPException(400, "port: 1024-65535")
    ep = body.endpoint.strip()
    if body.enabled and not re.fullmatch(r"[A-Za-z0-9.-]{3,253}|\[?[0-9a-fA-F:]+\]?", ep):
        raise HTTPException(400, "enter the public name (e.g. your DDNS name) or IP address devices use to reach your home")
    if body.dns and not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", body.dns.strip()):
        raise HTTPException(400, "DNS: an IPv4 address")
    c = load()
    if c["peers"] and str(net) != c["subnet"]:
        raise HTTPException(400, "remove all devices before changing the VPN subnet")
    if "server_private" not in c:
        c["server_private"], c["server_public"] = keypair()
    c.update(enabled=body.enabled, subnet=str(net), port=body.port, endpoint=ep, dns=body.dns.strip())
    apply(c)
    save(c)
    return _view(c)


class Peer(BaseModel):
    name: str
    full_tunnel: bool = False   # True: all traffic through home; False: only the NAS + home network


@router.post("/peers")
def add_peer(body: Peer, _=Depends(require_admin)):
    name = body.name.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._'-]{0,39}", name):
        raise HTTPException(400, "device name: letters, digits, spaces, . _ ' - (max 40)")
    c = load()
    if "server_public" not in c:
        raise HTTPException(400, "turn on the VPN server first")
    if any(p["name"].lower() == name.lower() for p in c["peers"]):
        raise HTTPException(409, "a device with that name exists")
    net = ipaddress.ip_network(c["subnet"])
    used = {str(next(net.hosts()))} | {p["address"] for p in c["peers"]}
    addr = next((str(h) for h in net.hosts() if str(h) not in used), None)
    if not addr:
        raise HTTPException(400, "the VPN subnet is full")
    priv, pub = keypair()
    p = {"id": secrets.token_hex(4), "name": name, "address": addr, "private": priv, "public": pub,
         "psk": _wg("genpsk"), "full_tunnel": body.full_tunnel, "enabled": True, "created": int(time.time())}
    c["peers"].append(p)
    apply(c)
    save(c)
    return {"id": p["id"]}


@router.get("/peers/{pid}/config")
def peer_config(pid: str, _=Depends(require_admin)):
    c = load()
    p = next((x for x in c["peers"] if x["id"] == pid), None)
    if not p:
        raise HTTPException(404, "no such device")
    text = render_client(c, p)
    svg = ""
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
        import io
        buf = io.BytesIO()
        img.save(buf)
        svg = buf.getvalue().decode()
    except Exception:
        pass
    return {"name": p["name"], "config": text, "qr_svg": svg}


class PeerUpdate(BaseModel):
    enabled: bool


@router.put("/peers/{pid}")
def update_peer(pid: str, body: PeerUpdate, _=Depends(require_admin)):
    c = load()
    p = next((x for x in c["peers"] if x["id"] == pid), None)
    if not p:
        raise HTTPException(404, "no such device")
    p["enabled"] = body.enabled
    apply(c)
    save(c)
    return {"ok": True}


@router.delete("/peers/{pid}")
def delete_peer(pid: str, _=Depends(require_admin)):
    c = load()
    c["peers"] = [x for x in c["peers"] if x["id"] != pid]
    apply(c)
    save(c)
    return {"ok": True}
