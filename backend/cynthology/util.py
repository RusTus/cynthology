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
