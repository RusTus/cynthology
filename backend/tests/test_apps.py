import json
import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from cynthology import apps  # noqa: E402


def test_catalog_is_complete():
    cat = apps.catalog()
    alts = json.load(open(os.path.join(apps.CATALOG_DIR, "alternatives.json")))
    for a in alts:
        for o in a["options"]:
            assert o["kind"] in ("spk", "container", "builtin")
            if o["kind"] == "container":
                assert o["app"] in cat, o
    for m in cat.values():
        assert "${DATA}" not in m["compose"]          # old format
        assert "/volume1" not in m["compose"]         # shares are resolved, never hard-coded
        assert apps.host_ports(m).isdisjoint({5000, 5001, 22, 139, 445}), m["id"]


def test_fill_placeholders():
    out = apps.fill("p=${SECRET} m=${SHARE:music} h=${F:hostname} tz=${TZ}", "s3cret", lambda n: f"/volume1/{n}", {"hostname": "panel.lan"})
    assert out.startswith("p=s3cret m=/volume1/music h=panel.lan tz=")
    assert "${" not in out


DATA_APP = {"id": "x", "name": "X", "port": 443, "holds_data": True, "compose": 'services:\n  x:\n    ports:\n      - "80:80"\n      - "443:443"\n      - "21:21"\n      - "61000-61099:61000-61099"\n      - "25:25"\n',
            "fields": [{"key": "hostname", "label": "Address", "pattern": "^[A-Za-z0-9.-]{1,253}$"},
                       {"key": "email", "label": "Email", "pattern": "^[^@\\s\"]+@[^@\\s\"]+$"}]}


def test_port_ranges_and_host_network():
    ports = apps.host_ports(DATA_APP)
    assert {80, 443, 21, 25, 61000, 61099} <= ports and 61050 in ports
    assert 8096 in apps.host_ports(apps.catalog()["jellyfin"])   # network_mode: host uses "port"


def test_install_questions_are_validated():
    m = DATA_APP
    with pytest.raises(HTTPException):
        apps.answers(m, {"hostname": "panel.example.com", "email": "not-an-email"})
    with pytest.raises(HTTPException):
        apps.answers(m, {"hostname": "bad host; rm -rf /", "email": "a@b.c"})
    assert apps.answers(m, {"hostname": "panel.example.com", "email": "me@example.com"}) == {"hostname": "panel.example.com", "email": "me@example.com"}
