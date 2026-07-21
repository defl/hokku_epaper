#!/bin/sh
# Root-run install step of the server self-update feature. Triggered via
# `sudo systemctl start --no-block hokku-server-self-update.service` by the
# unprivileged hokku-server process (see python/hokku/webserver/self_update.py)
# — never run this directly unless you know what's staged.
#
# Runs in its own systemd unit (hokku-server-self-update.service), deliberately
# NOT sharing hokku-server.service's cgroup, so it survives that service being
# stopped by its own postinst partway through this script's apt-get call.
set -e

UPDATE_DIR=/var/lib/hokku/update
STAGED_DEB="$UPDATE_DIR/staged.deb"
STAGED_META="$UPDATE_DIR/staged.json"
STATUS_FILE="$UPDATE_DIR/status.json"

# write_status <phase> [version] [error]
# Shells out to python3 (always present — it's the runtime this whole
# package exists to run) rather than hand-rolling JSON escaping here.
write_status() {
    phase="$1"
    version="$2"
    error="$3"
    python3 - "$STATUS_FILE" "$phase" "$version" "$error" <<'PYEOF'
import json
import sys
import time

status_file, phase, version, error = sys.argv[1:5]
data = {"phase": phase, "at": time.time()}
if version:
    data["version"] = version
if error:
    data["error"] = error
with open(status_file, "w") as f:
    json.dump(data, f)
PYEOF
}

fail() {
    write_status "error" "" "$1"
    echo "self-update failed: $1" >&2
    exit 1
}

if [ ! -f "$STAGED_DEB" ] || [ ! -f "$STAGED_META" ]; then
    fail "no staged update found ($STAGED_DEB / $STAGED_META missing)"
fi

PACKAGE=$(dpkg-deb -f "$STAGED_DEB" Package 2>/dev/null) || fail "staged file is not a valid .deb"
if [ "$PACKAGE" != "hokku-server" ]; then
    fail "staged .deb is package '$PACKAGE', expected 'hokku-server'"
fi

VERSION=$(dpkg-deb -f "$STAGED_DEB" Version 2>/dev/null) || fail "could not read staged .deb version"
EXPECTED_VERSION=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('expected_version_deb',''))" "$STAGED_META") \
    || fail "could not read $STAGED_META"
if [ "$VERSION" != "$EXPECTED_VERSION" ]; then
    fail "staged .deb is version '$VERSION', expected '$EXPECTED_VERSION'"
fi

write_status "installing" "$VERSION" ""

# apt-get (not `dpkg -i` + `apt-get install -f`): a single dependency-aware
# step that pulls in any new Depends the target version introduced, instead
# of a two-step unpack-then-fix-up sequence. --allow-downgrades covers a user
# who manually side-installed a newer dev build before "updating" to an
# older-looking-but-current release tag.
if ! APT_OUTPUT=$(apt-get install -y --allow-downgrades "$STAGED_DEB" 2>&1); then
    # Tail the output so a huge dependency-resolution dump doesn't blow past
    # what's reasonable to store/display; the full log is still in the
    # journal for hokku-server-self-update.service.
    TAIL=$(printf '%s' "$APT_OUTPUT" | tail -c 4000)
    fail "apt-get install failed: $TAIL"
fi

write_status "done" "$VERSION" ""
rm -f "$STAGED_DEB" "$STAGED_META"
