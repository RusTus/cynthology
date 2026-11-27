"""HTTPS certificate management: self-signed (default) or an uploaded certificate."""
import datetime
import ipaddress
import os
import socket
import ssl
import subprocess
import tempfile

import psutil
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import settings
from .auth import require_admin
from .util import DEV, STATE_DIR, run

router = APIRouter(prefix="/api/security", tags=["security"])
TLS_DIR = os.path.join(STATE_DIR, "tls") if DEV else "/etc/cynthology/tls"
CERT = os.path.join(TLS_DIR, "cert.pem")
KEY = os.path.join(TLS_DIR, "key.pem")
KIND = os.path.join(TLS_DIR, "kind")  # "self-signed" | "uploaded" | "letsencrypt"


def local_ips() -> list[str]:
    ips = []
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family in (socket.AF_INET, socket.AF_INET6):
                ip = a.address.split("%")[0]
                try:
                    if not ipaddress.ip_address(ip).is_link_local:
                        ips.append(ip)
                except ValueError:
                    pass
    return sorted(set(ips))


def generate_self_signed() -> None:
    """10-year self-signed cert valid for hostname, hostname.local, localhost and all local IPs."""
    os.makedirs(TLS_DIR, mode=0o700, exist_ok=True)
    host = socket.gethostname()
    san = [f"DNS:{host}", f"DNS:{host}.local", "DNS:localhost"] + [f"IP:{ip}" for ip in local_ips()]
    with tempfile.TemporaryDirectory() as d:
        key, crt = os.path.join(d, "k.pem"), os.path.join(d, "c.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-days", "3650", "-keyout", key, "-out", crt,
                        "-subj", f"/CN={host}/O=Cynthology self-signed",
                        "-addext", "subjectAltName=" + ",".join(san),
                        "-addext", "basicConstraints=critical,CA:FALSE",
                        "-addext", "extendedKeyUsage=serverAuth"],
                       check=True, capture_output=True)
        _install(crt, key, "self-signed")


def _install(crt: str, key: str, kind: str) -> None:
    os.makedirs(TLS_DIR, mode=0o700, exist_ok=True)
    for src, dst, mode in ((crt, CERT, 0o644), (key, KEY, 0o600)):
        with open(src, "rb") as f:
            data = f.read()
        tmp = dst + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
    with open(KIND, "w") as f:
        f.write(kind)


def ensure_cert() -> bool:
    """Create a self-signed cert if none exists. Returns False if openssl is unavailable."""
    if os.path.exists(CERT) and os.path.exists(KEY):
        return True
    try:
        generate_self_signed()
        return True
    except (OSError, subprocess.CalledProcessError) as e:
        print("could not create TLS certificate:", e)
        return False


def cert_info() -> dict:
    if not os.path.exists(CERT):
        return {"present": False}
    out = subprocess.run(["openssl", "x509", "-in", CERT, "-noout", "-subject", "-issuer", "-enddate",
                          "-ext", "subjectAltName"], capture_output=True, text=True).stdout
    info = {"present": True, "kind": open(KIND).read().strip() if os.path.exists(KIND) else "unknown", "san": []}
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("subject="):
            info["subject"] = line.split("=", 1)[1].strip()
        elif line.startswith("issuer="):
            info["issuer"] = line.split("=", 1)[1].strip()
        elif line.startswith("notAfter="):
            exp = datetime.datetime.strptime(line.split("=", 1)[1].strip(), "%b %d %H:%M:%S %Y %Z")
            info["expires"] = exp.strftime("%Y-%m-%d")
            info["days_left"] = (exp - datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)).days
        elif "Subject Alternative Name" in line and i + 1 < len(lines):
            info["san"] = [s.strip() for s in lines[i + 1].split(",")]
    return info


def restart_soon() -> None:
    """Restart the UI service shortly after the HTTP response has been sent."""
    run(["systemd-run", "--on-active=2", "--unit=cynthology-ui-restart", "--collect",
         "systemctl", "restart", "cynthology-ui"], check=False)


@router.get("")
def security(_=Depends(require_admin)):
    s = settings.get_all()
    return {"cert": cert_info(), "https_redirect": s["https_redirect"],
            "http_port": s["http_port"], "https_port": s["https_port"]}


