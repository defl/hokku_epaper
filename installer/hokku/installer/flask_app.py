"""Flask setup wizard for hokku-installer.

Serves the initial configuration form and applies the settings when the user
submits. Also handles captive portal detection probes from iOS, Android, and
Windows so the setup page opens automatically when a phone connects.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import zoneinfo
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request

from hokku.installer import network_config, setup_state, system_config, wifi_scanner
from hokku.installer.network_config import StaticConfig
from hokku.installer.validators import (
    validate_country_code,
    validate_ipv4,
    validate_linux_password,
    validate_mdns_hostname,
    validate_prefix_length,
    validate_ssid,
    validate_timezone,
    validate_wifi_password,
)

logger = logging.getLogger(__name__)


def _resolve_template_folder() -> str:
    pkg_dir = Path(__file__).resolve().parent
    candidates = [
        pkg_dir / "templates",
        Path("/usr/share/hokku-installer/templates"),
    ]
    for c in candidates:
        if c.is_dir():
            return str(c)
    return str(candidates[0])


# Curated country list: ISO 3166-1 alpha-2 → display name.
# Covers the most common WiFi regulatory domains; sorted by name.
_COUNTRIES = [
    ("AU", "Australia"),
    ("AT", "Austria"),
    ("BE", "Belgium"),
    ("BR", "Brazil"),
    ("CA", "Canada"),
    ("CN", "China"),
    ("CZ", "Czech Republic"),
    ("DK", "Denmark"),
    ("FI", "Finland"),
    ("FR", "France"),
    ("DE", "Germany"),
    ("GR", "Greece"),
    ("HK", "Hong Kong"),
    ("HU", "Hungary"),
    ("IN", "India"),
    ("IE", "Ireland"),
    ("IL", "Israel"),
    ("IT", "Italy"),
    ("JP", "Japan"),
    ("KR", "Korea"),
    ("LU", "Luxembourg"),
    ("MX", "Mexico"),
    ("NL", "Netherlands"),
    ("NZ", "New Zealand"),
    ("NO", "Norway"),
    ("PL", "Poland"),
    ("PT", "Portugal"),
    ("RU", "Russia"),
    ("SA", "Saudi Arabia"),
    ("SG", "Singapore"),
    ("ZA", "South Africa"),
    ("ES", "Spain"),
    ("SE", "Sweden"),
    ("CH", "Switzerland"),
    ("TW", "Taiwan"),
    ("TR", "Turkey"),
    ("AE", "United Arab Emirates"),
    ("GB", "United Kingdom"),
    ("US", "United States"),
]


def _sorted_timezones() -> list[str]:
    try:
        zones = sorted(zoneinfo.available_timezones())
        return zones if zones else _FALLBACK_TIMEZONES
    except Exception:
        return _FALLBACK_TIMEZONES


def _detected_timezone() -> str:
    try:
        tz_file = Path("/etc/timezone")
        if tz_file.exists():
            return tz_file.read_text().strip()
    except OSError:
        pass
    return "UTC"


_FALLBACK_TIMEZONES = [
    "Africa/Johannesburg",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/New_York",
    "America/Sao_Paulo",
    "America/Toronto",
    "Asia/Bangkok",
    "Asia/Dubai",
    "Asia/Hong_Kong",
    "Asia/Kolkata",
    "Asia/Seoul",
    "Asia/Shanghai",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Australia/Melbourne",
    "Australia/Sydney",
    "Europe/Amsterdam",
    "Europe/Berlin",
    "Europe/Brussels",
    "Europe/London",
    "Europe/Madrid",
    "Europe/Moscow",
    "Europe/Paris",
    "Europe/Rome",
    "Europe/Stockholm",
    "Europe/Zurich",
    "Pacific/Auckland",
    "Pacific/Honolulu",
    "UTC",
]


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=_resolve_template_folder(),
    )

    # ── captive portal detection ─────────────────────────────────────────────
    # All DNS queries resolve to 192.168.11.1 (dnsmasq captive config), so
    # these well-known probe URLs arrive here. Redirect to our setup page so
    # iOS/Android/Windows auto-open the captive portal dialog.

    @app.route("/hotspot-detect.html")  # iOS / macOS
    @app.route("/library/test/success.html")  # iOS older
    def _ios_probe():
        return redirect("/", 302)

    @app.route("/generate_204")  # Android
    @app.route("/gen_204")
    def _android_probe():
        return redirect("/", 302)

    @app.route("/ncsi.txt")  # Windows 10
    def _win10_probe():
        return redirect("/", 302)

    @app.route("/connecttest.txt")  # Windows 11
    def _win11_probe():
        return redirect("/", 302)

    @app.route("/redirect")  # Windows connectivity check
    def _win_redirect():
        return redirect("/", 302)

    # ── main setup page ──────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            countries=_COUNTRIES,
            timezones=_sorted_timezones(),
            detected_timezone=_detected_timezone(),
            detected_country="GB",
        )

    @app.route("/api/scan")
    def api_scan():
        networks = wifi_scanner.scan_networks()
        return jsonify(networks)

    @app.route("/setup", methods=["POST"])
    def setup():
        errors = _validate_form(request.form)

        def _render_form(errors, status=400):
            return render_template(
                "index.html",
                errors=errors,
                form=request.form,
                countries=_COUNTRIES,
                timezones=_sorted_timezones(),
                detected_timezone=_detected_timezone(),
                detected_country="GB",
            ), status

        if errors:
            return _render_form(errors)

        try:
            _apply_settings(request.form)
        except Exception as exc:
            logger.exception("Failed to apply settings")
            return _render_form({"_apply": str(exc)}, 500)

        # Determine the URL the user can reach hokku-server at after reboot.
        mdns_on = request.form.get("mdns_enabled") == "on"
        mdns_name = request.form.get("mdns_name", "hokku").strip() or "hokku"
        network_mode = request.form.get("network_mode", "dhcp")
        static_ip = request.form.get("static_ip", "").strip()

        if network_mode == "static" and static_ip:
            server_url = f"http://{static_ip}:8080"
        elif mdns_on:
            server_url = f"http://{mdns_name}.local:8080"
        else:
            server_url = "http://&lt;your-pi-ip&gt;:8080"

        # Reboot after a short delay so the response reaches the browser.
        threading.Timer(2.0, _reboot).start()

        return render_template("success.html", server_url=server_url)

    return app


# ── form validation ──────────────────────────────────────────────────────────


def _validate_form(form) -> dict[str, str]:
    errors: dict[str, str] = {}

    def check(field, validator, *args):
        ok, reason = validator(form.get(field, "").strip(), *args)
        if not ok:
            errors[field] = reason

    check("ssid", validate_ssid)
    check("wifi_password", validate_wifi_password)
    check("country_code", validate_country_code)
    check("timezone", validate_timezone)

    hostname = form.get("hostname", "").strip()
    ok, reason = validate_mdns_hostname(hostname)
    if not ok:
        errors["hostname"] = reason

    if form.get("mdns_enabled") == "on":
        mdns_name = form.get("mdns_name", "").strip()
        ok, reason = validate_mdns_hostname(mdns_name)
        if not ok:
            errors["mdns_name"] = reason

    if form.get("network_mode") == "static":
        check("static_ip", validate_ipv4)
        check("static_prefix", validate_prefix_length)
        check("static_gateway", validate_ipv4)
        # DNS is optional; validate only if provided.
        dns = form.get("static_dns", "").strip()
        if dns:
            for entry in dns.replace(";", ",").split(","):
                entry = entry.strip()
                if entry:
                    ok, reason = validate_ipv4(entry)
                    if not ok:
                        errors["static_dns"] = f"Invalid DNS address: {reason}"
                        break

    password = form.get("admin_password", "").strip()
    confirm = form.get("admin_password_confirm", "").strip()
    if password:
        ok, reason = validate_linux_password(password)
        if not ok:
            errors["admin_password"] = reason
        elif password != confirm:
            errors["admin_password_confirm"] = "Passwords do not match"  # noqa: S105

    return errors


# ── apply settings ───────────────────────────────────────────────────────────


def _apply_settings(form) -> None:
    hostname = form.get("hostname", "hokku").strip() or "hokku"
    timezone = form.get("timezone", "UTC").strip()
    country_code = form.get("country_code", "").strip()
    ssid = form.get("ssid", "").strip()
    wifi_password = form.get("wifi_password", "").strip()
    network_mode = form.get("network_mode", "dhcp")
    mdns_on = form.get("mdns_enabled") == "on"
    mdns_name = form.get("mdns_name", hostname).strip() or hostname
    ssh_on = form.get("ssh_enabled") == "on"
    samba_on = form.get("samba_enabled") == "on"
    admin_password = form.get("admin_password", "").strip()

    static: StaticConfig | None = None
    if network_mode == "static":
        static = StaticConfig(
            ip=form.get("static_ip", "").strip(),
            prefix=int(form.get("static_prefix", "24").strip()),
            gateway=form.get("static_gateway", "").strip(),
            dns=form.get("static_dns", "8.8.8.8").strip(),
        )

    # Persist settings for audit/re-setup before making any changes.
    setup_state.save_settings(
        {
            "hostname": hostname,
            "timezone": timezone,
            "country_code": country_code,
            "ssid": ssid,
            "network_mode": network_mode,
            "static": static._asdict() if static else None,
            "mdns_enabled": mdns_on,
            "mdns_name": mdns_name,
            "ssh_enabled": ssh_on,
            "samba_enabled": samba_on,
            "password_set": bool(admin_password),
        }
    )

    system_config.set_hostname(hostname)
    system_config.set_timezone(timezone)
    if country_code:
        system_config.set_wifi_country(country_code)

    network_config.write_wifi_connection(ssid, wifi_password, static)

    system_config.set_ssh(ssh_on)
    system_config.set_samba(samba_on)

    if admin_password:
        system_config.set_user_password(admin_password)

    system_config.seed_hokku_server_config(mdns_name if mdns_on else None)

    # Hand the USB data port over to host mode on the way out of setup, so the
    # appliance can flash a frame ("Flash a screen") once it's up. It costs the
    # gadget serial console, which is why this happens HERE and not in the
    # image: setup mode keeps the console, and every route back to setup mode
    # (reset.sh, the WiFi watchdog) restores it. Deliberately non-fatal — a
    # wrong port role is a missing convenience, not a reason to fail a setup
    # that has already applied the network config and can no longer be retried
    # from this AP.
    try:
        system_config.set_usb_mode("host")
    except Exception:
        logger.exception("Could not switch the USB port to host mode — continuing")

    setup_state.mark_setup_complete()
    logger.info("Setup complete — rebooting")


def _reboot() -> None:
    subprocess.run(["systemctl", "reboot"], check=False)  # noqa: S607
