#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml"]
# ///

"""
Build iPXE-based UEFI HTTP Boot installers.

Architecture:
  UEFI HTTP Boot downloads per-node iPXE EFI binary (BOOTX64.EFI)
  iPXE loads shared vmlinuz-lts + stock initramfs-lts + per-node overlay over HTTP
  Alpine boots, our overlay runs the installer directly from inittab
  Installer configures networking, downloads OS image, writes to disk, reboots

Usage:
  ./ipxe-installer/build.py [--config config.yaml] [--output output/installer] [node ...]

Only 'chroot' requires sudo — invoked automatically.

Output:
  <output>/
    vmlinuz-lts            Alpine LTS kernel (shared)
    initramfs-lts          Alpine stock initramfs (shared, unmodified)
    modloop-lts            Alpine modules squashfs (shared)
    <hostname>/
      BOOTX64.EFI          Per-node iPXE EFI binary
      overlay.tar.gz        Per-node apkovl (fetched by Alpine init over HTTP)

Build requirements: build-essential zlib1g-dev binutils-dev liblzma-dev
"""

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlretrieve

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATES = SCRIPT_DIR / "templates"
SHARED_TEMPLATES = SCRIPT_DIR.parent / "templates"
IPXE_DIR = SCRIPT_DIR.parent / "ipxe"

ALPINE_VERSION = "3.21"
ALPINE_RELEASE = "3.21.6"
ALPINE_ARCH = "x86_64"
ALPINE_MIRROR = (
    f"https://dl-cdn.alpinelinux.org/alpine/v{ALPINE_VERSION}/releases/{ALPINE_ARCH}"
)
ALPINE_NETBOOT = f"alpine-netboot-{ALPINE_RELEASE}-{ALPINE_ARCH}.tar.gz"
ALPINE_MINIROOTFS = f"alpine-minirootfs-{ALPINE_RELEASE}-{ALPINE_ARCH}.tar.gz"


def run(cmd, **kwargs):
    """Run a command, letting stdout/stderr pass through."""
    subprocess.run(cmd, check=True, **kwargs)


def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


def resolve_node(cfg, hostname):
    node_overrides = (cfg.get("nodes") or {}).get(hostname) or {}

    def get(key, required=True):
        val = node_overrides.get(key, cfg.get(key))
        if val is None and required:
            sys.exit(
                f"ERROR: '{key}' not set for node '{hostname}' and no global default"
            )
        return val

    return {
        "hostname": hostname,
        "image": get("image"),
        "disk": get("disk"),
        "netplan": get("netplan"),
        "ssh_keys": get("ssh_keys"),
        "http_server": get("http_server"),
        "boot_cmdline": get("boot_cmdline", required=False) or "",
        "wipe_all_disks": bool(get("wipe_all_disks", required=False)),
        "poweroff": bool(get("poweroff", required=False)),
    }


def parse_disk(disk):
    """Parse disk config. Returns dict with mode and parameters.

    Formats:
      disk: /dev/sda                          -> single disk
      disk: { raid: [/dev/sda, /dev/sdb] }    -> explicit RAID1
      disk: { raid: auto, max_size_tb: 2 }    -> auto-detect (default < 2TB)
    """
    if isinstance(disk, str):
        return {"mode": "single", "target": disk}
    if isinstance(disk, dict) and "raid" in disk:
        raid = disk["raid"]
        if raid == "auto":
            return {"mode": "auto", "max_size_tb": disk.get("max_size_tb", 2)}
        if not isinstance(raid, list) or len(raid) != 2:
            sys.exit(f"ERROR: raid requires exactly 2 disks or 'auto', got {raid}")
        return {"mode": "raid", "disk1": raid[0], "disk2": raid[1]}
    sys.exit(f"ERROR: invalid disk config: {disk}")


def download_cached(url, cache_dir, filename):
    cached = cache_dir / filename
    if cached.exists():
        print(f"    Using cached: {cached}")
        return cached
    print(f"    Downloading: {url}")
    urlretrieve(url, cached)
    return cached


