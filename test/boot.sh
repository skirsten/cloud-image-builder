#!/bin/bash
#
# Boot a built cloud image in QEMU for testing.
#
# Usage: ./test/boot.sh <image> <userdata>
#   image:    image name (e.g. nvidia-ml), must exist in output/
#   userdata: path to cloud-init userdata file

set -eo pipefail

IMAGE="$1"
USERDATA="$2"

if [ -z "$IMAGE" ] || [ -z "$USERDATA" ]; then
	echo "Usage: $0 <image> <userdata>"
	exit 1
fi

QCOW="output/cloudimg-$IMAGE-amd64.qcow2"
if [ ! -f "$QCOW" ]; then
	echo "Image '$IMAGE' not found in output/"
	exit 1
fi

cloud-localds workdir/seed.raw "$USERDATA"

qemu-img create -F qcow2 -b "$(pwd)/$QCOW" -f qcow2 workdir/test-disk.qcow2 50G

qemu-system-x86_64 \
	-cpu host -machine type=q35,accel=kvm -smp $(nproc) -m 8192 \
	-nographic \
	-netdev id=net00,type=user,hostfwd=tcp::2222-:22 \
	-device virtio-net-pci,netdev=net00 \
	-snapshot \
	\
	-drive "if=virtio,format=qcow2,file=workdir/test-disk.qcow2" \
	\
	-drive if=virtio,format=raw,file=workdir/seed.raw \
	-drive if=pflash,format=raw,file=/usr/share/OVMF/OVMF_CODE_4M.fd,readonly=on \
	\
	-fsdev local,security_model=mapped,id=fsdev0,path="$(pwd)/workdir/shared" \
	-device virtio-9p-pci,id=fs0,fsdev=fsdev0,mount_tag=shared
