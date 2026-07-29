#!/bin/bash
# Container entrypoint for the deb e2e smoke test.
#
# The .deb is already installed at image-build time (so its slow pip postinst is
# cached in a layer). Here we boot the *installed* server exactly as the appliance
# would run it — the `hokku-server` entry point against a config — then run the
# HTTP driver that uploads every test image and validates the dither output.
#
# We deliberately launch the entry point directly rather than via systemd: this
# container has no init, and the point of the docker tier is packaging + deps +
# functional coverage. The systemd .service unit is exercised by the Pi
# hardware-in-the-loop tier.
set -euo pipefail

CONFIG=/opt/e2e/test-config.json
# Which expectations file the driver validates against (roomy default; override
# with -e EXPECTATIONS=/opt/e2e/expectations-constrained.json for a --memory run).
EXPECTATIONS="${EXPECTATIONS:-/opt/e2e/expectations.json}"

# Fresh, writable, EMPTY upload dir so every image arrives via the upload API
# (the real path we want to cover) rather than being pre-seeded on disk.
rm -rf /var/lib/hokku-e2e
mkdir -p /var/lib/hokku-e2e/images /var/lib/hokku-e2e/cache

# Optional self-imposed memory cap: -e MEMORY_BUDGET_MB=300 writes
# memory_budget_mb into the config so the server limits its own decode budget /
# worker count regardless of the container's actual RAM. Demonstrates the
# explicit-cap path (distinct from a real --memory cgroup limit).
if [ -n "${MEMORY_BUDGET_MB:-}" ]; then
    python3 - "$CONFIG" "$MEMORY_BUDGET_MB" <<'PY'
import json, sys
path, mb = sys.argv[1], int(sys.argv[2])
cfg = json.load(open(path))
cfg["memory_budget_mb"] = mb
json.dump(cfg, open(path, "w"))
print(f"config memory_budget_mb set to {mb}")
PY
fi

echo "== booting installed hokku-server =="
which hokku-server
hokku-server "$CONFIG" &
SERVER_PID=$!

cleanup() {
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "== running e2e driver =="
set +e
python3 /opt/e2e/e2e_deb_smoke.py \
    --base-url http://127.0.0.1:8080 \
    --images-dir /opt/e2e/images \
    --expectations "$EXPECTATIONS" \
    --startup-timeout 120 \
    --convert-timeout 900
RC=$?
set -e

echo "== e2e driver exit code: $RC =="
exit "$RC"
