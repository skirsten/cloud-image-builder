{{- /* This is a gomplate template. Render it with: just render-takeover */ -}}
#!/bin/bash
#
# Reimage a running machine via kexec into a minimal Alpine rescue environment.
#
set -eo pipefail

REPLACEMENT_IMAGE={{ .takeover.image | quote }}
RESCUE_ROOTFS="https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/x86_64/alpine-minirootfs-3.23.2-x86_64.tar.gz"
# {{- if and (has .takeover "staging_disk") .takeover.staging_disk }}
STAGING_DISK={{ .takeover.staging_disk | quote }}
# {{- else }}
STAGING_DISK=""
# {{- end }}

cd $(mktemp -d -p /dev/shm)

cat <<EOF >/etc/systemd/system/unmodeset.service
[Unit]
Description=Unload nvidia modesetting modules from kernel
Documentation=man:modprobe(8)
DefaultDependencies=no
After=umount.target
Before=kexec.target

[Service]
Type=oneshot
ExecStart=modprobe -r nvidia_drm

[Install]
WantedBy=kexec.target
EOF
systemctl daemon-reload
systemctl enable unmodeset.service

export DEBIAN_FRONTEND=noninteractive

echo "Installing packages"
apt-get update
apt-get install -y --no-install-recommends kexec-tools systemd-container lz4

echo "Downloading rescue rootfs from $RESCUE_ROOTFS"
wget -q "$RESCUE_ROOTFS" -O rescue.tar.gz
mkdir rescue
tar xf rescue.tar.gz -C rescue

if [ -n "$STAGING_DISK" ]; then
	# Stage the image on a separate disk so it does not have to fit in RAM.
	if [ ! -b "$STAGING_DISK" ]; then
		echo "ERROR: staging disk $STAGING_DISK is not a block device"
		exit 1
	fi

	# The staging disk must be ext4: it is built into the kernel, so the rescue
	# initramfs (which ships no modules) can mount it. Other filesystems (xfs,
	# btrfs, ...) are loadable modules and are not available there.
	echo "Staging replacement image on $STAGING_DISK"
	STAGING_MNT=$(mktemp -d)
	mount "$STAGING_DISK" "$STAGING_MNT"
	wget -q "$REPLACEMENT_IMAGE" -O "$STAGING_MNT/replacement.img"
	umount "$STAGING_MNT"
	rmdir "$STAGING_MNT"
else
	echo "Downloading replacement image from $REPLACEMENT_IMAGE"
	wget -q "$REPLACEMENT_IMAGE" -O /replacement.img

	IMAGE_SIZE=$(stat -c %s /replacement.img)
	AVAIL_MEM=$(awk '/^MemAvailable:/ { print $2 * 1024 }' /proc/meminfo)
	if [ "$IMAGE_SIZE" -gt "$AVAIL_MEM" ]; then
		echo "ERROR: replacement image ($(numfmt --to=iec "$IMAGE_SIZE")) exceeds available memory ($(numfmt --to=iec "$AVAIL_MEM"))"
		echo "The image will be copied to a tmpfs after kexec and must fit in RAM."
		echo "Set takeover.staging_disk to stage the image on a separate disk instead."
		exit 1
	fi
fi

systemd-nspawn -D rescue /sbin/apk add bash lsblk blkid qemu-img mdadm util-linux parted --update-cache

OLD_ROOT_PART=$(findmnt --noheadings --output SOURCE --mountpoint /)
ROOT_DEV=$(lsblk --noheadings --paths --output PKNAME $OLD_ROOT_PART)
# {{- if (has .takeover "raid") }}
# {{- if eq (printf "%v" .takeover.raid) "auto" }}

echo "RAID1 auto-detect mode"
# {{- else }}

RAID_DISK1={{ index .takeover.raid 0 | quote }}
RAID_DISK2={{ index .takeover.raid 1 | quote }}

if [ ! -b "$RAID_DISK1" ]; then
	echo "ERROR: RAID_DISK1=$RAID_DISK1 is not a block device"
	exit 1
fi
if [ ! -b "$RAID_DISK2" ]; then
	echo "ERROR: RAID_DISK2=$RAID_DISK2 is not a block device"
	exit 1
