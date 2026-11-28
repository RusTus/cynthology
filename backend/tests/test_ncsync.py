import base64
import os
import time

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cynthology import auth, files, ncsync, passkeys, privileges, shares, twofa, webdav  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "sub" / "a.txt").write_text("hello")
    (docs / ".snapshots").mkdir()
    monkeypatch.setattr(ncsync, "FILE", str(tmp_path / "nc.json"))
    monkeypatch.setattr(privileges, "FILE", str(tmp_path / "priv.json"))
    monkeypatch.setattr(passkeys, "FILE", str(tmp_path / "pk.json"))
    monkeypatch.setattr(shares, "load", lambda: [{"name": "docs", "path": str(docs), "read_users": [], "write_users": ["alice"]}])
    monkeypatch.setattr(shares, "home_path", lambda u: None)
    monkeypatch.setattr(auth, "_is_admin", lambda u: False)
    monkeypatch.setattr(privileges, "_is_admin", lambda u: False)
    monkeypatch.setattr(files, "_matches", lambda u, lst: u in lst)
    monkeypatch.setattr(twofa, "enabled", lambda u: False)
    monkeypatch.setattr(webdav, "authenticate", lambda header, ip: None)       # (account passwords: not in this test)
    ncsync.FLOWS.clear()
    app = FastAPI()
    app.include_router(ncsync.router)
    app.include_router(ncsync.api)
    app.mount("/remote.php", ncsync.dav_app)
    app.dependency_overrides[auth.current_user] = lambda: {"user": "alice", "admin": False}
    return TestClient(app), docs


def basic(u, p):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


def test_login_flow_v2(env):
    c, _ = env
    assert c.get("/status.php").json()["installed"] is True
    caps = c.get("/ocs/v2.php/cloud/capabilities?format=json").json()["ocs"]["data"]
    assert caps["capabilities"]["dav"]["chunking"] == "1.0"
    s = c.post("/index.php/login/v2", headers={"User-Agent": "Mozilla/5.0 (Linux) mirall/3.13.0 (Nextcloud, linux-6.1)"}).json()
    poll = s["poll"]["token"]
    login = s["login"].rsplit("/", 1)[1]
    assert c.post("/index.php/login/v2/poll", data={"token": poll}).status_code == 404       # not yet
    assert c.get(f"/index.php/login/v2/flow/{login}").status_code == 200
    assert c.post(f"/api/ncsync/grant?flow={login}").json() == {"ok": True}
    r = c.post("/index.php/login/v2/poll", content=f"token={poll}", headers={"Content-Type": "application/x-www-form-urlencoded"}).json()
    assert r["loginName"] == "alice" and len(r["appPassword"]) > 40
    assert c.post("/index.php/login/v2/poll", data={"token": poll}).status_code == 404       # only once
    u = c.get("/ocs/v1.php/cloud/user?format=json", headers=basic("alice", r["appPassword"])).json()
    assert u["ocs"]["data"]["id"] == "alice" and u["ocs"]["meta"]["statuscode"] == 100
    apps = c.get("/api/ncsync/apps").json()
    assert len(apps) == 1 and "Nextcloud" in apps[0]["name"]
    c.delete(f"/api/ncsync/apps/{apps[0]['id']}")
    assert c.get("/ocs/v1.php/cloud/user", headers=basic("alice", r["appPassword"])).status_code == 401


def test_flow_v1_redirect(env):
    c, _ = env
    r = c.post("/api/ncsync/grant").json()
    assert r["redirect"].startswith("nc://login/server:http://testserver&user:alice&password:")


