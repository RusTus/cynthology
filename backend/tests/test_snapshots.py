import datetime
import os

os.environ["CYN_DEV"] = "1"

from cynthology import snapshots  # noqa: E402

UTC = datetime.timezone.utc


def make(tmp_path, monkeypatch, schedule="daily", keep=3, existing=()):
    share = {"name": "s", "path": str(tmp_path), "snap_schedule": schedule, "snap_keep": keep}
    monkeypatch.setattr(snapshots.shares_mod, "load", lambda: [share])
    for n in existing:
        os.makedirs(tmp_path / ".snapshots" / n)
    return share


def test_first_run_takes_snapshot(tmp_path, monkeypatch):
    make(tmp_path, monkeypatch)
    assert snapshots.run_due() == ["s"]
    assert len(snapshots.list_snaps({"path": str(tmp_path)})) == 1


def test_not_due_yet(tmp_path, monkeypatch):
    now = datetime.datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    make(tmp_path, monkeypatch, existing=["@GMT-2026.11.25-01.00.00"])
    assert snapshots.run_due(now) == []


def test_due_with_slack(tmp_path, monkeypatch):
    # hourly timer fires a little early: 23h57m after the last one still counts as a day
    now = datetime.datetime(2026, 11, 26, 0, 57, tzinfo=UTC)
    make(tmp_path, monkeypatch, existing=["@GMT-2026.11.25-01.00.00"])
    assert snapshots.run_due(now) == ["s"]


def test_retention_prunes_oldest(tmp_path, monkeypatch):
    old = [f"@GMT-2026.11.{d:02d}-01.00.00" for d in range(1, 6)]
    share = make(tmp_path, monkeypatch, schedule="off", keep=2, existing=old)
    assert snapshots.prune(share) == 3
    assert snapshots.list_snaps(share) == old[-2:]


def test_keep_zero_keeps_all(tmp_path, monkeypatch):
    old = [f"@GMT-2026.11.{d:02d}-01.00.00" for d in range(1, 6)]
    share = make(tmp_path, monkeypatch, keep=0, existing=old)
    assert snapshots.prune(share) == 0


def test_ignores_foreign_dirs(tmp_path, monkeypatch):
    share = make(tmp_path, monkeypatch, existing=["@GMT-2026.11.01-01.00.00", "manual-backup"])
    assert snapshots.list_snaps(share) == ["@GMT-2026.11.01-01.00.00"]
