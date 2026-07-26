#!/bin/bash
# Full deb e2e loop on local Docker:
#   build the .deb (fresh, cached if sources unchanged)
#     -> install it in a Debian container
#       -> boot the installed hokku-server
#         -> upload every images/test/ image via the API
#           -> validate all are present and dithered to the 6-colour palette
#
# Run from anywhere in the repo. Exit code is the driver's verdict (0 = pass).
#
# Flags:
#   --rebuild-deb   force a .deb rebuild even if sources are unchanged
#   --no-cache      docker build --no-cache (re-run the slow pip postinst layer)
set -euo pipefail

# Windows/Git-Bash robustness. On MSYS (Git Bash) the shell rewrites unix-style
# args that look like paths, which mangles docker's container-internal paths
# (e.g. --workdir /workspace/python -> C:/Program Files/Git/workspace/python).
# Disable that; both vars are simply ignored on Linux (Pi / CI).
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'
# The locally cached debian:bookworm may be arm64 (pulled for Pi image work);
# running the amd64 host under qemu emulation is slow and flaky for numba JIT.
# Pin amd64, overridable for an arm host: DOCKER_DEFAULT_PLATFORM=linux/arm64 ...
export DOCKER_DEFAULT_PLATFORM="${DOCKER_DEFAULT_PLATFORM:-linux/amd64}"

# `docker build <context>` needs a host-native path: the daemon's -v tolerates
# the /c/... form but the CLI's build-context stat does not. cygpath exists only
# on Windows; on Linux this is an identity passthrough.
to_host_path() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_DIR="$REPO_ROOT/build"
HASH_FILE="$BUILD_DIR/.e2e_deb_src.sha"
IMAGE_TAG="hokku-e2e"

REBUILD_DEB=0
DOCKER_BUILD_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --rebuild-deb) REBUILD_DEB=1 ;;
        --no-cache) DOCKER_BUILD_ARGS+=(--no-cache) ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

# ── 1. build the deb, fresh, but cache when sources are unchanged ───────────
# Hash the inputs that actually go into the deb. If unchanged and a deb exists,
# skip the (multi-minute) rebuild.
src_hash() {
    {
        find "$REPO_ROOT/python/hokku" "$REPO_ROOT/python/debian" \
             "$REPO_ROOT/firmware/release" "$REPO_ROOT/images/logo" \
             -type f \( ! -name '*.pyc' \) -print0 2>/dev/null | sort -z | xargs -0 sha256sum
        sha256sum "$REPO_ROOT/python/pyproject.toml"
    } | sha256sum | cut -d' ' -f1
}

newest_deb() {
    ls -t "$BUILD_DIR"/hokku-server_*.deb 2>/dev/null | head -1
}

CUR_HASH="$(src_hash)"
DEB="$(newest_deb || true)"
PREV_HASH=""
[ -f "$HASH_FILE" ] && PREV_HASH="$(cat "$HASH_FILE")"

if [ "$REBUILD_DEB" -eq 1 ] || [ -z "$DEB" ] || [ "$CUR_HASH" != "$PREV_HASH" ]; then
    echo "== building hokku-server .deb (sources changed or --rebuild-deb) =="
    bash "$REPO_ROOT/python/build-deb-docker.sh"
    echo "$CUR_HASH" > "$HASH_FILE"
    DEB="$(newest_deb)"
else
    echo "== reusing cached .deb (sources unchanged): $(basename "$DEB") =="
fi

[ -n "$DEB" ] || { echo "no .deb produced or found in $BUILD_DIR" >&2; exit 1; }
echo "using deb: $DEB"

# ── 2. stage a flat, minimal docker build context ──────────────────────────
# Under build/ (a host-native path cygpath resolves cleanly), not /tmp.
CTX="$BUILD_DIR/.e2e-ctx"
rm -rf "$CTX"
mkdir -p "$CTX"
trap 'rm -rf "$CTX"' EXIT
cp "$DEB" "$CTX/hokku-server.deb"
cp -r "$REPO_ROOT/images/test" "$CTX/images"
rm -f "$CTX/images/CREDITS.md"  # not an image; driver ignores it anyway
cp "$REPO_ROOT/tools/e2e_deb_smoke.py" "$CTX/"
cp "$SCRIPT_DIR/test-config.json" "$SCRIPT_DIR/expectations.json" \
   "$SCRIPT_DIR/entrypoint.sh" "$SCRIPT_DIR/Dockerfile" "$CTX/"

# ── 3. build the test image ────────────────────────────────────────────────
echo "== docker build $IMAGE_TAG =="
docker build "${DOCKER_BUILD_ARGS[@]}" -t "$IMAGE_TAG" "$(to_host_path "$CTX")"

# ── 4. run the loop; the container exit code is the driver verdict ──────────
# Pin the container's memory so the run is deterministic regardless of the host's
# Docker VM size, and so it actually exercises the cgroup-aware memory budget
# (auto-detected → decode budget + worker count). 1 GB comfortably renders every
# test image (incl. the 38.9 MP panorama). Override with HOKKU_E2E_MEMORY.
# NOTE: the server's warmed baseline is ~290 MB on amd64, so <~768 MB will OOM
# here (arm64 / the Pi is lighter and runs in ~464 MB); keep this >= 768m.
MEM="${HOKKU_E2E_MEMORY:-1g}"
echo "== docker run $IMAGE_TAG (--memory=$MEM) =="
docker run --rm --memory="$MEM" --memory-swap="$MEM" "$IMAGE_TAG"
