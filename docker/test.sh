#!/usr/bin/env bash
# Build the test image for one architecture and run it: docker/test.sh x86_64|aarch64
# Run from the repository root. A foreign architecture needs qemu registered with the kernel.
set -eu

case "${1:-}" in
    x86_64 | amd64) arch=x86_64 platform=linux/amd64 qemu=amd64 ;;
    aarch64 | arm64) arch=aarch64 platform=linux/arm64 qemu=arm64 ;;
    *)
        echo "usage: $0 x86_64|aarch64" >&2
        exit 2
        ;;
esac

if ! docker run --rm --platform "$platform" ubuntu:24.04 true >/dev/null 2>&1; then
    echo "Docker cannot run $platform containers on this $(uname -m) host." >&2
    echo "Register qemu emulation for it (lasts until reboot), then rerun:" >&2
    echo "  docker run --privileged --rm tonistiigi/binfmt --install $qemu" >&2
    exit 1
fi

tag="rookery-test:$arch"
docker build --platform "$platform" -f docker/Dockerfile -t "$tag" .
docker run --rm --platform "$platform" -e GITHUB_TOKEN -e GH_TOKEN "$tag"
