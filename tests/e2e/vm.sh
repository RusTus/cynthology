#!/bin/bash
# Boot a throwaway Debian 13 VM, install Cynthology from this checkout with the real installer,
# and write the connection details to $E2E_DIR/env for the tests (tests/e2e/test_e2e.py).
#
#   tests/e2e/vm.sh            (needs qemu-system-x86, qemu-utils, cloud-image-utils; KVM if available)
#
# The VM gets four empty 3 GB data drives, a random password and a fresh SSH key; nothing is kept.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
W=${E2E_DIR:-/tmp/e2e}
mkdir -p "$W"
cd "$W"
BASE_URL=https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2
[ -s base.qcow2 ] || curl -fsSL --proto =https --retry 3 -o base.qcow2 "$BASE_URL"

rm -f boot.qcow2 d?.qcow2 seed.img key key.pub qemu.pid serial.log
qemu-img create -q -f qcow2 -F qcow2 -b base.qcow2 boot.qcow2 16G
for i in 1 2 3 4; do qemu-img create -q -f qcow2 "d$i.qcow2" 3G; done
ssh-keygen -q -t ed25519 -N '' -f key
PASS=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)
cat > user-data <<EOF
#cloud-config
hostname: cyn-e2e
users:
  - name: nasadmin
    groups: [sudo, users]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: false
    plain_text_passwd: "$PASS"
    ssh_authorized_keys: ["$(cat key.pub)"]
ssh_pwauth: false
EOF
cloud-localds seed.img user-data

if [ -w /dev/kvm ]; then ACCEL=(-accel kvm -cpu host); else echo "(no KVM - this will be slow)"; ACCEL=(-accel tcg,thread=multi -cpu max); fi
DISKS=()
for i in 1 2 3 4; do DISKS+=(-drive "file=d$i.qcow2,if=none,id=d$i" -device "ide-hd,drive=d$i,bus=ahci.$((i-1)),serial=E2EDISK$i"); done
qemu-system-x86_64 -machine q35 "${ACCEL[@]}" -smp 4 -m 6144 \
  -drive file=boot.qcow2,if=virtio -drive file=seed.img,if=virtio,format=raw \
  -device ahci,id=ahci "${DISKS[@]}" \
  -netdev user,id=n0,hostfwd=tcp::2222-:22,hostfwd=tcp::5101-:5001 -device virtio-net,netdev=n0 \
  -display none -serial file:serial.log -daemonize -pidfile qemu.pid

SSH=(ssh -i "$W/key" -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 nasadmin@localhost)
echo "==> waiting for the VM"
for i in $(seq 1 120); do "${SSH[@]}" true 2>/dev/null && break; sleep 5; done
"${SSH[@]}" true
"${SSH[@]}" 'cloud-init status --wait >/dev/null 2>&1 || true'

echo "==> installing Cynthology from $(git -C "$REPO" rev-parse --short HEAD)"
git -C "$REPO" archive --format=tar --prefix=src/ HEAD | "${SSH[@]}" 'rm -rf src && tar x'
"${SSH[@]}" 'cd src && sudo ./install.sh > /tmp/install.log 2>&1; rc=$?; tail -5 /tmp/install.log; exit $rc'
"${SSH[@]}" 'sudo apt-get install -y -q smbclient >/dev/null 2>&1 || true'
for i in $(seq 1 60); do curl -sk -o /dev/null -w '%{http_code}' https://localhost:5101/ | grep -q 200 && break; sleep 5; done

cat > env <<EOF
export CYN_E2E_URL=https://localhost:5101
export CYN_E2E_SSH="ssh -i $W/key -p 2222 nasadmin@localhost"
export CYN_E2E_USER=nasadmin
export CYN_E2E_PASS=$PASS
EOF
echo "==> ready: source $W/env && pytest -q tests/e2e"
