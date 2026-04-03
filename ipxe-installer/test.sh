#!/bin/bash
#
# Boot the per-node iPXE EFI binary in QEMU for testing.
# Tests the real UEFI HTTP Boot path end-to-end.
#
# Prerequisites:
#   - Build output in output/installer/ (run ./ipxe-installer/build.py first)
#   - Serve it over HTTP: python3 -m http.server -d output/installer 8080
#   - OVMF firmware installed (apt install ovmf)
#
# Usage: ./ipxe-installer/test.sh <hostname>

set -eo pipefail

if [ -z "$1" ]; then
	echo "Usage: $0 <hostname>"
	echo "  hostname must match a node directory in output/installer/"
	exit 1
fi

HOST="$1"
DIR="output/installer"
EFI="$DIR/$HOST/BOOTX64.EFI"
OVMF="/usr/share/OVMF/OVMF_CODE_4M.fd"

if [ ! -f "$EFI" ]; then
	echo "Missing: $EFI"
	echo "Run: ./ipxe-installer/build.py $HOST"
	exit 1
fi

if [ ! -f "$OVMF" ]; then
	echo "Missing: $OVMF"
	echo "Run: sudo apt install ovmf"
	exit 1
fi

# Create a FAT disk image with the EFI binary
EFIDISK=$(mktemp --suffix=.img)
trap "rm -f '$EFIDISK'" EXIT
dd if=/dev/zero of="$EFIDISK" bs=1M count=64 2>/dev/null
mkfs.fat -F 32 "$EFIDISK" >/dev/null
mmd -i "$EFIDISK" ::EFI ::EFI/BOOT
mcopy -i "$EFIDISK" "$EFI" ::EFI/BOOT/BOOTX64.EFI

echo "Booting iPXE EFI for $HOST"
echo "  efi:  $EFI ($(du -h "$EFI" | cut -f1))"
echo ""
echo "Press Ctrl-A X to exit QEMU"
echo ""

qemu-system-x86_64 \
	-cpu host -machine type=q35,accel=kvm -smp 2 -m 4096 \
	-nographic \
	-drive if=pflash,format=raw,readonly=on,file="$OVMF" \
	-drive format=raw,file="$EFIDISK" \
	-netdev id=net0,type=user \
	-device virtio-net-pci,netdev=net0 \
	-no-reboot