def build_tar_gz(source_dir, output_path):
    """Build a gzip-compressed tar archive (Alpine apkovl format)."""
    run(["tar", "czf", str(output_path), "-C", str(source_dir), "."])


def parse_netplan_network(netplan):
    """Extract networking details from netplan config.

    Returns dict with: address (CIDR), ip, netmask, gateway, nameservers
    """
    import ipaddress

    network = netplan.get("network", {})

    address = gateway = None
    nameservers = []
    for section in ["bonds", "ethernets", "bridges", "vlans"]:
        for _, cfg in (network.get(section) or {}).items():
            if not address:
                for addr in cfg.get("addresses") or []:
                    address = str(addr)
                    break
            if not gateway:
                for route in cfg.get("routes") or []:
                    if route.get("to") in ("default", "0.0.0.0/0"):
                        gateway = route["via"]
                        break
                if not gateway and cfg.get("gateway4"):
                    gateway = cfg["gateway4"]
            if not nameservers:
                nameservers = cfg.get("nameservers", {}).get("addresses", [])

    if not address:
        sys.exit("ERROR: could not find IP address in netplan config")
    if not nameservers:
        nameservers = ["8.8.8.8", "8.8.4.4"]

    iface_net = ipaddress.ip_interface(address)
    return {
        "address": address,
        "ip": str(iface_net.ip),
        "netmask": str(iface_net.network.netmask),
        "gateway": gateway,
        "nameservers": nameservers,
    }


def generate_ip_cmdline(net):
    """Generate kernel ip= parameter for Alpine's initramfs.

    Format: ip=client_ip::gateway:netmask::interface:dns1:dns2
    Alpine's init will configure this before fetching modloop/apk repos.
    """
    dns1 = net["nameservers"][0] if net["nameservers"] else ""
    dns2 = net["nameservers"][1] if len(net["nameservers"]) > 1 else ""
    gw = net["gateway"] or ""
    # Leave interface blank — Alpine's init will pick the first one with link
    return f'ip={net["ip"]}::{gw}:{net["netmask"]}::::{dns1}:{dns2}'


def extract_netboot(cache_dir):
    """Download and extract Alpine netboot tarball. Returns dict of boot file paths."""
    tarball = download_cached(
        f"{ALPINE_MIRROR}/{ALPINE_NETBOOT}",
        cache_dir,
        ALPINE_NETBOOT,
    )
    boot_dir = cache_dir / "netboot"
    vmlinuz = boot_dir / "boot" / "vmlinuz-lts"
    if not vmlinuz.exists():
        print("==> Extracting Alpine netboot")
        boot_dir.mkdir(exist_ok=True)
        run(["tar", "xzf", str(tarball), "-C", str(boot_dir)])
        # Fix permissions (tar preserves 600 on initramfs files)
        for f in (boot_dir / "boot").iterdir():
            if f.is_file():
                f.chmod(0o644)
    return {
        "vmlinuz": boot_dir / "boot" / "vmlinuz-lts",
        "initramfs": boot_dir / "boot" / "initramfs-lts",
        "modloop": boot_dir / "boot" / "modloop-lts",
    }


def prepare_extras(cache_dir, workdir):
    chroot = workdir / "chroot"
    print("==> Installing extra packages via Alpine chroot")
    minirootfs_path = download_cached(
        f"{ALPINE_MIRROR}/{ALPINE_MINIROOTFS}",
        cache_dir,
        ALPINE_MINIROOTFS,
    )
    chroot.mkdir()
    run(["tar", "xzf", str(minirootfs_path), "--no-same-owner", "-C", str(chroot)])

    repos = chroot / "etc" / "apk" / "repositories"
    repos.write_text(
        f"https://dl-cdn.alpinelinux.org/alpine/v{ALPINE_VERSION}/main\n"
        f"https://dl-cdn.alpinelinux.org/alpine/v{ALPINE_VERSION}/community\n"
    )
    (chroot / "etc" / "resolv.conf").write_text(
        "nameserver 8.8.8.8\nnameserver 8.8.4.4\n"
    )

    run(
        [
            "sudo",
            "chroot",
            str(chroot),
            "apk",
            "add",
            "--no-cache",
            "qemu-img",
            "mdadm",
            "util-linux",
            "e2fsprogs",
            "parted",
        ]
    )
    return chroot


