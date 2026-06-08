#!/bin/bash

set -eo pipefail -o xtrace

# Important: DKMS installation (e.g. NVIDIA driver) will often not detect the chroot or just hang.
# So for now we only support building on the same kernel as the chroot.
# In MAAS mode this runs in a full VM (not a chroot), and packer-maas removes the running kernel before our customize script.
if [ -z "$MAAS_BUILD" ]; then
	dpkg-query -W -f='${binary:Package}\n' linux-image-* | head -n 1 | sed 's/linux-image-//' | grep -q $(uname -r)
fi

export DEBIAN_FRONTEND=noninteractive

# Setup sources

# tailscale
mkdir -p --mode=0755 /usr/share/keyrings
wget -qO /usr/share/keyrings/tailscale-archive-keyring.gpg https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg
wget -qO /etc/apt/sources.list.d/tailscale.list https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list

# docker
mkdir -p --mode=0755 /etc/apt/keyrings
wget -qO /etc/apt/keyrings/docker.asc https://download.docker.com/linux/ubuntu/gpg

cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

apt-get update

apt-get upgrade -y

# apt-get remove -y linux-virtual linux-image-virtual
# apt-get autoremove -y

if [ -z "$MAAS_BUILD" ]; then
	apt-get install -y linux-image-generic
fi

# Install base packages

apt-get install -y wget curl git nfs-client hwloc numactl google-perftools bsdmainutils mdadm

update-pciids

# Software

apt-get install -y podman tailscale wireguard docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

wget -qO /usr/local/bin/k3s-install.sh https://get.k3s.io
chmod +x /usr/local/bin/k3s-install.sh

for minor in $(seq 34 100); do
	channel="v1.${minor}"
	version=$(wget -SqO /dev/null "https://update.k3s.io/v1-release/channels/${channel}" 2>&1 | grep -i Location | sed -e 's|.*/||' || true)
	[ -z "$version" ] && break
	version_url=$(echo "$version" | sed 's/+/%2B/g')
	wget -qO "/usr/local/bin/k3s-${channel}" "https://github.com/k3s-io/k3s/releases/download/${version_url}/k3s"
	chmod +x "/usr/local/bin/k3s-${channel}"
done

# UFW

ufw logging low
ufw allow ssh/tcp

# Note: Some networks NAT internet ingress to these ranges... Can't do this:
# ufw allow from 10.0.0.0/8
# ufw allow from 172.16.0.0/12
# ufw allow from 192.168.0.0/16

if [ -n "$MAAS_BUILD" ]; then
	sed -i 's/^ENABLED=no/ENABLED=yes/' /etc/ufw/ufw.conf
else
	ufw --force enable
fi

# Bake md modules into initramfs (needed for RAID1 boot).
if dpkg-query -W -f='${binary:Package}\n' 'linux-image-*' 2>/dev/null | grep -q .; then
	update-initramfs -u -k all
fi

# Report

apt list --installed >/run/reports/apt_packages
