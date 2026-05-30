# Agent rules — python (webserver + screens)

## Build permission (`.deb`)
- DO NOT build without explicit per-change authorisation ("build it", "go ahead and build")
- One authorisation covers one build only — never chain builds
- CANNOT build from a dirty working tree — commit all changes first

## Building the hokku-server `.deb` (Windows via Docker)
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
cd /build/python
chmod -x debian/* 2>/dev/null || true
chmod +x debian/rules debian/postinst
./build-deb.sh
mkdir -p /src/build
cp /build/build/hokku-server_*.deb /src/build/
'
```
- Output: `<repo-root>/build/hokku-server_<version>_all.deb`
- Upload: `gh release upload <tag> build/hokku-server_<version>_all.deb --clobber`
- Remove stale: `gh release delete-asset <tag> <old_file> --yes`
- After upload: run hokku_setup.bat → Advanced → Clear .cache on dev machine

### Version update (before every push)

The Debian revision suffix is always `-1` — it never changes. The version number itself changes with every push via the dev version formula in root `AGENTS.md`. Before every `git push`, update:
- `pyproject.toml` `version = "..."` → PEP 440 form (e.g. `3.0.1.dev47`)
- `debian/changelog` first entry → Debian form (e.g. `3.0.1~dev.47-1`)

Add a one-liner changelog entry for each push. Include the version file changes in the same commit as the code change.

### Docker build pitfalls
- Prefix docker invocation with `MSYS_NO_PATHCONV=1` (Git Bash auto-rewrites container paths)
- Copy source out of bind mount before `chmod` (NTFS has no POSIX bits; chmod is a no-op on the mount)
- Install `pybuild-plugin-pyproject` explicitly (not pulled by trixie debhelper by default)
- `postinst` pip installs must use `--break-system-packages` (trixie python3 is externally-managed)

## Dithering
- Pipeline documented in `docs/dithering.md` — keep in sync on any change to algorithms, palette, saturation/vividness knobs, B&W detection, or cache versioning
- Benchmark reference numbers in `docs/dithering.md` section 13 — re-run `test_dither_quality_metrics` and update table when pipeline changes
- Metric definitions in `docs/image_quality.md` — cross-reference, do not duplicate

## Image quality metrics
- Comparator: `hokku_server/image_quality.py`, documented in `docs/image_quality.md`
- Tests: `tests/test_image_quality.py`
- Update `docs/image_quality.md` (including reference numbers) when adding/removing/changing metrics
