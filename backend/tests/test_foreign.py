import os

os.environ["CYN_DEV"] = "1"

from cynthology import storage  # noqa: E402


def a(level, devices=4):
    return {"level": level, "devices": devices}


def test_guess_raid_type():
    assert storage._guess_raid([a("5"), a("5")]) == "shr"
    assert storage._guess_raid([a("6"), a("6")]) == "shr2"
    assert storage._guess_raid([a("1", 1)]) == "basic"
    assert storage._guess_raid([a("1", 2)]) == "raid1"
    assert storage._guess_raid([a("10")]) == "raid10"


def test_tolerance():
    assert storage._tolerance(a("1", 3)) == 2
    assert storage._tolerance(a("5")) == 1 and storage._tolerance(a("6")) == 2 and storage._tolerance(a("0")) == 0


def test_vg_names():
    assert storage.VG_RE.match("vg_cyn12").group(1) == "12"
    assert not storage.VG_RE.match("vg1") and not storage.VG_RE.match("vg_cyn1x")
