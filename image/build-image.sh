#!/bin/bash
# Build the Hokku appliance image using pi-gen.
#
# Prerequisites: Docker must be running.
# Run from the repo root:  bash image/build-image.sh
#
# Environment overrides:
#   DEB_SERVER     — path to hokku-server_*.deb  (default: build/hokku-server_*.deb)
#   DEB_INSTALLER  — path to hokku-installer_*.deb (default: build/hokku-installer_*.deb)
#   SKIP_WHEELS    — set to 1 to skip downloading arm64 wheels (use cached ones)
#   PIGEN_DIR      — pi-gen clone directory (default: .pigen)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── locate .deb packages ─────────────────────────────────────────────────────

DEB_SERVER="${DEB_SERVER:-$(ls "$REPO_ROOT"/build/hokku-server_*.deb 2>/dev/null | sort | tail -1)}"
DEB_INSTALLER="${DEB_INSTALLER:-$(ls "$REPO_ROOT"/build/hokku-installer_*.deb 2>/dev/null | sort | tail -1)}"

if [ -z "$DEB_SERVER" ] || [ ! -f "$DEB_SERVER" ]; then
    echo "ERROR: hokku-server .deb not found." >&2
    echo "       Build it with:  cd python && bash build-deb.sh" >&2
    echo "       Or set DEB_SERVER=/path/to/hokku-server_VERSION_all.deb" >&2
    exit 1
fi
if [ -z "$DEB_INSTALLER" ] || [ ! -f "$DEB_INSTALLER" ]; then
    echo "ERROR: hokku-installer .deb not found." >&2
    echo "       Build it with:  cd installer && bash build-deb.sh" >&2
    echo "       Or set DEB_INSTALLER=/path/to/hokku-installer_VERSION_all.deb" >&2
    exit 1
fi

echo "hokku-server  : $DEB_SERVER"
echo "hokku-installer: $DEB_INSTALLER"

# ── pi-gen clone ─────────────────────────────────────────────────────────────

PIGEN_DIR="${PIGEN_DIR:-$REPO_ROOT/.pigen}"

if [ ! -d "$PIGEN_DIR/.git" ]; then
    echo "Cloning pi-gen (arm64 branch) to $PIGEN_DIR ..."
    # Use the arm64 branch — purpose-built for 64-bit Pi OS. It handles the
    # raspberrypi-archive keyring and ARCH correctly, unlike master (armhf).
    git clone --depth 1 --branch arm64 https://github.com/RPi-Distro/pi-gen.git "$PIGEN_DIR"
else
    echo "Using existing pi-gen at $PIGEN_DIR"
fi

# ── stage-hokku setup ────────────────────────────────────────────────────────

STAGE_DIR="$PIGEN_DIR/stage-hokku"
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR/files"

# Copy stage scripts from our source tree.
cp -r "$SCRIPT_DIR/stage-hokku/." "$STAGE_DIR/"
chmod +x "$STAGE_DIR/"*.sh 2>/dev/null || true

# Copy the .deb packages into the stage files directory.
cp "$DEB_SERVER"    "$STAGE_DIR/files/"
cp "$DEB_INSTALLER" "$STAGE_DIR/files/"

# ── arm64 wheels ─────────────────────────────────────────────────────────────

WHEELS_DIR="$STAGE_DIR/files/wheels"

if [ "${SKIP_WHEELS:-0}" = "1" ] && [ -d "$WHEELS_DIR" ] && [ "$(ls -A "$WHEELS_DIR" 2>/dev/null)" ]; then
    echo "Skipping wheel download (SKIP_WHEELS=1, using existing $WHEELS_DIR)"
else
    echo "Downloading arm64 pip wheels..."
    bash "$SCRIPT_DIR/download-wheels.sh" "$WHEELS_DIR"
fi

# ── pi-gen config + image markers ────────────────────────────────────────────

# Config
cp "$SCRIPT_DIR/config" "$PIGEN_DIR/config"

# Suppress the stage2 image (the default Pi OS Lite image).
# We only want one image — from stage-hokku.
touch "$PIGEN_DIR/stage2/SKIP_IMAGES"

# Mark stage-hokku as the export point.
touch "$STAGE_DIR/EXPORT_IMAGE"

# ── inject Debian keyring into stage0 ────────────────────────────────────────
# stage0's 00-configure-apt runs apt-get update which needs the Debian Bookworm
# signing keys. Copy them from the build host (Debian Trixie container) into
# the rootfs's trusted.gpg.d before configure-apt runs.
# "00-aaa" sorts before "00-configure-apt" alphabetically, so it runs first.
mkdir -p "$PIGEN_DIR/stage0/00-aaa-fix-keys"
cat > "$PIGEN_DIR/stage0/00-aaa-fix-keys/00-run.sh" << 'FIX_KEYS_EOF'
#!/bin/bash -e

mkdir -p "${ROOTFS_DIR}/etc/apt/trusted.gpg.d"
for keyfile in /usr/share/keyrings/debian-archive-*.gpg; do
    [ -f "$keyfile" ] && cp "$keyfile" "${ROOTFS_DIR}/etc/apt/trusted.gpg.d/"
done
echo "[fix-keys] Copied $(ls "${ROOTFS_DIR}/etc/apt/trusted.gpg.d/" | wc -l) keyring file(s)"
FIX_KEYS_EOF
chmod +x "$PIGEN_DIR/stage0/00-aaa-fix-keys/00-run.sh"

# ── build ────────────────────────────────────────────────────────────────────

echo ""
echo "Starting pi-gen Docker build..."
echo "This typically takes 20–60 minutes."
echo ""

cd "$PIGEN_DIR"
bash build-docker.sh

# ── collect output ───────────────────────────────────────────────────────────

mkdir -p "$REPO_ROOT/build"
DEPLOY_DIR="$PIGEN_DIR/deploy"

shopt -s nullglob
images=("$DEPLOY_DIR"/*.img.xz)
shopt -u nullglob

if [ ${#images[@]} -eq 0 ]; then
    echo "ERROR: No .img.xz found in $DEPLOY_DIR" >&2
    ls "$DEPLOY_DIR" || true
    exit 1
fi

for img in "${images[@]}"; do
    dest="$REPO_ROOT/build/$(basename "$img")"
    cp "$img" "$dest"
    echo "Image: $dest  ($(du -sh "$dest" | cut -f1))"
done
