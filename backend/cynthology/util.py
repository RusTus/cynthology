"""Shared helpers: command execution, dev-mode switch, simple errors."""
import os
import shlex
import subprocess

from fastapi import HTTPException

# CYN_DEV=1 lets the UI run on a laptop/CI without root and without
# touching real disks: privileged commands are logged instead of executed.
DEV = os.environ.get("CYN_DEV") == "1"
STATE_DIR = os.path.abspath(os.environ.get("CYN_STATE", "/var/lib/cynthology" if not DEV else "./.cyn-state"))
os.makedirs(STATE_DIR, exist_ok=True)


def run(cmd: list[str], *, check: bool = True, privileged: bool = True, input: str | None = None) -> str:
    """Run a command and return stdout. In dev mode privileged commands are only echoed."""
    if DEV and privileged:
        print("[dev] would run:", shlex.join(cmd))
        return ""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, input=input, timeout=600)
    except FileNotFoundError:
        raise HTTPException(500, f"command not found: {cmd[0]}")
    if check and res.returncode != 0:
        raise HTTPException(500, f"{shlex.join(cmd)} failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout


def read_only(cmd: list[str]) -> str:
    """Run a non-destructive command even in dev mode; empty string if it fails."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return res.stdout if res.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def valid_name(name: str, kind: str = "name") -> str:
    import re
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name or ""):
        raise HTTPException(400, f"invalid {kind}: {name!r}")
    return name


def one_line(text: str, limit: int = 200) -> str:
    """Free text that ends up inside a config file (descriptions, labels): one line, no control
    characters, so it can never start a new option or section."""
    import re
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(text or ""))[:limit].strip()


NFS_CLIENT_RE = r"(\*|[A-Za-z0-9][A-Za-z0-9.-]{0,252}|[0-9a-fA-F:.]{2,45})(/[0-9]{1,3})?"


def valid_nfs_clients(text: str) -> str:
    """Space-separated hosts / IP addresses / subnets (no export options, no wildcard-everything)."""
    import re
    parts = (text or "").split()
    for p in parts:
        if p == "*" or not re.fullmatch(NFS_CLIENT_RE, p):
            raise HTTPException(400, f"NFS clients: {p!r} is not a host, IP address or subnet (e.g. 192.168.1.0/24)")
    return " ".join(parts)


def valid_principal(entry: str) -> str:
    """A user or @group for share permissions: local (alice, @users) or domain (CORP\\alice,
    @CORP\\Domain Users). No quotes, % or other characters Samba would interpret."""
    import re
    if not re.fullmatch(r"@?([A-Za-z0-9][A-Za-z0-9_.-]{0,63}|[A-Za-z0-9][A-Za-z0-9_.-]{0,14}\\[A-Za-z0-9][A-Za-z0-9 _.$-]{0,63})", entry or ""):
        raise HTTPException(400, f"not a user or @group name: {entry!r}")
    return entry
