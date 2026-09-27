# Agent rules for hokku_epaper

Sub-directory rules: [firmware/huessen_epf1301/AGENTS.md](firmware/huessen_epf1301/AGENTS.md) | [python/AGENTS.md](python/AGENTS.md)

## Firmware flashing — STOP rules (read before ANY flash)

Flashing can permanently brick a device whenever the only way back into the chip's
download/recovery mode runs through the firmware you are replacing. This is not
hypothetical: it bricked `bigme_f7` on 2026-06-13 — a crash-on-boot app removed the
only software BROM trigger, and there was no hardware-independent way back in
(see `docs/screens/bigme_f7/hardware_facts.md` → "BROM / Recovery").

No tool can force these rules — there is no preflight script you are guaranteed to
run. The forcing function is THIS FILE, which you read every session. Apply the
rules directly. They are absolute and override "it's easier", "to be safe", and
"looks fine".

1. **Dual-slot / A/B only — else do not flash.** You MUST flash into the device's
   inactive OTA slot (or its equivalent A/B / dual-bank mechanism — every supported
   SoC has one; find it) and leave a known-good image bootable in the other slot, so
   a bad image falls back automatically. If you cannot flash via the dual-slot
   mechanism, you are NOT allowed to flash. **STOP and ask the human.**

2. **If the device has dual-slot / A-B support, it MUST be used *correctly* — not
   just "used".** Assume the SoC or bootloader provides an A/B / dual-bank / OTA-slot
   mechanism with a verified-flag and automatic rollback (they almost always do;
   confirm before assuming otherwise). Where it exists, you MUST drive it exactly as
   designed, not merely write into a second region:
   - Write the new image to the **inactive** slot only. NEVER overwrite the
     currently-booting known-good slot.
   - Leave the new slot **un-verified / non-active**. The new image must mark *itself*
     verified only after it has booted and passed a self-check, so the bootloader
     **auto-rolls-back** to the known-good slot on a failed boot. Marking it
     active/verified up front throws away the entire safety net.
   - You MUST understand this device's slot-selection and verified-flag semantics
     **before** flashing. If you don't know them, you don't have a recovery path —
     **STOP**, determine them (dump/reverse the bootloader, read the SDK), or ask.

3. **Early recovery hatch is mandatory — else do not flash.** Every firmware you
   build MUST include an early recovery hatch: code that runs at the very start of
   boot, BEFORE any init that can fail, and can force the device into its
   download/recovery mode (e.g. poll UART for a key → set the download-boot flag →
   reboot into BROM). If you cannot add a working recovery hatch, you are NOT allowed
   to flash. **STOP and ask the human.**

4. **Never full-erase or touch the bootloader without explicit approval.** You MUST
   NOT full-chip-erase, and MUST NOT write the bootloader / boot partitions, unless
   you actually changed the bootloader. This ALWAYS requires explicit human approval:
   state exactly what you will erase/write and wait for it. Otherwise flash only the
   app partitions you changed; leave the bootloader and the known-good slot intact.

## Colour calibration — USB upload ONLY, never over HTTP

**Getting a calibration target onto a panel MUST use the direct USB/serial frame
upload.** The `frame` console command (see
[`docs/screens/bigme_f7/custom_firmware.md`](docs/screens/bigme_f7/custom_firmware.md))
plus [`tools/f7_send_frame.py`](tools/f7_send_frame.py) push the exact bytes down
the UART. That is what the firmware feature exists for.

**Do NOT propose, and do not fall back to, serving the target over HTTP from a
hokku server** — not production, not the test Pi, not a throwaway local server.
It does not work for this purpose and the human has ruled it out. Concretely, the
server route is wrong because:

- the render pipeline re-processes the image (autocontrast, gamma, CLAHE,
  unsharp, error diffusion), so what reaches the glass is not the target that was
  generated — measuring it measures the pipeline, not the panel;
- it requires temporarily rewriting a live server's preset and the screen's
  `server_url`, both of which are disruptive and have to be restored;
- it makes the measurement depend on WiFi, poll timing and which host the screen
  happens to point at, so a session cannot be reproduced.

