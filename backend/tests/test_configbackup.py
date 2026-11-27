import json
import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from cynthology import configbackup as cb, firewall, shares  # noqa: E402


def test_roundtrip_and_wrong_password(monkeypatch, tmp_path):
    monkeypatch.setattr(shares, "load", lambda: [{"name": "family", "path": str(tmp_path), "read_users": [], "write_users": ["@users"]}])
    data = cb.build("correct horse")
    assert data.startswith(b"CYNCFG1\n") and b"users" not in data[8:200]          # encrypted, not readable
    files = cb.open_backup(data, "correct horse")
    assert {"manifest.json", "users.json", "shares.json"} <= set(files)
    with pytest.raises(HTTPException):
        cb.open_backup(data, "wrong")
    with pytest.raises(HTTPException):
        cb.open_backup(b"PK\x03\x04 a zip file", "x")
    s = cb.summary(files)
    assert s["shares"][0]["name"] == "family" and s["shares"][0]["state"] == "exists"
    assert s["users"][0]["name"] == "alice" and s["users"][0]["state"] == "new"


def test_restore_settings_turns_firewall_off(monkeypatch, tmp_path):
    monkeypatch.setattr(cb, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(firewall, "CFG", str(tmp_path / "firewall.json"))
    monkeypatch.setattr(firewall, "NFT", str(tmp_path / "fw.nft"))
    monkeypatch.setattr(cb, "_reapply", lambda report: None)
    fw = firewall.Config(enabled=True).model_dump()
    files = {"state/firewall.json": json.dumps(fw).encode(), "state/notify.json": b"[]",
             "state/../../etc/passwd": b"x", "state/sessions.json": b"{}"}
    report = []
    cb.restore_settings(files, report)
    assert json.load(open(tmp_path / "firewall.json"))["enabled"] is False
    assert (tmp_path / "notify.json").exists() and not (tmp_path / "sessions.json").exists()
    assert any("switched off" in r for r in report)
