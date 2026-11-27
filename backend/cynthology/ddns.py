"""DDNS: keep a host name pointing at this NAS's public IP address (DSM: External Access > DDNS).

Providers: DuckDNS, Cloudflare (API token), and the common "dyndns2" protocol used by No-IP, Dynu
and many others (preset or your own server). cynthology-ddns.timer checks every 10 minutes and
updates only when the address changed (or once a day, so free services don't expire the name).
"""
import base64
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_admin
from .util import DEV, STATE_DIR

router = APIRouter(prefix="/api/ddns", tags=["ddns"])
CFG = os.path.join(STATE_DIR, "ddns.json")
UA = "Cynthology-DDNS/1.0"
DYNDNS2 = {"noip": ("No-IP", "dynupdate.no-ip.com"), "dynu": ("Dynu", "api.dynu.com"),
           "custom": ("Other (dyndns2 protocol)", "")}
PROVIDERS = {"duckdns": "DuckDNS", "cloudflare": "Cloudflare", **{k: v[0] for k, v in DYNDNS2.items()}}
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)
SECRET_FIELDS = ("token", "password")


class Entry(BaseModel):
    id: str = ""
    provider: str
    hostname: str                  # e.g. mynas.duckdns.org
    username: str = ""             # dyndns2
    password: str = ""             # dyndns2 (empty = keep)
    token: str = ""                # duckdns / cloudflare (empty = keep)
    server: str = ""               # custom dyndns2 server
    enabled: bool = True


def load() -> list[dict]:
    try:
        return json.load(open(CFG))
    except (OSError, ValueError):
        return []


def save(items: list[dict]) -> None:
    with open(CFG + ".new", "w") as f:
        json.dump(items, f, indent=1)
    os.chmod(CFG + ".new", 0o600)
    os.replace(CFG + ".new", CFG)


def public_ip() -> str:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://ipv4.icanhazip.com"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            ip = urllib.request.urlopen(req, timeout=10).read().decode().strip()
            if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
                return ip
        except Exception:
            continue
    raise RuntimeError("couldn't find this network's public IP address (no internet?)")


