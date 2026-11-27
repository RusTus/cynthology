import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cynthology import auth, settings, snmp  # noqa: E402


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(snmp, "CONF", str(tmp_path / "snmpd.conf"))
    monkeypatch.setattr(snmp, "_default_networks", lambda: ["192.168.1.0/24"])
    app = FastAPI()
    app.include_router(snmp.router)
    app.dependency_overrides[auth.require_admin] = lambda: {"user": "admin", "admin": True}
    return TestClient(app)


def test_render_read_only(c):
    r = c.put("/api/snmp", json={"enabled": True, "community": "mon-1234", "networks": ["10.0.0.0/8", "fd00::/8"], "location": "Rack 1"})
    assert r.status_code == 200, r.text
    conf = open(snmp.CONF).read()
    assert "rocommunity mon-1234 10.0.0.0/8 -V all" in conf and "rocommunity6 mon-1234 fd00::/8 -V all" in conf
    assert "rwcommunity" not in conf and "rwuser" not in conf and "sysLocation Rack 1" in conf
    assert "auth_pw" not in r.json() and "community" in r.json()


def test_default_networks_when_empty(c):
    c.put("/api/snmp", json={"enabled": True, "community": "mon-1234"})
    assert "rocommunity mon-1234 192.168.1.0/24" in open(snmp.CONF).read()


@pytest.mark.parametrize("body", [
    {"enabled": True, "community": "public"},
    {"enabled": True, "community": "a b c d"},
    {"enabled": True, "community": "mon-1234\nrwcommunity x"},
    {"enabled": True, "v2c": False, "v3": True, "user": "mon", "auth_pw": "short", "priv_pw": "Priv-pass-456"},
    {"enabled": True, "v2c": False, "v3": True, "user": "mon", "auth_pw": "has space 123", "priv_pw": "Priv-pass-456"},
    {"enabled": True, "community": "mon-1234", "location": 'x"\nrwcommunity y'},
    {"enabled": True, "v2c": False, "v3": False},
    {"enabled": True, "community": "mon-1234", "networks": ["nope"]},
])
def test_refused(c, body):
    assert c.put("/api/snmp", json=body).status_code == 400


def test_v3_secrets_kept(c):
    r = c.put("/api/snmp", json={"enabled": True, "v2c": False, "v3": True, "user": "mon", "auth_pw": "Auth-pass-123", "priv_pw": "Priv-pass-456"})
    assert r.status_code == 200 and r.json()["has_v3_secrets"]
    r = c.put("/api/snmp", json={"enabled": True, "v2c": False, "v3": True, "user": "mon", "location": "B"})
    assert r.status_code == 200 and settings.get("snmp")["auth_pw"] == "Auth-pass-123"
    assert "rouser mon priv -V all" in open(snmp.CONF).read()


def test_usm_name():
    assert snmp._usm_name('usmUser 1 3 0x80001f88 "monitor" "monitor" NULL .1.3') == "monitor"
    assert snmp._usm_name("usmUser 1 3 0x80001f88 0x6d6f6e 0x6d6f6e NULL") == "mon"
