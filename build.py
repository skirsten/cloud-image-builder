#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

"""
Build cloud images with dependency tracking.

Each image tracks a hash of its inputs (upstream image serial, builder.sh,
provision script, and base image hash for derived images). Builds are
skipped automatically when nothing has changed.

Usage:
  ./build.py <image> [image ...]   Build specific image(s), rebuilding deps as needed
  ./build.py --all                 Build all images

Images: base, nvidia-gpu, nvidia-ml
"""

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
WORKDIR = ROOT / "workdir"

UPSTREAM_URL = "https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.img"
UPSTREAM_BUILD_INFO = (
    "https://cloud-images.ubuntu.com/releases/noble/release/unpacked/build-info.txt"
)

# Image dependency tree: image -> base image (None = builds on upstream)
DEPS = {
    "base": None,
    "nvidia-gpu": "base",
    "nvidia-ml": "base",
}


def run(*cmd, **kwargs):
    print(f"==> {' '.join(str(c) for c in cmd)}")
    subprocess.run(cmd, check=True, **kwargs)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def deps_hash(parts: list[str]) -> str:
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def get_upstream_serial() -> str:
    with urlopen(UPSTREAM_BUILD_INFO) as resp:
        for line in resp.read().decode().splitlines():
            if line.startswith("serial="):
                return line.split("=", 1)[1]
    sys.exit("ERROR: Could not find serial in upstream build-info.txt")


def ensure_upstream() -> str:
    """Download upstream cloud image if needed. Returns the serial."""
    serial = get_upstream_serial()
    local_serial_file = WORKDIR / "cloudimg-serial"
    local_image = WORKDIR / "cloudimg-amd64.qcow2"

    local_serial = (
        local_serial_file.read_text().strip() if local_serial_file.exists() else ""
    )

    if not local_image.exists() or serial != local_serial:
        WORKDIR.mkdir(exist_ok=True)
        print(f"==> Downloading upstream cloud image (serial {serial})")
        run("wget", "-S", "-O", str(local_image), UPSTREAM_URL)
        local_serial_file.write_text(serial)
    else:
        print(f"==> Upstream cloud image up to date (serial {serial})")

    return serial


def compute_build_hash(image: str, upstream_serial: str) -> str:
    """Compute a hash of all inputs for an image."""
    parts = [
        upstream_serial,
        file_hash(ROOT / "builder.sh"),
        file_hash(ROOT / "images" / f"{image}.sh"),
    ]

    base = DEPS[image]
    if base is not None:
        base_deps_file = OUTPUT / f"cloudimg-{base}-amd64.qcow2.deps"
        if not base_deps_file.exists():
            return ""  # base not built yet, force rebuild
        parts.append(base_deps_file.read_text().strip())

    return deps_hash(parts)


def is_up_to_date(image: str, build_hash: str) -> bool:
    output_image = OUTPUT / f"cloudimg-{image}-amd64.qcow2"
    deps_file = OUTPUT / f"cloudimg-{image}-amd64.qcow2.deps"

    if not output_image.exists() or not deps_file.exists():
        return False

    return deps_file.read_text().strip() == build_hash


