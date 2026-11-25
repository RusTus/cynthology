"""Cynthology management API + static web desktop.

Run (dev, no root, mocked disks):  CYN_DEV=1 uvicorn cynthology.main:app --reload --port 5000
Login in dev mode: admin / admin
"""
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import apps, auth, services, shares, spk, storage, sysinfo, users
from .util import DEV

app = FastAPI(title="Cynthology", version="0.1.0", docs_url="/api/docs" if DEV else None)
for r in (auth.router, sysinfo.router, storage.router, shares.router, users.router, services.router, apps.router, spk.router):
    app.include_router(r)

FRONTEND = os.path.abspath(os.environ.get("CYN_WEB", os.path.join(os.path.dirname(__file__), "..", "..", "frontend")))
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND, "index.html"))
