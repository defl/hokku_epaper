# `hokku-installer` — first-boot setup wizard

The Debian package that makes the [appliance image](../docs/appliance.md)
self-configuring. On a Pi that has never been set up it raises a WiFi access
point, serves a captive-portal setup form, applies the answers, and hands over to
`hokku-server`.

**Setting up an appliance, rather than working on this code?** See
[`docs/appliance.md`](../docs/appliance.md).

## Flow

```
first boot
   -> hokku-installer.service          (sentinel absent, so it runs)
   -> nmcli raises AP "Hokku Setup"    ap_manager.py, 192.168.11.1/24, open
   -> dnsmasq: DHCP .10-.100 + all DNS -> 192.168.11.1   (captive portal)
   -> Flask wizard on port 80          flask_app.py
   -> user submits the form
   -> system_config.py applies hostname/tz/country/WiFi/SSH/Samba/password
   -> setup_state.py writes the sentinel
   -> reboot
   -> hokku-installer.service skips    (sentinel present)
   -> hokku-server starts              (its own condition now satisfied)
```

The handover between the two services is done entirely with **systemd conditions
on a sentinel file**, `/var/lib/hokku-installer/setup_complete`:

- `hokku-installer.service` — `ConditionPathExists=!` … runs only when it's absent.
- `hokku-server.service` — `ConditionPathExists=|` … runs only when it's present
  (or when the installer isn't installed at all, so plain `apt` installs are
  unaffected).

Both units stay permanently enabled; the conditions alone decide which runs.
**Do not add `Conflicts=` between them** — it is bidirectional and resolved
during systemd's transaction verification, *before* either condition is
evaluated, which silently drops `hokku-server` from the boot transaction
entirely. That bug shipped once and cost a full debugging session; see the
comment in `debian/hokku-installer.service`.

## Recovery

**Automatic** — `hokku-wifi-watchdog.service` runs once after a post-setup boot.
It waits 90 s, then polls for up to 120 s more. If WiFi never connects (bad
password, network out of range) it deletes the sentinel and reboots, so the setup
AP comes back instead of leaving a headless brick on the shelf.

**Manual** — `reset.sh`, installed to `/usr/lib/hokku-installer/reset.sh`, does
the same thing on demand: clears the sentinel and reboots. Photos and server
settings are untouched.

## Layout

| Path | Purpose |
|---|---|
| `hokku/installer/flask_app.py` | Wizard routes, form validation, captive-portal probe URLs |
| `hokku/installer/ap_manager.py` | Raises/tears down the AP via `nmcli` |
| `hokku/installer/system_config.py` | Applies hostname, timezone, WiFi country, SSH, Samba, password |
| `hokku/installer/network_config.py` | Writes the target WiFi connection (DHCP or static) |
| `hokku/installer/setup_state.py` | Sentinel + saved answers |
| `hokku/installer/validators.py` | Field validation shared by form and tests |
| `files/dnsmasq-ap.conf` | DHCP range and catch-all DNS for the portal |
| `files/wifi-watchdog.sh` | The automatic-recovery check |
| `files/reset.sh` | Manual revert to setup mode |
| `debian/` | Packaging, systemd units, postinst |

## Notes for changes here

- **`ipv4.method=manual`, not `shared`**, in `ap_manager.py`. `shared` makes
  NetworkManager start its own dnsmasq, which fights ours over port 53.
- **A WiFi country must be set before the radio works.** A never-configured Pi
  comes up rfkill-soft-blocked, so the AP can't start and the installer
  crash-loops — a deadlock, since the country is normally collected *by* the
  wizard. The image bakes in a placeholder at build time
  (`os/pi/stage-hokku/02-systemd/00-run.sh`); the wizard overwrites it with the
  user's real answer.
- **`ssh-keygen -A` before enabling sshd.** pi-gen strips host keys from the
  image, and the mechanism that normally regenerates them is wired to
  raspi-config's own SSH toggle, not ours. Without it sshd exits with
  "no hostkeys available" and SSH silently never comes up.
- Field names in `flask_app.py` and `templates/index.html` must stay in sync with
  `validators.py`; `python/tests/` covers the validation paths.

## Building

```sh
cd installer && bash build-deb.sh   # -> build/hokku-installer_*.deb
```

The image build picks the newest one out of `build/` automatically — see
[`os/pi/README.md`](../os/pi/README.md).
