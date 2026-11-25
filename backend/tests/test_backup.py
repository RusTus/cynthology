import datetime
import os

os.environ["CYN_DEV"] = "1"

from cynthology import backup  # noqa: E402

MON_0300 = datetime.datetime(2026, 11, 23, 3, 0)   # a Monday


def task(days=(0, 1, 2, 3, 4, 5, 6), at="02:00", enabled=True):
    return {"id": "t1", "schedule": {"enabled": enabled, "time": at, "days": list(days)}}


def patch(monkeypatch, t, last_started=None):
    monkeypatch.setattr(backup, "load_tasks", lambda: [t])
    monkeypatch.setattr(backup, "read_status", lambda tid: {"started": last_started} if last_started else {"state": "never"})


def test_due_after_scheduled_time(monkeypatch):
    patch(monkeypatch, task())
    assert backup.due(MON_0300) == ["t1"]


def test_not_due_before_time(monkeypatch):
    patch(monkeypatch, task(at="04:00"))
    assert backup.due(MON_0300) == []


def test_not_due_twice_same_day(monkeypatch):
    patch(monkeypatch, task(), last_started=MON_0300.replace(hour=2, minute=1).timestamp())
    assert backup.due(MON_0300) == []


def test_due_again_next_day(monkeypatch):
    patch(monkeypatch, task(), last_started=(MON_0300 - datetime.timedelta(days=1)).timestamp())
    assert backup.due(MON_0300) == ["t1"]


def test_weekday_filter(monkeypatch):
    patch(monkeypatch, task(days=[5, 6]))     # weekends only
    assert backup.due(MON_0300) == []


def test_disabled(monkeypatch):
    patch(monkeypatch, task(enabled=False))
    assert backup.due(MON_0300) == []


def test_missed_by_more_than_12h_waits_for_next(monkeypatch):
    # NAS was off all day: don't start a 02:00 backup at 15:00, wait for the next slot
    patch(monkeypatch, task())
    assert backup.due(MON_0300.replace(hour=15)) == []


def test_next_run():
    assert backup.next_run(task(days=[2]), MON_0300) == datetime.datetime(2026, 11, 25, 2, 0)   # Wednesday
    assert backup.next_run(task(enabled=False), MON_0300) is None


def test_sftp_path_relative_to_home():
    t = {"id": "x", "dest": {"type": "sftp", "host": "nas2", "user": "bk", "port": 22, "path": "backups/a"}}
    repo, env = backup.repo_and_env(t)
    assert repo == "sftp:bk@nas2:backups/a"
    t["dest"]["path"] = "/srv/backups"
    assert backup.repo_and_env(t)[0] == "sftp:bk@nas2:/srv/backups"


def test_s3_repo_string():
    t = {"id": "x", "dest": {"type": "s3", "endpoint": "https://s3.eu.wasabisys.com/", "bucket": "b", "path": "nas",
                             "access_key": "A", "secret_key": "S"}}
    repo, env = backup.repo_and_env(t)
    assert repo == "s3:https://s3.eu.wasabisys.com/b/nas"
    assert env["AWS_ACCESS_KEY_ID"] == "A" and env["AWS_SECRET_ACCESS_KEY"] == "S"
