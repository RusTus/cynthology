import json
import os
import urllib.parse

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from cynthology import notify  # noqa: E402


def test_build_payloads():
    url, body, hdr = notify.build({"type": "ntfy", "server": "https://ntfy.sh", "topic": "nas-x1", "token": "tk"}, "critical", "Drive failed", "sdb")
    assert url == "https://ntfy.sh/nas-x1" and body == b"sdb" and hdr["Priority"] == "5" and hdr["Authorization"] == "Bearer tk"
    url, body, hdr = notify.build({"type": "ntfy", "server": "https://ntfy.sh", "topic": "t"}, "info", "Größe", "")
    assert hdr["Title"].startswith("=?UTF-8?B?") and "Authorization" not in hdr      # headers stay Latin-1 safe
    url, body, _ = notify.build({"type": "telegram", "bot_token": "1:abc", "chat_id": "42"}, "warning", "T", "D")
    assert url.endswith("/bot1:abc/sendMessage") and json.loads(body)["chat_id"] == "42"
    url, body, _ = notify.build({"type": "discord", "webhook_url": "https://discord.com/api/webhooks/1/x"}, "critical", "T", "D")
    assert json.loads(body)["embeds"][0]["color"] == 0xDC2626
    url, body, _ = notify.build({"type": "pushover", "app_token": "a", "user_key": "u"}, "critical", "T", "D")
    assert urllib.parse.parse_qs(body.decode())["priority"] == ["1"]


@pytest.mark.parametrize("c", [
    {"type": "ntfy", "min_level": "warning", "server": "https://ntfy.sh", "topic": "a b"},
    {"type": "ntfy", "min_level": "warning", "server": "http://ntfy.lan", "topic": "x", "token": "secret"},
    {"type": "telegram", "min_level": "warning", "bot_token": "nope", "chat_id": "1"},
    {"type": "discord", "min_level": "warning", "webhook_url": "https://evil.example/api/webhooks/1/x"},
    {"type": "pushover", "min_level": "warning", "app_token": "short", "user_key": "x" * 30},
    {"type": "ntfy", "min_level": "loud", "server": "https://ntfy.sh", "topic": "x"},
])
def test_validate_rejects(c):
    with pytest.raises(HTTPException):
        notify.validate(c)


def test_push_respects_min_level(monkeypatch):
    chans = [{"id": "a", "type": "ntfy", "server": "https://ntfy.sh", "topic": "x", "min_level": "critical", "enabled": True},
             {"id": "b", "type": "ntfy", "server": "https://ntfy.sh", "topic": "y", "min_level": "info", "enabled": True},
             {"id": "c", "type": "ntfy", "server": "https://ntfy.sh", "topic": "z", "min_level": "info", "enabled": False}]
    monkeypatch.setattr(notify, "load", lambda: chans)
    notify._sent.clear()
    notify.push("warning", "Volume almost full", "")
    assert [s["channel"] for s in notify._sent] == ["b"]
    notify._sent.clear()
    notify.push("critical", "Pool degraded", "")
    assert sorted(s["channel"] for s in notify._sent) == ["a", "b"]
