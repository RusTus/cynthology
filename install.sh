#!/bin/bash
# Cynthology alpha installer: turns a fresh minimal Debian 13 (trixie) or 12 (bookworm) x86-64
# install into a Cynthology NAS. Run as root:   bash install.sh
# Web UI afterwards: http://<this-box>:5000  (log in with your Debian user that has sudo, or root)
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/cynthology
NOSYSTEMD="${CYN_NO_SYSTEMD:-0}"   # used by CI/container tests
CHROOT="${CYN_CHROOT:-0}"         # set by the ISO installer (late_command): enable services, don't start them

[ "$(id -u)" = 0 ] || { echo "Run as root (sudo bash install.sh)"; exit 1; }
[ "$(uname -m)" = x86_64 ] || { echo "Cynthology alpha supports x86-64 only"; exit 1; }
. /etc/os-release
case "${ID}:${VERSION_ID:-}" in debian:12|debian:13) ;; *) echo "Warning: tested on Debian 12/13, you have $PRETTY_NAME - continuing" ;; esac

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
  python3 python3-fastapi python3-uvicorn python3-psutil libpam0g ca-certificates curl sudo rsync util-linux acl openssl \
  mdadm lvm2 btrfs-progs e2fsprogs gdisk parted smartmontools \
  samba smbclient nfs-kernel-server avahi-daemon \
  docker.io initramfs-tools
# optional packages whose names differ between Debian releases
for alts in "wsdd2 wsdd" "docker-compose docker-compose-plugin"; do
  for p in $alts; do
    if apt-cache show "$p" >/dev/null 2>&1; then apt-get install -y -q --no-install-recommends "$p" && break; fi
  done
done

# Virtual machine guest tools (clean shutdown from the hypervisor, IP shown in its console)
VIRT=$(systemd-detect-virt 2>/dev/null || true)
VENDOR=$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || true)
case "$VIRT:$VENDOR" in
  vmware*|*VMware*) apt-get install -y -q open-vm-tools ;;
  kvm*|qemu*|*QEMU*) apt-get install -y -q qemu-guest-agent ;;
  microsoft*|*Microsoft*) apt-get install -y -q hyperv-daemons ;;
esac

echo "==> Installing Cynthology to $DEST"
mkdir -p "$DEST" /var/lib/cynthology /etc/exports.d
rsync -a --delete --exclude .cyn-state --exclude __pycache__ "$SRC/backend" "$SRC/frontend" "$SRC/catalog" "$DEST/"
rsync -a "$SRC/rootfs/" /

echo "==> Configuring services"
getent group cynthology-admin >/dev/null || groupadd cynthology-admin
getent group synocommunity >/dev/null || groupadd -r synocommunity
# the installing user (or, from the ISO, the first user created) becomes a Cynthology admin
ADMIN_USER="${SUDO_USER:-$(getent passwd 1000 | cut -d: -f1)}"
if [ -n "$ADMIN_USER" ] && [ "$ADMIN_USER" != root ]; then usermod -aG cynthology-admin,users,sudo "$ADMIN_USER"; fi
# Samba: include the share file Cynthology manages
touch /etc/samba/cyn-shares.conf
# Global options go right after [global]; the include must be the LAST line, otherwise any
# [global] lines after it would be parsed as options of the last share in the included file.
sed -i -e '/^\s*include = \/etc\/samba\/cyn-shares.conf/d' -e '/^\s*# Cynthology:/d' \
       -e '/^\s*server min protocol = SMB2_10$/d' -e '/^\s*vfs objects = btrfs$/d' -e '/^\s*map to guest = never$/d' /etc/samba/smb.conf
sed -i '/^\[global\]/a \   # Cynthology: global options\n   server min protocol = SMB2_10' /etc/samba/smb.conf
sed -i 's/^\(\s*\)map to guest = .*/\1map to guest = never/' /etc/samba/smb.conf   # no anonymous fallback
printf '\n# Cynthology: shared folders (keep this the last line)\ninclude = /etc/samba/cyn-shares.conf\n' >> /etc/samba/smb.conf
sed -i '/^\[homes\]/,/^\[/{s/^\(\s*browseable\s*=\s*\).*/\1no/}' /etc/samba/smb.conf || true
touch /etc/exports.d/cyn.exports
# /etc/issue shows the web address on the console
cat > /etc/issue <<'ISSUE'
Cynthology alpha \n  (\l)

  Web management:  https://\4:5001   (or http://\4:5000)

ISSUE
cp "$SRC"/systemd/cynthology-* /etc/systemd/system/
cat "$SRC/VERSION" 2>/dev/null > /etc/cynthology-release || echo "Cynthology dev" > /etc/cynthology-release

if [ "$CHROOT" = 1 ]; then
  systemctl enable cynthology-ui cynthology-snapshots.timer cynthology-health.timer smbd avahi-daemon docker
  systemctl enable wsdd2 2>/dev/null || systemctl enable wsdd 2>/dev/null || true
elif [ "$NOSYSTEMD" != 1 ]; then
  systemctl daemon-reload
  systemctl enable --now cynthology-ui cynthology-snapshots.timer cynthology-health.timer smbd avahi-daemon docker
  systemctl restart cynthology-ui   # pick up new code when upgrading
  systemctl enable --now wsdd2 2>/dev/null || systemctl enable --now wsdd 2>/dev/null || true
fi
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "Cynthology installed.  Open  https://${IP:-<this-box>}:5001  (or http://${IP:-<this-box>}:5000)"
echo "and sign in as ${ADMIN_USER:-root}. The browser warns once about the self-signed certificate - that is expected."
