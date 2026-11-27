import os

os.environ["CYN_DEV"] = "1"

from cynthology import directory  # noqa: E402


def test_render_global():
    g = directory.render_global("corp.example.com", "corp", "/volume1/homes")
    assert "security = ADS" in g and "realm = CORP.EXAMPLE.COM" in g and "workgroup = CORP" in g
    assert "idmap config CORP : backend = rid" in g and "template homedir = /volume1/homes/%U" in g


def test_include_and_restore(tmp_path, monkeypatch):
    conf = tmp_path / "smb.conf"
    conf.write_text("[global]\n   workgroup = WORKGROUP\n   server role = standalone server\n   security = user\n\n[homes]\n   browseable = no\n")
    monkeypatch.setattr(directory, "SMB_CONF", str(conf))
    directory._set_include(True)
    t = conf.read_text()
    assert "#cyn-domain#    workgroup = WORKGROUP" in t and "#cyn-domain#    security = user" in t
    assert t.index(f"include = {directory.GLOBAL_INC}") < t.index("[homes]")        # end of [global]
    assert t.index("server role") < t.index(f"include = {directory.GLOBAL_INC}")
    directory._set_include(False)
    assert conf.read_text() == "[global]\n   workgroup = WORKGROUP\n   server role = standalone server\n   security = user\n\n[homes]\n   browseable = no\n"


def test_nsswitch(tmp_path, monkeypatch):
    ns = tmp_path / "nsswitch.conf"
    ns.write_text("passwd:         files systemd\ngroup:          files systemd\nhosts: files dns\n")
    monkeypatch.setattr(directory, "NSSWITCH", str(ns))
    directory._nsswitch(True)
    assert "passwd:         files systemd winbind" in ns.read_text() and "hosts: files dns" in ns.read_text()
    directory._nsswitch(False)
    assert "winbind" not in ns.read_text()
