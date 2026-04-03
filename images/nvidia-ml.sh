#!/bin/bash

set -eo pipefail -o xtrace

# Important: DKMS installation (e.g. NVIDIA driver) will often not detect the chroot or just hang.
# So for now we only support building on the same kernel as the chroot:

dpkg-query -W -f='${binary:Package}\n' linux-image-* | head -n 1 | sed 's/linux-image-//' | grep -q $(uname -r)

export DEBIAN_FRONTEND=noninteractive

# Setup sources

wget -qO /tmp/cuda-keyring_1.1-1_all.deb https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
dpkg -i /tmp/cuda-keyring_1.1-1_all.deb

wget -qO /tmp/doca-host_3.2.1-044000-25.10-ubuntu2404_amd64.deb https://www.mellanox.com/downloads/DOCA/DOCA_v3.2.1/host/doca-host_3.2.1-044000-25.10-ubuntu2404_amd64.deb
dpkg -i /tmp/doca-host_3.2.1-044000-25.10-ubuntu2404_amd64.deb

apt-get update

# DOCA-OFED Driver

apt-get install mft=4.34.1-10 # not sure why its not automatically selecting this version

apt-get install -y autotools-dev debhelper dkms dh-dkms
apt-get install -y doca-ofed

systemctl enable openibd

# NVIDIA Driver

apt-get install -y nvidia-driver-pinning-580

# <= Hopper
apt-get install -y --allow-downgrades nvidia-open nvidia-fabricmanager nvidia-container-toolkit

# # >= Blackwell
# apt-get install -y --allow-downgrades nvidia-open nvlink5 nvidia-container-toolkit

systemctl enable nvidia-persistenced
systemctl enable nvidia-fabricmanager
systemctl enable nvidia-cdi-refresh

# Tweaks

echo "nvidia-peermem" >/etc/modules-load.d/nvidia-peermem.conf
echo "mlx5_core" >/etc/modules-load.d/mlx5_core.conf

update-initramfs -u -k all

# Report

apt list --installed >/run/reports/apt_packages
