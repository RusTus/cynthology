import datetime
import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from cynthology import hardware as hw  # noqa: E402


def test_hdidle_config():
    assert "START_HD_IDLE=false" in hw.render_hdidle(0, ["sdb"])
    conf = hw.render_hdidle(20, ["sdb", "sdc"])
    assert "START_HD_IDLE=true" in conf and '-i 0 -a sdb -i 1200 -a sdc -i 1200' in conf


def test_next_time_and_units():
    tz = datetime.timezone.utc
    entries = [{"action": "shutdown", "time": "23:30", "days": [0, 1, 2, 3, 4]},
               {"action": "startup", "time": "07:00", "days": [0, 1, 2, 3, 4]},
               {"action": "startup", "time": "09:00", "days": [5, 6], "enabled": False}]
    fri_night = datetime.datetime(2026, 11, 27, 23, 45, tzinfo=tz)      # a Friday, after the shutdown
    assert hw.next_time(entries, "startup", fri_night) == datetime.datetime(2026, 11, 30, 7, 0, tzinfo=tz)   # Monday
    assert hw.next_time(entries, "shutdown", fri_night) == datetime.datetime(2026, 11, 30, 23, 30, tzinfo=tz)
    units = hw.render_shutdown_units(entries)
    assert "OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 23:30:00" in units["cyn-power-off.timer"]
    assert hw.render_shutdown_units([e for e in entries if e["action"] == "startup"]) == {}


def test_magic_packet():
    p = hw.magic_packet("00:11:22:33:44:55")
    assert len(p) == 102 and p[:6] == b"\xff" * 6 and p[6:12] == bytes.fromhex("001122334455")
    with pytest.raises(HTTPException):
        hw.magic_packet("nope")
