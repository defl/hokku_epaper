#!/bin/bash
# Build the Bigme F7 (XR872) firmware via Docker and collect it into firmware/release/.
#
# Portable entry point (Git Bash on Windows, Linux, Raspberry Pi). Runs the
# hokku-bigme-f7 builder image with the repo and the xr872_sdk checkout mounted,
# then runs ci-build.sh inside it. Equivalent to the manual docker command in
# docs/screens/bigme_f7/firmware_build.md.
#
# Env overrides:
#   XR872_SDK       path to the xr872_sdk checkout (default: sibling of the repo)
#   BUILDER_IMAGE   builder image tag (default: hokku-bigme-f7-builder:latest)
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
SDK="${XR872_SDK:-$REPO/../xr872_sdk}"
IMAGE="${BUILDER_IMAGE:-hokku-bigme-f7-builder:latest}"

if [ ! -d "$SDK" ]; then
    echo "ERROR: xr872_sdk not found at '$SDK' — clone it or set XR872_SDK." >&2
    exit 1
fi

# On Windows/Git Bash, Docker Desktop needs Windows-style mount paths.
mount_path() {
    case "$(uname -s)" in
        MINGW* | MSYS* | CYGWIN*) (cd "$1" && pwd -W) ;;
        *) (cd "$1" && pwd) ;;
    esac
}

exec docker run --rm \
    -v "$(mount_path "$REPO"):/hokku_epaper" \
    -v "$(mount_path "$SDK"):/xr872_sdk" \
    "$IMAGE" bash -lc \
    'cd /hokku_epaper/firmware/bigme_f7 && XR872_SDK=/xr872_sdk CC_DIR=/usr/bin bash ci-build.sh'
