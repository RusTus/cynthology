"""Notifications: one place every alert goes through (Control Panel > Notifications).

notify(level, title, detail, event) writes the alert log, sends the email (if set up) and pushes it
to the phone services you added: ntfy (ntfy.sh or your own server), Telegram (a bot), Discord (a
channel webhook) and Pushover. Each service has its own minimum level (info / warning / critical).
Pushes are sent in parallel with a short timeout; a broken service never stops the others or the
thing that raised the alert.
"""
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_admin
from .util import DEV, STATE_DIR

router = APIRouter(prefix="/api/notify", tags=["notify"])
CFG = os.path.join(STATE_DIR, "notify.json")
LEVELS = {"info": 0, "warning": 1, "critical": 2}
TYPES = {"ntfy": "ntfy", "telegram": "Telegram", "discord": "Discord", "pushover": "Pushover",
         "gotify": "Gotify", "webhook": "Webhook (Home Assistant, n8n, Slack…)", "sms": "SMS (Twilio)"}
SECRETS = ("token", "bot_token", "webhook_url", "app_token", "user_key", "url", "secret", "auth_token")
UA = "Cynthology-Notify/1.0"
_sent: list[dict] = []           # dev mode: what would have been sent (tests look at it)


# ---------- storage ----------

def load() -> list[dict]:
    try:
        return json.load(open(CFG))
    except (OSError, ValueError):
        return []


def save(items: list[dict]) -> None:
    fd = os.open(CFG + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # tokens: root only
    with os.fdopen(fd, "w") as f:
        json.dump(items, f, indent=1)
    os.replace(CFG + ".new", CFG)


# ---------- senders ----------

def _post(url: str, data: bytes, headers: dict) -> None:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read(2000)
    except urllib.error.HTTPError as e:
        body = e.read(400).decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body.strip()[:200]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"can't reach the service: {e.reason}")


def build(ch: dict, level: str, title: str, detail: str) -> tuple[str, bytes, dict]:
    """(url, body, headers) for one push (pure; unit tested)."""
    host = socket.gethostname()
    full = f"[{host}] {title}"
    t = ch["type"]
    if t == "ntfy":
        url = ch.get("server", "https://ntfy.sh").rstrip("/") + "/" + urllib.parse.quote(ch["topic"])
        hdr = {"Title": full,
               "Priority": {"critical": "5", "warning": "4"}.get(level, "3"),
               "Tags": {"critical": "rotating_light", "warning": "warning"}.get(level, "information_source")}
        if not full.isascii():          # (HTTP headers are Latin-1: send the title RFC 2047-encoded)
            import base64
            hdr["Title"] = "=?UTF-8?B?" + base64.b64encode(full.encode()).decode() + "?="
        if ch.get("token"):
            hdr["Authorization"] = f"Bearer {ch['token']}"
        return url, (detail or title).encode(), hdr
    if t == "telegram":
        icon = {"critical": "🚨", "warning": "⚠️"}.get(level, "ℹ️")
        body = {"chat_id": ch["chat_id"], "text": f"{icon} {full}\n\n{detail}".strip()[:4000], "disable_web_page_preview": True}
        return f"https://api.telegram.org/bot{ch['bot_token']}/sendMessage", json.dumps(body).encode(), {"Content-Type": "application/json"}
    if t == "discord":
        color = {"critical": 0xDC2626, "warning": 0xF59E0B}.get(level, 0x3B82F6)
        body = {"username": "Cynthology", "embeds": [{"title": full[:256], "description": (detail or "")[:4000], "color": color}]}
        return ch["webhook_url"], json.dumps(body).encode(), {"Content-Type": "application/json"}
    if t == "pushover":
        body = urllib.parse.urlencode({"token": ch["app_token"], "user": ch["user_key"], "title": full[:250],
                                       "message": (detail or title)[:1024], "priority": {"critical": 1}.get(level, 0)}).encode()
        return "https://api.pushover.net/1/messages.json", body, {"Content-Type": "application/x-www-form-urlencoded"}
    if t == "gotify":
        url = ch["server"].rstrip("/") + "/message"
        body = {"title": full, "message": detail or title, "priority": {"critical": 8, "warning": 5}.get(level, 2)}
        return url, json.dumps(body).encode(), {"Content-Type": "application/json", "X-Gotify-Key": ch["app_token"]}
    if t == "webhook":
        if ch.get("format") == "slack":           # Slack, Mattermost, Rocket.Chat, Google Chat ("text")
            icon = {"critical": ":rotating_light:", "warning": ":warning:"}.get(level, ":information_source:")
            body = json.dumps({"text": f"{icon} *{full}*\n{detail}".strip()}).encode()
        else:                                      # plain JSON (Home Assistant, n8n, Node-RED, your own script)
            body = json.dumps({"host": host, "level": level, "title": title, "detail": detail, "time": int(time.time())}).encode()
        hdr = {"Content-Type": "application/json"}
        if ch.get("secret"):                      # so the receiver can check it really came from this NAS
            import hashlib
            import hmac
            hdr["X-Cynthology-Signature"] = "sha256=" + hmac.new(ch["secret"].encode(), body, hashlib.sha256).hexdigest()
        return ch["url"], body, hdr
    if t == "sms":
        text = f"{full}: {detail}" if detail else full
        body = urllib.parse.urlencode({"From": ch["from_number"], "To": ch["to_number"], "Body": text[:600]}).encode()
        import base64
        auth = base64.b64encode(f"{ch['account_sid']}:{ch['auth_token']}".encode()).decode()
        return (f"https://api.twilio.com/2010-04-01/Accounts/{ch['account_sid']}/Messages.json", body,
                {"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {auth}"})
    raise ValueError(f"unknown service {t}")


