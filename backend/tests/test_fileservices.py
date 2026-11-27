import base64
import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from cynthology import fileservices, shares, webdav  # noqa: E402


def test_rsyncd_modules_follow_permissions():
    conf = fileservices.render_rsyncd([
        {"name": "family", "path": "/volume1/family", "description": "x\ny = z", "read_users": ["@kids"], "write_users": ["alice", "CORP\\bob"]},
        {"name": "usb1", "path": "/media/usb1", "usb": True, "read_users": [], "write_users": ["@users"]},
        {"name": "vault", "path": "/volume1/vault", "encrypted": True, "_unlocked": False, "read_users": [], "write_users": ["@users"]},
    ], "/volume1/homes")
    assert "[family]" in conf and "auth users = @cynthology-admin:rw, alice:rw, @kids:ro" in conf
    assert "CORP" not in conf and "\ny = z" not in conf
    assert "[usb1]" not in conf and "[vault]" not in conf
    assert "uid = %RSYNC_USER_NAME%" in conf and "use chroot = yes" in conf
    assert "path = /volume1/homes/%RSYNC_USER_NAME%" in conf


def test_sshd_sftp_only_for_users():
    assert "ForceCommand" not in fileservices.render_sshd(False, "/volume1")
    on = fileservices.render_sshd(True, "/volume1")
    assert "Match Group users,!cynthology-admin" in on and "ForceCommand internal-sftp -d /volume1" in on


@pytest.fixture
def dav(tmp_path, monkeypatch):
    share = {"name": "docs", "path": str(tmp_path / "docs"), "read_users": [], "write_users": ["@users"], "recycle_bin": True}
    os.makedirs(share["path"])
    monkeypatch.setattr(shares, "load", lambda: [share])
    c = TestClient(webdav.app)
    c.headers["Authorization"] = "Basic " + base64.b64encode(b"admin:admin").decode()
    return c, share["path"]


def test_webdav_round_trip(dav):
    c, root = dav
    assert c.request("PROPFIND", "/", headers={"Depth": "1"}).text.count("<D:href>/docs/</D:href>") == 1
    assert c.request("MKCOL", "/docs/a b").status_code == 201
    assert c.put("/docs/a b/x.html", content=b"<script>1</script>").status_code == 201
    r = c.get("/docs/a%20b/x.html")
    assert r.content == b"<script>1</script>" and "sandbox" in r.headers["content-security-policy"]
    r = c.request("PROPFIND", "/docs/a%20b/", headers={"Depth": "1"})
    assert r.status_code == 207 and "<D:href>/docs/a%20b/x.html</D:href>" in r.text and "getcontentlength>18<" in r.text
    assert c.request("MOVE", "/docs/a%20b/x.html", headers={"Destination": "http://nas/docs/y.html"}).status_code == 201
    assert c.request("COPY", "/docs/y.html", headers={"Destination": "/docs/z.html", "Overwrite": "F"}).status_code == 201
    assert c.request("COPY", "/docs/y.html", headers={"Destination": "/docs/z.html", "Overwrite": "F"}).status_code == 412
    lk = c.request("LOCK", "/docs/new.txt", content=b"<lockinfo/>")
    assert lk.status_code == 201 and lk.headers["Lock-Token"].startswith("<opaquelocktoken:")
    assert c.delete("/docs/y.html").status_code == 204
    assert os.path.exists(os.path.join(root, "#recycle", "y.html"))        # deleted to the recycle bin


def test_webdav_refuses_escape_and_bad_login(dav):
    c, root = dav
    assert c.request("PROPFIND", "/docs/../../etc", headers={"Depth": "0"}).status_code in (400, 403, 404)
    os.symlink("/etc", os.path.join(root, "link"))
    assert c.get("/docs/link/hostname").status_code == 403
    assert c.request("MOVE", "/docs", headers={"Destination": "/x"}).status_code == 403
    assert c.delete("/docs").status_code == 403
    r = c.get("/", headers={"Authorization": "Basic " + base64.b64encode(b"admin:wrong").decode()})
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
