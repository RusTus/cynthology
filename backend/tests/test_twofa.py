import base64
import os
import time

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402

from cynthology import auth, settings, twofa  # noqa: E402


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(twofa, "FILE", str(tmp_path / "2fa.json"))
    monkeypatch.setattr(settings, "FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(auth, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    auth.FAILS.clear(); auth.PENDING.clear(); auth.SESSIONS.clear()


def test_rfc6238_vector():
    # RFC 6238 appendix B, SHA1 seed "12345678901234567890", T=59s -> 94287082 (8 digits) -> 287082 (6)
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert twofa.totp(secret, 59 // 30) == "287082"
    assert twofa.totp(secret, 1111111109 // 30) == "081804"


def enroll(user="admin"):
    secret = twofa.new_secret()
    twofa._save({user: {"pending": secret}})
    data = twofa._load()
    step = twofa.match_step(secret, twofa.totp(secret, int(time.time() // 30)))
    data[user].update(secret=data[user].pop("pending"), enabled=True, last_step=step - 1)
    codes = twofa._new_backup_codes(data[user])
    twofa._save(data)
    return secret, codes


def test_code_cannot_be_replayed():
    secret, _ = enroll()
    code = twofa.totp(secret, int(time.time() // 30))
    assert twofa.verify("admin", code)
    assert not twofa.verify("admin", code)


def test_clock_drift_window():
    secret, _ = enroll()
    now = time.time()
    assert twofa.match_step(secret, twofa.totp(secret, int(now // 30) - 1), now) is not None
    assert twofa.match_step(secret, twofa.totp(secret, int(now // 30) - 3), now) is None


def test_backup_codes_single_use():
    _, codes = enroll()
    assert len(codes) == 10
    assert twofa.verify("admin", codes[0])
    assert not twofa.verify("admin", codes[0])
    assert twofa.verify("admin", codes[1].upper().replace("-", ""))   # forgiving format


def client():
    from fastapi.testclient import TestClient
    from cynthology.main import app
    return TestClient(app)


def login(c, pw="admin"):
    return c.post("/api/auth/login", json={"username": "admin", "password": pw})


def test_login_requires_code_when_enabled():
    secret, _ = enroll()
    c = client()
    r = login(c).json()
    assert r["need_otp"] and "token" not in r
    assert c.post("/api/auth/otp", json={"ticket": r["ticket"], "code": "000000"}).status_code == 401
    code = twofa.totp(secret, int(time.time() // 30))
    r2 = c.post("/api/auth/otp", json={"ticket": r["ticket"], "code": code})
    assert r2.status_code == 200 and r2.json()["token"]


def test_lockout_after_five_failures():
    c = client()
    for _ in range(5):
        assert login(c, "wrong").status_code == 401
    assert login(c, "admin").status_code == 429        # even the right password is blocked now


def test_admin_policy_forces_enrollment():
    settings.update(enforce_2fa_admins=True)
    c = client()
    r = login(c).json()
    assert r["enroll_required"]
    h = {"Authorization": f"Bearer {r['token']}"}
    assert c.get("/api/storage/pools", headers=h).status_code == 403    # blocked until set up
    s = c.post("/api/2fa/setup", headers=h).json()
    assert s["uri"].startswith("otpauth://totp/") and "<svg" in s["qr_svg"]
    code = twofa.totp(s["secret"], int(time.time() // 30))
    e = c.post("/api/2fa/enable", headers=h, json={"code": code}).json()
    assert len(e["backup_codes"]) == 10
    assert c.get("/api/storage/pools", headers=h).status_code == 200    # unlocked
    assert c.post("/api/2fa/disable", headers=h, json={"code": e["backup_codes"][0]}).status_code == 403


def test_cannot_enforce_policy_without_own_2fa():
    c = client()
    h = {"Authorization": f"Bearer {login(c).json()['token']}"}
    assert c.put("/api/2fa/policy", headers=h, json={"enforce_admins": True}).status_code == 400