def ensure_ipxe():
    """Clone iPXE repository if needed."""
    if (IPXE_DIR / ".git").exists():
        return
    print("==> Cloning iPXE")
    run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "https://github.com/ipxe/ipxe.git",
            str(IPXE_DIR),
        ]
    )


def build_ipxe_efi(script_path, output_path):
    """Build iPXE EFI binary with embedded boot script."""
    run(
        [
            "make",
            f"-j{os.cpu_count()}",
            "bin-x86_64-efi/ipxe.efi",
            f"EMBED={script_path}",
        ],
        cwd=IPXE_DIR / "src",
    )
    shutil.copy2(IPXE_DIR / "src" / "bin-x86_64-efi" / "ipxe.efi", output_path)


def build_node(node, boot, chroot, workdir, output_root):
    hostname = node["hostname"]
    http_server = node["http_server"]
    print(f"\n==> Building node: {hostname}")

    output_dir = output_root / hostname
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Build overlay CPIO (loaded as second initrd, overlays stock Alpine) ---
    overlay = workdir / f"overlay-{hostname}"
    overlay.mkdir()

    # Extra binaries from chroot
    for binpath in [
        "usr/bin/qemu-img",
        "sbin/mdadm",
        "sbin/blkid",
        "usr/sbin/partprobe",
        "sbin/blkdiscard",
        "bin/lsblk",
    ]:
        src = chroot / binpath
        if src.exists():
            dst = overlay / binpath
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # Shared libraries from chroot
    for lib_dir in ["lib", "usr/lib"]:
        src_dir = chroot / lib_dir
        if not src_dir.exists():
            continue
        dst_dir = overlay / lib_dir
        dst_dir.mkdir(parents=True, exist_ok=True)
        for f in src_dir.iterdir():
            if f.is_file() and ".so" in f.name:
                shutil.copy2(f, dst_dir / f.name)
            elif f.is_symlink() and ".so" in f.name:
                link_target = os.readlink(f)
                link_dst = dst_dir / f.name
                link_dst.unlink(missing_ok=True)
                os.symlink(link_target, link_dst)

    # Installer script + config
    opt = overlay / "opt"
    opt.mkdir(parents=True, exist_ok=True)
    (opt / "installer.sh").write_text((TEMPLATES / "installer.sh").read_text())
    (opt / "installer.sh").chmod(0o755)
    (opt / "install-to-disk.sh").write_text(
        (SHARED_TEMPLATES / "install-to-disk.sh").read_text()
    )
    (opt / "install-to-disk.sh").chmod(0o755)

    net = parse_netplan_network(node["netplan"])

    disk_cfg = parse_disk(node["disk"])
    if disk_cfg["mode"] == "auto":
        max_sectors = int(disk_cfg["max_size_tb"] * 1_000_000_000_000 / 512)
    else:
        max_sectors = 0
    (opt / "installer.env").write_text(
        (TEMPLATES / "installer.env")
        .read_text()
        .format(
            image=node["image"],
            http_server=http_server,
            target_disk=disk_cfg.get("target", ""),
            raid_disk1=disk_cfg.get("disk1", ""),
            raid_disk2=disk_cfg.get("disk2", ""),
            raid_auto=1 if disk_cfg["mode"] == "auto" else "",
            raid_max_sectors=max_sectors,
            wipe_all_disks=1 if node["wipe_all_disks"] else "",
            poweroff=1 if node["poweroff"] else "",
        )
    )

    # Run installer directly from inittab (bypass openrc)
    (overlay / "etc").mkdir(parents=True, exist_ok=True)
    (overlay / "etc" / "inittab").write_text(
        "::once:/opt/installer.sh </dev/console >/dev/console 2>&1\n"
        "tty1::respawn:/sbin/getty 38400 tty1\n"
        "ttyS0::respawn:/sbin/getty -L 115200 ttyS0 vt100\n"
        "::ctrlaltdel:/sbin/reboot\n"
    )

    # Allow root login on serial console (no password in installer)
    (overlay / "etc" / "securetty").write_text("console\ntty1\nttyS0\n")
    (overlay / "etc" / "shadow").write_text("root::0:0:99999:7:::\n")

    # OS overlay (written to installed disk after imaging)
    netplan_dir = overlay / "opt/overlay/etc/netplan"
    netplan_dir.mkdir(parents=True, exist_ok=True)
    (netplan_dir / "52-network.yaml").write_text(
        yaml.dump(node["netplan"], default_flow_style=False)
    )

    cloud_cfg_dir = overlay / "opt/overlay/etc/cloud/cloud.cfg.d"
    cloud_cfg_dir.mkdir(parents=True, exist_ok=True)
    ssh_keys_yaml = "\n".join(f"        - {key}" for key in node["ssh_keys"])
    instance_id = f"iid-{secrets.token_hex(8)}"
    (cloud_cfg_dir / "99_datasource.cfg").write_text(
        (TEMPLATES / "datasource.cfg")
        .read_text()
        .format(
            instance_id=instance_id,
            hostname=hostname,
            ssh_keys_yaml=ssh_keys_yaml,
        )
    )

    # Boot cmdline customization
    boot_cmdline = node["boot_cmdline"]
    if boot_cmdline:
        grub_d = overlay / "opt/overlay/etc/default/grub.d"
        grub_d.mkdir(parents=True, exist_ok=True)
        (grub_d / "99-custom-cmdline.cfg").write_text(
            f'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT {boot_cmdline}"\n'
        )

    # Build overlay tarball (Alpine apkovl format)
    print(f"==> Building overlay for {hostname}")
    overlay_path = output_dir / "overlay.tar.gz"
    build_tar_gz(overlay, overlay_path)
    overlay_size = overlay_path.stat().st_size / (1024 * 1024)
    shutil.rmtree(overlay)

    # --- iPXE EFI binary ---
    print(f"==> Building iPXE EFI binary for {hostname}")
    ipxe_script = workdir / "embed.ipxe"
    base_url = f"http://{http_server}"
    ip_param = generate_ip_cmdline(net)
    ipxe_script.write_text(
        (TEMPLATES / "boot.ipxe")
        .read_text()
        .format(
            base_url=base_url,
            ip_param=ip_param,
            alpine_version=ALPINE_VERSION,
            hostname=hostname,
        )
    )
    build_ipxe_efi(ipxe_script, output_dir / "BOOTX64.EFI")

    print(f"==> Done: {hostname} (overlay {overlay_size:.1f}M)")


