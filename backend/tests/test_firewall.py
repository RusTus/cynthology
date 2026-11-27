import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from cynthology import firewall as fw  # noqa: E402


def cfg(**kw):
    c = fw.Config(**kw).model_dump()
    return c


def test_render_rules():
    c = cfg(enabled=True, default="deny", rules=[
        fw.Rule(services=["web", "smb"], source="192.168.1.0/24"),
        fw.Rule(ports="8080, 9000-9100", protocol="both"),
        fw.Rule(action="deny", source="10.0.0.5"),
        fw.Rule(services=["ssh"], enabled=False)])
    t = fw.render(c)
    assert "ip saddr 192.168.1.0/24 tcp dport { 5000, 5001, 139, 445 } accept" in t
    assert "ip saddr 192.168.1.0/24 udp dport { 137, 138 } accept" in t
    assert "tcp dport { 8080, 9000-9100 } accept" in t and "udp dport { 8080, 9000-9100 } accept" in t
    assert "ip saddr 10.0.0.5/32 drop" in t
    assert "dport { 22 }" not in t
    assert t.rstrip().endswith("counter drop\n  }\n}".rstrip())
    # blocked addresses are dropped before established connections are accepted
    assert t.index("@autoblock4 drop") < t.index("established,related accept")


def test_disabled_firewall_keeps_only_autoblock():
    t = fw.render(cfg(enabled=False, default="deny", rules=[fw.Rule(services=["web"])]))
    assert "@autoblock4" in t and "dport" not in t and "counter drop" not in t


def test_lockout_check():
    c = cfg(enabled=True, default="deny", rules=[fw.Rule(services=["web"], source="192.168.1.0/24")])
    assert fw._allowed(c, "192.168.1.20", 5001)
    assert not fw._allowed(c, "10.1.1.1", 5001)
    assert not fw._allowed(c, "192.168.1.20", 22)
    c2 = cfg(enabled=True, default="allow", rules=[fw.Rule(action="deny", source="10.0.0.0/8")])
    assert not fw._allowed(c2, "10.2.3.4", 5000) and fw._allowed(c2, "192.168.0.9", 5000)


def test_validation():
    for bad in [dict(rules=[fw.Rule(ports="70000")]), dict(rules=[fw.Rule(source="not-an-ip")]),
                dict(rules=[fw.Rule(services=["telnet"])]), dict(default="maybe")]:
        with pytest.raises(HTTPException):
            fw.validate(fw.Config(**bad))
