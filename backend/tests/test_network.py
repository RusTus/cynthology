import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from cynthology.network import Bond, Ipv4, NetConfig, Nic, render, validate  # noqa: E402

NICS = ["ens33", "ens34", "ens35"]


def cfg(**kw):
    base = dict(nics=[Nic(name="ens33", ipv4=Ipv4(mode="static", address="192.168.1.50/24", gateway="192.168.1.1")),
                      Nic(name="ens34"), Nic(name="ens35", enabled=False)], dns=["1.1.1.1"])
    base.update(kw)
    return NetConfig(**base)


def test_render_static_dhcp_disabled():
    t = render(cfg())
    assert "iface ens33 inet static\n    address 192.168.1.50/24\n    gateway 192.168.1.1" in t
    assert "iface ens34 inet dhcp" in t
    assert "# ens35: disabled\niface ens35 inet manual" in t
    assert "iface lo inet loopback" in t and "inet6 auto" in t


def test_render_bond_lacp_jumbo():
    c = cfg(nics=[Nic(name="ens34"), Nic(name="ens35")],
            bonds=[Bond(name="bond0", mode="802.3ad", members=["ens34", "ens35"], mtu=9000,
                        ipv4=Ipv4(mode="static", address="10.0.0.5/24", gateway="10.0.0.1"))])
    t = render(c)
    assert "auto ens34\niface ens34 inet manual\n    bond-master bond0" in t
    assert "allow-hotplug" not in t
    assert "auto bond0\niface bond0 inet static" in t
    assert "bond-slaves ens34 ens35" in t and "bond-mode 802.3ad" in t and "bond-lacp-rate fast" in t
    assert "mtu 9000" in t


@pytest.mark.parametrize("bad,msg", [
    (dict(nics=[Nic(name="ens33", ipv4=Ipv4(mode="static", address="192.168.1.50"))]), "address must look like"),
    (dict(nics=[Nic(name="ens33", ipv4=Ipv4(mode="static", address="192.168.1.0/24"))]), "not a usable host"),
    (dict(nics=[Nic(name="ens33", ipv4=Ipv4(mode="static", address="192.168.1.50/24", gateway="10.0.0.1"))]), "is not in"),
    (dict(nics=[Nic(name="ens33", ipv4=Ipv4(mode="off")), Nic(name="ens34", enabled=False)]), "lose access"),
    (dict(nics=[Nic(name="ens33", ipv4=Ipv4(mode="static", address="192.168.1.50/24"))], dns=[]), "DNS"),
    (dict(nics=[Nic(name="ens33", mtu=20000)]), "MTU"),
    (dict(bonds=[Bond(members=["ens34"])]), "at least two"),
    (dict(bonds=[Bond(members=["ens34", "eth9"])]), "can't be used"),
    (dict(nics=[Nic(name="ens33", ipv4=Ipv4(mode="static", address="192.168.1.50/24", gateway="192.168.1.1")),
                Nic(name="ens34", ipv4=Ipv4(mode="static", address="10.0.0.5/24", gateway="10.0.0.1"))]), "one default gateway"),
])
def test_validation(bad, msg):
    with pytest.raises(HTTPException) as e:
        validate(cfg(**bad), NICS)
    assert msg in e.value.detail


def test_valid_config_passes():
    validate(cfg(), NICS)


def test_vlans_render_and_validate():
    from cynthology.network import Vlan
    c = cfg(vlans=[Vlan(parent="ens34", id=20, ipv4=Ipv4(mode="static", address="10.20.0.5/24")),
                   Vlan(parent="ens34", id=10)])
    validate(c, NICS)
    t = render(c)
    assert "auto ens34.10\niface ens34.10 inet dhcp\n    vlan-raw-device ens34" in t
    assert "iface ens34.20 inet static\n    address 10.20.0.5/24" in t
    assert t.index("ens34.10") < t.index("ens34.20")
    for bad in [Vlan(parent="ens35", id=5), Vlan(parent="ens34", id=0), Vlan(parent="ens34", id=5000), Vlan(parent="ens34", id=7, mtu=9000)]:
        with pytest.raises(HTTPException):
            validate(cfg(vlans=[bad]), NICS)
    with pytest.raises(HTTPException):
        validate(cfg(vlans=[Vlan(parent="ens34", id=7), Vlan(parent="ens34", id=7)]), NICS)
