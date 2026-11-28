import os
import time

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cynthology import auth, files, shares, versions  # noqa: E402

S1, S2, S3 = "@GMT-2026.11.01-10.00.00", "@GMT-2026.11.02-10.00.00", "@GMT-2026.11.03-10.00.00"


@pytest.fixture
def env(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "sub" / "a.txt").write_text("v3 current")
    for snap, text, mt in ((S1, "v1", 1000), (S2, "v1", 1000), (S3, "v2!", 2000)):     # S1 and S2 hold the same version
        d = docs / ".snapshots" / snap / "sub"
        d.mkdir(parents=True)
        (d / "a.txt").write_text(text)
        os.utime(d / "a.txt", (mt, mt))
    share = {"name": "docs", "path": str(docs), "read_users": ["bob"], "write_users": ["alice"], "recycle_bin": False}
    monkeypatch.setattr(shares, "load", lambda: [share])
    monkeypatch.setattr(files, "_matches", lambda u, lst: u in lst)
    app = FastAPI()
    app.include_router(versions.router)
    who = {"user": "alice", "admin": False}
    app.dependency_overrides[auth.current_user] = lambda: who
    return TestClient(app), docs, who


def test_lists_distinct_versions(env):
    c, docs, _ = env
    v = c.get("/api/versions/snapshots", params={"path": "/docs/sub/a.txt"}).json()
    assert [x["id"] for x in v["versions"]] == [S3, S2] and v["can_restore"]           # S1 = same as S2
    assert v["versions"][0]["download"] == f"/docs/.snapshots/{S3}/sub/a.txt"
    assert c.get("/api/versions/backups", params={"path": "/docs/sub/a.txt"}).json()["versions"] == []   # (admins only)


def test_restore_copy_and_replace(env):
    c, docs, _ = env
    r = c.post("/api/versions/restore", json={"path": "/docs/sub/a.txt", "source": "snapshot", "id": S2, "mode": "copy"}).json()
    assert r["name"].startswith("a (restored 2026-11-02") and r["name"].endswith(".txt")
    assert (docs / "sub" / r["name"]).read_text() == "v1" and (docs / "sub" / "a.txt").read_text() == "v3 current"
    c.post("/api/versions/restore", json={"path": "/docs/sub/a.txt", "source": "snapshot", "id": S3, "mode": "replace"})
    assert (docs / "sub" / "a.txt").read_text() == "v2!"
    assert not [n for n in os.listdir(docs / "sub") if n.startswith(".restoring")]


def test_read_only_user_cannot_restore(env):
    c, docs, who = env
    who["user"] = "bob"
    v = c.get("/api/versions/snapshots", params={"path": "/docs/sub/a.txt"}).json()
    assert v["versions"] and not v["can_restore"]
    assert c.post("/api/versions/restore", json={"path": "/docs/sub/a.txt", "source": "snapshot", "id": S3, "mode": "copy"}).status_code == 403
    who["user"] = "alice"
    assert c.post("/api/versions/restore", json={"path": "/docs/sub/a.txt", "source": "snapshot", "id": "../../etc", "mode": "copy"}).status_code == 400
    assert c.post("/api/versions/restore", json={"path": "/docs/sub/a.txt", "source": "backup", "id": "abcdef12", "task": "x"}).status_code == 403
