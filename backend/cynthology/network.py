"""Network settings (ifupdown, Debian's default): DHCP/static IPv4, MTU, DNS, link aggregation, VLANs.

Safe apply: the previous /etc/network/interfaces is kept, and a rollback is scheduled
(systemd timer, 90 s) BEFORE the new configuration is activated. The browser confirms from the
new address; if nobody confirms in time, the NAS restores the old settings by itself.
"""
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import time

import psutil
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import current_user, require_admin
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/network", tags=["network"])
ETC = os.path.join(STATE_DIR, "etc") if DEV else "/etc"
IFACES = os.path.join(ETC, "network", "interfaces")
BACKUP = IFACES + ".cyn-previous"
RESOLV = os.path.join(ETC, "resolv.conf")
RESOLV_BACKUP = RESOLV + ".cyn-previous"
CFG = os.path.join(STATE_DIR, "network.json")
ROLLBACK_SECONDS = 90
SKIP = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap", "wg")
BOND_MODES = {"balance-alb": "Adaptive Load Balancing (no switch setup needed)",
              "active-backup": "Active-Backup (failover only)",
              "802.3ad": "IEEE 802.3ad / LACP (needs a managed switch with LACP)"}


def _read(path: str) -> str:
    try:
        return open(path).read().strip()
    except OSError:
        return ""


def physical_nics() -> list[str]:
    out = []
    for n in sorted(os.listdir("/sys/class/net")):
        if n.startswith(SKIP) or n.startswith("bond"):
            continue
        if os.path.exists(f"/sys/class/net/{n}/device") or DEV:
            out.append(n)
    return out


def manager() -> str | None:
    """Who else manages networking here (Cynthology only drives ifupdown)."""
    def active(unit):
        return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0
    if active("NetworkManager"):
        return "NetworkManager"
    if os.path.isdir("/etc/netplan") and any(f.endswith(".yaml") for f in os.listdir("/etc/netplan")):
        return "netplan"
    if active("systemd-networkd") and not os.path.exists("/etc/network/interfaces"):
        return "systemd-networkd"
    return None


def status() -> dict:
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    ifs = []
    for n in sorted(addrs):
        if n.startswith(SKIP):
            continue
        st = stats.get(n)
        ifs.append({
            "name": n, "bond": n.startswith("bond"),
            "mac": next((a.address for a in addrs[n] if a.family == psutil.AF_LINK), ""),
            "up": bool(st and st.isup), "carrier": _read(f"/sys/class/net/{n}/carrier") == "1",
            "speed": st.speed if st and st.speed > 0 else None, "mtu": st.mtu if st else None,
            "ipv4": [f"{a.address}/{ipaddress.IPv4Network(f'0.0.0.0/{a.netmask}').prefixlen}" for a in addrs[n] if a.family == socket.AF_INET],
            "ipv6": [a.address.split("%")[0] for a in addrs[n] if a.family == socket.AF_INET6],
            "master": os.path.basename(os.path.realpath(f"/sys/class/net/{n}/master")) if os.path.exists(f"/sys/class/net/{n}/master") else None,
            "bond_mode": _read(f"/sys/class/net/{n}/bonding/mode").split(" ")[0] or None,
            "bond_active": _read(f"/sys/class/net/{n}/bonding/active_slave") or None,
        })
    gw = ""
    for line in _read("/proc/net/route").splitlines()[1:]:
        f = line.split()
        if f[1] == "00000000":
            gw = socket.inet_ntoa(int(f[2], 16).to_bytes(4, "little"))
            break
    dns = re.findall(r"^nameserver\s+(\S+)", _read("/etc/resolv.conf"), re.M)
    return {"interfaces": ifs, "gateway": gw, "dns": dns, "manager": manager(), "pending": pending()}


# ---------- configuration model ----------

class Ipv4(BaseModel):
    mode: str = "dhcp"            # dhcp | static | off
    address: str = ""             # CIDR, e.g. 192.168.1.50/24
    gateway: str = ""


