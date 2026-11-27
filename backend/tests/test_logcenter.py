import os

os.environ["CYN_DEV"] = "1"

from cynthology.logcenter import parse_audit  # noqa: E402


def test_parse_audit():
    assert parse_audit("alice|192.168.1.5|family|openat|ok|r|Photos/a.jpg") == {
        "user": "alice", "ip": "192.168.1.5", "share": "family", "action": "read", "ok": True, "path": "Photos/a.jpg"}
    assert parse_audit("bob|10.0.0.2|docs|openat|ok|w|report.docx")["action"] == "write"
    r = parse_audit("bob|10.0.0.2|docs|renameat|ok|a.txt|b.txt")
    assert r["action"] == "rename" and r["path"] == "a.txt → b.txt"
    assert parse_audit("bob|10.0.0.2|docs|unlinkat|ok|old.txt")["action"] == "delete"
    assert parse_audit("not an audit line") is None


def test_paths_relative_and_recycle():
    r = parse_audit("nasadmin|::1|docs|openat|ok|w|/volume1/docs/sub/report.txt")
    assert r["path"] == "sub/report.txt" and r["action"] == "write"
    r = parse_audit("nasadmin|::1|docs|renameat|ok|/volume1/docs/final.txt|/volume1/docs/#recycle/final.txt")
    assert r["action"] == "delete (to recycle bin)" and r["path"] == "final.txt"
    r = parse_audit("nasadmin|::1|docs|renameat|ok|/volume1/docs/a.txt|/volume1/docs/b.txt")
    assert r["path"] == "a.txt → b.txt"
