This is a project where you're writing firmware for an ESP32 that drives an e-ink display.

Python environment
==================
- The project venv is in `.venv` at the repo root. Always use it: `.venv/Scripts/python` (Windows) or `.venv/bin/python` (Linux/macOS).
- To recreate the venv from scratch: `pip install -r requirements.txt`
- `requirements.txt` at the repo root is the source of truth for dependencies. Versions are not pinned — it lists direct dependencies and pip resolves the rest.

Releases
========
- **NEVER** upload, replace, or delete GitHub release assets without an explicit "yes, publish" (or equivalent) from the user for that specific change. Building a `.deb` or merged firmware locally is fine; `gh release upload`, `gh release delete-asset`, `gh release create`, `gh release edit`, and any force-pushed tag are not.
- "I see the fix works" or "tests pass" are NOT release authorisations. Ask first, every time, even after several successful releases in a row in the same session.
- After the user confirms, state exactly what will be uploaded/removed (filenames, release tag) before running the `gh` commands, so the user has a last chance to veto.

Hardware
========
- The known facts are in docs/hardware_facts.md, though this might be wrong so treat with caution

NVS Config Version
==================
- Current config version: 1
- Stored as uint8 "cfg_ver" in NVS namespace "hokku"
- Defined in firmware/main/main.c as CONFIG_VERSION and in tools/hokku_config.py as CONFIG_VERSION
- INCREMENT THIS VALUE every time NVS config fields are added, removed, or changed
- Firmware refuses to boot if cfg_ver doesn't match its CONFIG_VERSION
- hokku-setup treats mismatched cfg_ver as unconfigured

Display driver
==============
- DO NOT MODIFY the display driver code (SPI init, CS management, BUSY polling, GPIO init, epaper_reset, epaper_init_panel, epaper_send_panel, epaper_display_dual, epaper_wait_busy). It must remain identical to the main branch. Changes that look harmless (manual CS, skipping gpio_reset_pin on BUSY, fixed delays instead of BUSY polling) all break the display in subtle ways.
- GPIO0 (SPI CS) is a boot strapping pin — the SPI driver must manage it (spics_io_num = PIN_EPAPER_CS), not manual gpio_set_level
- GPIO7 (BUSY) has an external pull-up on the PCB. gpio_reset_pin enables an internal pull-up too. Both are needed for correct BUSY signaling. Do not skip gpio_reset_pin for BUSY.
- display_message() must use split_and_display() — the exact same function used for downloaded images. The buffer layout must be identical: first 480K = panel 1, second 480K = panel 2.
- After flashing the factory firmware dump (.private/flash_dump.bin) before our firmware, wait 30s for the display controller to fully reset. The factory restore puts the display in a known good state.

Firmware packaging (single merged file)
=======================================
- Every firmware build **must** produce a single merged file named `hokku-firmware_<version>.bin` (e.g. `hokku-firmware_v2.1.20.bin`). The setup tool flashes this file at offset 0x0 — it contains bootloader + partition table + app at their correct offsets.
- **Do not** commit or release the individual `bootloader.bin` / `partition-table.bin` / `hokku_epaper.bin` parts. The tool does not support the split layout; it looks only for `hokku-firmware_*.bin`.
- Build the merged file with esptool's `merge-bin` after `idf.py build`:
  ```
  esptool --chip esp32s3 merge-bin --output firmware/release/hokku-firmware_<version>.bin \
      --flash-mode dio --flash-freq 80m --flash-size 16MB \
      0x0      firmware/build/bootloader/bootloader.bin \
      0x8000   firmware/build/partition_table/partition-table.bin \
      0x10000  firmware/build/hokku_epaper.bin
  ```
- `build.bat` / `build_worktree.bat` should run `idf.py build` then this merge step. Keep only the merged file under `firmware/release/`.
- When tagging a GitHub release, attach the merged file as the single firmware asset. The setup tool downloads it from the latest release if `firmware/release/` is empty. If no `hokku-firmware_*.bin` asset is found the tool aborts — never publish a release missing this file.

