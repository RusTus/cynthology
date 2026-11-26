"""Cynthology management API + static web desktop.

Run (dev, no root, mocked disks):  CYN_DEV=1 uvicorn cynthology.main:app --reload --port 5000
Login in dev mode: admin / admin
"""
import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import apps, auth, san, webstation, backup, containers, files, health, network, repl, services, settings, setup, shares, snapshots, spk, storage, sysinfo, tls, twofa, ups, update, usb, users
from .util import DEV

app = FastAPI(title="Cynthology", version="0.1.0", docs_url="/api/docs" if DEV else None)
for r in (auth.router, sysinfo.router, storage.router, shares.router, users.router, services.router, apps.router, spk.router,
          tls.router, setup.router, files.router, snapshots.router, health.router, backup.router, ups.router, twofa.router, repl.router, containers.router, usb.router, network.router, update.router, webstation.router, san.router):
    app.include_router(r)


@app.middleware("http")
async def https_redirect(request: Request, call_next):
    """With 'redirect HTTP to HTTPS' on, plain-HTTP requests are sent to the HTTPS port."""
    if request.url.scheme == "http":
        s = settings.get_all()
        if s["https_redirect"] and os.path.exists(tls.CERT):
            host = request.url.hostname or "localhost"
            if ":" in host:  # IPv6 literal
                host = f"[{host}]"
            url = f"https://{host}:{s['https_port']}{request.url.path}"
            if request.url.query:
                url += "?" + request.url.query
            return RedirectResponse(url, status_code=307 if request.method != "GET" else 302)
    return await call_next(request)


@app.on_event("startup")
def regenerate_share_configs():
    """After an upgrade, rewrite smb.conf/exports from shares.json so new share templates apply."""
    if DEV:
        return
    try:
        for p in storage.load_pools():   # keep each volume's own record up to date (used when importing it later)
            storage.write_pool_meta(p)
        if shares.load():
            shares.write_configs(shares.load())
    except Exception as e:
        print("could not regenerate share configs:", e)


FRONTEND = os.path.abspath(os.environ.get("CYN_WEB", os.path.join(os.path.dirname(__file__), "..", "..", "frontend")))
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND, "index.html"))
