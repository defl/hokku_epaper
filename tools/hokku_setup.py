#!/usr/bin/env python3
"""Hokku/Huessen E-Ink Frame Setup.

Main-menu driven installer that orchestrates:
  - Raspberry Pi OS SD card imaging + webserver install (optional)
  - ESP32-S3 frame configuration / firmware flashing (full or partial)
  - Cache management (prefetch all release assets, wipe cache)

Usage:
    python hokku_setup.py
"""
import shutil
import sys
import urllib.error
from pathlib import Path

import esp32_setup
import pi_installer
import release_cache


def _banner():
    print()
    print("  Hokku/Huessen E-Ink Frame Setup")
    print("  ================================")
    print()


def _fmt_size(n):
    if n < 1024:
        return f"{n} B"
    for unit, div in [("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)]:
        v = n / div
        if v < 1024:
            return f"{v:.1f} {unit}"
    return f"{n / 1024**3:.1f} GB"


# ---------- startup state scan ----------

def _scan_device_status():
    """Scan for an attached ESP32-S3 and return a dict summarising state, or
    None if no candidate device is attached. Any esptool/serial failure is
    swallowed so the menu always renders."""
    try:
        devices = esp32_setup.scan_devices()
    except Exception as e:
        return {"error": str(e)}
    # Prefer a real ESP32-S3; fall back to "some serial device" for diagnostics.
    esp32s = [d for d in devices if d["is_esp32"]]
    if esp32s:
        return {"device": esp32s[0]}
    if devices:
        return {"other_devices": devices}
    return None


def _print_device_status(status):
    print("  Connected frame")
    print("  ---------------")
    if status is None:
        print("  No serial devices detected. Connect the frame via USB to enable")
        print("  ESP32 options.")
        return
    if "error" in status:
        print(f"  Scan error: {status['error']}")
        return
    if "other_devices" in status:
        print("  No ESP32-S3 detected. Other serial ports present:")
        for d in status["other_devices"]:
            print(f"    {d['port']} — {d['description']}")
        return

    dev = status["device"]
    cfg = dev.get("config") or {}
    print(f"  Port:      {dev['port']}")
    if dev.get("has_hokku_firmware"):
        dv = dev.get("device_version") or "(unknown)"
        rv = dev.get("release_version")
        if rv and dev.get("firmware_current") is True:
            print(f"  Firmware:  {dv}  (up to date)")
        elif rv and dev.get("firmware_current") is False:
            print(f"  Firmware:  {dv}  (UPDATE AVAILABLE → {rv})")
        else:
            print(f"  Firmware:  {dv}")
    else:
        print("  Firmware:  (not Hokku firmware — will be overwritten)")

    if dev.get("config_version_ok") and cfg:
        ssid = cfg.get("wifi_ssid") or "(not set)"
        screen = cfg.get("screen_name") or "(unnamed)"
        url = cfg.get("image_url") or ""
        # Strip to "<ip>:<port>" for a compact line.
        server = url.replace("http://", "").split("/")[0] if url else "(not set)"
        print(f"  Config:    WiFi={ssid}, server={server}, name={screen}")
    elif dev.get("has_hokku_firmware"):
        print("  Config:    (none — device needs configuration)")
    else:
        print("  Config:    n/a")


# ---------- cache actions ----------

CACHE_PATTERNS = [
    ("Pi OS images",     "*raspios*.img*"),
    ("hokku-server deb", "hokku-server_*.deb"),
    ("firmware bundles", "firmware/**/*"),
    ("install settings", "settings.json"),
    ("partial downloads", "**/*.part"),
]


def _cache_entries():
    """Walk .cache/ and return a list of {path, size, label} for display."""
    cache = release_cache.CACHE_DIR
    if not cache.exists():
        return []
    entries = []
    for label, pattern in CACHE_PATTERNS:
        for p in sorted(cache.glob(pattern)):
            if p.is_file():
                entries.append({"path": p, "size": p.stat().st_size, "label": label})
    return entries


def _parse_firmware_tag(filename):
    """Extract the tag from 'hokku-firmware_<tag>.bin'. Returns tag or 'local'."""
    stem = Path(filename).stem  # drops .bin
    if stem.startswith("hokku-firmware_"):
        return stem[len("hokku-firmware_"):]
    return "local"


def _fetch_firmware_from_github():
    """Download the merged firmware asset from the latest GitHub release into
    .cache/firmware/<tag>/. Returns the cached Path or None on failure."""
    try:
        rel = release_cache.get_latest_release()
    except urllib.error.HTTPError as e:
        print(f"  ERROR: GitHub API returned {e.code} {e.reason}")
        return None
    except Exception as e:
        print(f"  ERROR: could not reach GitHub: {e}")
        return None

    tag = rel.get("tag_name", "latest")
    asset = release_cache.find_asset(rel, esp32_setup._is_merged_firmware_asset)
    if asset is None:
        print(f"  ERROR: release {tag} has no hokku-firmware_*.bin asset.")
        return None

    target_dir = esp32_setup.FIRMWARE_CACHE_DIR / tag
    return release_cache.ensure_cached_asset(asset, target_dir, label=f"(release {tag})")


def _import_firmware_from_local(local_path):
    """Copy a locally-built merged firmware into .cache/firmware/<tag>/.
    Returns the cached Path or None on failure."""
    tag = _parse_firmware_tag(local_path.name)
    target_dir = esp32_setup.FIRMWARE_CACHE_DIR / tag
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / local_path.name
    try:
        if target.resolve() != local_path.resolve():
            shutil.copy2(local_path, target)
            print(f"  Imported {local_path} -> {target} ({_fmt_size(target.stat().st_size)})")
        else:
            print(f"  Already at target path: {target}")
        return target
    except OSError as e:
        print(f"  ERROR: could not copy: {e}")
        return None


def _fetch_firmware_with_local_choice():
    """If a local firmware build exists, ask whether to import or download.
    Otherwise go straight to GitHub. Returns cached Path or None."""
    local = esp32_setup._merged_firmware_file(esp32_setup.LOCAL_FIRMWARE_DIR)
    if local is None:
        print("  No local firmware build found; downloading from GitHub.")
        return _fetch_firmware_from_github()

    print(f"  Local firmware build: {local}  ({_fmt_size(local.stat().st_size)})")
    print("    [L]  import the local build into .cache")
    print("    [D]  download the latest release from GitHub instead")
    print("    [S]  skip")
    while True:
        choice = input("    [L]> ").strip().lower() or "l"
        if choice in ("l", "local"):
            return _import_firmware_from_local(local)
        if choice in ("d", "download"):
            return _fetch_firmware_from_github()
        if choice in ("s", "skip"):
            print("  Firmware: skipped.")
            return None
        print(f"    Unknown choice {choice!r}; pick L, D, or S.")


def action_download_everything():
    """Prefetch the Pi OS image, hokku-server .deb, and merged firmware into
    .cache/. Each asset skips its download if already cached at the expected
    size. Returns 0 on success, 1 if any required asset failed."""
    print()
    print("  Download everything into .cache")
    print("  -------------------------------")
    release_cache.CACHE_DIR.mkdir(exist_ok=True)

    # 1. Pi OS image — separate API (downloads.raspberrypi.com)
    print()
    print("  [1/3] Pi OS Lite 64-bit image")
    try:
        image = pi_installer.prompt_image_path()  # handles cache + download, prompts for path
    except Exception as e:
        print(f"  ERROR: {e}")
        image = None
    if image is None:
        print("  Pi OS image: SKIPPED or failed.")

    # 2. hokku-server .deb — GitHub release
    print()
    print("  [2/3] hokku-server .deb")
    deb = pi_installer.fetch_latest_release_deb()
    if deb is None:
        print("  .deb: FAILED")

    # 3. Firmware merged bin.
    # If a local build exists (firmware/release/hokku-firmware_*.bin) ask the
    # user whether to import it or pull the latest release from GitHub —
    # they might be running this to capture a dev build in .cache/, or to
    # refresh an old cache from the official release. Don't guess.
    print()
    print("  [3/3] Merged firmware")
    merged = _fetch_firmware_with_local_choice()
    if merged is None:
        print("  Firmware: FAILED")

    print()
    print("  Cache contents after download:")
    for e in _cache_entries():
        print(f"    {e['path']}  ({_fmt_size(e['size'])})")
    return 0 if (deb and merged) else 1


def action_clear_cache():
    """Delete recognised asset files from .cache/. Leaves unrecognised files
    alone so a user who drops something in there by hand doesn't lose it."""
    print()
    print("  Clear .cache")
    print("  ------------")
    entries = _cache_entries()
    if not entries:
        print("  .cache/ is already empty (or contains only files the installer doesn't manage).")
        return 0
    total = sum(e["size"] for e in entries)
    print(f"  Will delete {len(entries)} file(s), {_fmt_size(total)} total:")
    for e in entries:
        print(f"    {e['path']}  ({_fmt_size(e['size'])})")
    print()
    ans = input("  Proceed? [y/N]: ").strip().lower()
    if ans not in ("y", "yes"):
        print("  Aborted.")
        return 0
    for e in entries:
        try:
            e["path"].unlink()
        except OSError as err:
            print(f"    failed to delete {e['path']}: {err}")
    # Also remove now-empty firmware/<tag>/ dirs.
    fw_base = release_cache.CACHE_DIR / "firmware"
    if fw_base.exists():
        for sub in fw_base.iterdir():
            if sub.is_dir() and not any(sub.iterdir()):
                sub.rmdir()
    print("  Done.")
    return 0


# ---------- main menu ----------

def _menu_default(status):
    """Pick a sensible default option based on device state."""
    if status is None or "device" not in status:
        return "1"  # no device → full Pi install likely
    dev = status["device"]
    if not dev.get("has_hokku_firmware"):
        return "3"  # configure + flash
    if not dev.get("config_version_ok"):
        return "4"  # has firmware, needs config
    if dev.get("firmware_current") is False:
        return "5"  # firmware update
    return "1"


def _print_menu(default):
    print("  What would you like to do?")
    options = [
        ("1", "Full install — image SD card, then configure + flash ESP32"),
        ("2", "Server only — image SD card with hokku-server, skip ESP32"),
        ("3", "ESP32: configure + flash firmware"),
        ("4", "ESP32: configure only (keep existing firmware)"),
        ("5", "ESP32: flash firmware only (keep existing config)"),
        ("6", "Advanced — install settings, cache management"),
        ("7", "Exit"),
    ]
    for num, label in options:
        marker = "  <-- default" if num == default else ""
        print(f"    [{num}] {label}{marker}")
    print()


# ---------- advanced submenu ----------

def action_show_settings():
    """Show the cached install settings and let the user re-enter them."""
    print()
    print("  Install settings (from .cache/settings.json)")
    print("  --------------------------------------------")
    s = release_cache.load_settings()
    if not s:
        print("  No cached settings yet — running the installer will create some.")
    else:
        def _show(label, key, mask=False):
            val = s.get(key)
            if val is None or val == "":
                display = "(unset)"
            elif mask:
                display = "(set)"
            else:
                display = val
            print(f"    {label:15s} {display}")
        _show("wifi_ssid:",    "wifi_ssid")
        _show("wifi_pass:",    "wifi_pass",  mask=True)
        _show("user:",         "user")
        _show("password:",     "password",   mask=True)
        _show("ssh_enabled:",  "ssh_enabled")
        _show("samba:",        "samba")
        _show("country:",      "country")
        _show("timezone:",     "timezone")

    print()
    print("    [1] Re-enter all settings (prompts with current values as defaults)")
    print("    [2] Clear all settings (next install starts from built-in defaults)")
    print("    [3] Back")
    choice = input("    [3]> ").strip() or "3"
    if choice == "1":
        # collect_install_config reads sticky, prompts, saves. Discard result —
        # we only want the save side-effect.
        pi_installer.collect_install_config()
        return 0
    if choice == "2":
        try:
            release_cache.SETTINGS_FILE.unlink(missing_ok=True)
            print("  Cleared.")
        except OSError as e:
            print(f"  Failed to clear: {e}")
            return 1
        return 0
    return 0  # back


def _print_advanced_menu():
    print()
    print("  Advanced")
    print("  --------")
    for num, label in [
        ("1", "Show / edit install settings"),
        ("2", "Download everything into .cache"),
        ("3", "Clear .cache"),
        ("4", "Back to main menu"),
    ]:
        print(f"    [{num}] {label}")
    print()


def action_advanced():
    """Advanced submenu loop — stays here until the user picks Back."""
    while True:
        _print_advanced_menu()
        choice = input("    [4]> ").strip() or "4"
        if choice == "1":
            action_show_settings()
        elif choice == "2":
            action_download_everything()
        elif choice == "3":
            action_clear_cache()
        elif choice == "4":
            return 0
        else:
            print(f"    Unknown choice {choice!r}.")


def _dispatch(choice):
    """Run the chosen action. Returns ('continue', rc) to re-display the menu,
    or ('exit', rc) to quit."""
    if choice == "1":
        # Full install: Pi OS SD, then ESP32 config+flash with pre-fill.
        result = pi_installer.run()
        pi_install_ran = result is not None
        pi_credentials = None
        if pi_install_ran:
            pi_credentials = {
                "wifi_ssid": result.get("wifi_ssid"),
                "wifi_pass": result.get("wifi_pass"),
                "server_ip": result.get("server_ip"),
            }
        else:
            print()
            print("  Pi install did not complete. Continuing to ESP32 phase anyway.")
        print()
        print("  ESP32 phase")
        print("  -----------")
        return "continue", esp32_setup.run(pi_credentials=pi_credentials,
                                           pi_install_ran=pi_install_ran)
    if choice == "2":
        # Server only: image the SD card, run through mDNS/HTTP wait, then stop.
        result = pi_installer.run()
        if result is None:
            print()
            print("  Pi install did not complete.")
            return "continue", 1
        print()
        if result.get("webserver_ok"):
            print(f"  Server ready at http://{result['hostname']}.local:8080/ "
                  f"(IP {result.get('server_ip') or '?'}).")
        else:
            print("  Server install submitted but HTTP probe timed out — "
                  "check the Pi directly.")
        return "continue", 0
    if choice == "3":
        return "continue", esp32_setup.run_configure_and_flash()
    if choice == "4":
        return "continue", esp32_setup.run_configure_only()
    if choice == "5":
        return "continue", esp32_setup.run_flash_only()
    if choice == "6":
        return "continue", action_advanced()
    if choice == "7":
        print("  Bye!")
        return "exit", 0
    print(f"  Unknown choice {choice!r}.")
    return "continue", 1


def main():
    _pause_on_exit = "--pause-on-exit" in sys.argv
    _banner()

    last_rc = 0
    first = True
    while True:
        # Rescan on every iteration — running an action (flash, configure,
        # imaging) changes device state, so a stale status line would mislead.
        if not first:
            print()
            print()
        first = False

        status = _scan_device_status()
        _print_device_status(status)
        print()

        default = _menu_default(status)
        _print_menu(default)

        choice = input(f"  [{default}]> ").strip() or default
        action, last_rc = _dispatch(choice)
        if action == "exit":
            break

    if _pause_on_exit:
        input("\n  Press Enter to close this window. ")
    sys.exit(last_rc or 0)


if __name__ == "__main__":
    main()