Build permission
================
- **DO NOT build the `.deb` unless the user explicitly gives permission** for that specific change. "Looks good" or "sounds reasonable" are NOT build authorisations. Wait for a clear "build it", "go ahead and build", or equivalent.
- **One permission is not all permissions.** Each build requires its own explicit authorisation. Do not chain builds or assume a previous "go ahead" covers the next one.

Building the hokku-server .deb on Windows (via Docker)
======================================================
`webserver/build-deb.sh` needs Debian tooling (`dpkg-buildpackage`, `debhelper`, `pybuild-plugin-pyproject`) that isn't available on Windows. Use Docker Desktop with a `debian:trixie` container. One-shot command that works from Git Bash:

```
MSYS_NO_PATHCONV=1 docker run --rm \
    -v "/c/Users/defl/workspace/hokku_epaper":/src \
    debian:trixie bash -c '
set -e
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    debhelper dh-python python3-all python3-setuptools \
    pybuild-plugin-pyproject >/dev/null 2>&1
cp -r /src /build
cd /build/webserver
chmod -x debian/* 2>/dev/null || true
chmod +x debian/rules debian/postinst
./build-deb.sh
mkdir -p /src/build
cp /build/build/hokku-server_*.deb /src/build/
'
```

**Build output goes into `<repo-root>/build/`, never the repo root.** `webserver/build-deb.sh` sweeps the `.deb` / `.buildinfo` / `.changes` files that `dpkg-buildpackage` drops next to the source dir into `build/`. Don't bypass that — orphan `.deb` files in the repo root are easy to forget about and `*.deb` is gitignored, so they vanish silently. Find a built artifact at `build/hokku-server_<version>_all.deb`.

Upload with `gh release upload <tag> build/hokku-server_<version>_all.deb --clobber` and delete the stale `.deb` with `gh release delete-asset <tag> <old_file> --yes`.

**Never build a `.deb` from a dirty working tree.** Commit (or stash) all changes before invoking `build-deb.sh` / `dpkg-buildpackage`. Reason: the produced filename only encodes the changelog version, not the working-tree state — a `.deb` built from uncommitted edits cannot be reproduced from the git history, and "which bytes are in this build" becomes unanswerable. If a build reveals a bug (missing dep, broken postinst, etc.), commit the fix first, then rebuild. The commit-then-build cycle is cheap; the build-then-commit cycle silently produces orphan artifacts.

**Every rebuild bumps the trailing build number — no exceptions.** Every time you produce a new `.deb` for the same upstream version (`2.2.2`, `3.0.0~alpha1`, anything), bump the trailing `-N` build number in `webserver/debian/changelog` (and `webserver/pyproject.toml` if it tracks it) by one. Add a fresh changelog entry describing what changed in this build — even a one-liner ("rebuild with python3-opencv added to Depends"). **Never** overwrite an existing `-N` with new contents, and never re-emit the same `-N` filename twice. Examples: rebuild after `2.2.2-1` → `hokku-server_2.2.2-2_all.deb`; rebuild after `3.0.0~alpha1-1` → `3.0.0~alpha1-2`; rebuild after that → `-3`. Reason: each `-N` must map 1:1 to a unique set of bytes so users can tell from the filename / `dpkg -l` output exactly which build they're running. Reusing `-1` for two different builds makes "I have hokku-server 3.0.0~alpha1-1 installed" ambiguous and destroys the only cheap way to verify the user has the bytes we think they do.

