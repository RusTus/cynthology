import os
import time

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cynthology import auth, files, sharelinks, shares  # noqa: E402

USER = {"user": "alice", "admin": False}


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    (root / "trip").mkdir(parents=True)
    (root / "trip" / "a.jpg").write_bytes(b"JPEG")
    (root / "page.html").write_text("<script>alert(1)</script>")
    os.symlink("/etc", root / "trip" / "etc")
    share = {"name": "docs", "path": str(root), "read_users": ["alice"], "write_users": []}
    monkeypatch.setattr(shares, "load", lambda: [share])
    monkeypatch.setattr(sharelinks, "CFG", str(tmp_path / "links.json"))
    monkeypatch.setattr(sharelinks, "KEY_FILE", str(tmp_path / "links.key"))
    monkeypatch.setattr(auth, "_is_admin", lambda u: False)
    monkeypatch.setattr(files, "_matches", lambda u, lst: u in lst)
    sharelinks._fails.clear()
    app = FastAPI()
    app.include_router(sharelinks.router)
    app.include_router(sharelinks.public)
    app.dependency_overrides[auth.current_user] = lambda: USER
    return TestClient(app), share


def test_folder_link_with_password(env):
    c, share = env
    link = c.post("/api/sharelinks", json={"path": "/docs/trip", "password": "s3cret", "max_downloads": 2}).json()
    assert link["has_password"] and "password" not in link
    info = c.get(f"/s/{link['id']}/info").json()
    assert info["needs_password"] and not info["unlocked"] and "items" not in info
    assert c.get(f"/s/{link['id']}/dl?p=a.jpg").status_code == 403
    assert c.post(f"/s/{link['id']}/unlock", json={"password": "nope"}).status_code == 403
    assert c.post(f"/s/{link['id']}/unlock", json={"password": "s3cret"}).status_code == 200
    info = c.get(f"/s/{link['id']}/info").json()
    assert [i["name"] for i in info["items"]] == ["a.jpg"]             # the symlink is not listed
    assert c.get(f"/s/{link['id']}/dl?p=../page.html").status_code == 404
    assert c.get(f"/s/{link['id']}/dl?p=etc/hostname").status_code == 404
    r = c.get(f"/s/{link['id']}/dl?p=a.jpg")
    assert r.content == b"JPEG" and r.headers["content-disposition"].startswith("attachment")
    assert c.get(f"/s/{link['id']}/dl").headers["content-type"] == "application/zip"
    assert c.get(f"/s/{link['id']}/dl?p=a.jpg").status_code == 410       # limit of 2 reached


def test_html_is_never_inline_and_access_loss_kills_link(env):
    c, share = env
    link = c.post("/api/sharelinks", json={"path": "/docs/page.html"}).json()
    r = c.get(f"/s/{link['id']}/dl?inline=1")
    assert r.headers["content-disposition"].startswith("attachment") and "sandbox" in r.headers["content-security-policy"]
    share["read_users"] = []                                            # alice loses access
    assert c.get(f"/s/{link['id']}/info").status_code == 404


def test_expiry_and_owner_only(env, monkeypatch):
    c, share = env
    link = c.post("/api/sharelinks", json={"path": "/docs/trip", "expires_days": 1}).json()
    items = sharelinks.load()
    items[0]["expires"] = int(time.time()) - 5
    sharelinks.save(items)
    assert c.get(f"/s/{link['id']}/info").status_code == 404
    monkeypatch.setitem(USER, "user", "bob")
    assert c.get("/api/sharelinks").json() == [] and c.delete(f"/api/sharelinks/{link['id']}").status_code == 404
    assert c.post("/api/sharelinks", json={"path": "/docs/trip"}).status_code == 403   # bob can't read docs
