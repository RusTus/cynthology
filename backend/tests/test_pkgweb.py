import os

os.environ["CYN_DEV"] = "1"

from cynthology import pkgweb  # noqa: E402

ADMINER = {"webservice": {
    "services": [{"service": "adminer", "display_name": "Adminer", "type": "apache_php", "root": "adminer", "backend": 2,
                  "php": {"profile_name": "x", "backend": 13, "extensions": ["mysqli", "pgsql", "pdo_dblib", "evil_ext"],
                          "user": "sc-adminer", "group": "http"}}],
    "portals": [{"service": "adminer", "type": "alias", "name": "Adminer", "alias": "adminer"}],
    "pkg_dir_prepare": [{"source": "/var/packages/adminer/target/web", "target": "adminer", "mode": "0755",
                         "user": "sc-adminer", "group": "http"}]}}
ARIANG = {"webservice": {"services": [{"service": "ariang", "type": "static", "root": "ariang"}],
                         "portals": [{"service": "ariang", "type": "alias", "alias": "ariang"}],
                         "pkg_dir_prepare": [{"source": "/var/packages/ariang/target/web", "target": "ariang"}]}}


def test_parse_real_packages():
    a = pkgweb.parse_webservice("adminer", ADMINER)
    assert a["services"][0]["php"]["user"] == "sc-adminer"
    assert a["services"][0]["php"]["extensions"] == ["mysqli", "pgsql", "pdo_dblib"]      # unknown ones dropped
    assert a["portals"] == [{"service": "adminer", "alias": "adminer", "name": "Adminer"}]
    s = pkgweb.parse_webservice("ariang", ARIANG)
    assert s["services"][0]["type"] == "static" and s["prepare"][0]["target"] == "ariang"


def test_hostile_resource_is_ignored():
    bad = {"webservice": {
        "services": [{"service": "x;evil", "type": "static", "root": "a"},
                     {"service": "ok", "type": "nginx_php", "root": "../../etc",},
                     {"service": "rootphp", "type": "nginx_php", "root": "r", "php": {"user": "root",
                      "php_settings": {"auto_prepend_file": "/etc/passwd\\n", "memory_limit": "256M"}}}],
        "portals": [{"service": "rootphp", "type": "alias", "alias": "a b"}, {"service": "rootphp", "type": "alias", "alias": "good"}],
        "pkg_dir_prepare": [{"source": "/etc", "target": "r"}, {"source": "/var/packages/other/target/web", "target": "r"},
                            {"source": "/var/packages/mine/target/../../../etc", "target": "r"}]}}
    p = pkgweb.parse_webservice("mine", bad)
    assert [s["service"] for s in p["services"]] == ["rootphp"]
    assert p["services"][0]["php"]["user"] == "sc-mine"                                     # never root
    assert p["services"][0]["php"]["settings"] == {"memory_limit": "256M"}
    assert [x["alias"] for x in p["portals"]] == ["good"] and p["prepare"] == []


def test_render():
    st = {"adminer": pkgweb.parse_webservice("adminer", ADMINER), "ariang": pkgweb.parse_webservice("ariang", ARIANG)}
    ng = "\n".join(pkgweb.render_nginx(st))
    assert "location ^~ /ariang/ { alias" in ng and f"proxy_pass http://127.0.0.1:{pkgweb.APACHE_PORT}/adminer/;" in ng
    ap = pkgweb.render_apache(st)
    assert "Alias /adminer " in ap and "AllowOverride All" in ap and "ariang" not in ap and "Listen 127.0.0.1:" in ap
    pool = pkgweb.render_fpm_pool(st["adminer"]["services"][0])
    assert "user = sc-adminer" in pool and "listen = /run/php/cyn-pkg-adminer.sock" in pool and "open_basedir" in pool
