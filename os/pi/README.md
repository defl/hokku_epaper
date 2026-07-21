# Appliance image build (`os/pi`)

Builds the flashable Raspberry Pi OS image described in
[`docs/appliance.md`](../../docs/appliance.md) — a Pi Zero 2 W SD-card image with
`hokku-server` and `hokku-installer` preinstalled, which boots into a WiFi setup
wizard on first run.

**Using the appliance, rather than building it?** You want
[`docs/appliance.md`](../../docs/appliance.md) instead; prebuilt images are
attached to every [GitHub release](https://github.com/defl/hokku_epaper/releases).

## How it works

[pi-gen](https://github.com/RPi-Distro/pi-gen) (Raspberry Pi's own image builder)
runs its standard `stage0`–`stage2`, then a custom `stage-hokku` on top:

| Stage step | What it does |
|---|---|
| `00-install` | Installs the `hokku-server` and `hokku-installer` `.deb`s plus their arm64 wheels into the rootfs |
| `01-pi-tweaks` | Appliance hardening — USB gadget serial console, journald to RAM, security-only unattended upgrades, Bluetooth/avahi/swap off, `gpu_mem=16` |
| `02-systemd` | Enables `hokku-installer`, sets a placeholder WiFi country, bypasses the stock first-boot user dialog, unlocks the default account |
| `03-cleanup` | Removes build leftovers |

`config` holds the pi-gen settings: `arm64`, Bookworm, `hokku`/`hokku` default
user, SSH off at image level (the wizard turns it on if asked), no baked-in WiFi.

## Building

You need both `.deb`s first:

```sh
cd python    && bash build-deb.sh   # -> build/hokku-server_*.deb
cd installer && bash build-deb.sh   # -> build/hokku-installer_*.deb
```

Then, from the repo root:

```sh
bash os/pi/build-image.sh
```

Output lands in `build/` as `image-<date>-hokku.img.xz` (~880 MB compressed,
~3.9 GB written). Expect roughly 45–50 minutes.

**Requires a Linux host.** pi-gen builds a Linux rootfs with `debootstrap` and
qemu user emulation; it does not run on Windows or macOS directly. Use WSL2, a
VM, or a Pi. The simplest path on any platform is to let CI do it — see below.

### Environment overrides

| Variable | Default | Purpose |
|---|---|---|
| `DEB_SERVER` | newest `build/hokku-server_*.deb` | Path to the server package |
| `DEB_INSTALLER` | newest `build/hokku-installer_*.deb` | Path to the installer package |
| `SKIP_WHEELS` | unset | Set to `1` to reuse already-downloaded arm64 wheels |
| `PIGEN_DIR` | `.pigen` | Where pi-gen is cloned |
| `PIGEN_DOCKER` | `1` | `1` uses pi-gen's `build-docker.sh`; `0` runs its native `build.sh` with `sudo` |

`PIGEN_DOCKER=0` exists for GitHub Actions: inside a runner, `build-docker.sh`'s
`binfmt_misc` registration doesn't reliably propagate into the nested
`--privileged` container it spawns, so pi-gen's `arch-test` fails even when the
host's qemu setup looks correct. Building directly on the runner's real,
unnested kernel avoids the problem. On a normal dev machine, leave it at `1`.

## Building in CI

[`.github/workflows/build-image.yml`](../../.github/workflows/build-image.yml)
builds the image on a GitHub-hosted runner:

- **Manually** — run the workflow, optionally pinning which `ci.yml` run the
  `.deb`s come from.
- **On release** — publishing a GitHub release builds the image against that
  release's commit and attaches the `.img.xz` to it automatically.

Before dispatching a manual build, check nothing is already running:

```sh
gh run list --workflow=build-image.yml --branch=<branch> --limit 3
```

Two concurrent builds of the same commit waste roughly 45 minutes of runner time
each, and their artifacts share a date-stamped filename.

## Testing a built image

Write it to a card, boot a Pi, and follow
[`docs/appliance.md`](../../docs/appliance.md). Because the appliance is headless
by design, the [USB serial console](../../docs/os_pi_usb_console.md) — baked in by
`01-pi-tweaks` — is the fastest way to see what a misbehaving image is doing: one
USB cable to your computer gives a login prompt with no monitor or keyboard.