def _get(url: str, headers: dict | None = None, data: bytes | None = None, method: str | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def update(e: dict, ip: str) -> str:
    """Point the name at ip. Returns a short status; raises RuntimeError with a readable reason."""
    host = e["hostname"]
    if e["provider"] == "duckdns":
        sub = host.removesuffix(".duckdns.org")
        code, body = _get("https://www.duckdns.org/update?" + urllib.parse.urlencode({"domains": sub, "token": e["token"], "ip": ip}))
        if body.strip().startswith("OK"):
            return "updated"
        raise RuntimeError("DuckDNS refused the update - check the domain and token")
    if e["provider"] == "cloudflare":
        auth = {"Authorization": f"Bearer {e['token']}", "Content-Type": "application/json"}
        api = "https://api.cloudflare.com/client/v4"
        parts = host.split(".")
        zone = None
        for i in range(len(parts) - 1):          # find the zone: try a.b.example.com, b.example.com, example.com
            code, body = _get(f"{api}/zones?" + urllib.parse.urlencode({"name": ".".join(parts[i:])}), auth)
            res = json.loads(body).get("result") or []
            if res:
                zone = res[0]["id"]
                break
        if not zone:
            raise RuntimeError("Cloudflare: no zone found for this name (or the token can't see it)")
        code, body = _get(f"{api}/zones/{zone}/dns_records?" + urllib.parse.urlencode({"type": "A", "name": host}), auth)
        recs = json.loads(body).get("result") or []
        payload = json.dumps({"type": "A", "name": host, "content": ip, "ttl": 300, "proxied": False}).encode()
        if recs:
            if recs[0]["content"] == ip:
                return "no change"
            code, body = _get(f"{api}/zones/{zone}/dns_records/{recs[0]['id']}", auth, payload, "PUT")
        else:
            code, body = _get(f"{api}/zones/{zone}/dns_records", auth, payload, "POST")
        if json.loads(body).get("success"):
            return "updated"
        errs = "; ".join(x.get("message", "") for x in json.loads(body).get("errors", []))
        raise RuntimeError(f"Cloudflare: {errs or 'update failed'} (the token needs Zone.DNS edit permission)")
    # dyndns2
    server = e.get("server") if e["provider"] == "custom" else DYNDNS2[e["provider"]][1]
    basic = base64.b64encode(f"{e['username']}:{e['password']}".encode()).decode()
    code, body = _get(f"https://{server}/nic/update?" + urllib.parse.urlencode({"hostname": host, "myip": ip}),
                      {"Authorization": f"Basic {basic}"})
    word = body.strip().split()[0] if body.strip() else str(code)
    if word in ("good", "nochg"):
        return "updated" if word == "good" else "no change"
    reasons = {"badauth": "wrong username or password", "nohost": "this host name doesn't exist in the account",
               "notfqdn": "the host name isn't complete", "abuse": "the provider blocked the account for too many updates",
               "911": "the provider has a problem - will retry", "!donator": "this feature needs a paid account"}
    raise RuntimeError(f"{PROVIDERS.get(e['provider'], 'Provider')}: {reasons.get(word, body.strip()[:120] or f'HTTP {code}')}")


def run_all(force: bool = False) -> list[dict]:
    items = load()
    if not any(e.get("enabled", True) for e in items):
        return items
    try:
        ip = "203.0.113.7" if DEV else public_ip()
    except RuntimeError as err:
        for e in items:
            e.update(last_error=str(err), last_check=int(time.time()))
        save(items)
        return items
    for e in items:
        if not e.get("enabled", True):
            continue
        stale = time.time() - e.get("last_update", 0) > 24 * 3600
        e["last_check"] = int(time.time())
        if not force and e.get("ip") == ip and not stale and not e.get("last_error"):
            continue
        try:
            e["status"] = "updated (dev)" if DEV else update(e, ip)
            e.update(ip=ip, last_update=int(time.time()), last_error=None)
        except Exception as err:
            e["last_error"] = str(err)
    save(items)
    return items


def _public(e: dict) -> dict:
    return {k: v for k, v in e.items() if k not in SECRET_FIELDS} | {f"has_{k}": bool(e.get(k)) for k in SECRET_FIELDS}


@router.get("")
def get_ddns(_=Depends(require_admin)):
    return {"providers": PROVIDERS, "entries": [_public(e) for e in load()]}


@router.post("")
def put_entry(body: Entry, _=Depends(require_admin)):
    if body.provider not in PROVIDERS:
        raise HTTPException(400, "unknown provider")
    body.hostname = body.hostname.strip().lower()
    if not HOST_RE.match(body.hostname):
        raise HTTPException(400, "enter the full host name, e.g. mynas.duckdns.org")
    if body.provider == "duckdns" and not body.hostname.endswith(".duckdns.org"):
        raise HTTPException(400, "DuckDNS names end in .duckdns.org")
    if body.provider == "custom" and not HOST_RE.match(body.server or ""):
        raise HTTPException(400, "enter the provider's update server, e.g. members.dyndns.org")
    items = load()
    old = next((e for e in items if e["id"] == body.id), {}) if body.id else {}
    e = body.model_dump()
    e["id"] = body.id or secrets.token_hex(3)
    for k in SECRET_FIELDS:                 # empty secret = keep the saved one
        if not e[k]:
            e[k] = old.get(k, "")
    if body.provider in ("duckdns", "cloudflare") and not e["token"]:
        raise HTTPException(400, "enter the token")
    if body.provider in DYNDNS2 and not (e["username"] and e["password"]):
        raise HTTPException(400, "enter the username and password")
    items = [x for x in items if x["id"] != e["id"]] + [{**old, **e, "ip": None, "last_error": None}]
    save(items)
    run_all(force=True)
    return _public(next(x for x in load() if x["id"] == e["id"]))


@router.delete("/{eid}")
def delete_entry(eid: str, _=Depends(require_admin)):
    save([e for e in load() if e["id"] != eid])
    return {"ok": True}


@router.post("/refresh")
def refresh(_=Depends(require_admin)):
    return [_public(e) for e in run_all(force=True)]


if __name__ == "__main__":
    for e in run_all():
        print(e["hostname"], e.get("ip"), e.get("last_error") or e.get("status", ""))
