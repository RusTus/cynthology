"""Storage failure tests against a real, freshly installed Cynthology VM with 6 empty drives:

    pull a drive -> pool degraded, data intact -> repair with a new drive -> healthy, data intact
    pull another -> repair -> reboot while it is still rebuilding -> the rebuild finishes by itself
    expand the pool with one more drive -> bigger, data intact

A "pulled" drive is deleted from the running kernel (/sys/block/<dev>/device/delete), which is what the
system sees when a disk is unplugged. Tests run in file order and build on each other.
    CYN_E2E_URL=... CYN_E2E_SSH=... CYN_E2E_USER=... CYN_E2E_PASS=... pytest -q tests/e2e/test_storage.py
"""
import hashlib
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
S = {}


def ssh(cmd: str, timeout: int = 300) -> str:
    r = subprocess.run(SSH + ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout + r.stderr


def client() -> httpx.Client:
    c = httpx.Client(base_url=URL, verify=False, timeout=120)      # (the VM's own self-signed certificate)
    tok = wait(lambda: (lambda r: r.status_code == 200 and r.json()["token"])(
        c.post("/api/auth/login", json={"username": USER, "password": PASS})), timeout=600, what="sign-in")
    c.headers["Authorization"] = "Bearer " + tok
    return c


@pytest.fixture(scope="module")
def api():
    return client()


def wait(cond, timeout=1200, step=5, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = cond()
        except (httpx.HTTPError, subprocess.SubprocessError):
            v = None
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timed out waiting for {what}")


def pool(c) -> dict:
    return next(p for p in c.get("/api/storage/pools").json() if p["label"] == "Failsafe")


def free_disks(c) -> list[str]:
    return sorted(d["name"] for d in c.get("/api/storage/disks").json() if d["usage"] == "free" and not d["system"])


def checksums_ok(c) -> bool:
    for name, want in S["sums"].items():
        tk = c.post("/api/files/ticket", json={"path": f"/failsafe/{name}"}).json()["url"]
        if hashlib.sha256(c.get(tk).content).hexdigest() != want:
            return False
    big = ssh("sha256sum /volume1/failsafe/big.bin 2>/dev/null || sudo sha256sum $(ls -d /volume*/failsafe)/big.bin").split()[0]
    return big == S["big"]


def pull(c, disk: str) -> None:
    """Unplug a member drive (as far as the running system can tell)."""
    ssh(f"echo 1 | sudo tee /sys/block/{disk}/device/delete >/dev/null; sync")
    ssh("sudo dd if=/dev/urandom of=$(ls -d /volume*/failsafe | head -1)/touch.bin bs=1M count=8 conv=fsync 2>/dev/null")   # (I/O makes md notice)
    wait(lambda: pool(c)["status"] == "degraded", timeout=300, what=f"pool degraded after pulling {disk}")


def test_pool_with_data(api):
    free = free_disks(api)
    assert len(free) >= 6, f"need 6 empty drives, have {free}"
    S["spare"] = free[3:]
    r = api.post("/api/storage/pools", json={"raid": "shr", "disks": free[:3], "label": "Failsafe"})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    wait(lambda: api.get(f"/api/storage/jobs/{job}").json().get("state") in ("done", "failed"), what="pool creation")
    assert api.get(f"/api/storage/jobs/{job}").json()["state"] == "done"
    p = pool(api)
    S["pool_id"], S["size"] = p["id"], p["total"]
    assert api.post("/api/shares", json={"name": "failsafe", "pool_id": p["id"]}).status_code == 200
    S["sums"] = {}
    for i in range(3):
        data = os.urandom(4_000_000)
        S["sums"][f"f{i}.bin"] = hashlib.sha256(data).hexdigest()
        api.post("/api/files/upload", params={"path": "/failsafe", "name": f"f{i}.bin"}, content=data).raise_for_status()
    out = ssh("d=$(ls -d /volume*/failsafe | head -1); sudo dd if=/dev/urandom of=$d/big.bin bs=1M count=400 conv=fsync 2>/dev/null; sudo sha256sum $d/big.bin")
    S["big"] = out.split()[0]
    wait(lambda: pool(api)["status"] == "healthy", what="initial sync")
    assert checksums_ok(api)


def test_pulled_drive_and_repair(api):
    p = pool(api)
    victim = sorted(p["disks"])[0]
    pull(api, victim)
    assert checksums_ok(api), "data must stay readable while degraded"
    new = S["spare"].pop(0)
    r = api.post(f"/api/storage/pools/{p['id']}/add-disk", json={"disk": new})
    assert r.status_code == 200, r.text
    wait(lambda: pool(api)["status"] == "healthy", what="rebuild")
    assert new in pool(api)["disks"] and checksums_ok(api)


def test_reboot_during_rebuild(api):
    p = pool(api)
    victim = sorted(p["disks"])[0]
    pull(api, victim)
    ssh("echo 3000 | sudo tee /proc/sys/dev/raid/speed_limit_max >/dev/null")    # (slow, so it's mid-rebuild at reboot)
    new = S["spare"].pop(0)
    assert api.post(f"/api/storage/pools/{p['id']}/add-disk", json={"disk": new}).status_code == 200
    wait(lambda: pool(api)["status"].startswith("rebuilding"), timeout=180, what="rebuild to start")
    ssh("sudo systemctl reboot", timeout=30)
    time.sleep(30)
    c = client()                                          # (after the reboot: new sign-in; drive names may differ)
    wait(lambda: pool(c)["status"] == "healthy", timeout=1800, what="rebuild to finish after the reboot")
    assert checksums_ok(c)
    S["c"] = c


def test_expand(api):
    c = S.get("c") or api
    p = pool(c)
    free = free_disks(c)
    assert free, "no drive left for the expansion"
    r = c.post(f"/api/storage/pools/{p['id']}/add-disk", json={"disk": free[0]})
    assert r.status_code == 200, r.text
    wait(lambda: pool(c)["status"] == "healthy" and pool(c)["total"] > S["size"] * 1.2, timeout=2400, what="expansion")
    assert checksums_ok(c)


def test_still_good_after_reboot(api):
    c = S.get("c") or api
    ssh("sudo systemctl reboot", timeout=30)
    time.sleep(30)
    c = client()
    wait(lambda: pool(c)["status"] == "healthy", timeout=600, what="pool after reboot")
    assert checksums_ok(c)
