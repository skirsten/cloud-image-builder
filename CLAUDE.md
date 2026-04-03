# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A toolchain for building customized Ubuntu 24.04 cloud images and deploying them to bare-metal servers. Two main subsystems:

1. **Image builder** (`build.py` + `builder.sh`) — Builds qcow2 cloud images inside QEMU/KVM VMs with dependency tracking. A "builder" VM boots the upstream Ubuntu cloud image, chroots into a target disk (attached via NVMe), and runs a provision script.
2. **iPXE installer** (`ipxe-installer/build.py`) — Produces per-node UEFI HTTP Boot artifacts (iPXE EFI binary + Alpine overlay) that network-boot a machine, download an OS image, and write it to disk.

Both share `templates/install-to-disk.sh` for the final disk-write + RAID1 + overlay logic.

## Commands

```bash
# Build images (requires KVM, OVMF, cloud-image-utils, qemu)
just build-base                    # Build base image
just build-nvidia-gpu              # Build nvidia-gpu (auto-rebuilds base if stale)
just build-all                     # Build all images

# Test a built image in QEMU (requires gomplate, config.yaml)
just test base                     # Boot image with cloud-init login
just test-takeover base            # Test image-takeover script

# Upload built image
just upload base r2/pkg/raw/cloudimg

# Render image-takeover script to stdout
just render-takeover

# iPXE installer (requires build-essential, zlib1g-dev, binutils-dev, liblzma-dev)
./ipxe-installer/build.py                      # Build all nodes
./ipxe-installer/build.py node1 node2          # Build specific nodes
./ipxe-installer/build.py --config other.yaml  # Custom config

# Format Python
ruff format build.py ipxe-installer/build.py
```

## Architecture Details

**Image dependency tree:** `base` builds on the upstream Ubuntu cloud image; `nvidia-gpu` and `nvidia-ml` both build on `base`. The `DEPS` dict in `build.py` defines this. Each built image gets a `.deps` hash file tracking its inputs — builds are skipped when the hash matches.

**Builder VM mechanics** (`builder.sh`): The builder VM boots from the upstream image. The target disk is attached as an NVMe namespace with `detached=on` (same partition UUIDs would conflict during boot). The builder attaches the NVMe namespace at runtime, chroots into the target, runs the provision script (`images/<name>.sh`), then powers off. A `shared/success` sentinel file signals success via 9p.

**Image takeover** (`templates/image-takeover.tmpl.sh`): A gomplate template that reimages a *running* machine by kexec-ing into a minimal Alpine initramfs. It downloads the replacement image to `/replacement.img`, builds a custom initramfs with Alpine + installer, then `kexec` + `systemctl kexec` into it. The init script mounts the old root read-only to grab the image, then runs `install-to-disk.sh`.

**iPXE installer flow**: UEFI HTTP Boot loads a per-node iPXE EFI binary (with embedded boot script) → iPXE fetches shared Alpine kernel + stock initramfs + per-node overlay via HTTP → Alpine boots, overlay's inittab runs `/opt/installer.sh` → downloads OS image, calls `install-to-disk.sh`.

**Config layering** (iPXE installer): Global defaults in config.yaml, per-node overrides under `nodes:`. `resolve_node()` merges them. Each node must have a `netplan` config at minimum.

## Key Conventions

- Python scripts use `uv run --script` shebangs with inline PEP 723 metadata (no venv needed).
- Config files are `config.yaml` (gitignored); `config.example.yaml` files show the schema.
- Templates use gomplate (`{{ .field }}`) for the takeover script, Python `.format()` for iPXE installer templates.
- Ruff for Python formatting (line-length 88, double quotes).
- Image output goes to `output/`, build working directory is `workdir/` — both gitignored.