def test_dav_listing_and_etags(env):
    c, docs = env
    pw = ncsync.new_app_password("alice", "test")
    H = basic("alice", pw)
    r = c.request("PROPFIND", "/remote.php/dav/files/alice/", headers={**H, "Depth": "1"})
    assert r.status_code == 207 and "/remote.php/dav/files/alice/docs/" in r.text and "oc:permissions>GCK<" in r.text
    r = c.request("PROPFIND", "/remote.php/dav/files/alice/docs", headers={**H, "Depth": "1"})
    assert "sub/" in r.text and ".snapshots" not in r.text
    def etag(path):
        t = c.request("PROPFIND", path, headers={**H, "Depth": "0"}).text
        return t.split("<d:getetag>")[1].split("</d:getetag>")[0]
    root0, docs0 = etag("/remote.php/dav/files/alice/"), etag("/remote.php/dav/files/alice/docs/")
    time.sleep(0.01)
    (docs / "sub" / "a.txt").write_text("changed deep inside")     # (as if from SMB)
    ncsync._gen_cache.clear()
    ncsync._tree.clear()
    assert etag("/remote.php/dav/files/alice/") != root0 and etag("/remote.php/dav/files/alice/docs/") != docs0
    assert c.request("PROPFIND", "/remote.php/dav/files/bob/", headers={**H, "Depth": "0"}).status_code == 403


def test_put_mtime_and_chunked_upload(env):
    c, docs = env
    H = basic("alice", ncsync.new_app_password("alice", "test"))
    r = c.put("/remote.php/dav/files/alice/docs/new.txt", content=b"abc", headers={**H, "X-OC-Mtime": "1600000000"})
    assert r.status_code == 201 and r.headers["X-OC-MTime"] == "accepted" and r.headers["OC-FileId"]
    assert int(os.stat(docs / "new.txt").st_mtime) == 1600000000
    dest = "http://testserver/remote.php/dav/files/alice/docs/sub/big.bin"
    assert c.request("MKCOL", "/remote.php/dav/uploads/alice/tx1", headers={**H, "Destination": dest}).status_code == 201
    for i, part in enumerate([b"A" * 1000, b"B" * 1000, b"C" * 5], start=1):
        assert c.put(f"/remote.php/dav/uploads/alice/tx1/{i}", content=part, headers=H).status_code == 201
    assert "<d:getcontentlength>1000</d:getcontentlength>" in c.request("PROPFIND", "/remote.php/dav/uploads/alice/tx1", headers={**H, "Depth": "1"}).text
    r = c.request("MOVE", "/remote.php/dav/uploads/alice/tx1/.file",
                  headers={**H, "Destination": dest, "OC-Total-Length": "2005", "X-OC-Mtime": "1600000001"})
    assert r.status_code == 201, r.text
    assert (docs / "sub" / "big.bin").read_bytes() == b"A" * 1000 + b"B" * 1000 + b"C" * 5
    r = c.request("MOVE", "/remote.php/dav/files/alice/docs/new.txt", headers={**H, "Destination": "http://testserver/remote.php/dav/files/alice/docs/sub/moved.txt"})
    assert r.status_code == 201 and (docs / "sub" / "moved.txt").exists()
    assert c.request("MOVE", "/remote.php/dav/files/alice/docs/sub/moved.txt",
                     headers={**H, "Destination": "http://testserver/etc/passwd"}).status_code == 400


def test_password_rules(env, monkeypatch):
    c, _ = env
    assert c.request("PROPFIND", "/remote.php/dav/files/alice/", headers=basic("alice", "wrong")).status_code == 401
    seen = []
    monkeypatch.setattr(webdav, "authenticate", lambda header, ip: seen.append(1) or {"user": "alice", "admin": False})
    assert c.request("PROPFIND", "/remote.php/dav/files/alice/", headers={**basic("alice", "acct-pw"), "Depth": "0"}).status_code == 207
    monkeypatch.setattr(twofa, "enabled", lambda u: True)          # with 2-step verification: app passwords only
    assert c.request("PROPFIND", "/remote.php/dav/files/alice/", headers=basic("alice", "acct-pw")).status_code == 401
    privileges.FILE and c  # (privileges: denied users are refused)
    import json
    open(privileges.FILE, "w").write(json.dumps({"sync": {"mode": "deny", "users": ["alice"]}}))
    pw = ncsync.new_app_password("alice", "t")
    assert c.request("PROPFIND", "/remote.php/dav/files/alice/", headers=basic("alice", pw)).status_code == 403
