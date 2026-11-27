"""Resource Monitor history: CPU, memory, network, disk I/O, load and temperature over time,
plus how full each volume is.

A sampler thread in the web service takes a sample every 10 s and keeps it in memory; once a
minute the batch goes into SQLite (<state>/metrics.db), so the disk sees one small write a
minute. Three resolutions are kept, like DSM's Resource Monitor history:
  - 10-second samples for 24 hours
  - 5-minute averages for 31 days
  - 1-hour averages for 400 days   (+ volume usage, hourly)
"""
import os
import sqlite3
import threading
import time

import psutil
from fastapi import APIRouter, Depends, HTTPException

from .auth import current_user
from .util import DEV, STATE_DIR

router = APIRouter(prefix="/api/system/history", tags=["metrics"])
DB = os.path.join(STATE_DIR, "metrics.db")
FIELDS = ("cpu", "mem", "rx", "tx", "rd", "wr", "load", "temp")
STEP = 10                                     # seconds between samples
TIERS = {0: (STEP, 24 * 3600), 1: (300, 31 * 86400), 2: (3600, 400 * 86400)}   # tier: (bucket s, keep s)
RANGES = {"1h": (3600, 0), "6h": (6 * 3600, 0), "24h": (86400, 0), "7d": (7 * 86400, 1),
          "30d": (30 * 86400, 1), "1y": (365 * 86400, 2)}
MAX_POINTS = 480
_lock = threading.Lock()
_started = False


def connect(path: str = DB) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=10)
    db.execute(f"CREATE TABLE IF NOT EXISTS samples (tier INTEGER, ts INTEGER, {', '.join(f + ' REAL' for f in FIELDS)},"
               " PRIMARY KEY (tier, ts))")
    db.execute("CREATE TABLE IF NOT EXISTS volumes (ts INTEGER, pool TEXT, used INTEGER, size INTEGER, PRIMARY KEY (ts, pool))")
    return db


def _temp() -> float | None:
    try:
        vals = [e.current for entries in (psutil.sensors_temperatures() or {}).values() for e in entries if e.current]
        return max(vals) if vals else None
    except (AttributeError, OSError):
        return None