The USB route has none of those properties: exact bytes, on demand, no server,
no config churn. Use it. If the firmware on a unit is too old to have `frame`,
the answer is to flash it (subject to the STOP rules above), not to reach for
HTTP.

## Python environment
- Venv: `.venv/` at repo root
- Windows: `.venv/Scripts/python` | Linux/macOS: `.venv/bin/python`
- Dependencies: `requirements.txt` (direct deps, unpinned; pip resolves transitive)
- Recreate: `pip install -r requirements.txt`

## Privacy & `.private/` — never leak into commits

- **Nothing from `.private/` may ever appear in committed code, docs, or commit messages** — not its
  file contents, and **not even its internal paths, filenames, directory names, version tags, or any
  identifier found inside it.** The material under `.private/` (OEM firmware dumps, vendor flash
  tools, per-unit data, etc.) is non-distributable third-party/owner IP; leaking even the path
  structure is a leak. `.private/` is gitignored and MUST stay that way.
- **Inside `.private/` you may use any names/identifiers freely** (real serials, tags, vendor
  tool-version folders, etc.). The restriction is purely about what crosses into tracked files.
- Tracked code that needs something from `.private/` MUST NOT hardcode the path/filename. Locate it at
  runtime via a generic, IP-free mechanism: a CLI arg / env var, or a config file that itself lives in
  `.private/` (referenced only through an env var or arg, never a literal path). Keep the indirection
  generic — no embedded serials, screen names, or vendor filenames.
- **Never put PII in commits** either — device **serial numbers or any part of them** (e.g. `6000xxx`
  suffixes, full `F7CLR0W2…` serials), emails, WiFi SSIDs/passwords, account names, MAC addresses, or a
  prior owner's data. In tracked files refer to things generically ("the connected unit", `<placeholder>`).
- If you find any of the above already committed, scrub it (rewrite **unpushed** history; **ask** before
  touching pushed history).

## Git / release rules
- CAN `git commit`; commit message MUST be descriptive
- CAN `git push` (no `--tags`) as part of the dev build workflow below — this is the normal agent push
- CANNOT `git tag` without explicit human permission
- CANNOT push a GitHub release without explicit human permission
- CANNOT force-push a tag without explicit human permission
- "Looks good", "tests pass", "I see the fix works" are NOT authorisations to tag/release
- Ask explicitly before every `gh release upload`, `gh release delete-asset`, `gh release create`, `gh release edit`, or force-push of a tag
- Before running `gh` commands after authorisation, state exact filenames and release tag for user veto

## Versioning — app (webserver + Debian package)

Versions follow semantic versioning with an even/odd PATCH convention:
- **Even PATCH (0, 2, 4, …)** — release track; human-tagged only, never set by agents
- **Odd PATCH (1, 3, 5, …)** — development track; set by agents on every push

Dev version formula (run before every `git push`):
```
LAST_TAG  = git describe --tags --match "v[0-9]*" --abbrev=0
MAJOR, MINOR, PATCH = parse LAST_TAG (strip leading v, split on .)
ODD_PATCH = PATCH + 1
N         = git rev-list LAST_TAG..HEAD --count
DEV_VER   = MAJOR.MINOR.ODD_PATCH-dev.N   (semver, e.g. 3.0.1-dev.47)
DEB_VER   = MAJOR.MINOR.ODD_PATCH~dev.N-1 (Debian, e.g. 3.0.1~dev.47-1)
PEP_VER   = MAJOR.MINOR.ODD_PATCH.devN    (PEP 440, e.g. 3.0.1.dev47)
```

Before every `git push`, agents MUST:
1. Compute the dev version using the formula above
2. Update `python/pyproject.toml` `version = "..."` to `PEP_VER`
3. Update `python/debian/changelog` first entry version to `DEB_VER`
4. Include these version file changes in the same commit as the code change

Agents MUST NOT use even PATCH — that is exclusively the release track (see `/release`).

## Versioning — firmware

