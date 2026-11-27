import os

os.environ["CYN_DEV"] = "1"

from cynthology.shares import smb_block  # noqa: E402

BASE = {"name": "family", "path": "/volume1/family", "smb": True, "read_users": [], "write_users": ["@users"], "recycle_bin": True}


def test_every_share_gets_apple_extensions_and_snapshots():
    b = smb_block(BASE)
    assert "vfs objects = catia fruit streams_xattr btrfs shadow_copy2 recycle" in b
    assert "fruit:metadata = stream" in b and "shadow:snapdir = .snapshots" in b
    assert "time machine" not in b


def test_time_machine_share():
    b = smb_block({**BASE, "name": "TimeMachine", "recycle_bin": False, "time_machine": True, "tm_max_gb": 500})
    assert "fruit:time machine = yes" in b and "fruit:time machine max size = 500G" in b
    assert "durable handles = yes" in b and "posix locking = no" in b
    assert "recycle" not in b


def test_unlimited_time_machine_has_no_max():
    assert "max size" not in smb_block({**BASE, "time_machine": True, "tm_max_gb": 0})


def test_usb_exfat_uses_streams_depot_and_no_snapshots():
    b = smb_block({**BASE, "usb": True, "fstype": "exfat", "recycle_bin": False})
    assert "vfs objects = catia fruit streams_depot" in b
    assert "btrfs" not in b and "shadow:" not in b


def test_encrypted_share_has_no_snapshot_modules():
    b = smb_block({**BASE, "encrypted": True})
    assert "shadow_copy2" not in b and "btrfs" not in b and "recycle" in b


def test_homes_block():
    from cynthology.shares import homes_smb_block
    b = homes_smb_block({**BASE, "name": "homes", "path": "/volume1/homes", "homes": True})
    assert "[home]\n   path = /volume1/homes/%U" in b and "valid users = %U" in b
    assert "[homes]\n   path = /volume1/homes/%S" in b and "browseable = no" in b
