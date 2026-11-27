import os

os.environ["CYN_DEV"] = "1"

from cynthology import vpn  # noqa: E402


def conf():
    return {"enabled": True, "subnet": "10.8.0.0/24", "port": 51820, "endpoint": "mynas.duckdns.org", "dns": "",
            "server_private": "SPRIV=", "server_public": "SPUB=",
            "peers": [{"name": "phone", "address": "10.8.0.2", "private": "PPRIV=", "public": "PPUB=", "psk": "PSK=", "full_tunnel": False},
                      {"name": "old", "address": "10.8.0.3", "private": "x", "public": "OLD=", "psk": "y", "enabled": False}]}


def test_server_conf():
    t = vpn.render_server(conf())
    assert "Address = 10.8.0.1/24" in t and "ListenPort = 51820" in t and "PrivateKey = SPRIV=" in t
    assert "PublicKey = PPUB=" in t and "AllowedIPs = 10.8.0.2/32" in t and "PresharedKey = PSK=" in t
    assert "OLD=" not in t                                  # disabled device
    assert "PostUp = /usr/lib/cynthology/vpn-route up %i 10.8.0.0/24" in t


def test_client_conf(monkeypatch):
    monkeypatch.setattr(vpn, "lan_networks", lambda: ["192.168.1.0/24"])
    c = conf()
    t = vpn.render_client(c, c["peers"][0])
    assert "Endpoint = mynas.duckdns.org:51820" in t and "AllowedIPs = 10.8.0.0/24, 192.168.1.0/24" in t
    assert "PublicKey = SPUB=" in t and "PrivateKey = PPRIV=" in t and "DNS" not in t
    monkeypatch.setattr(vpn, "upstream_dns", lambda: "192.168.1.1")
    full = vpn.render_client(c, {**c["peers"][0], "full_tunnel": True})
    assert "AllowedIPs = 0.0.0.0/0, ::/0" in full and "DNS = 192.168.1.1" in full
