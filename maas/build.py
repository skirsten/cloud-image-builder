#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

"""
Build MAAS-compatible Ubuntu images via canonical/packer-maas.

For each target, this generates a single customize script that inlines the
existing provision scripts (with MAAS_BUILD=1 set so the chroot-specific bits
get skipped), then invokes packer-maas's cloudimg build to produce a tar.gz
uploadable to MAAS with `filetype=tgz`.

Usage:
  ./maas/build.py <image> [image ...]   Build specific image(s)
  ./maas/build.py --all                 Build all images

Images: base, nvidia-gpu, nvidia-ml

Output: output/maas-<image>-amd64.tar.gz

Requirements: packer (>= 1.11), qemu-system, ovmf, cloud-image-utils, parted, make, git.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGES = ROOT / "images"
OUTPUT = ROOT / "output"
WORKDIR = ROOT / "workdir" / "maas"

PACKER_MAAS_REPO = "https://github.com/canonical/packer-maas.git"
PACKER_MAAS_REF = "main"

UBUNTU_SERIES = "noble"

# image -> provision scripts to source (in order)
TARGETS = {
    "base": ["base.sh"],
    "nvidia-gpu": ["base.sh", "nvidia-gpu.sh"],
    "nvidia-ml": ["base.sh", "nvidia-ml.sh"],
}

# Targets that need a kernel installed during build (for DKMS) and pinned via curtin.
NEEDS_KERNEL = {"nvidia-gpu", "nvidia-ml"}
KERNEL_PACKAGE = "linux-image-generic"

# packer-maas's cloudimg template hardcodes an 8G build disk, which fills up
# while DKMS-building DOCA/NVIDIA. Patch it after clone.
BUILD_DISK_SIZE = "20G"


def run(*cmd, **kwargs):
    print(f"==> {' '.join(str(c) for c in cmd)}")
    subprocess.run(cmd, check=True, **kwargs)


def ensure_packer_maas() -> Path:
    repo = WORKDIR / "packer-maas"
    if not repo.exists():
        WORKDIR.mkdir(parents=True, exist_ok=True)
        run(
            "git", "clone", "--depth=1", "--branch", PACKER_MAAS_REF,
            PACKER_MAAS_REPO, str(repo),
        )
    patch_disk_size(repo / "ubuntu" / "ubuntu-cloudimg.pkr.hcl")
    return repo


def patch_disk_size(pkr: Path) -> None:
    text = pkr.read_text()
    new = re.sub(r'disk_size\s*=\s*"\d+G"', f'disk_size = "{BUILD_DISK_SIZE}"', text)
    if new != text:
        pkr.write_text(new)
        print(f"==> Patched {pkr.name}: disk_size = \"{BUILD_DISK_SIZE}\"")


def render_customize(image: str) -> str:
    parts = [
        "#!/bin/bash",
        "set -eo pipefail -o xtrace",
        "",
        "export MAAS_BUILD=1",
        "mkdir -p /run/reports",
        "",
    ]
    if image in NEEDS_KERNEL:
        parts += [
            "# Install a kernel + headers before DKMS-based drivers run, and pin it",
            "# via curtin so MAAS doesn't replace it on deploy.",
            "export DEBIAN_FRONTEND=noninteractive",
            "apt-get update",
            f"apt-get install -y {KERNEL_PACKAGE} linux-headers-generic",
            "mkdir -p /curtin",
            # Resolve the meta-package to the concrete versioned kernel image so
            # curtin pins the exact kernel we built DKMS modules against.
            f"KPKG=$(dpkg-query -W -f='${{Depends}}\\n' {KERNEL_PACKAGE} \\",
            "       | tr ',' '\\n' | awk '{print $1}' | grep '^linux-image-[0-9]' | head -1)",
            'echo -n "$KPKG" >/curtin/CUSTOM_KERNEL',
            "",
        ]
    for script in TARGETS[image]:
        parts.append(f"# --- inlined from images/{script} ---")
        parts.append((IMAGES / script).read_text())
        parts.append("")
    if image in NEEDS_KERNEL:
        parts += [
            "# Ensure initramfs is current for the installed kernel.",
            "update-initramfs -u -k all",
        ]
    return "\n".join(parts)


def build(image: str) -> None:
    if image not in TARGETS:
        sys.exit(f"ERROR: Unknown image '{image}'. Available: {', '.join(TARGETS)}")

    packer_maas = ensure_packer_maas()
    ubuntu_dir = packer_maas / "ubuntu"

    customize_path = WORKDIR / f"customize-{image}.sh"
    customize_path.parent.mkdir(parents=True, exist_ok=True)
    customize_path.write_text(render_customize(image))
    customize_path.chmod(0o755)

    OUTPUT.mkdir(exist_ok=True)
    output_tarball = OUTPUT / f"maas-{image}-amd64.tar.gz"

    print(f"==> Building MAAS image '{image}' with packer-maas ({UBUNTU_SERIES})")
    env = os.environ.copy()
    env.setdefault("PACKER_LOG", "1")
    # packer-maas's Makefile handles OVMF copies, seed iso, and packer invocation.
    # CUSTOMIZE= is a Makefile var it forwards as -var customize_script=...
    run(
        "make",
        "-C", str(ubuntu_dir),
        "custom-cloudimg.tar.gz",
        f"SERIES={UBUNTU_SERIES}",
        f"CUSTOMIZE={customize_path}",
        env=env,
    )

    src = ubuntu_dir / "custom-cloudimg.tar.gz"
    if not src.exists():
        sys.exit(f"ERROR: packer-maas did not produce {src}")
    shutil.move(str(src), str(output_tarball))
    print(f"==> Built {output_tarball}")


def main():
    parser = argparse.ArgumentParser(
        description="Build MAAS-compatible images via packer-maas"
    )
    parser.add_argument(
        "images", nargs="*", help=f"Images to build ({', '.join(TARGETS)})"
    )
    parser.add_argument("--all", action="store_true", help="Build all images")
    args = parser.parse_args()

    if args.all:
        targets = list(TARGETS)
    elif args.images:
        targets = args.images
    else:
        parser.print_help()
        sys.exit(1)

    for t in targets:
        if t not in TARGETS:
            sys.exit(f"ERROR: Unknown image '{t}'. Available: {', '.join(TARGETS)}")

    for t in targets:
        build(t)


if __name__ == "__main__":
    main()
