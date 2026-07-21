"""Input validators for the hokku installer setup wizard.

Lifted from tools/pi_installer.py — validated across SSH, NetworkManager .nmconnection
ini values, and wpa_supplicant double-quoted strings.
"""

from __future__ import annotations

import zoneinfo

# Shared safe character set: printable ASCII (0x20-0x7E) minus characters that
# are hard to embed safely across bash double-quoted strings, bash heredocs,
# NetworkManager .nmconnection ini values, and wpa_supplicant double-quoted
# string values.
_DISALLOWED_ANY = set('"\\\n\r')


def _bad_chars(s, extra_disallowed=""):
    bad = set()
    for ch in s:
        code = ord(ch)
        if code < 0x20 or code > 0x7E:
            bad.add(ch)
        elif ch in _DISALLOWED_ANY or ch in extra_disallowed:
            bad.add(ch)
    return sorted(bad)


def _char_report(chars):
    return ", ".join(repr(c) for c in chars)


def validate_ssid(s):
    """Return (ok, reason). WPA SSID: 1-32 bytes, no `"`, `\\`, newlines."""
    if not s:
        return False, "SSID is empty"
    if len(s.encode("utf-8")) > 32:
        return False, f"SSID is {len(s.encode('utf-8'))} bytes (max 32)"
    bad = _bad_chars(s)
    if bad:
        return False, f"SSID contains disallowed characters: {_char_report(bad)}"
    return True, ""


def validate_wifi_password(s):
    """Return (ok, reason). WPA2 PSK: 8-63 printable ASCII (or empty for open network)."""
    if s == "":
        return True, ""  # open network
    if len(s) < 8:
        return False, "WiFi password must be at least 8 characters (WPA2 PSK requirement)"
    if len(s) > 63:
        return False, f"WiFi password is {len(s)} characters (max 63)"
    bad = _bad_chars(s)
    if bad:
        return False, f"WiFi password contains disallowed characters: {_char_report(bad)}"
    return True, ""


def validate_mdns_hostname(s):
    """Return (ok, reason). Valid mDNS label: a-z0-9 and hyphens, no leading/trailing hyphen."""
    if not s:
        return False, "Hostname is empty"
    if len(s) > 63:
        return False, f"Hostname is {len(s)} chars (max 63)"
    if not s[0].isalnum():
        return False, "Hostname must start with a letter or digit"
    if s[-1] == "-":
        return False, "Hostname must not end with a hyphen"
    for ch in s.lower():
        if not (ch.isalnum() or ch == "-"):
            return False, f"Hostname contains disallowed character: {ch!r} (allowed: a-z 0-9 -)"
    return True, ""


def validate_linux_password(s):
    """Return (ok, reason). chpasswd line: no `:` (separator), no newline/CR."""
    if not s:
        return False, "Password is empty"
    bad = _bad_chars(s, extra_disallowed=":")
    if bad:
        return False, f"Password contains disallowed characters: {_char_report(bad)}"
    return True, ""


def validate_country_code(s):
    """Return (ok, reason). ISO 3166-1 alpha-2. Two uppercase letters A-Z."""
    if not s:
        return False, "Country code is empty"
    if len(s) != 2:
        return False, f"Country code must be 2 letters (got {len(s)})"
    if not (s.isascii() and s.isalpha() and s.isupper()):
        return False, f"Country code must be 2 UPPERCASE ASCII letters (got {s!r})"
    return True, ""


def _available_timezones():
    """Return the IANA zone set from zoneinfo, or None if unavailable (e.g. Windows)."""
    try:
        tzs = set(zoneinfo.available_timezones())
        return tzs if tzs else None
    except Exception:
        return None


def validate_timezone(s):
    """Return (ok, reason). IANA zone name like Europe/London."""
    if not s:
        return False, "Timezone is empty"
    available = _available_timezones()
    if available is not None:
        if s in available:
            return True, ""
        return (
            False,
            f"{s!r} is not a known IANA timezone (e.g. Europe/London, America/New_York, UTC)",
        )
    # Fallback: enforce shape when tzdata not available.
    if " " in s or ".." in s:
        return False, f"Timezone looks malformed: {s!r} (expected e.g. Europe/London)"
    parts = s.split("/")
    for p in parts:
        if not p or not p[0].isalpha() or not all(c.isalnum() or c in "_-+" for c in p):
            return False, f"Timezone looks malformed: {s!r} (expected e.g. Europe/London)"
    return True, ""


def validate_ipv4(s):
    """Return (ok, reason). Dotted-decimal IPv4 address."""
    if not s:
        return False, "IP address is empty"
    parts = s.split(".")
    if len(parts) != 4:
        return False, f"Invalid IP address: {s!r} (expected 4 octets)"
    for part in parts:
        if not part.isdigit() or not (0 <= int(part) <= 255):
            return False, f"Invalid IP address: {s!r}"
    return True, ""


def validate_prefix_length(s):
    """Return (ok, reason). CIDR prefix length 1-30 as string."""
    if not s:
        return False, "Prefix length is empty"
    if not s.isdigit():
        return False, f"Prefix length must be a number (got {s!r})"
    n = int(s)
    if not (1 <= n <= 30):
        return False, f"Prefix length must be 1-30 (got {n})"
    return True, ""