class Sampler:
    def __init__(self):
        self.last = None
        self.buf: list[tuple] = []

    def sample(self, now: float) -> tuple | None:
        net, disk = psutil.net_io_counters(), psutil.disk_io_counters()
        cur = (now, net.bytes_recv, net.bytes_sent, disk.read_bytes if disk else 0, disk.write_bytes if disk else 0)
        row = None
        if self.last:
            dt = max(1e-3, now - self.last[0])
            rates = [max(0.0, (c - p) / dt) for c, p in zip(cur[1:], self.last[1:])]   # (counters reset: never negative)
            row = (int(now // STEP * STEP), psutil.cpu_percent(interval=None), psutil.virtual_memory().percent,
                   *rates, os.getloadavg()[0], _temp())
        else:
            psutil.cpu_percent(interval=None)                                      # (first call only primes it)
        self.last = cur
        return row


def rollup(db: sqlite3.Connection, now: int) -> None:
    """Average finished buckets of the finer tier into the coarser one, then drop old rows."""
    for tier in (1, 2):
        size = TIERS[tier][0]
        src = tier - 1
        last = db.execute("SELECT MAX(ts) FROM samples WHERE tier=?", (tier,)).fetchone()[0]
        start = (last + size) if last is not None else \
            (db.execute("SELECT MIN(ts) FROM samples WHERE tier=?", (src,)).fetchone()[0] or now) // size * size
        end = now // size * size                                                    # only complete buckets
        if start >= end:
            continue
        avgs = ", ".join(f"AVG({f})" for f in FIELDS)
        db.execute(f"INSERT OR REPLACE INTO samples SELECT ?, ts / ? * ?, {avgs} FROM samples "
                   "WHERE tier=? AND ts >= ? AND ts < ? GROUP BY ts / ?",
                   (tier, size, size, src, start, end, size))
    for tier, (_, keep) in TIERS.items():
        db.execute("DELETE FROM samples WHERE tier=? AND ts < ?", (tier, now - keep))
    db.execute("DELETE FROM volumes WHERE ts < ?", (now - TIERS[2][1],))


def record_volumes(db: sqlite3.Connection, now: int) -> None:
    from .storage import load_pools
    ts = now // 3600 * 3600
    for p in load_pools():
        try:
            v = os.statvfs(p["mount"])
        except OSError:
            continue
        size = v.f_blocks * v.f_frsize
        db.execute("INSERT OR REPLACE INTO volumes VALUES (?, ?, ?, ?)", (ts, p.get("name") or p["mount"], size - v.f_bfree * v.f_frsize, size))


def _loop() -> None:
    s = Sampler()
    last_flush = last_vol = 0.0
    while True:
        now = time.time()
        try:
            row = s.sample(now)
            if row:
                s.buf.append(row)
            if now - last_flush >= 60 and s.buf:
                with _lock:
                    db = connect()
                    with db:
                        db.executemany(f"INSERT OR REPLACE INTO samples VALUES (0, {', '.join('?' * (len(FIELDS) + 1))})", s.buf)
                        rollup(db, int(now))
                        if now - last_vol >= 3600:
                            record_volumes(db, int(now))
                            last_vol = now
                    db.close()
                s.buf.clear()
                last_flush = now
        except Exception as e:  # (monitoring must never take the web interface down)
            print("metrics:", e)
            s.buf.clear()
        time.sleep(STEP - (time.time() % STEP))


def start() -> None:
    global _started
    if _started or os.environ.get("CYN_NO_METRICS"):
        return
    _started = True
    threading.Thread(target=_loop, name="metrics", daemon=True).start()


def query(db: sqlite3.Connection, rng: str, now: int) -> dict:
    span, tier = RANGES[rng]
    size = TIERS[tier][0]
    # bucket so that at most MAX_POINTS come back; empty buckets stay gaps (None), so the chart
    # shows when the NAS was off instead of drawing a line across it
    bucket = -(-span // (MAX_POINTS * size)) * size          # (a multiple of the tier's step)
    start = (now - span) // bucket * bucket
    rows = db.execute(f"SELECT ts / ? * ? AS b, {', '.join(f'AVG({f})' for f in FIELDS)} FROM samples "
                      "WHERE tier=? AND ts >= ? GROUP BY b ORDER BY b", (bucket, bucket, tier, start)).fetchall()
    have = {r[0]: r[1:] for r in rows}
    ts = list(range(start, now // bucket * bucket + 1, bucket))
    out = {"t": ts, "step": bucket}
    for i, f in enumerate(FIELDS):
        out[f] = [None if b not in have or have[b][i] is None else round(have[b][i], 2) for b in ts]
    return out


@router.get("")
def history(range: str = "24h", _=Depends(current_user)):
    if range not in RANGES:
        raise HTTPException(400, "range: " + ", ".join(RANGES))
    with _lock:
        db = connect()
        try:
            return query(db, range, int(time.time()))
        finally:
            db.close()


@router.get("/volumes")
def volume_history(range: str = "30d", _=Depends(current_user)):
    span = {"7d": 7 * 86400, "30d": 30 * 86400, "1y": 365 * 86400}.get(range)
    if not span:
        raise HTTPException(400, "range: 7d, 30d or 1y")
    with _lock:
        db = connect()
        try:
            rows = db.execute("SELECT ts, pool, used, size FROM volumes WHERE ts >= ? ORDER BY ts", (int(time.time()) - span,)).fetchall()
        finally:
            db.close()
    vols: dict[str, dict] = {}
    for ts, pool, used, size in rows:
        v = vols.setdefault(pool, {"name": pool, "t": [], "used": [], "size": size})
        v["t"].append(ts)
        v["used"].append(used)
        v["size"] = size
    return list(vols.values())
