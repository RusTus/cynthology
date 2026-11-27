"""Login Portal: how the sign-in page looks, the NAS's own domain name, and HSTS.

  title / message / background / show_host  - shown on the sign-in page (public, read-only)
  domain                                    - e.g. nas.example.com: preferred in sharing links
  external_port                             - HTTPS port people reach from outside (router forward), 0 = same
  hsts                                      - tell browsers to only ever use HTTPS for that domain
HSTS covers a whole host name (every port), so it is only sent when the page was opened by the
configured domain name and the certificate is a trusted one - never on IP addresses or with the
self-signed certificate.
"""
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from . import settings
from .auth import require_admin
from .util import STATE_DIR

router = APIRouter(prefix="/api/portal", tags=["portal"])
BG = os.path.join(STATE_DIR, "portal-background.img")
BG_MAX = 6 * 1024 * 1024
BACKGROUNDS = ("default", "ocean", "dusk", "forest", "graphite", "aurora", "custom")
HOST_RE = re.compile(r"(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")
HSTS_VALUE = "max-age=31536000"


def get() -> dict:
    p = settings.get("portal") or {}
    return {"title": p.get("title", ""), "message": p.get("message", ""), "background": p.get("background", "default"),
            "show_host": p.get("show_host", True), "domain": p.get("domain", ""), "external_port": int(p.get("external_port") or 0),
            "hsts": bool(p.get("hsts"))}


@router.get("")
def public():
    """What the sign-in page needs (no sign-in: nothing private in here)."""
    p = get()
    return {k: p[k] for k in ("title", "message", "background", "show_host")} | {"has_background": os.path.exists(BG)}


@router.get("/background")
def background():
    from .account import _image_type
    try:
        with open(BG, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        raise HTTPException(404)
    return Response(data, media_type=_image_type(data) or "application/octet-stream",
                    headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


@router.get("/settings")
def get_settings(_=Depends(require_admin)):
    from . import tls
    return {**get(), "trusted_cert": tls.cert_info().get("kind") in ("letsencrypt", "uploaded"),
            "https_redirect": settings.get_all()["https_redirect"], "https_port": settings.get_all()["https_port"],
            "has_background": os.path.exists(BG)}


class Portal(BaseModel):
    title: str = ""
    message: str = ""
    background: str = "default"
    show_host: bool = True
    domain: str = ""
    external_port: int = 0
    hsts: bool = False


@router.put("/settings")
def put_settings(body: Portal, _=Depends(require_admin)):
    from . import tls
    title = re.sub(r"[\x00-\x1f\x7f]", "", body.title).strip()[:60]
    message = re.sub(r"[\x00-\x09\x0b-\x1f\x7f]", "", body.message).strip()[:400]
    if body.background not in BACKGROUNDS:
        raise HTTPException(400, "unknown background")
    if body.background == "custom" and not os.path.exists(BG):
        raise HTTPException(400, "upload a picture first")
    domain = body.domain.strip().lower().rstrip(".")
    if domain and not HOST_RE.fullmatch(domain):
        raise HTTPException(400, "domain: a full name like nas.example.com")
    if not 0 <= body.external_port <= 65535:
        raise HTTPException(400, "port: 1-65535 (0 = same as the HTTPS port)")
    if body.hsts:
        if not domain:
            raise HTTPException(400, "HSTS needs your domain name")
        if tls.cert_info().get("kind") not in ("letsencrypt", "uploaded"):
            raise HTTPException(400, "HSTS needs a trusted certificate for your domain (Control Panel > Security)")
    settings.update(portal={"title": title, "message": message, "background": body.background, "show_host": body.show_host,
                            "domain": domain, "external_port": body.external_port, "hsts": body.hsts})
    return get()


@router.put("/background")
async def put_background(request: Request, _=Depends(require_admin)):
    from .account import _image_type
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > BG_MAX:
            raise HTTPException(413, "picture too large (max. 6 MB)")
    if _image_type(data) is None:
        raise HTTPException(400, "not a JPEG, PNG or WebP picture")
    with open(BG + ".tmp", "wb") as f:
        f.write(data)
    os.replace(BG + ".tmp", BG)
    return {"ok": True}


@router.delete("/background")
def delete_background(_=Depends(require_admin)):
    try:
        os.remove(BG)
    except FileNotFoundError:
        pass
    p = get()
    if p["background"] == "custom":
        settings.update(portal={**(settings.get("portal") or {}), "background": "default"})
    return {"ok": True}


def hsts_header(request: Request) -> str | None:
    """HSTS for this response? Only over HTTPS, and only for the configured domain."""
    if request.url.scheme != "https":
        return None
    p = get()
    if p["hsts"] and p["domain"] and (request.url.hostname or "").lower() == p["domain"]:
        return HSTS_VALUE
    return None
