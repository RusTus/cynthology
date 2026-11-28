import os

os.environ["CYN_DEV"] = "1"

from cynthology import mariadb10  # noqa: E402


def test_password_and_wizard(tmp_path, monkeypatch):
    monkeypatch.setattr(mariadb10, "STATE", str(tmp_path / "m.json"))
    pw = mariadb10.root_password()
    assert pw == mariadb10.root_password() and len(pw) >= 16
    assert oct(os.stat(mariadb10.STATE).st_mode)[-3:] == "600"
    w = mariadb10.fill_wizard({"wizard_mysql_password_root": "typed-wrong", "wizard_mysql_password_owncloud": "x", "wizard_admin_password": "y"})
    assert w == {"wizard_mysql_password_root": pw, "wizard_mysql_password_owncloud": "x", "wizard_admin_password": "y"}
    assert mariadb10.fill_wizard({})["wizard_mysql_password_root"] == pw
    steps = [{"items": [{"subitems": [{"key": "wizard_mysql_password_root", "defaultValue": ""}, {"key": "wizard_owncloud_admin_password"}]}]}]
    out = mariadb10.prefill(steps)
    assert out[0]["items"][0]["subitems"][0]["defaultValue"] == pw and "defaultValue" not in out[0]["items"][0]["subitems"][1]
