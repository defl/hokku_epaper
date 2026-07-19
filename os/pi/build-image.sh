#!/bin/bash
# Build the Hokku appliance image using pi-gen.
#
# Prerequisites: Docker must be running.
# Run from the repo root:  bash os/pi/build-image.sh
#
# Environment overrides:
#   DEB_SERVER     — path to hokku-server_*.deb  (default: build/hokku-server_*.deb)
#   DEB_INSTALLER  — path to hokku-installer_*.deb (default: build/hokku-installer_*.deb)
#   SKIP_WHEELS    — set to 1 to skip downloading arm64 wheels (use cached ones)
#   PIGEN_DIR      — pi-gen clone directory (default: .pigen)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# SCRIPT_DIR is os/pi — two levels below the repo root (unlike the old
# one-level image/ layout), so climb up twice.
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

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

# Copy stage scripts from our source tree (maintains subdirectory structure).
cp -r "$SCRIPT_DIR/stage-hokku/." "$STAGE_DIR/"
chmod +x "$STAGE_DIR/prerun.sh" 2>/dev/null || true
find "$STAGE_DIR" -name "*.sh" -exec chmod +x {} \;

# .deb packages go in 00-install/files/ where 00-install/00-run.sh can find them.
mkdir -p "$STAGE_DIR/00-install/files"
cp "$DEB_SERVER"    "$STAGE_DIR/00-install/files/"
cp "$DEB_INSTALLER" "$STAGE_DIR/00-install/files/"

# ── arm64 wheels ─────────────────────────────────────────────────────────────

WHEELS_DIR="$STAGE_DIR/00-install/files/wheels"

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

# Remove packages from stage2 that don't exist in the Pi OS Bookworm archive
# (these were added to pi-gen's arm64 branch after the archive version we target).
sed -i '/rpi-swap\|rpi-loop-utils\|rpi-usb-gadget/d' \
    "$PIGEN_DIR/stage2/01-sys-tweaks/00-packages" 2>/dev/null || true
sed -i '/rpi-cloud-init-mods/d' \
    "$PIGEN_DIR/stage2/04-cloud-init/00-packages" 2>/dev/null || true

# Remove systemctl enables for rpi-*.service units that don't exist in the
# Pi OS Bookworm archive. Deleting the line is simpler than sed substitution.
SYS_TWEAKS_RUN="$PIGEN_DIR/stage2/01-sys-tweaks/01-run.sh"
if [ -f "$SYS_TWEAKS_RUN" ]; then
    sed -i '/systemctl enable rpi-resize/d' "$SYS_TWEAKS_RUN"
    sed -i '/systemctl enable rpi-loop/d'   "$SYS_TWEAKS_RUN"
    sed -i '/systemctl enable rpi-usb/d'    "$SYS_TWEAKS_RUN"
    echo "Patched stage2/01-sys-tweaks/01-run.sh to remove missing rpi service enables"
fi

# Mark stage-hokku as the export point.
touch "$STAGE_DIR/EXPORT_IMAGE"

# ── allow insecure apt repos during build ────────────────────────────────────
# Stage0's 00-configure-apt runs apt-get update against deb.debian.org but the
# bootstrapped rootfs doesn't yet have working Debian archive keys. Allow
# unauthenticated repos for the duration of the pi-gen build. This config is
# removed in our stage-hokku (before image export) so the final image is clean.
mkdir -p "$PIGEN_DIR/stage0/00-aaa-fix-keys"
cat > "$PIGEN_DIR/stage0/00-aaa-fix-keys/00-run.sh" << 'FIX_KEYS_EOF'
#!/bin/bash -e
# Write an apt config that allows unauthenticated repositories during build.
# Removed by stage-hokku/00-run-chroot.sh before image export.
mkdir -p "${ROOTFS_DIR}/etc/apt/apt.conf.d"
cat > "${ROOTFS_DIR}/etc/apt/apt.conf.d/00-build-insecure" << 'APTEOF'
// Allow unsigned repos during pi-gen build — removed by stage-hokku before export
Acquire::AllowInsecureRepositories "true";
Acquire::AllowDowngradeToInsecureRepositories "true";
APT::Get::AllowUnauthenticated "true";
APTEOF
echo "[fix-keys] apt configured to allow unauthenticated repos for build"
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
