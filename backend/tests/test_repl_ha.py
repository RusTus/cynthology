import os
import time

os.environ["CYN_DEV"] = "1"

from cynthology import repl  # noqa: E402


def test_watch_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(repl, "WATCH", str(tmp_path / "w.json"))
    old = "@GMT-2026.11.25-03.00.00"
    monkeypatch.setattr(repl, "received", lambda _: [{"source": "nas1", "share": "docs", "latest": old}])
    repl.put_watch("nas1", repl.Watch(max_age_h=26, address="192.0.2.10", port=5001), None)
    monkeypatch.setattr(repl, "DEV", False)
    monkeypatch.setattr(repl, "_reachable", lambda h, p: False)
    now = time.time()
    first = repl.health_issues(now)
    assert "repl-nas1-stale" in first and "repl-nas1-down" not in first           # (one failed check isn't "down")
    later = repl.health_issues(now + 20 * 60)
    assert later["repl-nas1-down"]["level"] == "critical" and "same name" in later["repl-nas1-down"]["detail"]
    monkeypatch.setattr(repl, "_reachable", lambda h, p: True)
    assert "repl-nas1-down" not in repl.health_issues(now + 30 * 60)
    assert repl._watch()["nas1"]["down_since"] is None


def test_replace_live_keeps_old(tmp_path, monkeypatch):
    live = tmp_path / "docs"
    live.mkdir()
    (live / "old.txt").write_text("old")
    replica = tmp_path / "replica"
    replica.mkdir()
    (replica / "new.txt").write_text("from primary")
    monkeypatch.setattr(repl.shares_mod, "load", lambda: [{"name": "docs", "path": str(live)}])
    monkeypatch.setattr(repl.shares_mod, "write_configs", lambda s: None)
    import cynthology.notify as nt
    monkeypatch.setattr(nt, "notify", lambda *a, **k: None)
    r = repl._replace_live({"name": "docs", "path": str(live)}, str(replica), repl.Failover(source="nas1", share="docs", snapshot="x", new_name="docs", replace=True))
    assert (live / "new.txt").read_text() == "from primary" and not (live / "old.txt").exists()
    assert os.path.exists(os.path.join(r["kept"], "old.txt"))
