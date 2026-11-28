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
# Release signing key (minisign public key). When set, only releases signed with the matching
# secret key are installed - a tampered or unsigned download is refused. See SIGNING.md.
CYN_RELEASE_KEY="RWQHL7Rt9zntymIp3Nx7OIwucg8wXizgbOGZRXDiBW/c0pdLUgWRcCZA"
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
if [ -n "$CYN_RELEASE_KEY" ] && [ "${CYN_CHANNEL:-}" != main ]; then
  command -v minisign >/dev/null || { apt-get update -q >/dev/null; apt-get install -y -q minisign >/dev/null; } \
    || die "could not install minisign to check the release signature"
  curl -fsL --proto =https --proto-redir =https --retry 3 -o "$TMP/cyn.tar.gz.minisig" "$URL.minisig" \
    || die "this release is not signed - refusing to install it (see SIGNING.md)"
  OUT=$(minisign -V -P "$CYN_RELEASE_KEY" -m "$TMP/cyn.tar.gz" -x "$TMP/cyn.tar.gz.minisig" 2>&1) \
    || die "the release signature is NOT valid - the download may have been tampered with. Nothing was installed."
  # the signed comment names the version: never go back to an older signed release unasked
  SIGNED=$(printf '%s\n' "$OUT" | sed -n 's/^Trusted comment: cynthology \([^ ]*\).*/\1/p')
  [ -n "$SIGNED" ] || die "the release signature has no version in it"
  if [ -n "${CYN_VERSION:-}" ] && [ "${CYN_VERSION#v}" != "$SIGNED" ]; then
    die "asked for $CYN_VERSION but the signed file is version $SIGNED"
  fi
  CUR=$(cat /etc/cynthology-release 2>/dev/null || true)
  case "$CUR" in [0-9]*) ;; *) CUR="" ;; esac           # (development installs have no version number)
  if [ -n "$CUR" ] && [ "$CUR" != "$SIGNED" ] && [ "$(printf '%s\n%s\n' "$CUR" "$SIGNED" | sort -V | tail -1)" = "$CUR" ] \
     && [ "${CYN_ALLOW_DOWNGRADE:-0}" != 1 ]; then
    die "$SIGNED is older than the installed $CUR (set CYN_ALLOW_DOWNGRADE=1 to go back on purpose)"
  fi
  echo "==> Signature OK: Cynthology $SIGNED, signed with the Cynthology release key"
fi
mkdir "$TMP/src"
tar xzf "$TMP/cyn.tar.gz" -C "$TMP/src" --strip-components=1
[ -f "$TMP/src/install.sh" ] || die "download does not contain install.sh"

ADMIN="${SUDO_USER:-}"
# Rollback point: the installed release and its settings are saved first (the newest 2 are kept). If the
# new version doesn't come up, the previous one is put back by itself; the Update page can also go back.
POINT=""
if [ -x /usr/lib/cynthology/rollback ] && [ "${CYN_NO_ROLLBACK:-0}" != 1 ] && [ -e /etc/cynthology-release ]; then
  POINT=$(/usr/lib/cynthology/rollback save "${SIGNED:-new}" | tee /dev/stderr | sed -n 's/^POINT=//p') || POINT=""
fi
rc=0
SUDO_USER="$ADMIN" bash "$TMP/src/install.sh" || rc=$?
if [ -n "$POINT" ]; then
  up=0
  if [ $rc = 0 ]; then
    echo "==> Checking that the new version answers"
    for _ in $(seq 1 36); do
      code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 https://127.0.0.1:5001/api/portal 2>/dev/null || true)
      [ "$code" = 200 ] && { up=1; break; }
      sleep 5
    done
  fi
  if [ $up != 1 ]; then
    echo "==> The new version didn't start properly (install rc=$rc) - going back to the previous one"
    echo "rolled-back" > /var/lib/cynthology/update-result      # (read when the web UI starts again)
    /usr/lib/cynthology/rollback restore "$(basename "$POINT")" \
      || { rm -f /var/lib/cynthology/update-result; die "rolling back failed too - see this log"; }
    exit 1
  fi
fi