class SecuritySettings(BaseModel):
    https_redirect: bool


@router.put("")
def put_security(body: SecuritySettings, _=Depends(require_admin)):
    settings.update(https_redirect=body.https_redirect)
    return {"ok": True}


@router.post("/cert/self-signed")
def regenerate(_=Depends(require_admin)):
    generate_self_signed()
    restart_soon()
    return {"ok": True, "cert": cert_info(), "note": "The web server restarts in a few seconds."}


class Upload(BaseModel):
    cert: str     # PEM: certificate followed by any intermediates
    key: str      # PEM private key (unencrypted)


@router.post("/cert/upload")
def upload(body: Upload, _=Depends(require_admin)):
    with tempfile.TemporaryDirectory() as d:
        crt, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
        with open(crt, "w") as f:
            f.write(body.cert.strip() + "\n")
        with open(key, "w") as f:
            f.write(body.key.strip() + "\n")
        try:
            ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(crt, key)
        except (ssl.SSLError, OSError) as e:
            raise HTTPException(400, f"certificate and key don't form a valid pair: {e}")
        _install(crt, key, "uploaded")
    restart_soon()
    return {"ok": True, "cert": cert_info(), "note": "The web server restarts in a few seconds."}


# ---------- Let's Encrypt (certbot) ----------
# HTTP-01 validation: Let's Encrypt connects to http://<name>/.well-known/acme-challenge/ on
# port 80, so the router must forward port 80 to this NAS. If Web Station runs, its nginx serves
# the challenge files (webroot); otherwise certbot briefly listens on port 80 itself (standalone).
# Renewal: Debian's certbot.timer; the deploy hook installs each new certificate.

ACME_WEBROOT = os.path.join(STATE_DIR, "acme") if DEV else "/var/lib/cynthology/acme"
LE_NAME = "cynthology"


class LetsEncrypt(BaseModel):
    domains: list[str]
    email: str = ""


def _port_listener(port: int) -> str | None:
    for c in psutil.net_connections(kind="tcp"):
        if c.status == "LISTEN" and c.laddr and c.laddr.port == port:
            try:
                return psutil.Process(c.pid).name() if c.pid else "another program"
            except psutil.Error:
                return "another program"
    return None


@router.post("/cert/letsencrypt")
def letsencrypt(body: LetsEncrypt, _=Depends(require_admin)):
    import re
    import shutil
    from . import containers
    doms = [d.strip().lower() for d in body.domains if d.strip()]
    if not doms or len(doms) > 20:
        raise HTTPException(400, "enter the name(s) the certificate is for, e.g. mynas.duckdns.org")
    for d in doms:
        if not re.match(r"^(?=.{1,253}$)(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", d) or d.startswith("*."):
            raise HTTPException(400, f"{d}: enter a public DNS name (no IP addresses or wildcards)")
        if d.endswith((".local", ".lan", ".home", ".internal")):
            raise HTTPException(400, f"{d} is a local name - Let's Encrypt can only certify names in public DNS")
    if body.email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", body.email):
        raise HTTPException(400, "bad email address")
    if DEV:
        return {"job": None}
    if not shutil.which("certbot"):
        raise HTTPException(400, "certbot isn't installed - update Cynthology (Control Panel > Update)")
    holder = _port_listener(80)
    if holder and holder != "nginx":
        raise HTTPException(400, f"port 80 is used by {holder}; Let's Encrypt needs it to check the name")
    cmd = ["certbot", "certonly", "--non-interactive", "--agree-tos", "--cert-name", LE_NAME, "--keep-until-expiring",
           "--expand", "--deploy-hook", "/usr/lib/cynthology/le-deploy"]
    cmd += ["-m", body.email] if body.email else ["--register-unsafely-without-email"]
    if holder == "nginx":
        os.makedirs(os.path.join(ACME_WEBROOT, ".well-known", "acme-challenge"), exist_ok=True)
        cmd += ["--webroot", "-w", ACME_WEBROOT]
    else:
        cmd += ["--standalone", "--preferred-challenges", "http"]
    for d in doms:
        cmd += ["-d", d]
    return {"job": containers.start_job("Let's Encrypt certificate", cmd)}
