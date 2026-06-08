#!/bin/bash

set -eo pipefail -o xtrace

# Important: DKMS installation (e.g. NVIDIA driver) will often not detect the chroot or just hang.
# So for now we only support building on the same kernel as the chroot.
# In MAAS mode this runs in a full VM and the wrapper installs the kernel itself.
if [ -z "$MAAS_BUILD" ]; then
	dpkg-query -W -f='${binary:Package}\n' linux-image-* | head -n 1 | sed 's/linux-image-//' | grep -q $(uname -r)
fi

export DEBIAN_FRONTEND=noninteractive

# Setup sources

wget -qO /tmp/cuda-keyring_1.1-1_all.deb https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
dpkg -i /tmp/cuda-keyring_1.1-1_all.deb

apt-get update

# NVIDIA Driver

apt-get install -y nvidia-driver-pinning-580

apt-get install -y --allow-downgrades nvidia-open nvidia-container-toolkit

systemctl enable nvidia-persistenced
# systemctl enable nvidia-fabricmanager # optional to support green boot on non-NVLink systems
systemctl enable nvidia-cdi-refresh

# Report

apt list --installed >/run/reports/apt_packages
