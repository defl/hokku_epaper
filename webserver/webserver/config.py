"""Server configuration load/save and defaults."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import ClassVar

from webserver.image import (
    ImageConfig,
    PRESET_IMAGE_CONFIGS,
    default_image_config,
    image_config_from_legacy_fat_dither_dict,
    merge_image,
)


def default_image() -> ImageConfig:
    return default_image_config()


@dataclass
class AppConfig:
    """Persisted server settings (JSON under HOKKU_CONFIG)."""

    _io_lock: ClassVar[threading.Lock] = threading.Lock()

    timezone: str = "America/Chicago"
    refresh_image_at_time: list[str] = field(
        default_factory=lambda: ["0600", "1200", "1800"],
    )
    upload_dir: str = "/images/upload"
    cache_dir: str = "/images/cache"
    port: int = 8080
    poll_interval_seconds: int = 10
    orientation: str = "landscape"
    debug_fast_refresh: bool = False
    image: ImageConfig = field(default_factory=default_image)

    def cache_slug(self) -> str:
        """Path-safe short fingerprint of fields that affect cached panel output."""
        payload = {
            "debug_fast_refresh": self.debug_fast_refresh,
            "image": self.image.cache_slug(),
            "orientation": self.orientation,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:14]

    @classmethod
    def from_dict(cls, data: dict) -> AppConfig:
        base = cls()
        image_cfg: ImageConfig | None = None
        if isinstance(data.get("image"), dict):
            image_cfg = merge_image(base.image, data["image"])
        elif isinstance(data.get("dither"), dict):
            if "prepare_autocontrast_cutoff" in data["dither"]:
                image_cfg = image_config_from_legacy_fat_dither_dict(data["dither"])
            else:
                image_cfg = merge_image(base.image, {"dither": data["dither"]})
        elif "dither_algorithm" in data or "dither_serpentine" in data:
            algo = data.get("dither_algorithm", "atkinson_hue_aware")
            serp = bool(data.get("dither_serpentine", False))
            preset = PRESET_IMAGE_CONFIGS.get(algo) or PRESET_IMAGE_CONFIGS["atkinson_hue_aware"]
            image_cfg = replace(preset, dither=replace(preset.dither, serpentine=serp))

        allowed = {f.name for f in fields(cls)}
        kwargs: dict = {}
        for k, v in data.items():
            if k not in allowed or k in (
                "image",
                "dither",
                "dither_algorithm",
                "dither_serpentine",
            ):
                continue
            kwargs[k] = v
        if image_cfg is not None:
            kwargs["image"] = image_cfg
        return replace(base, **kwargs)

    @classmethod
    def load_from_file(cls, path: Path | str | None = None) -> AppConfig:
        """Load JSON from ``path`` or from the ``HOKKU_CONFIG`` env path; create/rewrite file if needed."""
        with cls._io_lock:
            if path is None:
                raw = os.environ.get("HOKKU_CONFIG")
                if not raw or not str(raw).strip():
                    raise ValueError(
                        "HOKKU_CONFIG must be set to the absolute path of the config JSON file "
                        "(e.g. export HOKKU_CONFIG=/var/lib/hokku/config.json)"
                    )
                write_path = Path(raw)
            else:
                write_path = Path(path)

            cfg = cls()
            loaded_path = None

            if write_path.exists():
                try:
                    with open(write_path) as f:
                        user_config = json.load(f)
                    cfg = cls.from_dict(user_config)
                    loaded_path = write_path
                    print(f"  Config loaded from: {write_path}")
                except (json.JSONDecodeError, OSError) as e:
                    print(f"  Warning: failed to load config from {write_path}: {e}")

            if loaded_path is None:
                print(f"  No readable config at {write_path}, using defaults")

            need_rewrite = (not write_path.exists()) or (not os.access(write_path, os.W_OK))
            if need_rewrite:
                save_data = asdict(cfg)
                try:
                    write_path.parent.mkdir(parents=True, exist_ok=True)
                    if write_path.exists():
                        write_path.unlink()
                    with open(write_path, "w") as f:
                        json.dump(save_data, f, indent=2)
                    action = "created" if loaded_path is None else "rewritten (ownership fix)"
                    print(f"  Config {action} at: {write_path}")
                except OSError as e:
                    print(f"  Warning: couldn't make config writable at {write_path}: {e}")

            return cfg

    def save_to_file(self, path: Path | str | None = None) -> None:
        """Write this config as JSON to ``path`` or to the ``HOKKU_CONFIG`` env path."""
        with type(self)._io_lock:
            if path is None:
                raw = os.environ.get("HOKKU_CONFIG")
                path_obj = Path(raw) if raw else None
            else:
                path_obj = Path(path)
            if path_obj is None or not str(path_obj).strip():
                raise ValueError("HOKKU_CONFIG must be set before save_to_file()")
            save_data = asdict(self)
            with open(path_obj, "w") as f:
                json.dump(save_data, f, indent=2)
            print(f"  Config saved to: {path_obj}")


DEFAULT_CONFIG = AppConfig()
