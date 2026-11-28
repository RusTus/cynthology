import datetime
import hashlib
import hmac
import json
import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from cynthology import digest, notify, settings  # noqa: E402


def test_webhook_json_signed():
    url, body, hdr = notify.build({"type": "webhook", "url": "https://ha.local/api/webhook/x", "secret": "s3"}, "critical", "Pool degraded", "d")
    d = json.loads(body)
    assert url.endswith("/x") and d["level"] == "critical" and d["title"] == "Pool degraded"
    assert hdr["X-Cynthology-Signature"] == "sha256=" + hmac.new(b"s3", body, hashlib.sha256).hexdigest()


def test_webhook_slack_and_gotify_and_sms():
    _, body, _ = notify.build({"type": "webhook", "url": "https://hooks.slack.com/x", "format": "slack"}, "warning", "T", "D")
    assert json.loads(body)["text"].startswith(":warning: *[")
    url, body, hdr = notify.build({"type": "gotify", "server": "https://g.example.com/", "app_token": "Abc123456"}, "critical", "T", "D")
    assert url == "https://g.example.com/message" and json.loads(body)["priority"] == 8 and hdr["X-Gotify-Key"] == "Abc123456"
    sid = "AC" + "a" * 32
    url, body, hdr = notify.build({"type": "sms", "account_sid": sid, "auth_token": "b" * 32, "from_number": "+15550001111",
                                   "to_number": "+15550002222"}, "critical", "T", "D")
    assert sid in url and b"To=%2B15550002222" in body and hdr["Authorization"].startswith("Basic ")


@pytest.mark.parametrize("c", [
    {"type": "webhook", "url": "ftp://x", "min_level": "warning"},
    {"type": "webhook", "url": "https://x/\nInjected: 1", "min_level": "warning"},
    {"type": "sms", "account_sid": "nope", "auth_token": "b" * 32, "from_number": "+1555", "to_number": "+15550002222", "min_level": "warning"},
    {"type": "gotify", "server": "https://g", "app_token": "x", "min_level": "warning"},
])
def test_validation(c):
    with pytest.raises(HTTPException):
        notify.validate(c)


def test_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(digest, "STATE", str(tmp_path / "d.json"))
    settings.update(digest={"frequency": "weekly", "day": 0, "time": "08:00"})
    monday = datetime.datetime(2026, 11, 30, 8, 5)
    assert digest.due(monday) and not digest.due(monday.replace(hour=7)) and not digest.due(monday + datetime.timedelta(days=1))
    subject, body, summary = digest.build(0)
    for part in ("Health", "Storage pools", "Backups", "Updates", "Alerts since"):
        assert part in body
    assert subject.startswith("Status report")
    open(digest.STATE, "w").write(json.dumps({"last_sent": int(monday.timestamp())}))
    assert not digest.due(monday + datetime.timedelta(minutes=10))
