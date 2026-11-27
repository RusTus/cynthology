import os

os.environ["CYN_DEV"] = "1"

from cynthology import metrics  # noqa: E402


def _fill(db, start, end, cpu=lambda ts: 50.0):
    rows = [(0, ts, cpu(ts), 40.0, 1000.0, 500.0, 0.0, 0.0, 0.5, None) for ts in range(start, end, 10)]
    db.executemany("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def test_rollup_and_query(tmp_path):
    db = metrics.connect(str(tmp_path / "m.db"))
    now = 1_700_000_000 // 3600 * 3600 + 3 * 3600
    _fill(db, now - 3 * 3600, now - 3600, cpu=lambda ts: 20.0)     # 2 h of data, then 1 h "switched off"
    metrics.rollup(db, now)
    t1 = db.execute("SELECT COUNT(*), AVG(cpu) FROM samples WHERE tier=1").fetchone()
    assert t1 == (24, 20.0)                                            # 2 h of 5-minute buckets
    assert db.execute("SELECT COUNT(*) FROM samples WHERE tier=2").fetchone()[0] == 2
    metrics.rollup(db, now)                                            # idempotent
    assert db.execute("SELECT COUNT(*) FROM samples WHERE tier=1").fetchone()[0] == 24
    q = metrics.query(db, "6h", now)
    assert len(q["t"]) <= metrics.MAX_POINTS + 1 and q["step"] % 10 == 0
    assert q["cpu"][-1] is None and 20.0 in q["cpu"]                   # the off hour is a gap, not a line


def test_old_rows_dropped(tmp_path):
    db = metrics.connect(str(tmp_path / "m.db"))
    now = 1_700_000_000 // 300 * 300
    _fill(db, now - 2 * 86400, now - 2 * 86400 + 600)
    metrics.rollup(db, now)
    assert db.execute("SELECT COUNT(*) FROM samples WHERE tier=0").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM samples WHERE tier=1").fetchone()[0] == 2   # averages are kept longer


def test_sampler_rates():
    s = metrics.Sampler()
    assert s.sample(1000.0) is None
    row = s.sample(1010.0)
    assert row[0] == 1010 and len(row) == len(metrics.FIELDS) + 1 and all(v >= 0 for v in row[3:7])