def send(ch: dict, level: str, title: str, detail: str) -> None:
    url, body, hdr = build(ch, level, title, detail)
    if DEV:
        _sent.append({"channel": ch.get("id"), "url": url, "level": level, "title": title})
        return
    _post(url, body, hdr)


def push(level: str, title: str, detail: str) -> None:
    """Send to every enabled service whose minimum level this alert reaches (in parallel)."""
    chans = [c for c in load() if c.get("enabled", True) and LEVELS.get(level, 1) >= LEVELS.get(c.get("min_level", "warning"), 1)]
    errors = []

    def one(c):
        try:
            send(c, level, title, detail)
        except Exception as e:
            errors.append((c, str(e)))
    threads = [threading.Thread(target=one, args=(c,)) for c in chans]
    for th in threads:
        th.start()
    for th in threads:
        th.join(15)
    if errors:
        from . import health
        health._log([{"level": "warning", "title": f"Could not send a notification to {TYPES.get(c['type'], c['type'])} ({c.get('name') or c['type']})",
                      "detail": err, "event": "notify", "time": int(time.time())} for c, err in errors])


def recipients(cfg: dict, extra: list[str] | None = None) -> str:
    """The alert email's To: the address set under Notifications plus administrators who opted in."""
    if extra is None:
        try:
            from .account import alert_recipients
            extra = alert_recipients()
        except Exception:
            extra = []
    return ", ".join(dict.fromkeys([x.strip() for x in (cfg.get("to") or "").split(",") if x.strip()] + extra))


def notify(level: str, title: str, detail: str = "", event: str = "alert", log: bool = True) -> None:
    """Every alert: log it, email it, push it. Never raises."""
    from . import health
    try:
        if log:
            health._log([{"level": level, "title": title, "detail": detail, "event": event, "time": int(time.time())}])
        cfg = health.load_smtp()
        try:
            from .account import alert_recipients      # (administrators who opted in under Personal)
            extra = alert_recipients()
        except Exception:
            extra = []
        to = recipients(cfg, extra)
        if cfg.get("enabled") and cfg.get("host") and to and LEVELS.get(level, 1) >= LEVELS.get(cfg.get("min_level", "info"), 0):
            try:
                health.send_mail(title, detail, {**cfg, "to": to})
            except Exception as e:
                health._log([{"level": "warning", "title": "Could not send notification email", "detail": str(e),
                              "event": "mail", "time": int(time.time())}])
        push(level, title, detail)
    except Exception as e:  # (alerts must never break what raised them)
        print("notify failed:", e)


# ---------- API ----------

class Channel(BaseModel):
    id: str = ""
    type: str
    name: str = ""
    enabled: bool = True
    min_level: str = "warning"
    server: str = "https://ntfy.sh"   # ntfy
    topic: str = ""                   # ntfy
    token: str = ""                   # ntfy access token (optional; empty = keep)
    bot_token: str = ""               # telegram (empty = keep)
    chat_id: str = ""                 # telegram
    webhook_url: str = ""             # discord (empty = keep)
    app_token: str = ""               # pushover (empty = keep)
    user_key: str = ""                # pushover (empty = keep)
    url: str = ""                     # webhook (empty = keep)
    format: str = "json"              # webhook: json | slack
    secret: str = ""                  # webhook signing secret (optional; empty = keep)
    account_sid: str = ""             # sms (Twilio)
    auth_token: str = ""              # sms (empty = keep)
    from_number: str = ""             # sms
    to_number: str = ""               # sms


def _public(c: dict) -> dict:
    return {k: v for k, v in c.items() if k not in SECRETS} | {f"has_{k}": bool(c.get(k)) for k in SECRETS}


