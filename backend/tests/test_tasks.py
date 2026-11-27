import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from cynthology.tasks import on_calendar  # noqa: E402


def test_calendars():
    assert on_calendar({"every": "daily", "time": "03:30"}) == "*-*-* 03:30:00"
    assert on_calendar({"every": "hourly", "time": "00:15"}) == "*-*-* *:15:00"
    assert on_calendar({"every": "weekly", "time": "22:00", "days": ["sun", "wed"]}) == "Wed,Sun *-*-* 22:00:00"
    assert on_calendar({"every": "monthly", "time": "01:00", "day_of_month": 1}) == "*-*-01 01:00:00"
    assert on_calendar({"every": "boot"}) is None
    for bad in [{"every": "weekly", "time": "1:00", "days": []}, {"every": "monthly", "time": "1:00", "day_of_month": 31},
                {"every": "daily", "time": "25:00"}, {"every": "sometimes", "time": "1:00"}]:
        with pytest.raises(HTTPException):
            on_calendar(bad)