Firmware uses `PROTOCOL.CONFIG.N` versioning stored in `firmware/huessen_epf1301/VERSION`:
- **`PROTOCOL`** — server↔client wire protocol (HTTP API between device and server). Bump only on backwards-incompatible wire-protocol changes. Agents MUST warn the human and wait for their decision before bumping.
- **`CONFIG`** — NVS configuration schema. Bump when NVS fields are added, removed, or incompatibly changed. When bumping, also update `CONFIG_VERSION` in `tools/hokku_config.py` to the same value.
- **`N`** — monotonic counter for all firmware changes. **Never resets.** Agents increment `N` for every firmware code change; include the updated `firmware/huessen_epf1301/VERSION` in the same commit.

## Firmware — verify before push

Changing firmware C — **especially shared `firmware/common/`** — requires running the
**host test suite** locally before `git push`, not just an ESP-IDF/`idf.py` build. The
ESP-IDF build uses the real IDF headers, so it will NOT catch a newly-called IDF symbol
that the host mocks lack — that only fails later in CI's `test-firmware-*` jobs with
`-Werror=implicit-function-declaration`. For `common` and each affected screen:
`cmake -B <testdir>/build <testdir> && cmake --build <testdir>/build && ctest --test-dir <testdir>/build`
(on Windows/MSVC add `-C Debug` to ctest). When you call a new esp-idf function, add a
stub for it under `firmware/common/esp32/test/mocks/`.

## Flashing — always show what is being written

**Rule**: every flasher MUST name, for **each** region it writes, the file (or what the
image is), its size, and the destination offset/slot — before writing it. This holds
equally for the web "Flash a screen" UI and the `tools/` CLI scripts; a bare
"Flashing firmware..." or "Writing configuration..." is not enough. A flash that writes
three regions prints three lines. The descriptions live next to the flashing code (e.g.
`describe_flash_parts` / `describe_config_part` in `hokku.common.esp32.flasher`) and are
shared by every front end, so the web UI and the CLI cannot drift apart.

Rationale: a flash silently landing in a partition the device does not boot cost real
debugging time. What the operator sees must match what the flasher actually does.

## Tool scripts
- All standalone Python helper/dev scripts belong in `tools/`
- Do not create or leave `.py` files in the repo root

## Screen naming

**Rule**: Screen IDs always use `brand_model` format (e.g. `huessen_epf1301`, `bigme_f7`) — the full form, never a truncated alias (`huessen`, `bigme`). This applies everywhere a screen/firmware identifier appears: directory names under `docs/screens/`, `images/screens/`, `.private/screens/`, `python/hokku/screens/`, and `firmware/`; release artifact filenames (`hokku-<brand_model>-<version>.<ext>`); and GitHub Actions — job names (`test-firmware-<brand_model>`, `lint-firmware-<brand_model>`, `build-firmware-<brand_model>`), uploaded artifact names (`hokku-<brand_model>-firmware`), and Docker image tags built for a specific model (`hokku-<brand_model>-builder`).

## Hardware

**Rule**: `hardware_facts.md` contains ONLY empirically confirmed or definitively documented information (chip markings, measured values, official spec sheets). Inferences, SDK defaults, estimates, and unknowns belong in `hardware_guesses.md` in the same directory. Never mix the two.

- `huessen_epf1301` (Hokku/Huessen 13.3"): `docs/screens/huessen_epf1301/hardware_facts.md`
- `bigme_f7` (Bigme F7 7.3" ACeP): `docs/screens/bigme_f7/hardware_facts.md` + `hardware_guesses.md` — XR872AT SoC, firmware not yet dumped

## Datasheets

`docs/datasheets/` is a repository of third-party reference documents (PDFs).

**Rule**: PDF files are NOT committed (`.gitignore` covers `*.pdf`). Add the download
URL and notes to `docs/datasheets/ATTRIBUTION.md` and a `dl` line to
`docs/datasheets/download.sh` instead. Both files are committed; the PDFs are not.
Do this in the same commit as any other change that references the datasheet.

## Repository
- GitHub: `https://github.com/defl/hokku_epaper`
