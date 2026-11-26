import os

os.environ["CYN_DEV"] = "1"

from cynthology import ups  # noqa: E402


def cfg(**kw):
    return {**ups.DEFAULTS, "local_password": "pw123", **kw}


def test_disabled():
    f = ups.render(cfg(enabled=False))
    assert "MODE=none" in f["nut.conf"]
    assert "MONITOR" not in f["upsmon.conf"]


def test_usb_standalone_with_timer():
    f = ups.render(cfg(enabled=True, mode="usb", shutdown="timer", shutdown_minutes=3))
    assert "MODE=standalone" in f["nut.conf"]
    assert "driver = usbhid-ups" in f["ups.conf"]
    assert "LISTEN 127.0.0.1 3493" in f["upsd.conf"]
    assert "MONITOR ups@localhost 1 cynadmin pw123 primary" in f["upsmon.conf"]
    assert "START-TIMER shutdown-timer 180" in f["upssched.conf"]
    assert "CANCEL-TIMER shutdown-timer" in f["upssched.conf"]
    assert "POWERDOWNFLAG /etc/killpower" in f["upsmon.conf"]


def test_lowbatt_policy_has_no_timer():
    f = ups.render(cfg(enabled=True, mode="usb", shutdown="lowbatt"))
    assert "START-TIMER" not in f["upssched.conf"]
    assert "EXECUTE onbatt" in f["upssched.conf"]


def test_serving_other_machines_is_synology_compatible():
    f = ups.render(cfg(enabled=True, mode="usb", serve=True))
    assert "MODE=netserver" in f["nut.conf"]
    assert "LISTEN 0.0.0.0 3493" in f["upsd.conf"]
    assert "[monuser]" in f["upsd.users"] and "password = secret" in f["upsd.users"] and "upsmon secondary" in f["upsd.users"]


def test_snmp():
    f = ups.render(cfg(enabled=True, mode="snmp", snmp_host="192.168.1.20", snmp_community="private"))
    assert "driver = snmp-ups" in f["ups.conf"] and "port = 192.168.1.20" in f["ups.conf"] and "community = private" in f["ups.conf"]


def test_network_client_of_synology():
    f = ups.render(cfg(enabled=True, mode="network", server_host="192.168.1.10"))
    assert "MODE=netclient" in f["nut.conf"]
    assert "MONITOR ups@192.168.1.10 1 monuser secret secondary" in f["upsmon.conf"]
    assert "POWERDOWNFLAG /etc/killpower" not in f["upsmon.conf"]   # only the machine wired to the UPS powers it off