class Nic(BaseModel):
    name: str
    enabled: bool = True
    mtu: int = 1500
    ipv4: Ipv4 = Ipv4()


class Bond(BaseModel):
    name: str = "bond0"
    mode: str = "balance-alb"
    members: list[str]
    mtu: int = 1500
    ipv4: Ipv4 = Ipv4()


class Vlan(BaseModel):
    parent: str                   # a network port or bond, e.g. ens33 / bond0
    id: int                       # 802.1Q VLAN ID 1-4094
    mtu: int = 1500
    ipv4: Ipv4 = Ipv4()

    @property
    def name(self) -> str:
        return f"{self.parent}.{self.id}"


class NetConfig(BaseModel):
    nics: list[Nic]
    bonds: list[Bond] = []
    vlans: list[Vlan] = []
    dns: list[str] = []           # empty = from DHCP


def validate(cfg: NetConfig, nics: list[str]) -> None:
    used = set()
    for n in cfg.nics:            # (names go into /etc/network/interfaces: only real ports)
        if n.name not in nics:
            raise HTTPException(400, f"unknown network port {n.name!r}")
    for b in cfg.bonds:
        if not re.fullmatch(r"bond[0-9]", b.name):
            raise HTTPException(400, "bond names are bond0..bond9")
        if b.mode not in BOND_MODES:
            raise HTTPException(400, "unknown bond mode")
        if len(b.members) < 2:
            raise HTTPException(400, "a bond needs at least two network ports")
        for m in b.members:
            if m not in nics or m in used:
                raise HTTPException(400, f"port {m} can't be used in {b.name}")
            used.add(m)
    parents = {n.name: n.mtu for n in cfg.nics if n.enabled and n.name not in used} | {b.name: b.mtu for b in cfg.bonds}
    seen = set()
    for v in cfg.vlans:
        if not 1 <= v.id <= 4094:
            raise HTTPException(400, f"VLAN ID {v.id}: must be 1-4094")
        if v.parent not in parents:
            raise HTTPException(400, f"VLAN {v.id}: {v.parent} is not an enabled port or bond")
        if v.name in seen:
            raise HTTPException(400, f"VLAN {v.id} is defined twice on {v.parent}")
        if v.mtu > parents[v.parent]:
            raise HTTPException(400, f"{v.name}: MTU can't be larger than {v.parent}'s ({parents[v.parent]})")
        seen.add(v.name)
    gateways = 0
    has_address = False
    for name, ip, mtu in [(n.name, n.ipv4, n.mtu) for n in cfg.nics if n.enabled and n.name not in used] + \
                         [(b.name, b.ipv4, b.mtu) for b in cfg.bonds] + [(v.name, v.ipv4, v.mtu) for v in cfg.vlans]:
        if not 576 <= mtu <= 9216:
            raise HTTPException(400, f"{name}: MTU must be 576-9216")
        if ip.mode not in ("dhcp", "static", "off"):
            raise HTTPException(400, f"{name}: bad IPv4 mode")
        if ip.mode == "dhcp":
            has_address = True
        if ip.mode == "static":
            if "/" not in ip.address:
                raise HTTPException(400, f"{name}: address must look like 192.168.1.50/24 (the /24 is the subnet size)")
            try:
                iface = ipaddress.IPv4Interface(ip.address)
            except ValueError:
                raise HTTPException(400, f"{name}: address must look like 192.168.1.50/24")
            if iface.network.prefixlen > 30 or iface.ip in (iface.network.network_address, iface.network.broadcast_address):
                raise HTTPException(400, f"{name}: {ip.address} is not a usable host address")
            has_address = True
            if ip.gateway:
                try:
                    gwip = ipaddress.IPv4Address(ip.gateway)
                except ValueError:
                    raise HTTPException(400, f"{name}: bad gateway")
                if gwip not in iface.network:
                    raise HTTPException(400, f"{name}: gateway {gwip} is not in {iface.network}")
                gateways += 1
    if not has_address:
        raise HTTPException(400, "at least one port must have an address, or you would lose access to the NAS")
    if gateways > 1:
        raise HTTPException(400, "only one default gateway is allowed")
    for d in cfg.dns:
        try:
            ipaddress.ip_address(d)
        except ValueError:
            raise HTTPException(400, f"bad DNS server {d}")
    if not cfg.dns and not any(x.ipv4.mode == "dhcp" for x in list(cfg.nics) + list(cfg.bonds) + list(cfg.vlans)):
        raise HTTPException(400, "with only static addresses, enter at least one DNS server")