def validate(c: dict) -> None:
    t = c["type"]
    if t not in TYPES:
        raise HTTPException(400, "unknown service")
    if c.get("min_level") not in LEVELS:
        raise HTTPException(400, "minimum level: info, warning or critical")
    c["name"] = re.sub(r"[\x00-\x1f\x7f]+", " ", c.get("name", ""))[:60].strip()
    if t == "ntfy":
        if not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+(/[A-Za-z0-9._~/-]*)?", c.get("server", "")):
            raise HTTPException(400, "ntfy server: e.g. https://ntfy.sh or your own https://ntfy.example.com")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", c.get("topic", "")):
            raise HTTPException(400, "ntfy topic: letters, digits, - and _ (pick something hard to guess - anyone who knows it can read it)")
        if c.get("token") and c["server"].startswith("http://"):
            raise HTTPException(400, "use https:// when sending an access token")
    elif t == "telegram":
        if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{30,64}", c.get("bot_token", "")):
            raise HTTPException(400, "Telegram bot token: the one @BotFather gave you, like 123456789:AA...")
        if not re.fullmatch(r"-?\d{1,20}|@[A-Za-z0-9_]{5,32}", c.get("chat_id", "")):
            raise HTTPException(400, "Telegram chat ID: a number (message @userinfobot) or @channelname")
    elif t == "discord":
        if not re.fullmatch(r"https://(discord|discordapp)\.com/api/webhooks/\d+/[A-Za-z0-9_-]+", c.get("webhook_url", "")):
            raise HTTPException(400, "Discord: paste the channel's webhook URL (Channel settings → Integrations → Webhooks)")
    elif t == "pushover":
        if not re.fullmatch(r"[A-Za-z0-9]{30}", c.get("app_token", "")) or not re.fullmatch(r"[A-Za-z0-9]{30}", c.get("user_key", "")):
            raise HTTPException(400, "Pushover: the application API token and your user key (30 letters and digits each)")
    elif t == "gotify":
        if not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+(/[A-Za-z0-9._~/-]*)?", c.get("server", "")):
            raise HTTPException(400, "Gotify server: e.g. https://gotify.example.com")
        if not re.fullmatch(r"[A-Za-z0-9._-]{8,64}", c.get("app_token", "")):
            raise HTTPException(400, "Gotify: the application token (Apps → create application)")
    elif t == "webhook":
        if not re.fullmatch(r"https?://[^\s\x00-\x1f]{3,2000}", c.get("url", "")):
            raise HTTPException(400, "Webhook: the full address, starting with https:// (or http:// on your own network)")
        if c.get("format", "json") not in ("json", "slack"):
            raise HTTPException(400, "format: json or slack")
        if c.get("secret") and len(c["secret"]) > 200:
            raise HTTPException(400, "signing secret: up to 200 characters")
    elif t == "sms":
        if not re.fullmatch(r"AC[0-9a-fA-F]{32}", c.get("account_sid", "")) or not re.fullmatch(r"[0-9a-fA-F]{32}", c.get("auth_token", "")):
            raise HTTPException(400, "Twilio: your Account SID (AC…) and Auth Token, from the Twilio console")
        for k in ("from_number", "to_number"):
            if not re.fullmatch(r"\+[1-9]\d{6,14}", c.get(k, "")):
                raise HTTPException(400, "phone numbers in international format, e.g. +14155550123")


@router.get("")
def get_channels(_=Depends(require_admin)):
    return {"types": TYPES, "channels": [_public(c) for c in load()]}


@router.post("")
def put_channel(body: Channel, _=Depends(require_admin)):
    items = load()
    old = next((c for c in items if c["id"] == body.id), None) if body.id else None
    if body.id and not old:
        raise HTTPException(404, "no such service")
    c = body.model_dump()
    c["id"] = body.id or secrets.token_hex(3)
    for k in SECRETS:                         # empty = keep the saved one
        if not c[k] and old:
            c[k] = old.get(k, "")
    keep = {"ntfy": ("server", "topic", "token"), "telegram": ("bot_token", "chat_id"),
            "discord": ("webhook_url",), "pushover": ("app_token", "user_key"), "gotify": ("server", "app_token"),
            "webhook": ("url", "format", "secret"), "sms": ("account_sid", "auth_token", "from_number", "to_number")}.get(c["type"], ())
    c = {k: v for k, v in c.items() if k in ("id", "type", "name", "enabled", "min_level") + keep}
    validate(c)
    items = [x for x in items if x["id"] != c["id"]] + [c]
    save(items)
    return _public(c)


@router.delete("/{cid}")
def delete_channel(cid: str, _=Depends(require_admin)):
    save([c for c in load() if c["id"] != cid])
    return {"ok": True}


@router.post("/{cid}/test")
def test_channel(cid: str, _=Depends(require_admin)):
    c = next((x for x in load() if x["id"] == cid), None)
    if not c:
        raise HTTPException(404, "no such service")
    try:
        send(c, "info", "Test notification", "If you can read this, Cynthology can reach you here.")
    except Exception as e:
        raise HTTPException(502, str(e))
    return {"ok": True}
