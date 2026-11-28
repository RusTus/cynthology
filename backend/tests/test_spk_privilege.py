"""conf/privilege: SynoCommunity's shared group must be accepted; other existing groups not."""
import grp
import json
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from cynthology import spk


def _stage(tmp_path, priv):
    os.makedirs(tmp_path / "conf")
    (tmp_path / "conf" / "privilege").write_text(json.dumps(priv))
    return str(tmp_path)


@pytest.mark.parametrize("group,ok", [("synocommunity", True), ("sc-download", True), ("mysql", False), ("users", False)])
def test_existing_groups(tmp_path, monkeypatch, group, ok):
    gids = {"synocommunity": 987, "sc-download": 950, "mysql": 120, "users": 100}
    monkeypatch.setattr(grp, "getgrnam", lambda n: SimpleNamespace(gr_gid=gids[n]) if n in gids else (_ for _ in ()).throw(KeyError(n)))
    d = _stage(tmp_path, {"username": "sc-foo", "groupname": group})
    if ok:
        assert spk._privilege(d, "foo")[1] == group
    else:
        with pytest.raises(HTTPException):
            spk._privilege(d, "foo")
