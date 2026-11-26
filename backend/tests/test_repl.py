import os
import time

os.environ["CYN_DEV"] = "1"

from cynthology import repl  # noqa: E402

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG63examplekeyexamplekeyexamplekeyexampl"


def test_due(monkeypatch):
    now = time.time()
    tasks = [{"id": "a", "schedule": "hourly"}, {"id": "b", "schedule": "daily"}, {"id": "c", "schedule": "manual"}]
    started = {"a": now - 3600, "b": now - 3600}
    monkeypatch.setattr(repl, "load_tasks", lambda: tasks)
    monkeypatch.setattr(repl, "read_status", lambda tid: {"started": started.get(tid)})
    assert repl.due(now) == ["a"]                       # hourly is due, daily not yet, manual never


def test_authorized_keys_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(repl, "AUTH_KEYS", str(tmp_path / "authorized_keys"))
    repl._write_auth_keys([{"source": "nas1", "key": KEY, "comment": "cynthology-replication@nas1"}])
    line = open(tmp_path / "authorized_keys").read()
    assert line.startswith('command="sudo -n /usr/lib/cynthology/repl-receive nas1 \\"$SSH_ORIGINAL_COMMAND\\"",restrict ')
    assert repl._read_auth_keys() == [{"source": "nas1", "key": KEY, "comment": "cynthology-replication@nas1"}]


def test_replacing_a_sender_drops_its_old_key(tmp_path, monkeypatch):
    monkeypatch.setattr(repl, "AUTH_KEYS", str(tmp_path / "authorized_keys"))
    repl.add_sender(repl.Sender(source="nas1", public_key=KEY + " old"), None)
    repl.add_sender(repl.Sender(source="nas1", public_key=KEY.replace("AAAAC3", "BBBBC3") + " new"), None)
    keys = repl._read_auth_keys()
    assert len(keys) == 1 and keys[0]["comment"] == "new"


def test_rejects_bad_names():
    import pytest
    with pytest.raises(RuntimeError):
        repl._rdir("../x", "family")
    with pytest.raises(RuntimeError):
        repl._rdir("nas1", "../etc")
