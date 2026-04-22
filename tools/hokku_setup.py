#!/usr/bin/env python3
"""Hokku/Huessen E-Ink Frame Setup

End-to-end installer:
  1) Optionally images an SD card with Raspberry Pi OS and pre-configures
     it to run hokku-server on first boot.
  2) Waits for the Pi to come online at hokku-server.local.
  3) Detects the ESP32-S3 frame over USB, configures NVS, and flashes firmware.

Usage:
    python hokku_setup.py
"""
import sys

import esp32_setup
import pi_installer


def _banner():
    print()
    print("  Hokku/Huessen E-Ink Frame Setup")
    print("  ================================")
    print()


def _yesno(prompt, default_yes=True):
    suffix = "[Y/n]" if default_yes else "[y/N]"
    v = input(f"  {prompt} {suffix}: ").strip().lower()
    if not v:
        return default_yes
    return v in ("y", "yes")


def main():
    _pause_on_exit = "--pause-on-exit" in sys.argv
    _preselected_install = "--pi-install" in sys.argv  # carried across UAC relaunch
    _banner()

    pi_credentials = None
    pi_install_ran = False
    server_reachable = False

    if _preselected_install:
        do_install = True
        print("  (Continuing Pi OS install in elevated session.)")
    else:
        do_install = _yesno("Do you want to install Raspberry Pi OS on an SD card?", default_yes=False)

    if do_install:
        result = pi_installer.run()
        if result is None:
            print()
            print("  Pi install did not complete. Continuing to ESP32 phase anyway.")
        else:
            pi_install_ran = True
            pi_credentials = {
                "wifi_ssid": result.get("wifi_ssid"),
                "wifi_pass": result.get("wifi_pass"),
                "server_ip": result.get("server_ip"),
            }
            server_reachable = bool(result.get("webserver_ok"))
    else:
        print()
        print("  Checking for an existing hokku-server on the network...")
        server_reachable = pi_installer.check_existing_server()

    print()
    print("  ESP32 phase")
    print("  -----------")
    if pi_install_ran and pi_credentials and pi_credentials.get("server_ip"):
        print(f"  Will suggest server IP {pi_credentials['server_ip']} and the WiFi you just set.")
    elif not server_reachable:
        print("  NOTE: hokku-server is not reachable — you can still configure the ESP32.")

    rc = esp32_setup.run(pi_credentials=pi_credentials, pi_install_ran=pi_install_ran)
    if _pause_on_exit:
        input("\n  Press Enter to close this window. ")
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
