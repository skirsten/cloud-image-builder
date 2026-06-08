config := "config.yaml"

default:
    @just --list

# Build all images
build-all:
    ./build.py --all

# Build the base image
build-base:
    ./build.py base

# Build the nvidia-gpu image (rebuilds base if stale)
build-nvidia-gpu:
    ./build.py nvidia-gpu

# Build the nvidia-ml image (rebuilds base if stale)
build-nvidia-ml:
    ./build.py nvidia-ml

# Build all MAAS-compatible images (tar.gz, filetype=tgz) via packer-maas
build-maas-all:
    ./maas/build.py --all

# Build MAAS base image
build-maas-base:
    ./maas/build.py base

# Build MAAS nvidia-gpu image
build-maas-nvidia-gpu:
    ./maas/build.py nvidia-gpu

# Build MAAS nvidia-ml image
build-maas-nvidia-ml:
    ./maas/build.py nvidia-ml

# Upload a built MAAS image to a MAAS server (requires $MAAS_API_URL and $MAAS_API_KEY)
upload-maas image:
    ./maas/upload.py {{image}}

# Boot a built image in QEMU for testing
test image:
    #!/usr/bin/env bash
    set -eo pipefail
    userdata=$(mktemp --suffix=.yaml)
    trap "rm -f '$userdata'" EXIT
    gomplate -c .={{config}} -f templates/login.tmpl.yaml -o "$userdata"
    ./test/boot.sh {{image}} "$userdata"

# Test the image-takeover script in QEMU
test-takeover image:
    #!/usr/bin/env bash
    set -eo pipefail
    userdata=$(mktemp --suffix=.sh)
    trap "rm -f '$userdata'" EXIT
    gomplate -c .={{config}} -f templates/image-takeover.tmpl.sh -o "$userdata"
    ./test/boot.sh {{image}} "$userdata"

# Upload a built image to remote storage (e.g. just upload base r2/pkg/raw/cloudimg)
upload image dest:
    #!/usr/bin/env bash
    set -eo pipefail
    src="output/cloudimg-{{image}}-amd64.qcow2"
    if [ ! -f "$src" ]; then
        echo "Image '$src' not found"
        exit 1
    fi
    date=$(date -r "$src" +"%Y%m%d")
    mc cp "$src" "{{dest}}/cloudimg-{{image}}-${date}-amd64.qcow2"

# Render the image-takeover script to stdout
render-takeover:
    gomplate -c .={{config}} -f templates/image-takeover.tmpl.sh
