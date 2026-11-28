"""Upgrade test: the latest published release is installed and filled with data and settings
(CYN_E2E_PHASE=before), the VM is upgraded to this commit like an update does (install-checkout.sh),
then everything is checked again (CYN_E2E_PHASE=after). What was saved before is kept in
$E2E_DIR/upgrade-state.json between the two phases.
"""
import hashlib
import json
import os
import shlex
import subprocess
import time

import httpx
import pytest

URL = os.environ.get("CYN_E2E_URL", "https://localhost:5101")
SSH = shlex.split(os.environ.get("CYN_E2E_SSH", "ssh -p 2222 nasadmin@localhost"))
USER = os.environ.get("CYN_E2E_USER", "nasadmin")
PASS = os.environ.get("CYN_E2E_PASS", "")
PHASE = os.environ.get("CYN_E2E_PHASE", "before")
STATE = os.path.join(os.environ.get("E2E_DIR", "/tmp/e2e-upgrade"), "upgrade-state.json")
BOB_PW = "Upgrade-test-pw-42"

pytestmark = pytest.mark.skipif(not PASS, reason="needs an e2e VM (CYN_E2E_PASS)")


def ssh(cmd: str, timeout: int = 300) -> str:
    r = subprocess.run(SSH + ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout + r.stderr


def wait(cond, timeout=900, step=5, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = cond()
        except httpx.HTTPError:
            v = None
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timed out waiting for {what}")


def login(user=USER, pw=PASS) -> httpx.Client:
    c = httpx.Client(base_url=URL, verify=False, timeout=120)      # (the VM's own self-signed certificate)
    tok = wait(lambda: (lambda r: r.status_code == 200 and r.json()["token"])(
        c.post("/api/auth/login", json={"username": user, "password": pw})), timeout=300, what=f"sign-in as {user}")
    c.headers["Authorization"] = "Bearer " + tok
    return c


@pytest.mark.skipif(PHASE != "before", reason="before-upgrade phase")
def test_before_upgrade():
    c = login()
    st = {"version": c.get("/api/update").json()["current"]}
    free = [d["name"] for d in c.get("/api/storage/disks").json() if d["usage"] == "free" and not d["system"]]
    r = c.post("/api/storage/pools", json={"raid": "shr", "disks": free[:4], "label": "Upgrade"})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    wait(lambda: c.get(f"/api/storage/jobs/{job}").json().get("state") in ("done", "failed"), what="pool")
    pool = next(p for p in c.get("/api/storage/pools").json() if p["label"] == "Upgrade")
    assert c.post("/api/shares", json={"name": "keep", "pool_id": pool["id"], "description": "kept across updates"}).status_code == 200
    st["sums"] = {}
    for i in range(3):
        data = os.urandom(2_000_000)
        st["sums"][f"f{i}.bin"] = hashlib.sha256(data).hexdigest()
        c.post("/api/files/upload", params={"path": "/keep", "name": f"f{i}.bin"}, content=data).raise_for_status()
    assert c.post("/api/users", json={"name": "bob", "password": BOB_PW, "full_name": "Bob"}).status_code == 200
    assert c.post("/api/snapshots/keep").status_code == 200
    st["snapshots"] = c.get("/api/snapshots/keep").json()
    link = c.post("/api/sharelinks", json={"path": "/keep/f0.bin"}).json()
    st["link"] = link["id"]
    c.put("/api/portal/settings", json={"title": "Upgrade Test", "message": "hello"}).raise_for_status()
    st["services"] = ssh("systemctl is-active cynthology-ui smbd").split()
    with open(STATE, "w") as f:
        json.dump(st, f)


@pytest.mark.skipif(PHASE != "after", reason="after-upgrade phase")
def test_after_upgrade():
    st = json.load(open(STATE))
    c = login()
    now = c.get("/api/update").json()["current"]
    assert now != st["version"], f"still {now} - the upgrade didn't take"
    pool = next(p for p in c.get("/api/storage/pools").json() if p["label"] == "Upgrade")
    assert pool["status"] == "healthy" or pool["status"].startswith("optimizing"), pool["status"]
    share = next(s for s in c.get("/api/shares").json() if s["name"] == "keep")
    assert share["description"] == "kept across updates"
    for name, want in st["sums"].items():
        tk = c.post("/api/files/ticket", json={"path": f"/keep/{name}"}).json()["url"]
        assert hashlib.sha256(c.get(tk).content).hexdigest() == want, name
    login("bob", BOB_PW)                                            # (users and passwords kept)
    out = ssh(f"smbclient -U {shlex.quote('bob%' + BOB_PW)} //localhost/home -c ls 2>&1 | head -3")
    assert "NT_STATUS_LOGON_FAILURE" not in out, out
    assert len(c.get("/api/snapshots/keep").json()) >= len(st["snapshots"])
    pub = httpx.Client(base_url=URL, verify=False, timeout=60)
    assert hashlib.sha256(pub.get(f"/s/{st['link']}/dl").content).hexdigest() == st["sums"]["f0.bin"]
    assert pub.get("/api/portal").json()["title"] == "Upgrade Test"
    assert ssh("systemctl is-active cynthology-ui smbd").split() == ["active", "active"]
    failed = ssh("systemctl list-units --failed --no-legend --plain | grep -i cyn || true").strip()
    assert not failed, f"failed units after the upgrade: {failed}"
