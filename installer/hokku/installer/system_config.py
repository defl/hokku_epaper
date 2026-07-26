"""Apply system-level configuration changes: hostname, timezone, SSH, Samba, password.

All functions call system tools directly (hostnamectl, timedatectl, systemctl,
chpasswd) and raise RuntimeError on failure. The Flask POST handler should
catch these and return an error response rather than letting them bubble up.
"""

from __future__ import annotations

import grp
import json
import logging
import os
import pwd
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_HOKKU_USER = "hokku"
_DEFAULT_HOKKU_SERVER_CONFIG = Path("/usr/share/hokku-server/config.json.example")
_HOKKU_SERVER_CONFIG = Path("/var/lib/hokku/config.json")


def _run(cmd: list[str], *, input: str | None = None) -> None:
    result = subprocess.run(
        cmd,
        input=input,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd)!r} failed (exit {result.returncode}): {result.stderr.strip()}"
        )


def set_hostname(hostname: str) -> None:
    logger.info("Setting hostname to %r", hostname)
    _run(["hostnamectl", "set-hostname", hostname])


def set_timezone(timezone: str) -> None:
    logger.info("Setting timezone to %r", timezone)
    _run(["timedatectl", "set-timezone", timezone])


def set_wifi_country(country_code: str) -> None:
    logger.info("Setting WiFi country to %r", country_code)
    _run(["raspi-config", "nonint", "do_wifi_country", country_code])


def set_ssh(enabled: bool) -> None:
    action = "enable" if enabled else "disable"
    logger.info("%s SSH", action)
    if enabled:
        # pi-gen strips host keys from the golden image (so cloned devices
        # don't share identical keys); stock Raspberry Pi OS regenerates
        # them via a separate regenerate_ssh_host_keys.service that's only
        # wired up when SSH is turned on through raspi-config. Our wizard
        # enables ssh.service directly, bypassing that — confirmed live,
        # sshd's own ExecStartPre fails outright ("no hostkeys available")
        # with no keys present. -A generates any missing key types and is a
        # no-op for ones that already exist, so this is safe to run every
        # time the wizard (re-)enables SSH.
        _run(["ssh-keygen", "-A"])
    _run(["systemctl", action, "--now", "ssh"])


def set_samba(enabled: bool) -> None:
    action = "enable" if enabled else "disable"
    logger.info("%s Samba", action)
    _run(["systemctl", action, "--now", "smbd", "nmbd"])


def set_user_password(password: str) -> None:
    """Set the password for the hokku system user via chpasswd."""
    logger.info("Updating password for user %r", _HOKKU_USER)
    _run(["chpasswd"], input=f"{_HOKKU_USER}:{password}\n")


def seed_hokku_server_config(mdns_hostname: str | None) -> None:
    """Pre-write hokku-server config.json so it starts with the right mDNS name.

    hokku-server's ExecStartPre only copies the default config if the file does
    not already exist, so writing it here before first start is safe.
    """
    if _HOKKU_SERVER_CONFIG.exists():
        logger.info("hokku-server config already exists, skipping seed")
        return

    src = _DEFAULT_HOKKU_SERVER_CONFIG
    if not src.exists():
        logger.warning("No default hokku-server config found at %s, skipping seed", src)
        return

    try:
        config = json.loads(src.read_text(encoding="utf-8"))
        if mdns_hostname is not None:
            config["mdns_hostname"] = mdns_hostname
        _HOKKU_SERVER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        _HOKKU_SERVER_CONFIG.write_text(json.dumps(config, indent=2), encoding="utf-8")
        # Ensure hokku user owns the file.
        try:
            uid = pwd.getpwnam("hokku").pw_uid
            gid = grp.getgrnam("hokku").gr_gid
            os.chown(_HOKKU_SERVER_CONFIG, uid, gid)
        except (KeyError, PermissionError):
            pass  # user may not exist yet during testing
        logger.info("Seeded hokku-server config with mdns_hostname=%r", mdns_hostname)
    except Exception as exc:
        logger.warning("Could not seed hokku-server config: %s", exc)
