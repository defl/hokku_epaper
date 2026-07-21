"""Read/write the installer setup state sentinel and settings.

The sentinel file /var/lib/hokku-installer/setup_complete is touched once
setup finishes. Its presence tells systemd not to start hokku-installer and
to allow hokku-server to start.

The settings file /var/lib/hokku-installer/setup.json records what the user
configured, mainly for debugging and re-setup flows.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

DATA_DIR = Path("/var/lib/hokku-installer")
SENTINEL = DATA_DIR / "setup_complete"
SETTINGS_FILE = DATA_DIR / "setup.json"


def is_setup_complete() -> bool:
    return SENTINEL.exists()


def mark_setup_complete() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SENTINEL.touch()


def clear_setup_complete() -> None:
    SENTINEL.unlink(missing_ok=True)


def save_settings(settings: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write via temp file in same directory.
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".setup_", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
