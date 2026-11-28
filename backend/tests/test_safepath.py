"""Shared-folder operations must never follow a link a user planted (we run as root)."""
import os

import pytest
from fastapi import HTTPException

from cynthology import safepath


@pytest.fixture
def share(tmp_path):
    root = tmp_path / "share"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.txt").write_text("hello")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("root only")
    return root, outside


def test_open_dir_refuses_a_swapped_folder(share):
    root, outside = share
    full = str(root / "docs")                       # (checked with realpath: fine)
    os.rename(root / "docs", root / "docs.old")
    os.symlink(outside, root / "docs")              # (then swapped for a link)
    with pytest.raises(HTTPException) as e:
        safepath.open_dir(str(root), full)
    assert e.value.status_code == 409


def test_open_file_refuses_a_link(share):
    root, outside = share
    os.symlink(outside / "secret", root / "docs" / "b.txt")
    with pytest.raises(HTTPException):
        safepath.open_file(str(root), str(root / "docs" / "b.txt"))
    fd = safepath.open_file(str(root), str(root / "docs" / "a.txt"))
    assert os.read(fd, 10) == b"hello"
    os.close(fd)
    fd = safepath.open_file(str(root), str(root / "docs" / "a.txt"))
    with open(safepath.proc_path(fd)) as f:          # (the path keeps naming the opened file)
        assert f.read() == "hello"
    os.close(fd)


def test_makedirs_at_never_goes_through_a_link(share):
    root, outside = share
    os.symlink(outside, root / "#recycle")
    rfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(HTTPException):
            safepath.makedirs_at(rfd, "#recycle/x/y")
    finally:
        os.close(rfd)
    assert not (outside / "x").exists()


def test_copy_keeps_links_as_links_and_move(share, tmp_path):
    root, outside = share
    os.symlink(outside / "secret", root / "docs" / "link")
    (root / "dst").mkdir()
    s = safepath.open_dir(str(root), str(root))
    d = safepath.open_dir(str(root), str(root / "dst"))
    try:
        safepath.copy_at(s, "docs", d, "docs")
        assert (root / "dst" / "docs" / "a.txt").read_text() == "hello"
        assert os.path.islink(root / "dst" / "docs" / "link")
        safepath.move_at(d, "docs", s, "moved")
        assert (root / "moved" / "a.txt").exists() and not (root / "dst" / "docs").exists()
        safepath.remove_at(s, "moved")
        assert not (root / "moved").exists() and (outside / "secret").exists()
    finally:
        os.close(s)
        os.close(d)
