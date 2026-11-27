"""Injection guards from the security review."""
import pytest
from fastapi import HTTPException

from cynthology import shares, webstation
from cynthology.util import one_line, valid_nfs_clients


def test_one_line_strips_newlines():
    assert one_line("x\n   root preexec = id") == "x    root preexec = id"


@pytest.mark.parametrize("bad", ["*(rw,no_root_squash)", "*", "10.0.0.0/8\n/ *", "a;b", "-o"])
def test_nfs_clients_rejects(bad):
    with pytest.raises(HTTPException):
        valid_nfs_clients(bad)


def test_nfs_clients_ok():
    assert valid_nfs_clients(" 192.168.1.0/24  nas2.lan fd00::/64 ") == "192.168.1.0/24 nas2.lan fd00::/64"


def test_smb_comment_one_line():
    block = shares.smb_block({"name": "a", "path": "/volume1/a", "description": "x\n   root preexec = id",
                              "read_users": [], "write_users": ["@users"]})
    assert "\n   root preexec" not in block


@pytest.mark.parametrize("folder", ["a;\n}\nserver{", "a/b;c", "x{y}", "a/../b", 'a"b', "a$b"])
def test_webstation_folder_rejects(folder):
    with pytest.raises(HTTPException):
        webstation.site_root({"folder": folder}, "/volume1/web")


def test_webstation_folder_ok():
    assert webstation.site_root({"folder": "My Site/blog"}, "/volume1/web") == "/volume1/web/My Site/blog"
