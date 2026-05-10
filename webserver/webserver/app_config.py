"""AppConfig dataclass: persisted server settings.

Strict, schema-driven parser with a version field and migration chain.
Unversioned configs (no "version" key) are silently replaced with a
fresh default v1 on first load.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Literal

from webserver.image_config import ImageConfig, _image_config_from_dict  # noqa: F401 (re-exported)
from webserver.presets import DEFAULT_PRESET, PRESET_IMAGE_CONFIGS


Orientation = Literal["landscape", "portrait"]

_CURRENT_VERSION = 2


def _migrate_v1_to_v2(d: dict) -> dict:
    """Add image_worker_thread_count (default 1 = serial, matching old behaviour)."""
    d["image_worker_thread_count"] = 1
    return d


# v(N) → v(N+1) upgrade functions. Populated as the schema evolves.
_MIGRATIONS: dict[int, Callable[[dict], dict]] = {
    1: _migrate_v1_to_v2,
}


def _migrate(data: dict) -> dict:
    """Walk the migration chain to the current version."""
    ver = int(data["version"])
    while ver < _CURRENT_VERSION:
        data = _MIGRATIONS[ver](data)
        ver += 1
        data["version"] = ver
    return data


def _default_bw_image_config() -> ImageConfig:
    """B&W-safe ImageConfig: saturation boosters disabled to avoid colour confetti."""
    from webserver.image import _bw_safe_image_config  # avoid circular at module level
    return _bw_safe_image_config(PRESET_IMAGE_CONFIGS["floyd_steinberg"])


@dataclass(frozen=True)
class AppConfig:
    """Persisted server settings."""

    version: int = _CURRENT_VERSION
    refresh_image_at_time: tuple[str, ...] = ("0600", "1200", "1800")
    upload_dir: str = ""
    cache_dir: str = ""
    port: int = 8080
    poll_interval_seconds: int = 10
    debug_fast_refresh: bool = False
    orientation: Orientation = "landscape"
    auto_clear_cache: bool = False
    #: Zoom up to this fraction (e.g. 0.02 = 2 %) to eliminate letterbox bands.
    #: 0.0 = always letterbox (default, safe).
    crop_to_fill_threshold: float = 0.0
    #: Number of worker processes for parallel image rendering.
    #: 0 = auto (cpu_count − 1, capped by available RAM at ~50 MB/worker).
    #: 1 = serial (legacy default).
    #: N > 1 = exactly N workers; the user is responsible for having enough RAM.
    image_worker_thread_count: int = 1

    # Image pipeline: default, B&W, and face presets.
    image_config_default: ImageConfig = field(
        default_factory=lambda: PRESET_IMAGE_CONFIGS[DEFAULT_PRESET]
    )
    classifier_bw_detect_enabled: bool = True
    image_config_bw: ImageConfig = field(default_factory=_default_bw_image_config)
    classifier_face_detect_enabled: bool = True
    image_config_face: ImageConfig = field(
        default_factory=lambda: PRESET_IMAGE_CONFIGS["atkinson_hue_aware"]
    )

    def cache_slug(self) -> str:
        """Path-safe fingerprint of fields that affect cached panel output."""
        payload = {
            "image_config_default": self.image_config_default.cache_slug(),
            "image_config_bw": self.image_config_bw.cache_slug(),
            "image_config_face": self.image_config_face.cache_slug(),
            "classifier_bw_detect_enabled": self.classifier_bw_detect_enabled,
            "classifier_face_detect_enabled": self.classifier_face_detect_enabled,
            "orientation": self.orientation,
            "crop_to_fill_threshold": self.crop_to_fill_threshold,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:14]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
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
    def load(cls, path: Path) -> "AppConfig":
        """Read JSON from path. exit(1) on missing or unparseable JSON.

        Valid JSON without 'version' → returns a fresh default v1 and writes
        it back to *path* (self-healing).
        """
        if not path.exists():
            print(f"Error: config file not found: {path}", file=sys.stderr)
            print(
                "  To create one, copy the example and edit upload_dir / cache_dir:\n"
                "    cp webserver/config/config.example.json <your-config.json>\n"
                "  Then set upload_dir and cache_dir to writable directories.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Error: failed to load config from {path}: {e}", file=sys.stderr)
            sys.exit(1)

        try:
            cfg = cls.from_dict(data)
        except (TypeError, ValueError) as e:
            print(f"Error: failed to parse config from {path}: {e}", file=sys.stderr)
            sys.exit(1)

        if "version" not in data:
            # Write the default back so the next load is clean.
            cfg.save(path)
            print(f"  Unversioned config replaced with default v{_CURRENT_VERSION}: {path}")
        else:
            print(f"  Config loaded from: {path}")
        return cfg

    def save(self, path: Path) -> None:
        """Atomic JSON write (tmp + os.replace)."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        os.replace(tmp, path)
        print(f"  Config saved to: {path}")
