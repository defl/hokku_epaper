#!/usr/bin/env python3
"""Render what the Foyer screen renders: the live server's per-image decision.

Every bench render in this campaign used one config — the server's
``image_config_default`` — for every photograph. Production does not. Its
classifier (``ImageClassifier.decision_for``) gives a photograph with detected
faces ``image_config_face``, a near-greyscale one ``image_config_bw``, and passes
the face boxes on so CLAHE skips the faces; the crop-to-fill threshold (0.14
live, against the bench's 0) crops 3:2 photographs to fill instead of
letterboxing them. 204 of the 245 library photographs get the face preset, which
boosts colour a different way (uniform PIL ``color_enhance`` instead of adaptive
OKLab saturation and ``adaptive_vivid``) and compresses range in CIELAB. So for
83 % of the library, "shipped" on the bench was not what the wall showed.

This module does not reimplement any of that: it builds production's own
``AppConfig`` from the live server's config and asks production's own
``ImageClassifier`` — same B&W test, same YuNet face detector on the source
file — for each decision.

The live config also carries the "Flash a screen" WiFi credentials in
plaintext; they are dropped before anything is written to disk.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from hokku.webserver.app_config import AppConfig
from hokku.webserver.bounding_box import BoundingBox
from hokku.webserver.image_classifier import ImageClassifier, ImageClassifierDecision

LIVE_CONFIG = Path("build/camcal/live_config.json")
CLASSIFIER_CACHE = Path("build/camcal/classifier_cache")
_SECRET_PREFIXES = ("flash_wifi_",)


def fetch_live_config(base: str, dest: Path = LIVE_CONFIG) -> dict:
    """GET the server's config (read-only) and store it without credentials."""
    from hokku_server import _get_json  # noqa: PLC0415 — network path only

    data = dict(_get_json(base, "/hokku/api/config")["config"])
    for key in [k for k in data if k.startswith(_SECRET_PREFIXES)]:
        del data[key]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


def live_app_config(path: Path = LIVE_CONFIG, cache_dir: Path = CLASSIFIER_CACHE) -> AppConfig:
    """Production's AppConfig from the stored live config, cached locally."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data["cache_dir"] = str(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return AppConfig.from_dict(data)


def classifier(app: AppConfig) -> ImageClassifier:
    return ImageClassifier(app)


def decision_for(clf: ImageClassifier, image: Path) -> ImageClassifierDecision:
    sha1 = hashlib.sha1(image.read_bytes()).hexdigest()  # noqa: S324 — a cache key
    return clf.decision_for(image, sha1)


def preset_of(app: AppConfig, decision: ImageClassifierDecision) -> str:
    for name in ("face", "bw", "default"):
        if decision.image_config == getattr(app, f"image_config_{name}"):
            return name
    return "override"


def plan_tag(image_name: str, arm: str) -> str:
    """A capture tag that is readable and cannot collide.

    Every plan builder used ``Path(name).stem[:22]``, which is readable and is
    also how two different photographs get the same tag. A capture is written to
    ``<tag>__shot.jpg`` / ``__expected.png``, and the rating page keys its crops
    on the same string, so a collision does not fail — it silently overwrites one
    photograph's capture with another's and then shows the survivor under both
    names. Found in a 73-photograph plan where four tags each covered two
    pictures: two `Marieke en Dennis Boot-*` and two `MNQUIJEN.NL Fotografie-*`,
    whose names differ only beyond the 22nd character.

    The digest is of the full name, so it is stable across runs and a resumed
    capture still recognises its own files.
    """
    stem = Path(image_name).stem[:22].rstrip()
    digest = hashlib.sha256(image_name.encode()).hexdigest()[:4]
    return f"{stem}_{digest}__{arm}"


def plan_fields(app: AppConfig, decision: ImageClassifierDecision) -> dict:
    """What a plan entry must carry to render exactly this decision."""

    def boxes(bs):
        return [asdict(b) for b in bs] if bs else None

    return {
        "preset": preset_of(app, decision),
        "crop_to_fill_threshold": decision.crop_to_fill_threshold,
        "clahe_keepout": boxes(decision.clahe_keepout_bboxes),
        "face_crop": boxes(decision.face_crop_bboxes),
    }


def render_kwargs(entry: dict) -> dict:
    """``render_indices`` keyword arguments for a plan entry; {} for a legacy one."""

    def boxes(key):
        raw = entry.get(key)
        return tuple(BoundingBox(**b) for b in raw) if raw else None

    if "crop_to_fill_threshold" not in entry:
        return {}
    return {
        "crop_to_fill_threshold": float(entry["crop_to_fill_threshold"]),
        "clahe_keepout_bboxes_norm": boxes("clahe_keepout"),
        "crop_anchor_bboxes_norm": boxes("face_crop"),
    }
