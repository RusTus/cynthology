import os

os.environ["CYN_DEV"] = "1"

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from cynthology import webstation as ws  # noqa: E402


def site(**kw):
    base = {"id": "", "name": "Site", "enabled": True, "type": "port", "hostname": "", "http_port": 80, "https_port": 443,
            "redirect_https": False, "kind": "php", "folder": "", "proxy": ""}
    base.update(kw)
    return base


def test_render_php_name_and_proxy_sites():
    st = {"php": dict(ws.PHP_DEFAULTS), "sites": [
        site(id="a", name="Default"),
        site(id="b", name="Blog", type="name", hostname="blog.example.com", kind="static", folder="blog"),
        site(id="c", name="TV", type="name", hostname="tv.example.com", https_port=None, kind="proxy", proxy="http://127.0.0.1:8096"),
        site(id="d", name="Off", enabled=False, http_port=81, https_port=None)]}
    conf = ws.render(st, "/volume1/web", "8.4", ipv6=True)
    assert conf.count("listen 80 default_server;") == 1 and "listen [::]:443 ssl default_server;" in conf
    assert "fastcgi_pass unix:/run/php/php8.4-fpm.sock;" in conf
    assert "root /volume1/web/blog;" in conf and "server_name blog.example.com;" in conf
    assert "proxy_pass http://127.0.0.1:8096;" in conf and "map $http_upgrade" in conf
    assert "81" not in conf
    assert "[::]" not in ws.render(st, "/volume1/web", "8.4", ipv6=False)


def test_name_based_site_gets_default_when_alone():
    st = {"php": dict(ws.PHP_DEFAULTS), "sites": [site(type="name", hostname="a.example.com", https_port=None)]}
    assert "listen 80 default_server;" in ws.render(st, "/w", "8.4", ipv6=False)


def test_validation():
    others = [site(id="a", name="Default")]
    with pytest.raises(HTTPException):
        ws.validate(ws.Site(**site(name="Two")), others)                       # same ports, both port-based
    with pytest.raises(HTTPException):
        ws.validate(ws.Site(**site(name="X", http_port=5000, https_port=None)), [])
    with pytest.raises(HTTPException):
        ws.validate(ws.Site(**site(name="X", folder="../etc")), [])
    with pytest.raises(HTTPException):
        ws.validate(ws.Site(**site(name="X", kind="proxy", proxy="http://x;evil")), [])
    with pytest.raises(HTTPException):
        ws.validate(ws.Site(**site(name="X", type="name", hostname="bad name;")), [])
    ws.validate(ws.Site(**site(name="Blog", type="name", hostname="blog.example.com")), others)
    ws.validate(ws.Site(**site(name="Own", http_port=8080, https_port=8443)), others)


def test_php_ini():
    ini = ws.php_ini({**ws.PHP_DEFAULTS, "display_errors": True})
    assert "upload_max_filesize = 512M" in ini and "display_errors = On" in ini
