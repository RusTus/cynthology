import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cynthology import account, auth, health, logcenter, users  # noqa: E402


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(account, "PROFILES", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(logcenter, "AUTH_LOG", str(tmp_path / "auth.jsonl"))
    monkeypatch.setattr(health, "load_smtp", lambda: {})
    monkeypatch.setattr(auth, "_is_admin", lambda u: True)
    auth.SESSIONS.clear()
    auth.FAILS.clear()
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(account.router)
    return TestClient(app)


def login(c, agent="TestBrowser/1"):
    r = c.post("/api/auth/login", json={"username": "admin", "password": "admin"}, headers={"User-Agent": agent})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_sessions_listed_and_signed_out(c):
    a, b = login(c, "Laptop"), login(c, "Phone")
    me = c.get("/api/account", headers=a).json()
    assert len(me["sessions"]) == 2 and me["sessions"][0]["current"] and me["sessions"][0]["agent"] == "Laptop"
    assert all("token" not in s for s in me["sessions"])
    assert any(e["event"] == "login" for e in me["history"])
    assert c.post("/api/account/sessions/sign-out-others", headers=a).json()["ended"] == 1
    assert c.get("/api/account", headers=b).status_code == 401
    assert c.get("/api/account", headers=a).status_code == 200


def test_end_one_session_only_own(c):
    a, b = login(c), login(c)
    other = next(s["id"] for s in c.get("/api/account", headers=a).json()["sessions"] if not s["current"])
    assert c.delete(f"/api/account/sessions/{other}", headers=a).status_code == 200
    assert c.get("/api/account", headers=b).status_code == 401
    assert c.delete("/api/account/sessions/0000000000000000", headers=a).status_code == 404


def test_change_password(c):
    a = login(c)
    assert c.post("/api/account/password", json={"current": "wrong", "new": "abcdefgh1"}, headers=a).status_code == 403
    assert c.post("/api/account/password", json={"current": "admin", "new": "short"}, headers=a).status_code == 400
    r = c.post("/api/account/password", json={"current": "admin", "new": "a-new-passw0rd"}, headers=a)
    assert r.status_code == 200 and r.json()["token"]
    assert c.get("/api/account", headers=a).status_code == 401            # old sessions are gone...
    assert c.get("/api/account", headers={"Authorization": "Bearer " + r.json()["token"]}).status_code == 200


def test_profile(c, monkeypatch):
    a = login(c)
    assert c.put("/api/account", json={"email": "not-an-address"}, headers=a).status_code == 400
    assert c.put("/api/account", json={"email": "me@example.org", "alerts": True, "full_name": "Ada L"}, headers=a).status_code == 200
    me = c.get("/api/account", headers=a).json()
    assert (me["email"], me["alerts"], me["full_name"]) == ("me@example.org", True, "Ada L")
    assert account.alert_recipients() == ["me@example.org"]
    monkeypatch.setattr(auth, "_is_admin", lambda u: False)
    assert account.alert_recipients() == []                               # (no longer an administrator)
    assert c.put("/api/account", json={"full_name": "a:b"}, headers=a).status_code == 400


def test_password_rules():
    for bad in ("short", "line\nbreak-x", "x" * 300, "tab\there-ok?"):
        with pytest.raises(HTTPException):
            users.check_password(bad)
    users.check_password("Correct horse battery staple")


def test_prefs_and_wallpaper(c, tmp_path, monkeypatch):
    monkeypatch.setattr(account, "WALLS", str(tmp_path / "walls"))
    a = login(c)
    assert c.put("/api/account", json={"prefs": {"time": "12", "icons": "large", "resume": True}}, headers=a).status_code == 200
    assert c.put("/api/account", json={"prefs": {"time": "13"}}, headers=a).status_code == 400
    assert c.put("/api/account", json={"prefs": {"evil": 1}}, headers=a).status_code == 400
    assert c.get("/api/account", headers=a).json()["prefs"] == {"time": "12", "icons": "large", "resume": True}
    assert c.put("/api/account/wallpaper", content=b"<svg onload=alert(1)>", headers=a).status_code == 400
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    assert c.put("/api/account/wallpaper", content=png, headers=a).status_code == 200
    r = c.get("/api/account/wallpaper", headers=a)
    assert r.content == png and r.headers["content-type"] == "image/png"
    assert c.get("/api/account", headers=a).json()["wallpaper"] is True
    c.delete("/api/account/wallpaper", headers=a)
    assert c.get("/api/account/wallpaper", headers=a).status_code == 404


def test_storage_lists_only_accessible_shares(c, tmp_path, monkeypatch):
    from cynthology import files, shares
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    monkeypatch.setattr(shares, "homes_share", lambda: None)
    monkeypatch.setattr(shares, "load", lambda: [{"name": "a", "path": str(tmp_path / "a"), "quota_gb": 5},
                                                 {"name": "b", "path": str(tmp_path / "b")}])
    monkeypatch.setattr(files, "access", lambda u, s: "rw" if s["name"] == "a" else None)
    rows = c.get("/api/account/storage", headers=login(c)).json()
    assert [r["name"] for r in rows] == ["a"] and rows[0]["limit"] == 5 * 1024 ** 3 and rows[0]["free"] > 0