def render(cfg: NetConfig) -> str:
    """/etc/network/interfaces for a configuration (pure; unit tested)."""
    out = ["# Managed by Cynthology - edit in Control Panel > Network.", "", "source /etc/network/interfaces.d/*", "",
           "auto lo", "iface lo inet loopback", ""]
    members = {m: b.name for b in cfg.bonds for m in b.members}

    def addr_block(name: str, ip: Ipv4, mtu: int, extra: list[str]) -> list[str]:
        # "auto" (not "allow-hotplug"): brought up at boot AND by `ifup -a`, which restarts and rollbacks rely on
        lines = [f"auto {name}"]
        if ip.mode == "dhcp":
            lines.append(f"iface {name} inet dhcp")
        elif ip.mode == "static":
            lines += [f"iface {name} inet static", f"    address {ip.address}"]
            if ip.gateway:
                lines.append(f"    gateway {ip.gateway}")
        else:
            lines.append(f"iface {name} inet manual")
        if mtu != 1500:
            lines.append(f"    mtu {mtu}")
        lines += [f"    {e}" for e in extra]
        lines.append(f"iface {name} inet6 auto")
        return lines + [""]

    for n in cfg.nics:
        if n.name in members:
            out += [f"auto {n.name}", f"iface {n.name} inet manual", f"    bond-master {members[n.name]}"]
            if n.mtu != 1500:
                out.append(f"    mtu {n.mtu}")
            out.append("")
        elif not n.enabled:
            out += [f"# {n.name}: disabled", f"iface {n.name} inet manual", ""]
        else:
            out += addr_block(n.name, n.ipv4, n.mtu, [])
    for b in cfg.bonds:
        extra = [f"bond-slaves {' '.join(b.members)}", f"bond-mode {b.mode}", "bond-miimon 100"]
        if b.mode == "802.3ad":
            extra += ["bond-lacp-rate fast", "bond-xmit-hash-policy layer3+4"]
        if b.mode == "active-backup":
            extra.append(f"bond-primary {b.members[0]}")
        out += addr_block(b.name, b.ipv4, b.mtu, extra)
    for v in sorted(cfg.vlans, key=lambda v: (v.parent, v.id)):   # ifupdown creates <parent>.<id> as an 802.1Q interface
        out += addr_block(v.name, v.ipv4, v.mtu, [f"vlan-raw-device {v.parent}"])
    return "\n".join(out).rstrip() + "\n"


def current_config() -> dict:
    """Saved Cynthology config, or one derived from the running system for first use."""
    try:
        with open(CFG) as f:
            return {"vlans": [], **json.load(f)}
    except (OSError, ValueError):
        pass
    text = _read(IFACES)
    st = status()
    nics = []
    for n in physical_nics():
        mode = "static" if re.search(rf"^iface {n} inet static", text, re.M) else "dhcp"
        addr = next((i["ipv4"][0] for i in st["interfaces"] if i["name"] == n and i["ipv4"]), "")
        mtu = next((i["mtu"] for i in st["interfaces"] if i["name"] == n and i["mtu"]), 1500)
        nics.append({"name": n, "enabled": True, "mtu": mtu,
                     "ipv4": {"mode": mode, "address": addr if mode == "static" else "", "gateway": st["gateway"] if mode == "static" else ""}})
    return {"nics": nics, "bonds": [], "vlans": [], "dns": []}


# ---------- safe apply ----------

def pending() -> dict | None:
    try:
        with open(os.path.join(STATE_DIR, "network-pending.json")) as f:
            p = json.load(f)
        return p if p["deadline"] > time.time() else None
    except (OSError, ValueError, KeyError):
        return None


