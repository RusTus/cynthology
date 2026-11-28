"""PHP 8.3, installed on request (Package Center > Installed > PHP 8.3 > Install PHP 8.3).

Debian 13 only ships PHP 8.4; some packages (ownCloud 11, ...) don't run on it. PHP 8.3 comes from
packages.sury.org - the repository of Ondřej Surý, who maintains PHP in Debian. Its signing key is
checked against the known fingerprint, and APT is pinned so that this repository can only ever
provide php8.3 packages (and php-common, the small shared package they need) - nothing else on the
system is taken from it. Uninstalling removes it.
Packages that ask for PHP 8.3 then get PHP-FPM 8.3 pools and /usr/local/bin/php83 -> php8.3.
"""
import os
import shutil
import subprocess
import tempfile

from fastapi import HTTPException

from .util import DEV

FINGERPRINT = "15058500A0235D97F5D10063B188E2B695BD4743"      # DEB.SURY.ORG Automatic Signing Key
KEY_URL = "https://packages.sury.org/php/apt.gpg"
KEYRING = "/usr/share/keyrings/cynthology-sury-php.gpg"
SOURCES = "/etc/apt/sources.list.d/cynthology-sury-php.sources"
PIN = "/etc/apt/preferences.d/cynthology-sury-php"
BASE = ["php8.3-fpm", "php8.3-cli", "php8.3-common", "php8.3-opcache"]
APT_ENV = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}


def installed() -> bool:
    return DEV or os.path.exists("/usr/sbin/php-fpm8.3")


def render_pin() -> str:
    """Only php8.3* may come from sury.org (pure; unit tested)."""
    return ("# Managed by Cynthology: only PHP 8.3 comes from packages.sury.org\n"
            "Package: *\nPin: origin packages.sury.org\nPin-Priority: -1\n\n"
            # (php-common: the shared PHP folders + session clean-up, from the same maintainer; 8.3 needs his newer one)
            "Package: php8.3 php8.3-* php-common\nPin: origin packages.sury.org\nPin-Priority: 500\n")


def _codename() -> str:
    for line in open("/etc/os-release"):
        if line.startswith("VERSION_CODENAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return "trixie"


def install(extensions: list[str] | None = None) -> None:
    if DEV or installed() and not extensions:
        return
    if not os.path.exists(KEYRING):
        with tempfile.TemporaryDirectory() as d:
            key = os.path.join(d, "apt.gpg")
            r = subprocess.run(["curl", "-fsSL", "--proto", "=https", "--max-time", "60", "-o", key, KEY_URL], capture_output=True, text=True)
            if r.returncode != 0:
                raise HTTPException(502, "couldn't download the PHP 8.3 repository key: " + r.stderr.strip()[-200:])
            out = subprocess.run(["gpg", "--show-keys", "--with-colons", key], capture_output=True, text=True, env={**os.environ, "GNUPGHOME": d}).stdout
            fprs = [ln.split(":")[9] for ln in out.splitlines() if ln.startswith("fpr:")]
            if FINGERPRINT not in fprs:
                raise HTTPException(502, "the PHP 8.3 repository key isn't the expected one - not installing")
            shutil.copy(key, KEYRING)
            os.chmod(KEYRING, 0o644)
    with open(SOURCES, "w") as f:
        f.write(f"# Managed by Cynthology (PHP 8.3)\nTypes: deb\nURIs: https://packages.sury.org/php/\nSuites: {_codename()}\n"
                f"Components: main\nSigned-By: {KEYRING}\n")
    with open(PIN, "w") as f:
        f.write(render_pin())
    subprocess.run(["apt-get", "update", "-q"], capture_output=True, env=APT_ENV, timeout=600)
    pkgs = BASE + [f"php8.3-{e}" for e in (extensions or [])]
    r = subprocess.run(["apt-get", "install", "-y", "-q", "--no-install-recommends", *pkgs], capture_output=True, text=True, env=APT_ENV, timeout=1800)
    if r.returncode != 0:
        raise HTTPException(500, "couldn't install PHP 8.3: " + (r.stderr or r.stdout).strip()[-400:])
    link = "/usr/local/bin/php83"
    if os.path.islink(link):
        os.remove(link)
    os.symlink("/usr/bin/php8.3", link)


def uninstall() -> None:
    if DEV:
        return
    subprocess.run(["apt-get", "purge", "-y", "-q", "php8.3*"], capture_output=True, env=APT_ENV, timeout=900)
    for f in (SOURCES, PIN, KEYRING):
        if os.path.exists(f):
            os.remove(f)
    subprocess.run(["apt-get", "update", "-q"], capture_output=True, env=APT_ENV, timeout=600)
    link = "/usr/local/bin/php83"
    if os.path.islink(link):
        os.remove(link)
        os.symlink("/usr/bin/php8.4", link)
