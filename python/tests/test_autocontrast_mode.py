"""`prepare_autocontrast`: the stage that was an automatic white balance.

PIL's autocontrast stretches R, G and B independently, which tints a photograph
by however much its channels differ — measured up to 13 units of b* across the
library, on exactly the photographs whose ratings read "yellow skin". The stage
is now switchable, and the shipped presets turn it off.

Two properties matter beyond "the field exists": an upgrade must not silently
change what a running server renders, and "off" must really be off — the reason
a cutoff of 0 would not do, since at cutoff 0 PIL still stretches each channel
to its own extremes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from hokku.webserver.app_config import _CURRENT_VERSION, AppConfig, _migrate
from hokku.webserver.image_abc import _apply_prepare_enhancements
from hokku.webserver.image_config import ImageConfigError, image_config_from_dict_strict
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS


def _warm_photo() -> Image.Image:
    """Red and green span the full range, blue only 60-140: a warm-cast source."""
    y, x = np.mgrid[0:64, 0:64]
    return Image.fromarray(
        np.stack([255 * x / 63, 255 * y / 63, 60 + 80 * (x + y) / 126], -1).astype(np.uint8)
    )


def _blue_share(img) -> float:
    """Blue's share of total signal — moves when the colour balance moves."""
    a = np.asarray(img, dtype=float)
    return float(a[..., 2].sum() / a.sum())


def _identity_cfg(mode: str):
    """Every tonal stage neutral, so only autocontrast can change anything."""
    return replace(
        PRESET_IMAGE_CONFIGS["calibration_raw"],
        prepare_autocontrast=mode,
        prepare_autocontrast_cutoff=0.0,
    )


def test_off_leaves_the_picture_alone():
    img = _warm_photo()
    out = _apply_prepare_enhancements(img, _identity_cfg("off"))
    np.testing.assert_array_equal(np.asarray(out), np.asarray(img))


def test_per_channel_shifts_the_colour_balance():
    """The behaviour that shipped: a warm photo comes out less warm."""
    img = _warm_photo()
    out = _apply_prepare_enhancements(img, _identity_cfg("per_channel"))
    assert _blue_share(out) - _blue_share(img) > 0.02


def test_preserve_tone_expands_contrast_without_moving_colour():
    img = _warm_photo()
    out = _apply_prepare_enhancements(img, _identity_cfg("preserve_tone"))
    assert abs(_blue_share(out) - _blue_share(img)) < 0.01
    assert not np.array_equal(np.asarray(out), np.asarray(img))  # it did do something


@pytest.mark.parametrize("name", sorted(PRESET_IMAGE_CONFIGS))
def test_every_shipped_preset_has_autocontrast_off(name: str):
    assert PRESET_IMAGE_CONFIGS[name].prepare_autocontrast == "off"


def test_an_unknown_mode_is_rejected_with_the_allowed_values():
    blob = json.loads(json.dumps(asdict(PRESET_IMAGE_CONFIGS["default_general"])))
    blob["prepare_autocontrast"] = "auto"
    with pytest.raises(ImageConfigError) as excinfo:
        image_config_from_dict_strict(blob)
    assert "prepare_autocontrast" in str(excinfo.value)
    assert "'off'" in str(excinfo.value)


def test_upgrade_keeps_an_existing_pipeline_rendering_as_it_did():
    """A stored config predating the field must not change appearance on upgrade."""
    stored: dict[str, object] = {"version": 10}
    for key in ("image_config_default", "image_config_bw", "image_config_face"):
        blob = asdict(PRESET_IMAGE_CONFIGS["default_general"])
        del blob["prepare_autocontrast"]
        stored[key] = blob

    migrated = _migrate(json.loads(json.dumps(stored)))

    assert migrated["version"] == _CURRENT_VERSION
    for key in ("image_config_default", "image_config_bw", "image_config_face"):
        assert migrated[key]["prepare_autocontrast"] == "per_channel"
    cfg = AppConfig.from_dict(migrated)
    assert cfg.image_config_default.prepare_autocontrast == "per_channel"


def test_the_shipped_example_config_is_at_the_current_version():
    path = (
        Path(__file__).resolve().parents[1]
        / "hokku"
        / "webserver"
        / "config"
        / "config.json.example"
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == _CURRENT_VERSION
    AppConfig.from_dict(data)  # parses strictly at the current version
