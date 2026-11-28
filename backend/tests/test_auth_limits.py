"""Sign-in limits: IPv6 counted per /64, account limit only for unknown addresses, no unbounded growth."""
from cynthology import auth


def test_client_key_groups_ipv6_by_64():
    assert auth.client_key("2001:db8:1:2:aaaa::1") == auth.client_key("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert auth.client_key("192.0.2.7") == "192.0.2.7"
    assert auth.client_key("::ffff:192.0.2.7") == "192.0.2.7"


def test_account_key_only_for_unknown_addresses():
    auth._KNOWN.clear()
    assert any(k.startswith("acct:") for k in auth.attempt_keys("alice", "198.51.100.1"))
    auth.attempt_ok("alice", "198.51.100.1", auth.attempt_keys("alice", "198.51.100.1"))
    assert not any(k.startswith("acct:") for k in auth.attempt_keys("alice", "198.51.100.1"))


def test_checking_creates_no_entries():
    auth.FAILS.clear()
    auth._check_lock("u:nobody|203.0.113.1", "ip:203.0.113.1")
    assert auth.FAILS == {}


def test_long_usernames_refused():
    from fastapi.testclient import TestClient
    from cynthology.main import app
    r = TestClient(app).post("/api/auth/login", json={"username": "x" * 5000, "password": "y"})
    assert r.status_code == 422
