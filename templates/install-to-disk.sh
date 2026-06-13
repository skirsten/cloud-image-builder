#!/bin/sh
#
# Shared installer: write a qcow2 image to disk and configure the installed OS.
#
# Source this script after setting the required environment variables.
# Used by both the iPXE installer and the image-takeover rescue init.
#
# Required:
#   IMAGE_PATH    - Path to the downloaded qcow2 image
#   OVERLAY_DIR   - Directory with overlay files to copy onto installed OS
#
# Disk selection (one of):
#   TARGET_DISK           - Single disk device
#   RAID_DISK1/RAID_DISK2 - Explicit RAID1 pair
#   RAID_AUTO=1           - Auto-detect NVMe pair (needs RAID_MAX_SECTORS)
#
# Optional:
#   WIPE_ALL_DISKS=1 - Wipe all block devices before imaging
#   SNAPSHOT=1       - Poweroff instead of reboot when done (for VM snapshots)
#   STAGING_DISK     - Disk/partition the image is read from; its parent disk is
#                      excluded from wiping and RAID auto-detect, and must not be
#                      the target disk.
#

wipe_disk() {
	mdadm --zero-superblock "$1" 2>/dev/null || true
	dd if=/dev/zero of="$1" bs=1M count=1 2>/dev/null
	partprobe "$1" 2>/dev/null || true
	blkdiscard "$1" || true
}

# Resolve the parent disk of the staging device so it is never wiped or used
# as a RAID member. A whole disk has no PKNAME, so it is its own parent.
STAGING_PARENT=""
if [ -n "$STAGING_DISK" ]; then
	_sp=$(lsblk -dno PKNAME "$STAGING_DISK" 2>/dev/null)
	if [ -n "$_sp" ]; then
		STAGING_PARENT="/dev/$_sp"
	else
		STAGING_PARENT="$STAGING_DISK"
	fi
fi

if [ "$WIPE_ALL_DISKS" = "1" ]; then
	echo "==> Discarding all block devices"
	for dev in $(lsblk -dnpo NAME,TYPE | awk '$2 == "disk" {print $1}'); do
		if [ -n "$STAGING_PARENT" ] && [ "$dev" = "$STAGING_PARENT" ]; then
			echo "    skipping staging disk $dev"
			continue
		fi
		echo "    wiping $dev"
		wipe_disk "$dev"
	done
fi

if [ "$RAID_AUTO" = "1" ]; then
	echo "==> Auto-detecting NVMe boot disks (< ${RAID_MAX_SECTORS} sectors)"
	RAID_DISK1=""
	RAID_DISK2=""
	for dev in /dev/nvme*n1; do
		[ -b "$dev" ] || continue
		[ -n "$STAGING_PARENT" ] && [ "$dev" = "$STAGING_PARENT" ] && continue
		SIZE=$(cat /sys/block/$(basename "$dev")/size)
		if [ "$SIZE" -lt "$RAID_MAX_SECTORS" ]; then
			if [ -z "$RAID_DISK1" ]; then
				RAID_DISK1="$dev"
			elif [ -z "$RAID_DISK2" ]; then
				RAID_DISK2="$dev"
				break
			fi
		fi
	done
	if [ -z "$RAID_DISK1" ] || [ -z "$RAID_DISK2" ]; then
		echo "ERROR: Could not find two NVMe disks < ${RAID_MAX_SECTORS} sectors"
		lsblk
		exit 1
	fi
	echo "    Found: $RAID_DISK1, $RAID_DISK2"
fi

if [ -n "$RAID_DISK1" ] && [ -n "$RAID_DISK2" ]; then
	if [ -n "$STAGING_PARENT" ] && { [ "$RAID_DISK1" = "$STAGING_PARENT" ] || [ "$RAID_DISK2" = "$STAGING_PARENT" ]; }; then
		echo "ERROR: staging disk ($STAGING_PARENT) is also a RAID target; use a different disk"
		exit 1
	fi
	echo "==> Setting up RAID1: $RAID_DISK1 + $RAID_DISK2"
	wipe_disk "$RAID_DISK1"
	wipe_disk "$RAID_DISK2"
	mdadm --create /dev/md0 --level=1 --metadata=1.0 --raid-devices=2 \
		--run "$RAID_DISK1" "$RAID_DISK2"
	TARGET_DEV=/dev/md0
else
	if [ -n "$STAGING_PARENT" ] && [ "$TARGET_DISK" = "$STAGING_PARENT" ]; then
		echo "ERROR: staging disk ($STAGING_PARENT) is also the target disk; use a different disk"
		exit 1
	fi
	echo "==> Single-disk mode: $TARGET_DISK"
	wipe_disk "$TARGET_DISK"
	TARGET_DEV="$TARGET_DISK"
fi

echo "==> Writing image to $TARGET_DEV"
qemu-img convert -p -f qcow2 -O raw "$IMAGE_PATH" "$TARGET_DEV"
rm -f "$IMAGE_PATH"

sync
partprobe "$TARGET_DEV"
lsblk

echo "==> Waiting for partitions"
i=0
ROOT_PART=""
BOOT_PART=""
while [ $i -lt 10 ]; do
	for part in $(lsblk -nlpo NAME "$TARGET_DEV"); do
		case "$(blkid -s LABEL -o value "$part" 2>/dev/null)" in
		cloudimg-rootfs) ROOT_PART="$part" ;;
		BOOT) BOOT_PART="$part" ;;
		esac
	done
	[ -n "$ROOT_PART" ] && break
	i=$((i + 1))
	partprobe "$TARGET_DEV" 2>/dev/null || true
	sleep 1
done
if [ -z "$ROOT_PART" ]; then
	echo "ERROR: Could not find partitions on $TARGET_DEV"
	blkid
	lsblk
	exit 1
fi

echo "==> Configuring installed OS"
mount "$ROOT_PART" /mnt
mount "$BOOT_PART" /mnt/boot
mount --bind /dev /mnt/dev
mount --bind /proc /mnt/proc
mount --bind /sys /mnt/sys

cp -r "$OVERLAY_DIR"/. /mnt

if [ -n "$RAID_DISK1" ]; then
	mkdir -p /mnt/etc/mdadm
	mdadm --detail --scan >>/mnt/etc/mdadm/mdadm.conf
	chroot /mnt /usr/sbin/update-initramfs -u -k all
fi
chroot /mnt /usr/sbin/update-grub

for mnt in /mnt/sys /mnt/proc /mnt/dev /mnt/boot /mnt; do
	i=0
	while ! umount "$mnt" 2>/dev/null; do
		i=$((i + 1))
		if [ $i -ge 5 ]; then
			umount -l "$mnt"
			break
		fi
		sleep 1
	done
done

sync
echo "==> Installation complete"

if [ "$SNAPSHOT" = "1" ]; then
	poweroff -f
else
	reboot -f
fi
