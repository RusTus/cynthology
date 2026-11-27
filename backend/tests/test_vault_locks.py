import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from cynthology import backup, snapshots, vault  # noqa: E402


def test_snapshot_lock_blocks_delete_and_prune(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "LOCKS", str(tmp_path / "locks.json"))
    monkeypatch.setattr(snapshots, "supported", lambda s: True)
    share = {"name": "docs", "path": str(tmp_path / "docs"), "snap_lock_days": 7, "snap_keep": 1}
    os.makedirs(share["path"])
    import time
    a = snapshots.take(share)
    time.sleep(1.1)
    b = snapshots.take(share)
    assert snapshots.lock_until(share, a)
    with pytest.raises(HTTPException) as e:
        snapshots.delete(share, a)
    assert e.value.status_code == 423
    assert snapshots.prune(share) == 0 and set(snapshots.list_snaps(share)) == {a, b}   # locked ones survive retention
    share["snap_lock_days"] = 0
    time.sleep(1.1)
    c = snapshots.take(share)
    assert snapshots.lock_until(share, c) is None
    assert snapshots.lock_until(share, a)                # (turning the setting off doesn't unlock old ones)


def test_vault_env_and_destination():
    env = vault.render_env({"port": 6281, "append_only": True}, "/volume1/@vault")
    assert "BACKUP_DIR=/volume1/@vault" in env and "--append-only" in env and "--private-repos" in env and "--tls" in env
    assert "--append-only" not in vault.render_env({"port": 6281, "append_only": False}, "/x")
    repo, e = backup.repo_and_env({"id": "t1", "dest": {"type": "vault", "host": "192.168.1.60", "port": 6281,
                                                         "user": "home-nas", "secret_key": "pw", "path": "photos"}})
    assert repo == "rest:https://192.168.1.60:6281/home-nas/photos"
    assert e["RESTIC_REST_USERNAME"] == "home-nas" and e["RESTIC_REST_PASSWORD"] == "pw" and "pw" not in repo
    with pytest.raises(RuntimeError):
        backup.repo_and_env({"id": "t1", "dest": {"type": "vault", "host": "x -o y", "port": 6281, "user": "a"}})
