import datetime
import os

os.environ["CYN_DEV"] = "1"

from cynthology import settings, update  # noqa: E402


def test_window_and_once(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(update, "AUTO_STATE", str(tmp_path / "a.json"))
    assert not update.auto_due(datetime.datetime(2026, 11, 28, 4, 0))          # off by default
    settings.update(auto_update={"enabled": True, "frequency": "daily", "time": "03:30"})
    night = datetime.datetime(2026, 11, 28, 3, 45)
    assert update.auto_due(night) and not update.auto_due(night.replace(hour=2)) and not update.auto_due(night.replace(hour=11))
    update._save_auto_state(done_for=int(night.timestamp()))
    assert not update.auto_due(night + datetime.timedelta(hours=1)) and update.auto_due(night + datetime.timedelta(days=1))
    settings.update(auto_update={"enabled": True, "frequency": "weekly", "day": 6, "time": "03:30"})
    assert update.auto_due(datetime.datetime(2026, 11, 29, 4, 0)) and not update.auto_due(datetime.datetime(2026, 11, 30, 4, 0))


def test_busy_waits(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(update, "AUTO_STATE", str(tmp_path / "a.json"))
    settings.update(auto_update={"enabled": True, "time": "00:00"})
    monkeypatch.setattr(update, "auto_due", lambda now=None: True)
    monkeypatch.setattr(update, "busy", lambda: "busy: cyn-backup-x.service")
    called = []
    monkeypatch.setattr(update, "start", lambda _: called.append(1))
    update.auto_run()
    assert not called and "waiting" in update._auto_state()["last_result"] and not update._auto_state().get("done_for")
