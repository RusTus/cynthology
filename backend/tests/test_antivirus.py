import datetime
import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402

from cynthology import antivirus, shares  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("CFG", "STATUS", "FOUND", "LOG"):
        monkeypatch.setattr(antivirus, k, str(tmp_path / k))
    vol = tmp_path / "volume1"
    (vol / "docs" / "sub").mkdir(parents=True)
    monkeypatch.setattr(shares, "load", lambda: [{"name": "docs", "path": str(vol / "docs")}])
    real_qdir = antivirus._qdir
    monkeypatch.setattr(antivirus, "_qdir", lambda p: real_qdir(p) if os.getuid() == 0 else
                        (os.makedirs(vol / "docs" / ".cyn-quarantine", exist_ok=True), str(vol / "docs" / ".cyn-quarantine"))[1])
    return vol


def test_parse_and_command(env):
    out = "/volume1/docs/a b.exe: Win.Test.EICAR_HDB-1 FOUND\n/volume1/docs/x: OK\nLibClamAV Warning: whatever\n"
    assert antivirus.parse(out) == [("/volume1/docs/a b.exe", "Win.Test.EICAR_HDB-1")]
    cmd = antivirus.command(antivirus.config(), ["/volume1/docs"])
    assert cmd[-2:] == ["--", "/volume1/docs"] and "--exclude-dir=/\\.snapshots$" in cmd and "--max-filesize=200M" in cmd


def test_quarantine_and_restore(env):
    f = env / "docs" / "sub" / "evil.exe"
    f.write_bytes(b"X5O!P%")
    os.chmod(f, 0o640)
    rec = antivirus.quarantine(str(f), "EICAR")
    assert not f.exists() and os.path.exists(rec["stored"]) and rec["mode"] == 0o640
    antivirus._write(antivirus.FOUND, [rec])
    antivirus.restore(rec["id"], None)
    assert f.read_bytes() == b"X5O!P%" and oct(f.stat().st_mode & 0o777) == "0o640"
    assert antivirus._read(antivirus.FOUND, [])[0]["state"] == "restored"


def test_symlinked_folder_is_not_followed(env, tmp_path):
    secret = tmp_path / "etc"
    secret.mkdir()
    (secret / "passwd").write_text("root")
    os.symlink(secret, env / "docs" / "link")            # a user replaced a folder with a link to /etc
    with pytest.raises(OSError):
        antivirus.quarantine(str(env / "docs" / "link" / "passwd"), "EICAR")
    assert (secret / "passwd").read_text() == "root"
    with pytest.raises(OSError):
        antivirus.quarantine(str(tmp_path / "outside.txt"), "EICAR")       # not inside a shared folder


def test_restore_refuses_swapped_folder(env, tmp_path):
    f = env / "docs" / "sub" / "evil.exe"
    f.write_bytes(b"x")
    rec = antivirus.quarantine(str(f), "EICAR")
    antivirus._write(antivirus.FOUND, [rec])
    os.rmdir(env / "docs" / "sub")
    target = tmp_path / "etc2"
    target.mkdir()
    os.symlink(target, env / "docs" / "sub")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        antivirus.restore(rec["id"], None)
    assert e.value.status_code == 409 and not (target / "evil.exe").exists()


def test_schedule(env):
    antivirus._write(antivirus.CFG, {"schedule": "weekly", "day": 6, "time": "03:00"})
    sunday = datetime.datetime(2026, 11, 29, 3, 30)
    assert antivirus.due(sunday) and not antivirus.due(sunday - datetime.timedelta(days=1))
    antivirus._write(antivirus.STATUS, {"started": int(sunday.timestamp())})
    assert not antivirus.due(sunday + datetime.timedelta(minutes=15))
    assert datetime.datetime.fromtimestamp(antivirus._next(antivirus.config(), sunday)).date() == datetime.date(2026, 12, 6)
