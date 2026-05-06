"""Server configuration load/save and defaults."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import ClassVar

from webserver.display_image_config_presets import PRESET_DISPLAY_IMAGE_CONFIGS
from webserver.image import (
    DisplayImageConfig,
    ImageConfig,
    default_display_image_config,
    image_config_from_legacy_fat_dither_dict,
    merge_display_image,
    merge_image,
)


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
    debug_fast_refresh: bool = False
    display: DisplayImageConfig = field(default_factory=default_display_image_config)

    def cache_slug(self) -> str:
        """Path-safe short fingerprint of fields that affect cached panel output."""
        payload = {
            "debug_fast_refresh": self.debug_fast_refresh,
            "display": self.display.cache_slug(),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:14]

    @classmethod
    def from_dict(cls, data: dict) -> AppConfig:
        base = cls()
        disp = base.display
        used_display_key = False

        if isinstance(data.get("display"), dict):
            d = data["display"]
            patch: dict = {}
            if isinstance(d.get("image"), dict):
                patch["image"] = d["image"]
            if isinstance(d.get("orientation"), str) and d["orientation"] in ("landscape", "portrait"):
                patch["orientation"] = d["orientation"]
            if patch:
                disp = merge_display_image(base.display, patch)
            used_display_key = bool(patch)

        if not used_display_key:
            if isinstance(data.get("image"), dict):
                disp = merge_display_image(base.display, {"image": data["image"]})
            elif isinstance(data.get("dither"), dict):
                if "prepare_autocontrast_cutoff" in data["dither"]:
                    new_img = image_config_from_legacy_fat_dither_dict(data["dither"])
                else:
                    new_img = merge_image(base.display.image, {"dither": data["dither"]})
                disp = replace(base.display, image=new_img)
            elif "dither_algorithm" in data or "dither_serpentine" in data:
                algo = data.get("dither_algorithm", "atkinson_hue_aware")
                serp = bool(data.get("dither_serpentine", False))
                preset = PRESET_DISPLAY_IMAGE_CONFIGS.get(algo) or PRESET_DISPLAY_IMAGE_CONFIGS["atkinson_hue_aware"]
                new_img = replace(
                    preset.image,
                    dither=replace(preset.image.dither, serpentine=serp),
                )
                disp = replace(base.display, image=new_img)

        flat_ori = data.get("orientation")
        if isinstance(flat_ori, str) and flat_ori in ("landscape", "portrait"):
            disp = replace(disp, orientation=flat_ori)

        allowed = {f.name for f in fields(cls)}
        kwargs: dict = {}
        for k, v in data.items():
            if k not in allowed or k in (
                "display",
                "image",
                "dither",
                "dither_algorithm",
                "dither_serpentine",
                "orientation",
            ):
                continue
            kwargs[k] = v
        kwargs["display"] = disp
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