def _restart_networking() -> None:
    run(["/usr/lib/cynthology/net-restart"], check=False)


@router.get("")
def get_network(_=Depends(current_user)):
    return {"status": status(), "config": current_config(), "nics": physical_nics(), "bond_modes": BOND_MODES,
            "rollback_seconds": ROLLBACK_SECONDS}


@router.put("")
def apply(body: NetConfig, _=Depends(require_admin)):
    m = manager()
    if m and not DEV:
        raise HTTPException(409, f"networking on this system is managed by {m}; Cynthology's network settings work with "
                                 f"Debian's default (ifupdown). Change the network with {m}'s own tools.")
    nics = physical_nics()
    validate(body, nics)
    if body.bonds and not DEV and not os.path.exists("/etc/network/if-pre-up.d/ifenslave"):
        raise HTTPException(400, "link aggregation needs the 'ifenslave' package (re-run the Cynthology installer)")
    if pending():
        raise HTTPException(409, "a previous change is still waiting for confirmation")
    os.makedirs(os.path.dirname(IFACES), exist_ok=True)
    if os.path.exists(IFACES):
        shutil.copy2(IFACES, BACKUP)
    if os.path.exists(RESOLV) and not os.path.islink(RESOLV):
        shutil.copy2(RESOLV, RESOLV_BACKUP)
    # 1. arm the rollback BEFORE touching anything
    if not DEV:
        run(["systemd-run", f"--on-active={ROLLBACK_SECONDS}", "--timer-property=AccuracySec=1s", "--unit=cyn-net-rollback", "--collect",
             "/usr/lib/cynthology/net-rollback"])
    with open(os.path.join(STATE_DIR, "network-pending.json"), "w") as f:
        json.dump({"deadline": time.time() + ROLLBACK_SECONDS, "config": body.model_dump()}, f)
    # 2. write and activate
    with open(IFACES + ".tmp", "w") as f:
        f.write(render(body))
    os.replace(IFACES + ".tmp", IFACES)
    if body.dns and not os.path.islink(RESOLV):
        with open(RESOLV, "w") as f:
            f.write("# Managed by Cynthology\n" + "".join(f"nameserver {d}\n" for d in body.dns))
    if not DEV:
        # restart networking in 2 s, from systemd, so this HTTP response still reaches the browser
        run(["systemd-run", "--on-active=2", "--unit=cyn-net-apply", "--collect", "/usr/lib/cynthology/net-restart"])
    addresses = [x.ipv4.address.split("/")[0] for x in list(body.nics) + list(body.bonds) + list(body.vlans)
                 if x.ipv4.mode == "static" and getattr(x, "enabled", True)]
    return {"ok": True, "rollback_in": ROLLBACK_SECONDS, "static_addresses": addresses}


@router.post("/confirm")
def confirm(_=Depends(require_admin)):
    p = pending()
    if not p:
        raise HTTPException(409, "nothing to confirm (the change was already confirmed or rolled back)")
    if not DEV:
        subprocess.run(["systemctl", "stop", "cyn-net-rollback.timer"], capture_output=True)
    with open(CFG, "w") as f:
        json.dump(p["config"], f, indent=2)
    os.remove(os.path.join(STATE_DIR, "network-pending.json"))
    return {"ok": True}


def rollback() -> None:
    """Called by the cyn-net-rollback timer when the change was not confirmed in time."""
    pend = os.path.join(STATE_DIR, "network-pending.json")
    if not os.path.exists(pend):
        return
    if os.path.exists(BACKUP):
        shutil.copy2(BACKUP, IFACES)
    if os.path.exists(RESOLV_BACKUP):
        shutil.copy2(RESOLV_BACKUP, RESOLV)
    os.remove(pend)
    _restart_networking()
    from .backup import _alert   # logs + emails
    _alert({"name": "network"}, "warning", "Network change rolled back",
           "The new network settings were not confirmed within 90 seconds, so the previous settings were restored.")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["rollback"]:
        rollback()
