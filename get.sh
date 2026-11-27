#!/bin/bash
# Cynthology bootstrap installer.
#
# Start from a clean, minimal Debian 13 (trixie) x86-64 install, then run as root:
#
#   wget -qO- https://raw.githubusercontent.com/RusTus/cynthology/main/get.sh | sudo bash
#
# Options (environment variables):
#   CYN_VERSION=v0.2.0-alpha   install a specific release instead of the latest
#   CYN_CHANNEL=main           install the current development code from the main branch
set -euo pipefail

REPO="RusTus/cynthology"
die() { echo "Error: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root:  wget -qO- https://raw.githubusercontent.com/$REPO/main/get.sh | sudo bash"
[ "$(uname -m)" = x86_64 ] || die "Cynthology needs an x86-64 (amd64) machine."
. /etc/os-release
if [ "${ID:-}" != debian ] || [ "${VERSION_ID:-}" != 13 ]; then
  echo "Cynthology is built for a clean Debian 13 (trixie) install. This system is: ${PRETTY_NAME:-unknown}."
  [ "${CYN_FORCE:-0}" = 1 ] || die "set CYN_FORCE=1 to try anyway."
fi
if [ -e /etc/cynthology-release ]; then
  echo "Cynthology $(cat /etc/cynthology-release) is already installed - upgrading in place."
fi
# a desktop environment would fight over ports, networking and power management
if dpkg -l 2>/dev/null | grep -qE '^ii\s+(gnome-shell|plasma-desktop|xfce4-session|lxqt-session|cinnamon) '; then
  echo "Warning: a desktop environment is installed. A minimal Debian install (no desktop) is recommended."
fi

command -v curl >/dev/null || { apt-get update -q && apt-get install -y -q curl ca-certificates; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
if [ "${CYN_CHANNEL:-}" = main ]; then
  URL="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
elif [ -n "${CYN_VERSION:-}" ]; then
  URL="https://github.com/$REPO/releases/download/$CYN_VERSION/cynthology-install.tar.gz"
else
  URL="https://github.com/$REPO/releases/latest/download/cynthology-install.tar.gz"
fi

echo "==> Downloading $URL"
curl -fL --proto =https --proto-redir =https --retry 3 -o "$TMP/cyn.tar.gz" "$URL"
if [ "${CYN_CHANNEL:-}" != main ]; then
  curl -fsL --proto =https --proto-redir =https --retry 3 -o "$TMP/cyn.tar.gz.sha256" "$URL.sha256" \
    && (cd "$TMP" && sed 's/cynthology-install.tar.gz/cyn.tar.gz/' cyn.tar.gz.sha256 | sha256sum -c --quiet -) \
    || die "checksum verification failed"
fi
mkdir "$TMP/src"
tar xzf "$TMP/cyn.tar.gz" -C "$TMP/src" --strip-components=1
[ -f "$TMP/src/install.sh" ] || die "download does not contain install.sh"

ADMIN="${SUDO_USER:-}"
SUDO_USER="$ADMIN" bash "$TMP/src/install.sh"
