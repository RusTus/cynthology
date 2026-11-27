import os
import time

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cynthology import auth, passkeys  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(passkeys, "FILE", str(tmp_path / "pk.json"))
    monkeypatch.setattr(auth, "SESSIONS_FILE", str(tmp_path / "s.json"))
    passkeys.PENDING.clear()
    auth.FAILS.clear()
    app = FastAPI()
    app.include_router(passkeys.router)
    app.include_router(passkeys.public)
    app.dependency_overrides[auth.current_user] = lambda: {"user": "admin", "admin": True}
    return app


def client(app, base):
    return TestClient(app, base_url=base)


@pytest.mark.parametrize("base", ["https://192.168.1.20:5001", "http://nas.example.com:5000", "https://[fd00::1]:5001"])
def test_refused_without_name_or_https(env, base):
    r = client(env, base).post("/api/passkeys/register/begin")
    assert r.status_code == 400


def test_register_begin_options(env):
    c = client(env, "https://nas.example.com:5001")
    r = c.post("/api/passkeys/register/begin").json()
    pk = r["options"]["publicKey"]
    assert pk["rp"]["id"] == "nas.example.com" and pk["user"]["name"] == "admin"
    assert pk["authenticatorSelection"]["userVerification"] == "required"
    assert c.get("/api/passkeys").json() == {"host": "nas.example.com", "usable_here": True, "keys": []}
    # localhost over plain HTTP is a secure context too
    assert client(env, "http://localhost:5000").post("/api/passkeys/register/begin").status_code == 200


def test_expired_or_reused_token(env):
    c = client(env, "https://nas.example.com")
    tok = c.post("/api/passkeys/register/begin").json()["token"]
    passkeys.PENDING[tok]["exp"] = time.time() - 1
    assert c.post("/api/passkeys/register/complete", json={"token": tok, "credential": {}}).status_code == 400
    assert c.post("/api/passkeys/register/complete", json={"token": tok, "credential": {}}).status_code == 400


def test_unknown_passkey_refused(env):
    c = client(env, "https://nas.example.com")
    b = c.post("/api/auth/passkey/begin", json={}).json()
    assert b["options"]["publicKey"]["rpId"] == "nas.example.com" and b["options"]["publicKey"]["allowCredentials"] == []
    r = c.post("/api/auth/passkey/complete", json={"token": b["token"], "credential": {"id": "nope", "response": {}}})
    assert r.status_code == 401
    assert len(auth.FAILS["ip:testclient"]) == 1


def test_ticket_needs_a_key_for_this_address(env):
    auth.PENDING["t1"] = {"user": "admin", "exp": time.time() + 60, "keys": [], "ip": "x"}
    passkeys.save({"admin": {"handle": "AAAA", "keys": [{"id": "k", "data": "", "name": "x", "rp_id": "other.example.com", "created": 1}]}})
    r = client(env, "https://nas.example.com").post("/api/auth/passkey/begin", json={"ticket": "t1"})
    assert r.status_code == 400 and "code from your app" in r.text
    assert passkeys.has("ADMIN")
    passkeys.remove_user("admin")
    assert not passkeys.has("admin")
