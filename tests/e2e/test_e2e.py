"""End-to-end tests against a real, freshly installed Cynthology (a throwaway VM).

    CYN_E2E_URL=https://localhost:5101 CYN_E2E_SSH="ssh -p 2222 -i key nasadmin@localhost" \
    CYN_E2E_USER=nasadmin CYN_E2E_PASS=... pytest -q tests/e2e

The VM needs at least 4 empty data drives. Tests run in file order and build on each other
(pool -> shared folder -> files -> ...); the last one reboots the VM.
"""
import hashlib
import io
import json
import os
import shlex
import subprocess
import time
import urllib.parse

import httpx
import pytest

URL = os.environ.get("CYN_E2E_URL", "https://localhost:5101")
SSH = shlex.split(os.environ.get("CYN_E2E_SSH", "ssh -p 2222 nasadmin@localhost"))
USER = os.environ.get("CYN_E2E_USER", "nasadmin")
PASS = os.environ.get("CYN_E2E_PASS", "")
ART = os.environ.get("CYN_E2E_ARTIFACTS", "e2e-artifacts")
S = {}                       # state shared between the tests (they run in order)


def ssh(cmd: str, timeout: int = 300) -> str:
    r = subprocess.run(SSH + ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout + r.stderr


@pytest.fixture(scope="session")
def api():
    # (the VM has a self-signed certificate made at install time - nothing else is talked to)
    c = httpx.Client(base_url=URL, verify=False, timeout=120)
    r = c.post("/api/auth/login", json={"username": USER, "password": PASS})
    assert r.status_code == 200, r.text
    c.headers["Authorization"] = "Bearer " + r.json()["token"]
    return c


def wait(cond, timeout=900, step=5, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timed out waiting for {what}")


def test_installed_and_signed_in(api):
    assert api.get("/").status_code == 200
    me = api.get("/api/auth/me").json()
    assert me["user"] == USER and me["admin"]
    assert "active" in ssh("systemctl is-active cynthology-ui smbd")


def test_create_pool(api):
    free = [d["name"] for d in api.get("/api/storage/disks").json() if d["usage"] == "free" and not d["system"]]
    assert len(free) >= 4, f"need 4 empty drives, have {free}"
    r = api.post("/api/storage/pools", json={"raid": "shr", "disks": free[:4], "label": "E2E"})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    wait(lambda: api.get(f"/api/storage/jobs/{job}").json().get("state") in ("done", "failed"), what="pool creation")
    assert api.get(f"/api/storage/jobs/{job}").json()["state"] == "done", api.get(f"/api/storage/jobs/{job}").text
    pool = next(p for p in api.get("/api/storage/pools").json() if p["label"] == "E2E")
    assert pool["total"] > 0 and pool["status"] != "degraded"
    S["pool"] = pool


def test_shared_folder_and_files(api):
    r = api.post("/api/shares", json={"name": "e2e", "pool_id": S["pool"]["id"], "description": "end-to-end test"})
    assert r.status_code == 200, r.text
    data = os.urandom(3_000_000)
    S["sum"] = hashlib.sha256(data).hexdigest()
    r = api.post("/api/files/upload", params={"path": "/e2e", "name": "random.bin"}, content=data)
    assert r.status_code == 200, r.text
    names = [x["name"] for x in api.get("/api/files/list", params={"path": "/e2e"}).json()["items"]]
    assert "random.bin" in names
    tk = api.post("/api/files/ticket", json={"path": "/e2e/random.bin"}).json()["url"]
    assert hashlib.sha256(api.get(tk).content).hexdigest() == S["sum"]


def test_smb(api):
    out = ssh(f"smbclient -U {shlex.quote(USER + '%' + PASS)} //localhost/e2e -c ls")
    assert "random.bin" in out, out


def test_snapshot(api):
    r = api.post("/api/snapshots/e2e")
    assert r.status_code == 200, r.text
    assert len(api.get("/api/snapshots/e2e").json()) >= 1


def test_preview_thumbnail(api):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (1200, 800), (30, 140, 200)).save(buf, "JPEG")
    api.post("/api/files/upload", params={"path": "/e2e", "name": "photo.jpg"}, content=buf.getvalue()).raise_for_status()
    r = api.get("/api/preview/thumb", params={"path": "/e2e/photo.jpg"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp", r.text[:200]


def test_sharing_link(api):
    link = api.post("/api/sharelinks", json={"path": "/e2e/random.bin", "password": "letmein", "expires_days": 1}).json()
    pub = httpx.Client(base_url=URL, verify=False, timeout=60)          # a visitor: no account
    assert pub.get(f"/s/{link['id']}/dl").status_code == 403
    assert pub.post(f"/s/{link['id']}/unlock", json={"password": "letmein"}).status_code == 200
    assert hashlib.sha256(pub.get(f"/s/{link['id']}/dl").content).hexdigest() == S["sum"]


def test_webdav(api):
    api.put("/api/fileservices/webdav", json={"enabled": True, "http": False, "https": True}).raise_for_status()
    wait(lambda: "200" in ssh("curl -sk -o /dev/null -w '%{http_code}' -u " + shlex.quote(f"{USER}:{PASS}") +
                              " https://localhost:5006/e2e/random.bin"), timeout=120, what="WebDAV")


def test_configuration_backup(api):
    r = api.post("/api/config/backup", json={"password": "e2e-backup-pw"})
    assert r.status_code == 200 and r.content.startswith(b"CYNCFG1\n")
    api.post("/api/config/restore/upload", content=r.content).raise_for_status()
    inspect = api.post("/api/config/restore/inspect", json={"password": "e2e-backup-pw"}).json()
    assert any(s["name"] == "e2e" for s in inspect["shares"])


def test_security_advisor(api):
    r = api.post("/api/security/advisor").json()
    assert set(r["counts"]) == {"critical", "warning", "info", "ok"} and len(r["checks"]) > 10


def test_every_page_opens_without_errors(api):
    """Open every app and every Control Panel section in a real browser; any JavaScript error fails."""
    from playwright.sync_api import sync_playwright
    os.makedirs(ART, exist_ok=True)
    errors = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_context(viewport={"width": 1366, "height": 900}, ignore_https_errors=True).new_page()
        page.on("pageerror", lambda e: errors.append(f"{page.url}: {e}"))
        # (errors inside async code - "Uncaught (in promise)" - only show up on the console)
        page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text and errors.append(m.text))
        page.goto(URL)
        page.fill("#login-user", USER)
        page.fill("#login-pass", PASS)
        page.click("button[type=submit]")
        page.wait_for_function("typeof WM === 'object' && !document.querySelector('#desktop').classList.contains('hidden')",
                               timeout=60000)
        page.evaluate("document.querySelectorAll('.wiz').forEach(w => w.remove())")
        close_all = "document.querySelectorAll('.wiz').forEach((w) => w.remove()); Object.keys(WM.wins).forEach((k) => WM.close(k))"
        apps = page.evaluate("Object.keys(WM.apps)")
        assert len(apps) > 5, apps
        for app in apps:
            page.evaluate(f"{close_all}; WM.open({json.dumps(app)})")
            page.wait_for_timeout(2500)
            page.evaluate("document.querySelectorAll('.wiz').forEach(w => w.remove())")
            page.screenshot(path=f"{ART}/app-{app}.png")
        keys = page.evaluate("Object.keys(CP_SECTIONS)")
        for k in keys:
            page.evaluate(f"{close_all}; WM.open('control')")
            page.wait_for_timeout(800)
            page.evaluate("document.querySelectorAll('.wiz').forEach(w => w.remove())")
            page.click(f".cp-item[data-k={json.dumps(k)}]")
            page.wait_for_timeout(2500)
            page.screenshot(path=f"{ART}/cp-{k}.png")
        b.close()
    assert not errors, "\n".join(errors)


def test_survives_reboot(api):
    ssh("sudo systemctl reboot", timeout=30)
    time.sleep(20)
    wait(lambda: "active" in ssh("systemctl is-active cynthology-ui", timeout=20), timeout=600, step=10, what="reboot")
    c = httpx.Client(base_url=URL, verify=False, timeout=60)
    tok = wait(lambda: (lambda r: r.status_code == 200 and r.json()["token"])(
        c.post("/api/auth/login", json={"username": USER, "password": PASS})), timeout=300, what="sign-in after reboot")
    c.headers["Authorization"] = "Bearer " + tok
    pool = next(p for p in c.get("/api/storage/pools").json() if p["label"] == "E2E")
    assert pool["status"] in ("healthy",) or pool["status"].startswith("optimizing"), pool["status"]
    tk = c.post("/api/files/ticket", json={"path": "/e2e/random.bin"}).json()["url"]
    assert hashlib.sha256(c.get(tk).content).hexdigest() == S["sum"]
