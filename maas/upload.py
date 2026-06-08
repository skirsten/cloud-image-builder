#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

"""
Upload built MAAS image tarballs to a MAAS server.

Uses MAAS's chunked upload API so large tarballs aren't rejected by a
reverse proxy (413 Request Entity Too Large) or MAAS's own request size
limit:
  1. POST /api/2.0/boot-resources/ with metadata (name, sha256, size)
     but no content — server creates the resource and returns an
     upload_uri per file.
  2. PUT 4 MiB chunks to the upload_uri until the declared size is sent.

Auth: MAAS_API_URL and MAAS_API_KEY (same env vars the Terraform MAAS
provider uses). MAAS_API_URL is the base URL — `/api/2.0/...` is
appended here (a trailing /api/2.0/ on the env var is tolerated).

HTTP is done via curl so HTTP_PROXY / HTTPS_PROXY (including socks5://)
work the same way they do elsewhere in our infra.

Usage:
  ./maas/upload.py <image> [image ...]
"""

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB — matches what the MAAS CLI uses


def maas_auth_header(api_key: str) -> str:
    """OAuth1 PLAINTEXT header — MAAS's auth scheme."""
    try:
        consumer_key, token_key, token_secret = api_key.split(":")
    except ValueError:
        sys.exit("MAAS_API_KEY must be 'consumer_key:token_key:token_secret'")
    # PLAINTEXT signature: empty consumer secret + & + token secret.
    # URL-encode the & so it survives the quoted Authorization param value.
    params = [
        ("oauth_consumer_key", consumer_key),
        ("oauth_token", token_key),
        ("oauth_signature_method", "PLAINTEXT"),
        ("oauth_signature", f"%26{token_secret}"),
        ("oauth_nonce", secrets.token_hex(16)),
        ("oauth_timestamp", str(int(time.time()))),
        ("oauth_version", "1.0"),
    ]
    return "OAuth " + ", ".join(f'{k}="{v}"' for k, v in params)


def curl_args_for_proxy() -> list[str]:
    proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or ""
    )
    if proxy.startswith("socks5://"):
        return ["--socks5", proxy.removeprefix("socks5://")]
    if proxy.startswith("socks5h://"):
        return ["--socks5-hostname", proxy.removeprefix("socks5h://")]
    if proxy:
        return ["--proxy", proxy]
    return []


def api_base(api_url: str) -> str:
    base = api_url.rstrip("/")
    if base.endswith("/api/2.0"):
        base = base[: -len("/api/2.0")]
    return base


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_upload_uris(resource: dict) -> list[str]:
    """Walk the boot-resource response and return all upload_uri strings."""
    uris: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("upload_uri"), str):
                uris.append(o["upload_uri"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(resource)
    return uris


def upload(image: str, api_url: str, api_key: str) -> None:
    src = OUTPUT / f"maas-{image}-amd64.tar.gz"
    if not src.exists():
        sys.exit(f"Image '{src}' not found")

    size = src.stat().st_size
    print(f"==> {src.name}: {size / 1e9:.2f} GB; computing sha256")
    digest = sha256_of(src)

    base = api_base(api_url)
    proxy = curl_args_for_proxy()

    # Step 1: create resource without content; MAAS returns upload_uri(s).
    print(f"==> Creating boot resource custom/{image}")
    cmd = [
        "curl", "-sS", "--fail-with-body",
        "-H", f"Authorization: {maas_auth_header(api_key)}",
        "-H", "Accept: application/json",
        "-F", f"name=custom/{image}",
        "-F", "architecture=amd64/generic",
        "-F", "filetype=tgz",
        "-F", f"size={size}",
        "-F", f"sha256={digest}",
        *proxy,
        f"{base}/api/2.0/boot-resources/",
    ]
    cp = subprocess.run(cmd, capture_output=True, text=True, check=True)
    try:
        resource = json.loads(cp.stdout)
    except json.JSONDecodeError:
        sys.exit(f"Unexpected response from MAAS: {cp.stdout!r}")

    uris = collect_upload_uris(resource)
    if not uris:
        sys.exit(
            "No upload_uri in response. Response was:\n"
            + json.dumps(resource, indent=2)
        )

    # Step 2: PUT chunks to each upload URI.
    for uri in uris:
        # MAAS returns either an absolute path ("/MAAS/api/2.0/files/.../upload/")
        # or a full URL. urljoin handles both against the original api_url.
        full = urljoin(api_url, uri)
        print(f"==> PUT chunks -> {full}")
        with open(src, "rb") as f:
            uploaded = 0
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                # Fresh OAuth nonce per chunk — MAAS treats reused nonces as replays.
                cmd = [
                    "curl", "-sS", "--fail-with-body",
                    "-X", "PUT",
                    "-H", f"Authorization: {maas_auth_header(api_key)}",
                    "-H", "Content-Type: application/octet-stream",
                    "--data-binary", "@-",
                    *proxy,
                    full,
                ]
                subprocess.run(cmd, input=chunk, check=True)
                uploaded += len(chunk)
                pct = uploaded / size * 100
                print(f"    {pct:5.1f}%  ({uploaded:>14,} / {size:,} bytes)", end="\r")
            print()

    print(f"==> Uploaded as custom/{image}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload built MAAS image tarballs to a MAAS server"
    )
    parser.add_argument("images", nargs="+", help="Image name(s) to upload")
    args = parser.parse_args()

    api_url = os.environ.get("MAAS_API_URL")
    api_key = os.environ.get("MAAS_API_KEY")
    if not api_url or not api_key:
        sys.exit("MAAS_API_URL and MAAS_API_KEY must be set")

    for image in args.images:
        upload(image, api_url, api_key)


if __name__ == "__main__":
    main()
