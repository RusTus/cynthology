import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cynthology import auth, fileservices, privileges, shares  # noqa: E402

GROUPS = {"alice": {"users", "sales"}, "bob": {"users"}, "boss": {"users", "sales", "cynthology-admin"}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(privileges, "FILE", str(tmp_path / "p.json"))
    monkeypatch.setattr(privileges, "_groups_of", lambda u: GROUPS.get(u, {"users"}))
    monkeypatch.setattr(privileges, "_is_admin", lambda u: u == "boss")
    monkeypatch.setattr(privileges, "local_people", lambda: ["alice", "bob"])
    app = FastAPI()
    app.include_router(privileges.router)
    app.dependency_overrides[auth.require_admin] = lambda: {"user": "boss", "admin": True}
    return TestClient(app)


def test_default_everyone(env):
    assert privileges.allowed("files", "alice", "8.8.8.8") and privileges.denied_people("smb") == []


def test_allow_only_group(env):
    assert env.put("/api/privileges/smb", json={"mode": "allow", "groups": ["sales"]}).status_code == 200
    assert privileges.allowed("smb", "alice") and not privileges.allowed("smb", "bob")
    assert privileges.allowed("smb", "boss")                       # administrators always
    assert privileges.smb_invalid_users() == ["bob"]
    assert "invalid users = bob" in shares.smb_block({"name": "x", "path": "/v/x", "read_users": ["@users"], "write_users": []})


def test_deny_group_spares_admins(env):
    env.put("/api/privileges/sftp", json={"mode": "deny", "groups": ["sales"]})
    assert privileges.denied_people("sftp") == ["alice"]
    conf = fileservices.render_sshd(True, "/volume1", privileges.denied_people("sftp"), ["192.168.1.0/24"])
    assert "DenyUsers alice" in conf and "Address *,!192.168.1.0/24" in conf and "boss" not in conf


def test_networks(env):
    r = env.put("/api/privileges/files", json={"mode": "all", "networks": ["192.168.1.0/24", "10.0.0.5"]})
    assert r.json()["networks"] == ["192.168.1.0/24", "10.0.0.5/32"]
    assert privileges.allowed("files", "alice", "192.168.1.20") and privileges.allowed("files", "alice", "::ffff:10.0.0.5")
    assert not privileges.allowed("files", "alice", "8.8.8.8") and not privileges.allowed("files", "alice", "127.0.0.1")
    assert privileges.allowed("files", "boss", "8.8.8.8")
    assert shares.smb_block({"name": "x", "path": "/v/x", "read_users": [], "write_users": []}).count("hosts allow") == 0


@pytest.mark.parametrize("body", [{"mode": "maybe"}, {"users": ["a b"]}, {"groups": ["x;y"]}, {"networks": ["not-a-net"]}])
def test_bad_rules(env, body):
    assert env.put("/api/privileges/smb", json=body).status_code == 400


def test_require_app_dependency(env):
    from fastapi import Depends
    app = FastAPI()

    @app.get("/x")
    def x(user=Depends(privileges.require_app("files"))):
        return user
    env.put("/api/privileges/files", json={"mode": "deny", "users": ["alice"]})
    app.dependency_overrides[auth.current_user] = lambda: {"user": "alice", "admin": False}
    r = TestClient(app).get("/x")
    assert r.status_code == 403 and "File Station" in r.text
    app.dependency_overrides[auth.current_user] = lambda: {"user": "bob", "admin": False}
    assert TestClient(app).get("/x").status_code == 200