fi
echo "RAID1 mode: $RAID_DISK1 + $RAID_DISK2"
# {{- end }}
# {{- end }}

mkdir rescue/overlay
# {{- if not (and (has .takeover "snapshot") .takeover.snapshot) }}

mkdir -p rescue/overlay/etc/cloud/cloud.cfg.d

cat <<'DATASOURCE_EOF' >rescue/overlay/etc/cloud/cloud.cfg.d/99_datasource.cfg
datasource_list: [None]

network:
  config: disabled

datasource:
  None:
    metadata:
      instance-id: INSTANCE_ID_PLACEHOLDER

    userdata_raw: |
{{ .cloud_init | strings.Indent 6 }}
DATASOURCE_EOF

# Fill in instance-id at runtime (needs openssl on the target)
sed -i "s/INSTANCE_ID_PLACEHOLDER/iid-$(openssl rand -hex 8)/" \
	rescue/overlay/etc/cloud/cloud.cfg.d/99_datasource.cfg

# Preserve existing network config
mkdir -p rescue/overlay/etc/netplan
netplan get >rescue/overlay/etc/netplan/52-network.yaml
# {{- end }}
# {{- if .takeover.boot_cmdline }}

mkdir -p rescue/overlay/etc/default/grub.d
echo 'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT {{ .takeover.boot_cmdline }}"' \
	>rescue/overlay/etc/default/grub.d/99-custom-cmdline.cfg
# {{- end }}

cat <<'INSTALL_SH' >rescue/install-to-disk.sh
{{ file.Read "templates/install-to-disk.sh" }}
INSTALL_SH
chmod +x rescue/install-to-disk.sh

cat <<'INSTALLER_ENV' >rescue/installer.env
{{- if and (has .takeover "staging_disk") .takeover.staging_disk }}
STAGING_DISK={{ .takeover.staging_disk | quote }}
{{- end }}
{{- if and (has .takeover "wipe_all_disks") .takeover.wipe_all_disks }}
WIPE_ALL_DISKS=1
{{- end }}
{{- if and (has .takeover "snapshot") .takeover.snapshot }}
SNAPSHOT=1
{{- end }}
{{- if (has .takeover "raid") }}
{{- if eq (printf "%v" .takeover.raid) "auto" }}
RAID_AUTO=1
RAID_MAX_SECTORS={{ math.Div (math.Mul .takeover.raid_max_size_tb 1000000000000) 512 }}
{{- else }}
RAID_DISK1={{ index .takeover.raid 0 | quote }}
RAID_DISK2={{ index .takeover.raid 1 | quote }}
{{- end }}
{{- end }}
INSTALLER_ENV

cat <<'EOF' >rescue/init
#!/bin/bash

set -eo pipefail -o xtrace

function failure {
  echo "Reboot due to failure"
  sleep 60
  reboot -f # in hopes of rebooting to the old OS
  exit 1
}
trap failure EXIT

mount -t devtmpfs dev /dev
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t tmpfs tmpfs /tmp # backed by memory

ip link set up dev lo

. /installer.env

partprobe $ROOT_DEV
lsblk

if [ -n "$STAGING_DISK" ]; then
  # Read the image directly from the staging disk (survives the wipe because
  # it lives on a different disk than the target).
  mkdir -p /staging
  mount "$STAGING_DISK" /staging
  export IMAGE_PATH=/staging/replacement.img
else
  # Copy the image off the about-to-be-wiped old root into tmpfs.
  mount -o ro $OLD_ROOT_PART /mnt
  cp /mnt/replacement.img /tmp/replacement.img
  umount /mnt
  export IMAGE_PATH=/tmp/replacement.img
fi

export OVERLAY_DIR=/overlay
export TARGET_DISK=$ROOT_DEV

trap - EXIT
. /install-to-disk.sh
EOF

chmod +x rescue/init

echo "Building initramfs"
(
	find rescue -printf "%P\0" |
		cpio --directory="rescue" --null --create --owner root:root --format=newc
) | lz4c -l >initramfs.lz4

echo "Running kexec"
kexec -l /boot/vmlinuz --initrd initramfs.lz4 \
	--command-line="console=tty1 console=ttyS0 OLD_ROOT_PART=$OLD_ROOT_PART ROOT_DEV=$ROOT_DEV"
systemctl kexec
