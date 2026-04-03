#!/bin/sh
set -ex

# Redirect all output to console + log file
mkdir -p /var/log
mkfifo /tmp/logpipe
tee /var/log/installer.log < /tmp/logpipe 2>&1 | tee /dev/ttyS0 > /dev/tty1 &
exec > /tmp/logpipe 2>&1
rm /tmp/logpipe

. /opt/installer.env

echo "==> Waiting for network"
i=0
while [ $i -lt 60 ]; do
	wget -q --spider "$REPLACEMENT_IMAGE" 2>/dev/null && break
	i=$((i + 1))
	echo "  attempt $i..."
	sleep 1
done

echo "==> Loading kernel modules"
if ! modprobe nvme 2>/dev/null; then
	echo "    Downloading modloop..."
	wget "$MODLOOP_URL" -O /tmp/modloop-lts
	mkdir -p /.modloop
	mount -t squashfs -o loop /tmp/modloop-lts /.modloop
	ln -sf /.modloop/modules /lib/modules
	modprobe nvme
fi
modprobe ext4 2>/dev/null || true
modprobe md_mod 2>/dev/null || true
modprobe raid1 2>/dev/null || true
sleep 2

echo "==> Downloading replacement image"
wget "$REPLACEMENT_IMAGE" -O /tmp/replacement.img

export IMAGE_PATH=/tmp/replacement.img
export OVERLAY_DIR=/opt/overlay
. /opt/install-to-disk.sh
