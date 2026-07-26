"""AppConfig dataclass: persisted server settings.

Strict, schema-driven parser with a version field and migration chain.
Unversioned configs (no "version" key) are silently replaced with a
fresh default v1 on first load.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

from hokku.webserver.filesystem import atomic_write_json
from hokku.webserver.image_config import (
    ImageConfig,
    _image_config_from_dict,
)
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS

logger = logging.getLogger(__name__)

_CURRENT_VERSION = 9


def _migrate_v1_to_v2(d: dict) -> dict:
    """Add image_worker_thread_count (default 1 = serial, matching old behaviour)."""
    d["image_worker_thread_count"] = 1
    return d


def _migrate_v2_to_v3(d: dict) -> dict:
    """Add face_detector (default 'yunet_opencv', matching pre-3.0 behaviour)."""
    d["face_detector"] = "yunet_opencv"
    return d


def _migrate_v3_to_v4(d: dict) -> dict:
    """Add mdns_hostname (default 'hokku' — advertise as hokku.local).

    Replaces the old boolean mdns_enabled field. Empty string = off.
    """
    d.pop("mdns_enabled", None)  # remove boolean if present from earlier alpha
    d["mdns_hostname"] = "hokku"
    return d


def _migrate_v4_to_v5(d: dict) -> dict:
    """Remove face_detector — there is now only one backend (yunet_opencv)."""
    d.pop("face_detector", None)
    return d


def _migrate_v5_to_v6(d: dict) -> dict:
    """Drop the global orientation — orientation is now per-screen only."""
    d.pop("orientation", None)
    return d


def _migrate_v6_to_v7(d: dict) -> dict:
    """Add flash_wifi_ssid/pass + ssid2/pass2 (remembered credentials for screen flashing)."""
    d.setdefault("flash_wifi_ssid", "")
    d.setdefault("flash_wifi_pass", "")
    d.setdefault("flash_wifi_ssid2", "")
    d.setdefault("flash_wifi_pass2", "")
    return d


def _migrate_v7_to_v8(d: dict) -> dict:
    """Add classifier_face_aware_crop_enabled (default False — opt-in)."""
    d.setdefault("classifier_face_aware_crop_enabled", False)
    return d


def _migrate_v8_to_v9(d: dict) -> dict:
    """Add memory_budget_mb + the firmware-library fields, and drop the old
    image_worker_thread_count knob.

    - memory_budget_mb (default 0 = auto-detect, cgroup-aware): the render worker
      count is now derived from the memory budget + cgroup-aware CPU count, so the
      manual image_worker_thread_count knob is gone.
    - firmware_dir / firmware_github_repo: the downloadable firmware library.
      Offline-first is preserved by behaviour, not a flag — the download dir is
      empty until the user acts and the server reaches GitHub only on a button click.
    """
    d.setdefault("memory_budget_mb", 0)
    d.pop("image_worker_thread_count", None)
    d.setdefault("firmware_dir", "/var/lib/hokku/firmware")
    d.setdefault("firmware_github_repo", "defl/hokku_epaper")
    return d


# v(N) → v(N+1) upgrade functions. Populated as the schema evolves.
_MIGRATIONS: dict[int, Callable[[dict], dict]] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
    4: _migrate_v4_to_v5,
    5: _migrate_v5_to_v6,
    6: _migrate_v6_to_v7,
    7: _migrate_v7_to_v8,
    8: _migrate_v8_to_v9,
}


def _migrate(data: dict) -> dict:
    """Walk the migration chain to the current version."""
    ver = int(data["version"])
    while ver < _CURRENT_VERSION:
        data = _MIGRATIONS[ver](data)
        ver += 1
        data["version"] = ver
    return data


@dataclass(frozen=True)
class AppConfig:
    """Persisted server settings."""

    version: int = _CURRENT_VERSION
    refresh_image_at_time: tuple[str, ...] = ("0600", "1200", "1800")
    upload_dir: str = "/var/lib/hokku/images"
    cache_dir: str = "/var/lib/hokku/cache"
    port: int = 8080
    #: Size of the WSGI server's fixed request-thread pool (waitress). BOUNDED on
    #: purpose: the old Werkzeug dev server spawned one thread per request and hit
    #: the Pi's process limit ("can't start new thread") when several screens
    #: polled it at once. A small pool is plenty for this appliance — request
    #: handlers just return cached bytes; rendering runs in its own worker.
    server_threads: int = 4
    poll_interval_seconds: int = 10
    debug_fast_refresh: bool = False
    auto_clear_cache: bool = False
    #: Zoom up to this fraction (e.g. 0.02 = 2 %) to eliminate letterbox bands.
    #: 0.0 = always letterbox (default, safe).
    crop_to_fill_threshold: float = 0.10
    #: Memory the server may use, in MB. This is the master resource knob:
    #: both the image decode budget and the render worker count are derived
    #: from it (see resource_budget.py).
    #: 0 = auto-detect (cgroup-aware: honours a docker --memory / systemd
    #:     MemoryMax / k8s limit, falling back to physical RAM).
    #: N > 0 = explicit cap in MB, clamped to physically-detected RAM so a
    #:     too-high value can't invite the OOM killer.
    #: The render worker count is derived from this budget and the cgroup-aware
    #: CPU count (see resource_budget.py) — there is no separate worker knob.
    memory_budget_mb: int = 0

    # Image pipeline: default, B&W, and face presets.
    image_config_default: ImageConfig = field(
        default_factory=lambda: PRESET_IMAGE_CONFIGS["floyd_steinberg_hue_aware"]
    )
    classifier_bw_detect_enabled: bool = True
    image_config_bw: ImageConfig = field(
        default_factory=lambda: PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"]
    )
    classifier_face_detect_enabled: bool = True
    classifier_face_detect_clahe_keepout: bool = True
    #: When a photo has detected face(s), center the crop-to-fill window on
    #: them instead of the image center ("face-aware cropping") so an
    #: aggressive crop doesn't cut off heads. Opt-in — off by default.
    classifier_face_aware_crop_enabled: bool = False
    image_config_face: ImageConfig = field(
        default_factory=lambda: PRESET_IMAGE_CONFIGS["atkinson_hue_aware"]
    )

    #: mDNS / Bonjour hostname (the part before ``.local``).
    #: The server advertises itself as ``<mdns_hostname>.local`` on the LAN.
    #: Empty string disables mDNS advertisement entirely.
    mdns_hostname: str = "hokku"

    #: Remembered WiFi credentials for the "Flash a screen" feature.
    #: Stored in plaintext — acceptable for a local-network appliance.
    flash_wifi_ssid: str = ""
    flash_wifi_pass: str = ""
    flash_wifi_ssid2: str = ""
    flash_wifi_pass2: str = ""

    #: Writable directory holding firmware downloaded from GitHub (plus the pin
    #: ``selection.json`` and per-file ``.meta.json`` sidecars). Layered on top of
    #: the read-only bundled firmware that ships in the package.
    firmware_dir: str = "/var/lib/hokku/firmware"
    #: ``owner/repo`` whose GitHub Releases firmware is downloaded from. The server
    #: only contacts GitHub when the user clicks "Check GitHub" / "Download" — that
    #: click is the consent; there is no separate enable flag.
    firmware_github_repo: str = "defl/hokku_epaper"

    def cache_slug(self) -> str:
        """Path-safe fingerprint of fields that affect cached panel output."""
        payload = {
            "image_config_default": self.image_config_default.cache_slug(),
            "image_config_bw": self.image_config_bw.cache_slug(),
            "image_config_face": self.image_config_face.cache_slug(),
            "classifier_bw_detect_enabled": self.classifier_bw_detect_enabled,
            "classifier_face_detect_enabled": self.classifier_face_detect_enabled,
            "classifier_face_detect_clahe_keepout": self.classifier_face_detect_clahe_keepout,
            "classifier_face_aware_crop_enabled": self.classifier_face_aware_crop_enabled,
            "crop_to_fill_threshold": self.crop_to_fill_threshold,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:14]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        """Parse from a dict. Returns a fresh default v1 when 'version' is absent."""
        if not isinstance(data, dict):
            raise ValueError("config must be a JSON object")

        if "version" not in data:
            # Unversioned (legacy or hand-written) — return default.
            return cls()

        data = _migrate(data)

        image_config_default = _image_config_from_dict(
            data.get("image_config_default"), field_path="image_config_default"
        )
        image_config_bw = _image_config_from_dict(
            data.get("image_config_bw"), field_path="image_config_bw"
        )
        image_config_face = _image_config_from_dict(
            data.get("image_config_face"), field_path="image_config_face"
        )

        _image_fields = {"image_config_default", "image_config_bw", "image_config_face"}

        kwargs: dict[str, Any] = {
            "image_config_default": image_config_default,
            "image_config_bw": image_config_bw,
            "image_config_face": image_config_face,
        }
        for f in fields(cls):
            if f.name in _image_fields:
                continue
            if f.name in data:
                kwargs[f.name] = data[f.name]

        rat = kwargs.get("refresh_image_at_time")
        if isinstance(rat, list):
            kwargs["refresh_image_at_time"] = tuple(rat)

        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def load(cls, path: Path) -> AppConfig:
        """Read JSON from path. exit(1) on unparseable JSON.

        Missing file or valid JSON without 'version' → writes a fresh default
        and continues (self-healing).
        """
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            logger.info("Config not found — created default v%s: %s", _CURRENT_VERSION, path)
            return cfg
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load config from %s: %s", path, e)
            sys.exit(1)

        try:
            cfg = cls.from_dict(data)
        except (TypeError, ValueError) as e:
            logger.error("Failed to parse config from %s: %s", path, e)
            sys.exit(1)

        if "version" not in data:
            # Write the default back so the next load is clean.
            cfg.save(path)
            logger.info("Unversioned config replaced with default v%s: %s", _CURRENT_VERSION, path)
        else:
            logger.info("Config loaded from: %s", path)
        return cfg

    def save(self, path: Path) -> None:
        atomic_write_json(path, self.to_dict())
        logger.info("Config saved to: %s", path)
