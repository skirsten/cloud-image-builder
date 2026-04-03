# cloud-image-builder

Simple, modern, and clean tooling for building customized Ubuntu cloud images and deploying them to bare-metal servers. The core build process is hand-written artisanal code — no Packer, no Ansible, no abstraction layers. Just Python, shell, and QEMU.

## Image Builder

Builds customized Ubuntu 24.04 qcow2 images inside QEMU/KVM VMs with automatic dependency tracking.

```bash
just build-base        # Build the base image
just build-nvidia-gpu  # Build nvidia-gpu (rebuilds base if stale)
just build-all         # Build everything
```

Images: `base` → upstream Ubuntu cloud image, `nvidia-gpu` and `nvidia-ml` → built on `base`.

Provision scripts live in `images/`. Each image tracks a hash of its inputs and skips rebuilds when nothing changed.

## Image Takeover

Reimages a running machine by kexec-ing into a minimal Alpine rescue environment. Works on both bare-metal and VMs — useful for replacing the boot image on a VM which can then be snapshotted.

```bash
just render-takeover    # Render the script (uses config.yaml + gomplate)
just test-takeover base # Test in QEMU
```

## iPXE Installer

Produces per-node UEFI HTTP Boot artifacts that network-boot bare-metal machines and image them.

```bash
./ipxe-installer/build.py                # Build all nodes
./ipxe-installer/build.py node1 node2    # Build specific nodes
```

Configure nodes in `ipxe-installer/config.yaml` (see `config.example.yaml`).

## Requirements

```bash
# Image builder
sudo apt install qemu-system-x86 qemu-utils cloud-image-utils ovmf

# iPXE installer (additional)
sudo apt install build-essential zlib1g-dev binutils-dev liblzma-dev
```

- [uv](https://github.com/astral-sh/uv) — Python scripts use inline PEP 723 metadata, no venv needed
- [just](https://github.com/casey/just) — task runner
- [gomplate](https://github.com/hairyhenderson/gomplate) — template rendering for takeover scripts

## Configuration

Copy `config.example.yaml` to `config.yaml` and fill in your values. The iPXE installer has its own config at `ipxe-installer/config.yaml`.
