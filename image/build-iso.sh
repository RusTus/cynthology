#!/bin/bash
# Build the Cynthology installer ISO by remastering the official Debian 13 netinst image.
# Needs: curl, xorriso, git.   Usage: image/build-iso.sh [version]   -> dist/cynthology-<version>-amd64.iso
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-$(git -C "$ROOT" describe --tags --always 2>/dev/null || echo dev)}"
MIRROR="https://cdimage.debian.org/debian-cd/current/amd64/iso-cd"
CACHE="${CACHE_DIR:-$ROOT/dist/cache}"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$CACHE" "$ROOT/dist"

echo "==> Fetching Debian netinst"
curl -fsSL "$MIRROR/SHA256SUMS" -o "$CACHE/SHA256SUMS"
LINE=$(grep -E ' debian-[0-9.]+-amd64-netinst\.iso$' "$CACHE/SHA256SUMS")
ISO_NAME=${LINE##* }
[ -f "$CACHE/$ISO_NAME" ] || curl -fL --retry 3 "$MIRROR/$ISO_NAME" -o "$CACHE/$ISO_NAME"
(cd "$CACHE" && echo "$LINE" | sha256sum -c -)

echo "==> Staging Cynthology $VERSION"
git -C "$ROOT" archive --format=tar --prefix=cynthology/ HEAD | tar x -C "$WORK"
echo "$VERSION" > "$WORK/cynthology/VERSION"

echo "==> Writing ISO"
OUT="$ROOT/dist/${ISO_OUT_NAME:-cynthology-$VERSION-amd64.iso}"
rm -f "$OUT"
xorriso -indev "$CACHE/$ISO_NAME" -outdev "$OUT" \
  -volid "CYNTHOLOGY" \
  -map "$WORK/cynthology" /cynthology \
  -map "$ROOT/image/grub.cfg" /boot/grub/grub.cfg \
  -map "$ROOT/image/isolinux.cfg" /isolinux/isolinux.cfg \
  -rm /md5sum.txt -- \
  -boot_image any replay
(cd "$ROOT/dist" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")
echo "==> $OUT"
