import os
import time

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cynthology import auth, cloudsync, shares  # noqa: E402


@pytest.fixture
def c(tmp_path, monkeypatch):
    d = tmp_path / "cs"
    d.mkdir()
    for k, v in {"DIR": d, "CONNS": d / "c.json", "TASKS": d / "t.json", "CONF": d / "rclone.conf"}.items():
        monkeypatch.setattr(cloudsync, k, str(v))
    share = tmp_path / "docs"
    (share / "sub").mkdir(parents=True)
    monkeypatch.setattr(shares, "load", lambda: [{"name": "docs", "path": str(share)}])
    monkeypatch.setattr(cloudsync, "_obscure", lambda pw: "OBS(" + pw + ")")
    cloudsync.OAUTH.clear()
    app = FastAPI()
    app.include_router(cloudsync.router)
    app.dependency_overrides[auth.require_admin] = lambda: {"user": "admin", "admin": True}
    return TestClient(app)


def test_connection_conf_and_secrets(c):
    r = c.post("/api/cloudsync/connections", json={"name": "Nextcloud", "type": "webdav",
               "cfg": {"url": "https://cloud.example.org/remote.php/dav/files/me", "vendor": "nextcloud", "user": "me", "pass": "pw"}})
    assert r.status_code == 200, r.text
    assert "pass" not in r.json()["cfg"]
    conf = open(cloudsync.write_conf()).read()
    assert "type = webdav" in conf and "pass = OBS(pw)" in conf
    assert oct(os.stat(cloudsync.CONF).st_mode)[-3:] == "600"
    cid = r.json()["id"]
    # editing without retyping the password keeps it
    c.put(f"/api/cloudsync/connections/{cid}", json={"name": "NC", "type": "webdav", "cfg": {"url": "https://x.org/dav", "user": "me", "pass": ""}})
    assert "pass = OBS(pw)" in open(cloudsync.write_conf()).read()


@pytest.mark.parametrize("cfg", [{"url": "https://a\n[evil]"}, {"command": "x"}, {"url": " https://a"}, {"url": "file:///etc"}])
def test_bad_settings_refused(c, cfg):
    assert c.post("/api/cloudsync/connections", json={"name": "x", "type": "webdav", "cfg": cfg}).status_code == 400


def test_oauth_service_needs_sign_in(c):
    assert c.post("/api/cloudsync/connections", json={"name": "g", "type": "drive"}).status_code == 400
    cloudsync.OAUTH["s1"] = {"type": "drive", "token": '{"access_token":"a"}', "started": time.time(), "proc": None}
    r = c.post("/api/cloudsync/connections", json={"name": "g", "type": "drive", "oauth_session": "s1"})
    assert r.status_code == 200 and r.json()["signed_in"] and "token" not in r.json()["cfg"]
    assert 'token = {"access_token":"a"}' in open(cloudsync.write_conf()).read()


def test_oauth_finish_checks_state(c):
    class P:
        def poll(self):
            return None
    cloudsync.OAUTH["s"] = {"type": "drive", "state": "abc", "out": [], "started": time.time(), "proc": P()}
    r = c.post("/api/cloudsync/oauth/finish", json={"session": "s", "url": "http://localhost:53682/?state=WRONG&code=x"})
    assert r.status_code == 400
    r = c.post("/api/cloudsync/oauth/finish", json={"session": "s", "url": "http://localhost:53682/?error=access_denied"})
    assert r.status_code == 400 and "access_denied" in r.text


def test_tasks_and_commands(c):
    cid = c.post("/api/cloudsync/connections", json={"name": "S3", "type": "s3", "cfg": {"access_key_id": "k", "secret_access_key": "s"}}).json()["id"]
    base = {"name": "Docs", "connection": cid, "share": "docs", "local_path": "sub", "remote_path": "Backups/NAS"}
    assert c.post("/api/cloudsync/tasks", json={**base, "local_path": "../.."}).status_code == 400
    assert c.post("/api/cloudsync/tasks", json={**base, "remote_path": "a/../../b"}).status_code == 400
    assert c.post("/api/cloudsync/tasks", json={**base, "bwlimit": "1M; rm"}).status_code == 400
    t = c.post("/api/cloudsync/tasks", json={**base, "mirror": True}).json()
    cmd = cloudsync.command(t)
    assert cmd[0] == "sync" and cmd[1].endswith("/docs/sub") and cmd[2] == f"c{cid}:Backups/NAS" and "#recycle/**" in cmd
    t2 = c.post("/api/cloudsync/tasks", json={**base, "direction": "download"}).json()
    cmd = cloudsync.command(t2)
    assert cmd[0] == "copy" and cmd[1] == f"c{cid}:Backups/NAS"
    t3 = c.post("/api/cloudsync/tasks", json={**base, "direction": "both"}).json()
    assert cloudsync.command(t3, resync=True)[:1] == ["bisync"] and "--resync" in cloudsync.command(t3, resync=True)
    assert c.delete(f"/api/cloudsync/connections/{cid}").status_code == 409        # still in use
    assert set(cloudsync.due()) == {t["id"], t2["id"], t3["id"]}
    cloudsync.write_status(t["id"], started=int(time.time()))
    assert t["id"] not in cloudsync.due()
