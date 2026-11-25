#!/bin/bash
# CairnOS alpha installer: turns a fresh minimal Debian 13 (trixie) or 12 (bookworm) x86-64
# install into a CairnOS NAS. Run as root:   bash install.sh
# Web UI afterwards: http://<this-box>:5000  (log in with your Debian user that has sudo, or root)
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/cairnos
NOSYSTEMD="${CAIRN_NO_SYSTEMD:-0}"   # used by CI/container tests

[ "$(id -u)" = 0 ] || { echo "Run as root (sudo bash install.sh)"; exit 1; }
[ "$(uname -m)" = x86_64 ] || { echo "CairnOS alpha supports x86-64 only"; exit 1; }
. /etc/os-release
case "${ID}:${VERSION_ID:-}" in debian:12|debian:13) ;; *) echo "Warning: tested on Debian 12/13, you have $PRETTY_NAME - continuing" ;; esac

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
  python3 python3-fastapi python3-uvicorn python3-psutil libpam0g ca-certificates curl sudo rsync util-linux acl \
  mdadm lvm2 btrfs-progs e2fsprogs gdisk parted smartmontools \
  samba smbclient nfs-kernel-server avahi-daemon \
  docker.io initramfs-tools
# optional packages whose names differ between Debian releases
for alts in "wsdd2 wsdd" "docker-compose docker-compose-plugin"; do
  for p in $alts; do
    if apt-cache show "$p" >/dev/null 2>&1; then apt-get install -y -q --no-install-recommends "$p" && break; fi
  done
done

echo "==> Installing CairnOS to $DEST"
mkdir -p "$DEST" /var/lib/cairn /etc/exports.d
rsync -a --delete --exclude .cairn-state --exclude __pycache__ "$SRC/backend" "$SRC/frontend" "$SRC/catalog" "$DEST/"
rsync -a "$SRC/rootfs/" /

echo "==> Configuring services"
getent group cairn-admin >/dev/null || groupadd cairn-admin
getent group synocommunity >/dev/null || groupadd -r synocommunity
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then usermod -aG cairn-admin,users "$SUDO_USER"; fi
# Samba: include the share file CairnOS manages
touch /etc/samba/cairn-shares.conf
grep -q cairn-shares.conf /etc/samba/smb.conf || sed -i '/^\[global\]/a \   include = /etc/samba/cairn-shares.conf\n   server min protocol = SMB2_10\n   vfs objects = btrfs\n   map to guest = never' /etc/samba/smb.conf
sed -i '/^\[homes\]/,/^\[/{s/^\(\s*browseable\s*=\s*\).*/\1no/}' /etc/samba/smb.conf || true
touch /etc/exports.d/cairn.exports
# /etc/issue shows the web address on the console
cat > /etc/issue <<'ISSUE'
CairnOS alpha \n  (\l)

  Web management:  http://\4:5000

ISSUE
cp "$SRC/systemd/cairn-ui.service" /etc/systemd/system/
echo "CairnOS 0.1.0-alpha" > /etc/cairnos-release

if [ "$NOSYSTEMD" != 1 ]; then
  systemctl daemon-reload
  systemctl enable --now cairn-ui smbd avahi-daemon docker
  systemctl enable --now wsdd2 2>/dev/null || systemctl enable --now wsdd 2>/dev/null || true
fi
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "CairnOS installed.  Open  http://${IP:-<this-box>}:5000  and sign in with ${SUDO_USER:-root}'s password."
