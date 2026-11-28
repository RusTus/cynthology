"""File operations inside shared folders that can't be redirected by links.

The web server runs as root, and shared folders belong to users, who can rename things and create
symlinks there at any time (SMB, SFTP, rsync...). Checking a path with realpath() and then using the
path string leaves a gap: a folder on the way can be swapped for a link to /etc in between. Here
every path below the share root is walked one component at a time with O_NOFOLLOW, and the work is
then done relative to the opened folder (dir_fd), so it stays inside the share whatever happens to
the names meanwhile.

    fd = open_dir(root, full)          # the folder at full (a realpath inside root)
    fd, name = open_parent(root, full)
    fd = open_file(root, full)         # read-only, the file itself
    proc_path(fd)                      # a path that keeps naming what fd is (for tools / FileResponse)
"""
import errno
import os
import stat

from fastapi import HTTPException

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _parts(root: str, full: str) -> tuple[str, list[str]]:
    root = os.path.realpath(root)
    rel = os.path.relpath(full, root)
    if rel == ".":
        return root, []
    parts = rel.split(os.sep)
    if any(p in ("", ".", "..") for p in parts):
        raise HTTPException(403, "path leaves the shared folder")
    return root, parts


def _walk(root: str, parts: list[str]) -> int:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for p in parts:
            try:
                nfd = os.open(p, DIR_FLAGS, dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise HTTPException(409, f"'{p}' was changed into a link or a file meanwhile - try again")
                if e.errno == errno.ENOENT:
                    raise HTTPException(404, f"'{p}' no longer exists")
                raise
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_dir(root: str, full: str) -> int:
    root, parts = _parts(root, full)
    return _walk(root, parts)


def open_parent(root: str, full: str) -> tuple[int, str]:
    root, parts = _parts(root, full)
    if not parts:
        raise HTTPException(400, "that's the shared folder itself")
    return _walk(root, parts[:-1]), parts[-1]


def open_file(root: str, full: str, flags: int = os.O_RDONLY) -> int:
    """The file itself (not a link), opened relative to its checked folder."""
    pfd, name = open_parent(root, full)
    try:
        return os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=pfd)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise HTTPException(409, "that item is a link")
        if e.errno == errno.ENOENT:
            raise HTTPException(404, "no longer exists")
        raise
    finally:
        os.close(pfd)


def proc_path(fd: int) -> str:
    """/proc/<pid>/fd/<fd>: opening this opens exactly what fd is, whatever its name is now
    (also from a child process, as root)."""
    return f"/proc/{os.getpid()}/fd/{fd}"


def lstat_at(dfd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def makedirs_at(dfd: int, rel: str, mode: int = 0o2775) -> int:
    """mkdir -p rel below the open folder dfd without following links; returns an fd of the last folder."""
    fd = os.dup(dfd)
    try:
        for p in [p for p in rel.split("/") if p]:
            if p in (".", ".."):
                raise HTTPException(400, "bad path")
            try:
                os.mkdir(p, mode, dir_fd=fd)
            except FileExistsError:
                pass
            try:
                nfd = os.open(p, DIR_FLAGS, dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise HTTPException(409, f"'{p}' is a link or a file - move it out of the way first")
                raise
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def chown_at(dfd: int, name: str, uid: int, gid: int) -> None:
    os.chown(name, uid, gid, dir_fd=dfd, follow_symlinks=False)


def remove_at(dfd: int, name: str) -> None:
    """rm -rf of one entry of an open folder (never through links: shutil.rmtree's fd-based walk)."""
    import shutil
    st = lstat_at(dfd, name)
    if st is None:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(name, dir_fd=dfd)
    else:
        os.unlink(name, dir_fd=dfd)


def _copy_file(sfd: int, sname: str, dfd: int, dname: str, st: os.stat_result) -> None:
    src = os.open(sname, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=sfd)
    try:
        dst = os.open(dname, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        try:
            left = st.st_size
            while left > 0:
                try:     # (on Btrfs within one volume this shares the blocks: instant)
                    n = os.copy_file_range(src, dst, min(left, 1 << 30))
                except OSError as e:
                    if e.errno not in (errno.EXDEV, errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
                        raise
                    n = os.sendfile(dst, src, None, min(left, 1 << 30))
                if n == 0:
                    break
                left -= n
            os.fchmod(dst, stat.S_IMODE(st.st_mode))
            os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
        finally:
            os.close(dst)
    finally:
        os.close(src)


def copy_at(sfd: int, sname: str, dfd: int, dname: str, owner: tuple[int, int] | None = None) -> None:
    """cp -a of one entry from one open folder into another; links are copied as links, never followed.
    The copy belongs to owner (uid, gid) when given, else keeps the original owner."""
    st = lstat_at(sfd, sname)
    if st is None:
        raise HTTPException(404, f"{sname} no longer exists")
    own = owner or (st.st_uid, st.st_gid)
    if stat.S_ISLNK(st.st_mode):
        os.symlink(os.readlink(sname, dir_fd=sfd), dname, dir_fd=dfd)
    elif stat.S_ISREG(st.st_mode):
        _copy_file(sfd, sname, dfd, dname, st)
    elif stat.S_ISDIR(st.st_mode):
        os.mkdir(dname, 0o700, dir_fd=dfd)
        s2 = os.open(sname, DIR_FLAGS, dir_fd=sfd)
        try:
            d2 = os.open(dname, DIR_FLAGS, dir_fd=dfd)
            try:
                for n in os.listdir(s2):
                    copy_at(s2, n, d2, n, owner)
                os.fchmod(d2, stat.S_IMODE(st.st_mode))
                os.utime(d2, ns=(st.st_atime_ns, st.st_mtime_ns))
            finally:
                os.close(d2)
        finally:
            os.close(s2)
    else:
        return      # (devices, sockets, fifos: not copied)
    try:
        os.chown(dname, *own, dir_fd=dfd, follow_symlinks=False)
    except OSError:
        pass


def move_at(sfd: int, sname: str, dfd: int, dname: str) -> None:
    """mv: a rename when both are on the same file system (subvolume), else copy + delete."""
    if lstat_at(sfd, sname) is None:
        raise HTTPException(404, f"{sname} no longer exists")
    if lstat_at(dfd, dname) is not None:
        raise HTTPException(409, f"{dname} already exists")
    try:
        os.rename(sname, dname, src_dir_fd=sfd, dst_dir_fd=dfd)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        copy_at(sfd, sname, dfd, dname)
        remove_at(sfd, sname)
