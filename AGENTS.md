# Agent rules for hokku_epaper

Sub-directory rules: [firmware/AGENTS.md](firmware/AGENTS.md) | [python/AGENTS.md](python/AGENTS.md)

## Python environment
- Venv: `.venv/` at repo root
- Windows: `.venv/Scripts/python` | Linux/macOS: `.venv/bin/python`
- Dependencies: `requirements.txt` (direct deps, unpinned; pip resolves transitive)
- Recreate: `pip install -r requirements.txt`

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

Firmware uses `PROTOCOL.CONFIG.N` versioning stored in `firmware/VERSION`:
- **`PROTOCOL`** — server↔client wire protocol (HTTP API between device and server). Bump only on backwards-incompatible wire-protocol changes. Agents MUST warn the human and wait for their decision before bumping.
- **`CONFIG`** — NVS configuration schema. Bump when NVS fields are added, removed, or incompatibly changed. When bumping, also update `CONFIG_VERSION` in `tools/hokku_config.py` to the same value.
- **`N`** — monotonic counter for all firmware changes. **Never resets.** Agents increment `N` for every firmware code change; include the updated `firmware/VERSION` in the same commit.

## Tool scripts
- All standalone Python helper/dev scripts belong in `tools/`
- Do not create or leave `.py` files in the repo root

## Hardware
- Per-screen hardware facts live under `docs/screens/<screen_id>/hardware_facts.md` (may be inaccurate — treat with caution)
- EPF1301 (Hokku 13.3"): `docs/screens/epf1301/hardware_facts.md`

## Repository
- GitHub: `https://github.com/defl/hokku_epaper`
