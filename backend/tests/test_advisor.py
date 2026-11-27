import os

os.environ["CYN_DEV"] = "1"

from cynthology import advisor, notify  # noqa: E402


def test_scan_shape_and_daily_only_reports_new(tmp_path, monkeypatch):
    monkeypatch.setattr(advisor, "CACHE", str(tmp_path / "adv.json"))
    sent = []
    monkeypatch.setattr(notify, "notify", lambda *a, **k: sent.append(a))
    r = advisor.run_all()
    assert set(r["counts"]) == {"critical", "warning", "info", "ok"}
    assert all({"id", "status", "title", "detail"} <= set(c) for c in r["checks"])
    order = [c["status"] for c in r["checks"]]
    assert order == sorted(order, key=["critical", "warning", "info", "ok"].index)
    advisor.daily()                                   # cache is fresh: nothing happens
    assert sent == []
    import json
    old = json.load(open(advisor.CACHE))
    old["time"] -= 2 * 86400
    old["checks"] = [c for c in old["checks"] if c["id"] != "firewall"]    # pretend the firewall finding is new
    json.dump(old, open(advisor.CACHE, "w"))
    advisor.daily()
    assert len(sent) == 1 and "1 new" in sent[0][1]
