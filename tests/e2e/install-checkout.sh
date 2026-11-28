#!/bin/bash
# Install (or upgrade to) this checkout on the e2e VM - what an update does: unpack and run install.sh.
#   tests/e2e/install-checkout.sh <E2E_DIR> <SSH_PORT>
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
W=$1; SSH_PORT=$2
SSH=(ssh -i "$W/key" -p "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 nasadmin@localhost)
echo "==> installing Cynthology from $(git -C "$REPO" rev-parse --short HEAD)"
git -C "$REPO" archive --format=tar --prefix=src/ HEAD | "${SSH[@]}" 'rm -rf src && tar x'
"${SSH[@]}" 'cd src && sudo ./install.sh > /tmp/install.log 2>&1; rc=$?; tail -5 /tmp/install.log; exit $rc'
