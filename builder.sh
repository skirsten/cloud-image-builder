#!/bin/bash

set -eo pipefail -o xtrace

function failure {
	echo "Shutting down due to failure"
	poweroff
}
trap failure EXIT

export DEBIAN_FRONTEND=noninteractive

apt-get update

apt-get install -y nvme-cli

# See build.py why this is necessary
nvme attach-ns /dev/nvme0 -n 1 -c 0

mkdir /mnt/shared
mount -t 9p -o trans=virtio shared /mnt/shared -oversion=9p2000.L,posixacl,msize=104857600

disk=/dev/nvme0n1

for part in $(lsblk -nlpo NAME "$disk"); do
	case "$(blkid -s LABEL -o value "$part" 2>/dev/null)" in
		cloudimg-rootfs) ROOT_PART="$part" ;;
		BOOT) BOOT_PART="$part" ;;
		UEFI) UEFI_PART="$part" ;;
	esac
done

ROOT_PARTNUM=$(echo "$ROOT_PART" | grep -o '[0-9]*$')
growpart $disk "$ROOT_PARTNUM" || true
e2fsck -f -p "$ROOT_PART"
resize2fs "$ROOT_PART"

lsblk

# mount

rootmnt=$(mktemp -d --suffix=.rootmnt)
hostmnt=$(mktemp -d --suffix=.hostmnt)

mount -o rw "$ROOT_PART" $rootmnt
mount "$BOOT_PART" $rootmnt/boot
mount "$UEFI_PART" $rootmnt/boot/efi

mount --bind /dev $rootmnt/dev
mount --bind /dev/pts $rootmnt/dev/pts
mount --bind /proc $rootmnt/proc
mount --bind /sys $rootmnt/sys
mount --bind /tmp $rootmnt/tmp

readlink -e /etc/resolv.conf | grep -q "/run/systemd/resolve/stub-resolv.conf"
readlink -m $rootmnt/etc/resolv.conf | grep -q "$rootmnt/run/systemd/resolve/stub-resolv.conf"

# setup overlay on /run
mkdir -p $hostmnt/run/{upper,work,merged}

mount -t overlay overlay -o lowerdir=$rootmnt/run,upperdir=$hostmnt/run/upper,workdir=$hostmnt/run/work $hostmnt/run/merged
mount --bind $hostmnt/run/merged $rootmnt/run

mkdir -p $rootmnt/run/systemd/resolve
cp /run/systemd/resolve/stub-resolv.conf $rootmnt/run/systemd/resolve/stub-resolv.conf

# setup overlay on /var/cache/apt
mkdir -p $hostmnt/var/cache/apt/{upper,work,merged}

mount -t overlay overlay -o lowerdir=$rootmnt/var/cache/apt,upperdir=$hostmnt/var/cache/apt/upper,workdir=$hostmnt/var/cache/apt/work $hostmnt/var/cache/apt/merged
mount --bind $hostmnt/var/cache/apt/merged $rootmnt/var/cache/apt

# setup cache for pip and other stuff
mkdir -p $hostmnt/root/.cache
mkdir -p $rootmnt/root/.cache # TODO: Circumvent this?
mount --bind $hostmnt/root/.cache $rootmnt/root/.cache

# chroot

cp /mnt/shared/provision.sh $rootmnt/run/provision.sh
mkdir $rootmnt/run/reports

chroot $rootmnt /run/provision.sh

cp -r $rootmnt/run/reports /mnt/shared

# unmount

umount $rootmnt/root/.cache

umount $rootmnt/var/cache/apt
umount $hostmnt/var/cache/apt/merged

umount $rootmnt/run
umount $hostmnt/run/merged

umount $rootmnt/tmp

for mnt in $rootmnt/sys $rootmnt/proc $rootmnt/dev/pts $rootmnt/dev; do
	i=0
	while ! umount $mnt; do
		i=$((i + 1))
		if [ $i -ge 5 ]; then
			umount -l $mnt
			break
		fi
		sleep 1
	done
done

umount $rootmnt/boot/efi
umount $rootmnt/boot
umount $rootmnt

rm -r $rootmnt
rm -fr $hostmnt

touch /mnt/shared/success

# shutdown

trap - EXIT
poweroff
