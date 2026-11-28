#!/bin/bash
# Boot a throwaway Debian 13 VM, install Cynthology from this checkout with the real installer
# (E2E_INSTALL=release: the latest published release instead - the upgrade test then upgrades it),
# and write the connection details to $E2E_DIR/env for the tests (tests/e2e/test_e2e.py).
#
#   tests/e2e/vm.sh            (needs qemu-system-x86, qemu-utils, cloud-image-utils; KVM if available)
#
# The VM gets E2E_DISKS (default 4) empty 3 GB data drives, a random password and a fresh SSH key; nothing is kept.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
W=${E2E_DIR:-/tmp/e2e}
SSH_PORT=${E2E_SSH_PORT:-2222}
WEB_PORT=${E2E_WEB_PORT:-5101}
NDISKS=${E2E_DISKS:-4}
mkdir -p "$W"
cd "$W"
BASE_URL=https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2
[ -s base.qcow2 ] || curl -fsSL --proto =https --retry 3 -o base.qcow2 "$BASE_URL"

rm -f boot.qcow2 d?.qcow2 seed.img key key.pub qemu.pid serial.log
qemu-img create -q -f qcow2 -F qcow2 -b base.qcow2 boot.qcow2 16G
for i in $(seq 1 "$NDISKS"); do qemu-img create -q -f qcow2 "d$i.qcow2" 3G; done
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
# (one AHCI controller has 6 ports: a second one for drives 7+)
DISKS=(-device ahci,id=ahci2)
for i in $(seq 1 "$NDISKS"); do
  if [ "$i" -le 6 ]; then bus="ahci.$((i-1))"; else bus="ahci2.$((i-7))"; fi
  DISKS+=(-drive "file=d$i.qcow2,if=none,id=d$i" -device "ide-hd,drive=d$i,bus=$bus,serial=E2EDISK$i")
done
qemu-system-x86_64 -machine q35 "${ACCEL[@]}" -smp 4 -m 6144 \
  -drive file=boot.qcow2,if=none,id=boot -device virtio-blk-pci,drive=boot,bootindex=0 \
  -drive file=seed.img,if=none,id=seed,format=raw -device virtio-blk-pci,drive=seed \
  -device ahci,id=ahci "${DISKS[@]}" \
  -netdev user,id=n0,hostfwd=tcp::$SSH_PORT-:22,hostfwd=tcp::$WEB_PORT-:5001 -device virtio-net,netdev=n0 \
  -display none -serial file:serial.log -daemonize -pidfile qemu.pid 2> qemu.err || { cat qemu.err; exit 1; }

SSH=(ssh -i "$W/key" -p "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 nasadmin@localhost)
echo "==> waiting for the VM"
for i in $(seq 1 120); do "${SSH[@]}" true 2>/dev/null && break; sleep 5; done
if ! "${SSH[@]}" true; then
  echo "VM didn't come up. qemu running: $(kill -0 "$(cat qemu.pid 2>/dev/null)" 2>/dev/null && echo yes || echo no)"
  echo "--- console:"; tail -60 serial.log; cat qemu.err; exit 1
fi
"${SSH[@]}" 'cloud-init status --wait >/dev/null 2>&1 || true'

if [ "${E2E_INSTALL:-checkout}" = release ]; then
  # the latest published release, the way people install it (get.sh: download + signature check)
  echo "==> installing the latest published release"
  "${SSH[@]}" 'sudo bash -s > /tmp/install.log 2>&1; rc=$?; tail -5 /tmp/install.log; exit $rc' < "$REPO/get.sh"
else
  "$REPO/tests/e2e/install-checkout.sh" "$W" "$SSH_PORT"
fi
"${SSH[@]}" 'sudo apt-get install -y -q smbclient >/dev/null 2>&1 || true'
for i in $(seq 1 60); do curl -sk -o /dev/null -w '%{http_code}' "https://localhost:$WEB_PORT/" | grep -q 200 && break; sleep 5; done

cat > env <<EOF
export CYN_E2E_URL=https://localhost:$WEB_PORT
export CYN_E2E_SSH="ssh -i $W/key -p $SSH_PORT nasadmin@localhost"
export CYN_E2E_USER=nasadmin
export CYN_E2E_PASS=$PASS
EOF
echo "==> ready: source $W/env && pytest -q tests/e2e"
