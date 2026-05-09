"""AppConfig dataclass: persisted server settings.

Strict, schema-driven parser. No legacy migration helpers — config files are
written by us, read by us, and meant to be edited by hand or via the web UI
(which writes them back through save() and triggers a process restart).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

from webserver.dither import DitherConfig
from webserver.image import ImageConfig
from webserver.presets import DEFAULT_PRESET, PRESET_IMAGE_CONFIGS


Orientation = Literal["landscape", "portrait"]


@dataclass(frozen=True)
class AppConfig:
    """Persisted server settings."""

    refresh_image_at_time: tuple[str, ...] = ("0600", "1200", "1800")
    upload_dir: str = ""
    cache_dir: str = ""
    port: int = 8080
    poll_interval_seconds: int = 10
    debug_fast_refresh: bool = False
    orientation: Orientation = "landscape"
    image: ImageConfig = field(default_factory=lambda: PRESET_IMAGE_CONFIGS[DEFAULT_PRESET])

    def cache_slug(self) -> str:
        """Path-safe fingerprint of fields that affect cached panel output.

        Bumps if image pipeline or orientation changes; refresh times,
        ports, etc. don't invalidate caches.
        """
        payload = {
            "image": self.image.cache_slug(),
            "orientation": self.orientation,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:14]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        """Strict parser — only known fields, no migration of legacy shapes."""
        if not isinstance(data, dict):
            raise ValueError("config must be a JSON object")

        # Build the ImageConfig from a nested dict (ImageConfig + dither sub-dict)
        image = _image_config_from_dict(data.get("image"))

        kwargs: dict[str, Any] = {"image": image}
        for f in fields(cls):
            if f.name in ("image",):
                continue
            if f.name in data:
                kwargs[f.name] = data[f.name]

        # tuple coercion for refresh_image_at_time
        rat = kwargs.get("refresh_image_at_time")
        if isinstance(rat, list):
            kwargs["refresh_image_at_time"] = tuple(rat)

        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        """Read JSON from path and parse strictly. exit(1) on missing/unparseable."""
        if not path.exists():
            print(f"Error: config file not found: {path}", file=sys.stderr)
            sys.exit(1)
        try:
            with open(path) as f:
                data = json.load(f)
            cfg = cls.from_dict(data)
            print(f"  Config loaded from: {path}")
            return cfg
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
            print(f"Error: failed to load config from {path}: {e}", file=sys.stderr)
            sys.exit(1)

    def save(self, path: Path) -> None:
        """Atomic JSON write (tmp + os.replace). Caller decides whether to restart."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        os.replace(tmp, path)
        print(f"  Config saved to: {path}")


def _image_config_from_dict(blob: Any) -> ImageConfig:
    """Build an ImageConfig from a nested JSON object (or default if absent)."""
    if blob is None:
        return PRESET_IMAGE_CONFIGS[DEFAULT_PRESET]
    if not isinstance(blob, dict):
        raise ValueError("config['image'] must be an object")

    dither_blob = blob.get("dither")
    if not isinstance(dither_blob, dict):
        raise ValueError("config['image']['dither'] must be an object")
    dither_kwargs = {f.name: dither_blob[f.name] for f in fields(DitherConfig) if f.name in dither_blob}
    missing = {f.name for f in fields(DitherConfig)} - dither_kwargs.keys()
    if missing:
        raise ValueError(f"config['image']['dither'] missing fields: {sorted(missing)}")
    dither = DitherConfig(**dither_kwargs)

    image_kwargs: dict[str, Any] = {"dither": dither}
    for f in fields(ImageConfig):
        if f.name == "dither":
            continue
        if f.name not in blob:
            raise ValueError(f"config['image'] missing field: {f.name}")
        image_kwargs[f.name] = blob[f.name]
    return ImageConfig(**image_kwargs)