def main():
    parser = argparse.ArgumentParser(
        description="Build iPXE-based UEFI HTTP Boot installers"
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output", default="output/installer")
    parser.add_argument("nodes", nargs="*", help="Nodes to build (default: all)")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = SCRIPT_DIR / config_path
    cfg = load_config(config_path)

    all_nodes = list((cfg.get("nodes") or {}).keys())
    if not all_nodes:
        sys.exit("ERROR: no nodes defined in config")

    targets = args.nodes if args.nodes else all_nodes
    for t in targets:
        if t not in (cfg.get("nodes") or {}):
            sys.exit(f"ERROR: node '{t}' not found (available: {', '.join(all_nodes)})")

    output_root = Path(args.output)
    if not output_root.is_absolute():
        output_root = SCRIPT_DIR / output_root
    nodes = [resolve_node(cfg, t) for t in targets]

    cache_dir = (
        Path(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")))
        / "cloud-image-builder"
    )
    cache_dir.mkdir(parents=True, exist_ok=True)

    ensure_ipxe()

    workdir = Path(tempfile.mkdtemp())
    try:
        boot = extract_netboot(cache_dir)
        chroot = prepare_extras(cache_dir, workdir)

        output_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(boot["vmlinuz"], output_root / "vmlinuz-lts")
        shutil.copy2(boot["initramfs"], output_root / "initramfs-lts")
        shutil.copy2(boot["modloop"], output_root / "modloop-lts")

        for node in nodes:
            build_node(node, boot, chroot, workdir, output_root)

        print(f"\n==> All done")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