def build_image(image: str, upstream_serial: str) -> None:
    """Build a single image (assumes deps are already built)."""
    build_hash = compute_build_hash(image, upstream_serial)

    if build_hash and is_up_to_date(image, build_hash):
        print(f"==> {image} is up to date, skipping")
        return

    print(f"==> Building {image}")

    base = DEPS[image]
    if base is None:
        target_image = "cloudimg-amd64.qcow2"
    else:
        target_image = str(Path("..") / "output" / f"cloudimg-{base}-amd64.qcow2")

    # Clean up workdir
    for f in ["cloudimg-builder-amd64.qcow2", "cloudimg-target-amd64.qcow2"]:
        (WORKDIR / f).unlink(missing_ok=True)
    shared = WORKDIR / "shared"
    if shared.exists():
        import shutil

        shutil.rmtree(shared)

    disk_size = 20 * 1024 * 1024 * 1024

    # fmt: off
    run(
        "qemu-img", "create",
        "-F", "qcow2", "-b", "cloudimg-amd64.qcow2", "-f", "qcow2",
        str(WORKDIR / "cloudimg-builder-amd64.qcow2"), str(disk_size),
    )
    run(
        "qemu-img", "create",
        "-F", "qcow2", "-b", target_image, "-f", "qcow2",
        str(WORKDIR / "cloudimg-target-amd64.qcow2"), str(disk_size),
    )

    run("cloud-localds", str(WORKDIR / "seed.raw"), str(ROOT / "builder.sh"))

    shared.mkdir()
    import shutil

    shutil.copy2(ROOT / "images" / f"{image}.sh", shared / "provision.sh")

    # Note: target image is mounted via NVMe with detached=on.
    # Both images have the same partition UUIDs so the NVMe partitions must not
    # be visible during boot — they are attached later by builder.sh.
    run(
        "qemu-system-x86_64",
        "-cpu", "host", "-machine", "type=q35,accel=kvm",
        "-smp", str(os.cpu_count()), "-m", "8192",
        "-nographic",
        "-netdev", "id=net00,type=user",
        "-device", "virtio-net-pci,netdev=net00",
        "-drive", f"if=virtio,format=qcow2,file={WORKDIR}/cloudimg-builder-amd64.qcow2",
        "-device", "nvme-subsys,id=nvme-subsys-0,nqn=subsys0",
        "-device", "nvme,serial=deadbeef,subsys=nvme-subsys-0",
        "-drive", f"format=qcow2,file={WORKDIR}/cloudimg-target-amd64.qcow2,if=none,id=nvm-1",
        "-device", "nvme-ns,drive=nvm-1,nsid=1,shared=off,detached=on",
        "-drive", f"if=virtio,format=raw,file={WORKDIR}/seed.raw",
        "-drive", f"if=pflash,format=raw,file=/usr/share/OVMF/OVMF_CODE_4M.fd,readonly=on",
        "-fsdev", f"local,security_model=mapped,id=fsdev0,path={shared}",
        "-device", "virtio-9p-pci,id=fs0,fsdev=fsdev0,mount_tag=shared",
    )
    # fmt: on

    if not (shared / "success").exists():
        sys.exit(f"ERROR: Build of {image} failed")

    OUTPUT.mkdir(exist_ok=True)
    output_image = OUTPUT / f"cloudimg-{image}-amd64.qcow2"

    print("==> Converting to final image")
    # fmt: off
    run(
        "qemu-img", "convert",
        str(WORKDIR / "cloudimg-target-amd64.qcow2"),
        "-f", "qcow2", "-O", "qcow2", "-c",
        str(output_image),
    )
    # fmt: on

    # Recompute hash now that output exists (for base, the deps file is needed by children)
    build_hash = compute_build_hash(image, upstream_serial)
    output_image.with_suffix(".qcow2.deps").write_text(build_hash)

    print(f"==> Built {output_image}")


def build_with_deps(image: str, upstream_serial: str, built: set[str]) -> None:
    """Build an image and its dependencies."""
    if image in built:
        return

    base = DEPS[image]
    if base is not None:
        build_with_deps(base, upstream_serial, built)

    build_image(image, upstream_serial)
    built.add(image)


def main():
    parser = argparse.ArgumentParser(
        description="Build cloud images with dependency tracking"
    )
    parser.add_argument(
        "images", nargs="*", help="Images to build (base, nvidia-gpu, nvidia-ml)"
    )
    parser.add_argument("--all", action="store_true", help="Build all images")
    args = parser.parse_args()

    if args.all:
        targets = list(DEPS.keys())
    elif args.images:
        targets = args.images
    else:
        parser.print_help()
        sys.exit(1)

    for t in targets:
        if t not in DEPS:
            sys.exit(f"ERROR: Unknown image '{t}'. Available: {', '.join(DEPS.keys())}")

    upstream_serial = ensure_upstream()

    built: set[str] = set()
    for target in targets:
        build_with_deps(target, upstream_serial, built)


if __name__ == "__main__":
    main()
