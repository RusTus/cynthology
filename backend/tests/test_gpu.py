import os

os.environ["CYN_DEV"] = "1"

from cynthology import apps, hardware  # noqa: E402

COMPOSE = "services:\n  jf:\n    image: x\n    restart: unless-stopped\n    ${GPU}\n    network_mode: host\n"


def test_intel(monkeypatch):
    monkeypatch.setattr(hardware, "video_accel", lambda: {"kind": "intel", "dri": ["/dev/dri/renderD128"], "name": "UHD 630"})
    out = apps.fill(COMPOSE, "s")
    assert "    devices:\n      - /dev/dri:/dev/dri\n" in out and "${GPU}" not in out and out.endswith("    network_mode: host\n")


def test_nvidia(monkeypatch):
    monkeypatch.setattr(hardware, "video_accel", lambda: {"kind": "nvidia", "dri": [], "name": "RTX"})
    out = apps.fill(COMPOSE, "s")
    assert "        - driver: nvidia\n" in out and "/dev/dri" not in out


def test_none(monkeypatch):
    monkeypatch.setattr(hardware, "video_accel", lambda: {"kind": None, "dri": [], "name": ""})
    assert apps.fill(COMPOSE, "s") == "services:\n  jf:\n    image: x\n    restart: unless-stopped\n    network_mode: host\n"
