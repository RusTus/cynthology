import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from cynthology import san  # noqa: E402


def test_target_validation():
    san._check_target(san.TargetBody(name="esx-hosts", initiators=["iqn.1998-01.com.vmware:esx01-1a2b3c4d"],
                                     chap_user="esxhost", chap_password="twelve-chars"))
    for bad in [dict(name="ESX"), dict(name="a b"), dict(name="ok", initiators=["not-an-iqn"]),
                dict(name="ok", chap_user="u", chap_password="short"), dict(name="ok", chap_user="bad user")]:
        with pytest.raises(HTTPException):
            san._check_target(san.TargetBody(**bad))


def test_iqn_prefix_is_valid():
    assert san.IQN_RE.match(san.iqn_prefix() + ".esx")
