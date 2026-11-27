import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cynthology import auth, portal, settings, tls  # noqa: E402


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(portal, "BG", str(tmp_path / "bg.img"))
    monkeypatch.setattr(tls, "cert_info", lambda: {"kind": "self-signed"})
    app = FastAPI()
    app.include_router(portal.router)
    app.dependency_overrides[auth.require_admin] = lambda: {"user": "admin", "admin": True}
    return TestClient(app)


def test_public_page_settings(c):
    r = c.put("/api/portal/settings", json={"title": "Family\nNAS", "message": "Hi!\nSign in.", "background": "aurora"})
    assert r.status_code == 200
    pub = c.get("/api/portal").json()
    assert pub["title"] == "FamilyNAS" and pub["message"] == "Hi!\nSign in." and pub["background"] == "aurora"
    assert "domain" not in pub and "hsts" not in pub
    assert c.put("/api/portal/settings", json={"background": "custom"}).status_code == 400      # no picture yet
    assert c.put("/api/portal/background", content=b"\xff\xd8\xff" + b"0" * 50).status_code == 200
    assert c.put("/api/portal/settings", json={"background": "custom"}).status_code == 200
    assert c.get("/api/portal/background").headers["content-type"] == "image/jpeg"


@pytest.mark.parametrize("domain", ["nas", "-bad.example.com", "a b.com", "http://nas.example.com"])
def test_bad_domain(c, domain):
    assert c.put("/api/portal/settings", json={"domain": domain}).status_code == 400


def test_hsts_needs_domain_and_trusted_cert(c, monkeypatch):
    assert c.put("/api/portal/settings", json={"domain": "nas.example.com", "hsts": True}).status_code == 400
    monkeypatch.setattr(tls, "cert_info", lambda: {"kind": "letsencrypt"})
    assert c.put("/api/portal/settings", json={"hsts": True}).status_code == 400              # no domain
    assert c.put("/api/portal/settings", json={"domain": "NAS.Example.com.", "hsts": True}).status_code == 200

    class R:
        def __init__(self, scheme, host):
            self.url = type("U", (), {"scheme": scheme, "hostname": host})()
    assert portal.hsts_header(R("https", "nas.example.com")) == portal.HSTS_VALUE
    assert portal.hsts_header(R("https", "192.168.1.20")) is None
    assert portal.hsts_header(R("http", "nas.example.com")) is None


def test_sharelinks_prefer_domain(c):
    from cynthology import sharelinks
    c.put("/api/portal/settings", json={"domain": "nas.example.com", "external_port": 443})
    assert sharelinks.addresses(None)["preferred"] == "https://nas.example.com"
    c.put("/api/portal/settings", json={"domain": "nas.example.com"})
    assert sharelinks.addresses(None)["preferred"] == "https://nas.example.com:5001"