Things that look harmless but break the build — lessons learned:
- **Path translation.** Git Bash auto-rewrites `/src/webserver` (a container path) into `C:/Program Files/Git/src/webserver` during `docker run -w /src/webserver`, which errors out with "invalid working directory". Prefix the entire docker invocation with `MSYS_NO_PATHCONV=1` to disable the rewrite.
- **Executable bits on the Windows volume.** The `debian/` config files (`install`, `control`, `changelog`) appear as mode 0755 through the bind mount because NTFS has no POSIX bit. `debhelper` treats any executable `debian/install` as an executable config (to be run as a script) rather than the plain list-of-files format, and blows up. Copy the `webserver/` dir *out* of the mount to `/build` inside the container first, then `chmod -x debian/*` and re-add `+x` on `debian/rules` and `debian/postinst` only. Never `chmod` on the mount itself — it's a no-op through the Windows bind.
- **Missing pybuild plugin.** Trixie's `debhelper` doesn't pull `pybuild-plugin-pyproject` by default; without it `dh_auto_configure` fails with "PEP517 plugin dependencies are not available". Install it explicitly.
- **`pip install --break-system-packages` in postinst.** Trixie's python3 is externally-managed; if the `.deb`'s postinst calls `pip install` (e.g. for pillow-heif) it must pass `--break-system-packages`, otherwise installation fails on the target Pi.

After upload, run hokku_setup.bat → Advanced → Clear .cache on the dev machine so the next install fetches the new `.deb` rather than reusing an old cached one.

Flashing procedure
==================
- For reliable results, flash the factory dump first, wait 30s, then flash our firmware: factory dump → 30s wait → bootloader + partition table + app → NVS config
- esptool works any time USB is connected regardless of firmware state (resets into ROM bootloader)
- Reflash strategy depends on regime: USB_AWAKE never deep-sleeps so the chip is always reachable while plugged into a computer. BATTERY_IDLE has only a 5 s awake window per refresh — to reflash, plug into USB to push the chip into USB_AWAKE first.

Dithering
=========
- The image dithering pipeline is explained in detail for humans in docs/dithering.md. If you change anything that affects dither output — algorithms, palette, saturation/vividness knobs, B&W detection, cache versioning — update docs/dithering.md to match. It is the one document that's meant to stay in sync with the code for non-AI readers.
- docs/dithering.md section 13 holds the benchmark reference numbers (neutral_leak, sat_hit, overall_dE across the production presets). Re-run test_dither_quality_metrics when the pipeline changes and update that table with the new numbers.
- Metric definitions (what neutral_leak means, how dE2000 is computed, etc.) live in docs/image_quality.md — do not duplicate them in dithering.md, just cross-reference.

Image quality metrics
=====================
- The image comparator (webserver/hokku_server/image_quality.py) is explained in detail for humans in docs/image_quality.md. If you add or remove a metric, change a metric's formula or thresholds, or add a new quality goal, update docs/image_quality.md to match — including the reference numbers table if the numbers shift. The unit tests live in webserver/tests/test_image_quality.py.

Reverse-engineering notes on the stock firmware
===============================================
- Everything we've learned about the stock E_Frame firmware (the one the device ships with) is written up for humans in docs/reverse_engineering_overview.md plus one file per firmware version (docs/reverse_engineering_v2.0.19_apr21.md, docs/reverse_engineering_v2.0.26_jun20.md). That's where the pin map, init-command bytes, display refresh and shutdown sequences, Ghidra findings, and things-we-got-wrong live.
- If you do another RE pass — new stock firmware version, new Ghidra run, a finding that contradicts what's in those docs — update them. If a new stock version is analyzed, add a docs/reverse_engineering_v<VER>_<DATE>.md file alongside the existing ones and add it to the list at the top of docs/reverse_engineering_overview.md. The binaries and scratch notes stay in .private/; the digested findings go in docs/.
- These docs are written for humans to onboard onto the display-driver code. Keep them complete and honest (including what didn't pan out) — they are explicitly not a summary for the AI.

Coding and compiling
====================
- always git commit firmware code before building and flashing, the comment is a 1 line summary of the change
- never use the ESP32 USB pins for anything, leave them in their original state such that USB always works
- always double check that you didn't create a fast boot loop by accident
- the firmware never auto-refreshes on boot (boot is not a refresh trigger). Refreshes happen only on schedule, button press, or first install. The reflash window on battery is 5 s by spec — if you need longer access, plug USB to enter USB_AWAKE which never sleeps.
- hard_reset after flashing ESP32 automatically
- the python environment to use is in .venv in the same directory as this file
