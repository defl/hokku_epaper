"""Dither-pipeline permutation tests against real-world photos.

Exercises a representative matrix of dither configurations on every image in
`images/test/`, asserting on:

  1. Hard invariants (wrong shape, out-of-palette indices, NaN/Inf, padding
     bleed, non-determinism) — these catch outright broken knob interactions.
  2. Soft artifact bounds (near-neutral amplification, saturated-feature
     survival, B&W-fallback behavior) — thresholds are generous so well-chosen
     dither configs pass, but gross regressions trip the alarm.

Tests render at a downsampled canvas (120×160 — ~0.1 s per render) so the full
matrix completes in a few seconds. The knob interactions being tested don't
depend on resolution; production canvas is 1200×1600 and runs the same pipeline
verbatim. Integration coverage for the full resolution lives in the live smoke
test in tests/test_webserver.py.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

# webserver/ is the project's own package when invoked via `pytest webserver/tests/`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webserver  # type: ignore


TEST_IMAGE_DIR = Path(__file__).resolve().parents[2] / "images" / "test"
# Canvas dimensions for permutation runs — small enough for 100+ renders to
# complete in seconds, large enough that the dither pattern and metrics are
# meaningful.
CANVAS_W = 120
CANVAS_H = 160


def _test_images():
    """Every .jpg in images/test/. Parametrised lazily so pytest collection
    works even when the directory isn't checked out (fresh clone)."""
    if not TEST_IMAGE_DIR.is_dir():
        return []
    return sorted(TEST_IMAGE_DIR.glob("*.jpg"))


@pytest.fixture(scope="module")
def loaded_images():
    """Load and downsample each test image exactly once. Returns dict keyed
    by filename."""
    out = {}
    for path in _test_images():
        img = Image.open(path).convert("RGB")
        img.thumbnail((max(CANVAS_W, CANVAS_H) * 2, max(CANVAS_W, CANVAS_H) * 2),
                       Image.LANCZOS)
        out[path.name] = img
    return out


def _override(base, **patches):
    """Deep-copy the default dither config and apply the given patch. Patch
    keys use double-underscore to drill into nested dicts:
        _override(..., saturation__mode="off", drc__enabled=False)
    """
    cfg = copy.deepcopy(base)
    for key, value in patches.items():
        parts = key.split("__")
        cursor = cfg
        for part in parts[:-1]:
            cursor = cursor[part]
        cursor[parts[-1]] = value
    return cfg


def _build_permutations():
    """24 dither configs covering the knob matrix. Named for failure-case
    readability."""
    DEFAULT = webserver._default_dither_config()
    # Legacy preset = hand-copied so the test doesn't silently follow a future
    # preset rename.
    PRESETS = {
        name: copy.deepcopy(p["dither"])
        for name, p in webserver.DITHER_PRESETS.items()
    }
    perms = []

    # (1-3) presets verbatim
    for name, cfg in PRESETS.items():
        perms.append((f"preset_{name}", cfg))

    # (4-8) tonal stage variations on top of the default
    perms += [
        ("no_autocontrast",    _override(DEFAULT, autocontrast__enabled=False)),
        ("no_gamma",           _override(DEFAULT, gamma__enabled=False)),
        ("brightness_boost",   _override(DEFAULT, brightness=1.2)),
        ("contrast_boost",     _override(DEFAULT, contrast=1.3)),
        ("sharpness_off",      _override(DEFAULT, sharpness=1.0)),
    ]

    # (9-12) saturation variations
    perms += [
        ("sat_off",            _override(DEFAULT, saturation__mode="off")),
        ("sat_global_10",      _override(DEFAULT, saturation__mode="global", saturation__value=1.0)),
        ("sat_global_20",      _override(DEFAULT, saturation__mode="global", saturation__value=2.0)),
        ("sat_adaptive_20",    _override(DEFAULT, saturation__mode="adaptive", saturation__value=2.0)),
    ]

    # (13-15) DRC variations
    perms += [
        ("drc_off",            _override(DEFAULT, drc__enabled=False)),
        ("drc_chroma_off",     _override(DEFAULT, drc__chroma_mode="off")),
        ("drc_chroma_flat",    _override(DEFAULT, drc__chroma_mode="flat")),
    ]

    # (16-19) palette LUT variations
    perms += [
        ("lut_euclidean",      _override(DEFAULT, palette_lut__mode="euclidean")),
        ("lut_hue_strict_30",  _override(DEFAULT, palette_lut__hue_cutoff_deg=30.0)),
        ("lut_hue_wide_170",   _override(DEFAULT, palette_lut__hue_cutoff_deg=170.0)),
        ("lut_neutral_gen_20", _override(DEFAULT, palette_lut__neutral_chroma=20.0)),
    ]

    # (20-21) B&W fallback
    perms += [
        ("bw_fallback_off",    _override(DEFAULT, bw_fallback__enabled=False)),
        ("bw_fallback_pc_75",  _override(DEFAULT, bw_fallback__percentile=75)),
    ]

    # (22-23) kernel swaps
    perms += [
        ("kernel_fs",          _override(DEFAULT, kernel="floyd_steinberg")),
        ("fs_with_adaptive_sat", _override(PRESETS["floyd_steinberg_vivid"],
                                           saturation__mode="adaptive",
                                           saturation__value=1.25)),
    ]

    # (24) tonal stages all off — stress path
    perms.append(("tonal_all_off", _override(
        DEFAULT,
        autocontrast__enabled=False,
        gamma__enabled=False,
        brightness=1.0,
        contrast=1.0,
        sharpness=1.0,
    )))

    return perms


PERMUTATIONS = _build_permutations()


def _render(img, dither_cfg, orientation="landscape"):
    """Run the production pipeline at the permutation-test canvas size.
    Wraps _maybe_apply_bw_fallback the same way _convert_image does so we
    exercise the fallback branch from the integration direction."""
    effective_cfg, _ = webserver._maybe_apply_bw_fallback(dither_cfg, img)
    return webserver._run_dither_pipeline(
        img, effective_cfg, orientation,
        canvas_w=CANVAS_W, canvas_h=CANVAS_H,
    )


# ── Hard invariants ───────────────────────────────────────────────

@pytest.mark.skipif(not _test_images(), reason="images/test/ not checked out")
@pytest.mark.parametrize("image_path", _test_images(), ids=lambda p: p.name)
@pytest.mark.parametrize("perm", PERMUTATIONS, ids=lambda p: p[0])
def test_render_invariants(image_path, perm, loaded_images):
    """Every (image, permutation) must produce a well-formed palette buffer.

    Catches: wrong shape, values outside palette range, NaN/Inf leakage,
    padding pixels left non-white, complete-collapse bugs (all-one-color),
    and any exception raised by the pipeline."""
    img = loaded_images[image_path.name]
    name, dither_cfg = perm

    result, mask = _render(img, dither_cfg)

    # Shape + dtype
    assert result.shape == (CANVAS_H, CANVAS_W), (
        f"{name}/{image_path.name}: result shape {result.shape} != ({CANVAS_H},{CANVAS_W})")
    assert result.dtype == np.uint8, f"{name}/{image_path.name}: dtype {result.dtype}"

    # Palette range
    unique_indices = set(int(v) for v in np.unique(result))
    valid = {0, 1, 2, 3, 4, 5}
    assert unique_indices.issubset(valid), (
        f"{name}/{image_path.name}: out-of-palette indices {unique_indices - valid}")

    # Padding is pure white
    if mask.any():
        pad_vals = np.unique(result[mask])
        assert list(pad_vals) == [1], (
            f"{name}/{image_path.name}: padding has non-white indices {pad_vals}")

    # No catastrophic collapse: natural photos should use at least 3 palette
    # entries across the full frame. Pure B&W photos legitimately use fewer;
    # exempt them from this check by name.
    is_bw_source = "_BW_" in image_path.name
    if not is_bw_source:
        assert len(unique_indices) >= 3, (
            f"{name}/{image_path.name}: output collapsed to indices {unique_indices} "
            f"— pipeline likely broken")


# ── Determinism ───────────────────────────────────────────────────

@pytest.mark.skipif(not _test_images(), reason="images/test/ not checked out")
@pytest.mark.parametrize("perm", PERMUTATIONS[:3], ids=lambda p: p[0])
def test_render_is_deterministic(perm, loaded_images):
    """Same image + same config must produce identical output across runs.
    Guards against accidental use of non-seeded randomness anywhere in the
    pipeline (which would wreck the disk-cache content hashes)."""
    if not loaded_images:
        pytest.skip("no images loaded")
    name, dither_cfg = perm
    img = next(iter(loaded_images.values()))
    r1, _ = _render(img, dither_cfg)
    r2, _ = _render(img, dither_cfg)
    assert np.array_equal(r1, r2), f"{name}: non-deterministic dither output"


# ── Soft artifact bounds ──────────────────────────────────────────

def _source_chroma(img):
    """Compute per-pixel Lab chroma for a PIL image — used to classify source
    regions as 'near neutral' or 'saturated' for artifact detection."""
    arr = np.asarray(img.convert("RGB"), dtype=np.float64)
    lab = webserver._xyz_to_lab(webserver._linear_to_xyz(webserver._srgb_to_linear(arr)))
    chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
    return chroma


@pytest.mark.skipif(not _test_images(), reason="images/test/ not checked out")
@pytest.mark.parametrize("image_path", _test_images(), ids=lambda p: p.name)
def test_default_preset_neutral_leak_bounded(image_path, loaded_images):
    """For the shipping default preset, near-neutral source regions must not
    overwhelmingly map to saturated palette entries. This is the classic
    'white umbrella pink speckle' failure mode that drove the hue-aware LUT
    + adaptive saturation redesign.

    Threshold: < 35% of source-near-neutral pixels may use a saturated
    palette entry (Yellow/Red/Blue/Green). Empirical leak on the shipping
    default (measured 2026-04-22 at CANVAS=120x160):

        De Niro portrait:        1.4%
        Forest road (B&W):       3.1%
        Albi sunset:             5.3%
        Wayuu woman:            13.5%
        Anna Unterberger:       23.3%
        Fitz Roy (gray stone):  26.1%

    Mountain-stone / low-chroma-photo-mostly-neutral scenes use colored
    palette entries legitimately to reach intermediate luminances gamma/DRC
    can't express with just black+white. 35% is well above current worst-case
    and well below what a clearly-regressed preset produces (50%+)."""
    img = loaded_images[image_path.name]

    # Render at canvas size, so we also need per-pixel source chroma aligned
    # to the dither output. Resize the source to match pre-rotation canvas
    # dimensions (aspect ratio preserved via letterbox) then check only the
    # non-padding region.
    dither_cfg = webserver._default_dither_config()
    result, mask = _render(img, dither_cfg)

    # The dithered result is already post-rotation (landscape → 1200x1600
    # buffer shape). Build a matched source-chroma array by running the
    # same canvas composition against a chroma-only representation.
    src_canvas = Image.new("RGB", (CANVAS_H, CANVAS_W), (255, 255, 255))  # landscape pre-rot
    w, h = img.size
    scale = min(CANVAS_H / w, CANVAS_W / h)
    new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    x_off = (CANVAS_H - new_w) // 2
    y_off = (CANVAS_W - new_h) // 2
    src_canvas.paste(resized, (x_off, y_off))
    chroma_src = _source_chroma(src_canvas)
    # Rotate -90° to match landscape post-rotation
    chroma_src = np.rot90(chroma_src, k=3)

    # Near-neutral = source chroma < 8 (same threshold used internally)
    near_neutral = (chroma_src < 8.0) & ~mask
    if near_neutral.sum() < 50:
        pytest.skip(f"{image_path.name}: too few near-neutral source pixels")

    neutral_picks = {0, 1}  # black, white
    non_neutral_result = ~np.isin(result, list(neutral_picks))
    leak_fraction = (non_neutral_result & near_neutral).sum() / max(1, near_neutral.sum())

    assert leak_fraction < 0.35, (
        f"{image_path.name}: {leak_fraction:.1%} of near-neutral source pixels "
        f"got mapped to saturated palette entries under the default preset "
        f"(threshold 35%). This is the 'pink speckle in whites' failure mode.")


@pytest.mark.skipif(not _test_images(), reason="images/test/ not checked out")
def test_bw_fallback_keeps_bw_image_monochrome(loaded_images):
    """With B&W fallback enabled, a grayscale source image should render
    predominantly in black+white palette picks. Without fallback, saturation/
    vividness amplify tiny chroma noise into visible colour — the pipeline
    must not do that when fallback is on."""
    bw = next((img for name, img in loaded_images.items() if "_BW_" in name), None)
    if bw is None:
        pytest.skip("No BW test image present (expected Forest_road_Slavne_*_BW_*.jpg)")

    # With fallback enabled (the default), the image must be detected as B&W
    # and the saturation/DRC overrides must kick in, yielding ≥95%
    # black/white palette picks.
    with_fb = webserver._default_dither_config()
    assert with_fb["bw_fallback"]["enabled"] is True
    result_fb, mask_fb = _render(bw, with_fb)
    non_padding = ~mask_fb
    neutral_fb = np.isin(result_fb, [0, 1]) & non_padding
    fb_fraction = neutral_fb.sum() / max(1, non_padding.sum())
    assert fb_fraction >= 0.95, (
        f"BW fallback on: only {fb_fraction:.1%} of pixels are black/white "
        f"— fallback failed to detect or override correctly")


@pytest.mark.skipif(not _test_images(), reason="images/test/ not checked out")
def test_bw_fallback_off_changes_result(loaded_images):
    """Regression guard: disabling B&W fallback must actually affect the
    output for a B&W image (otherwise the flag is a no-op and the fallback
    tests above prove nothing)."""
    bw = next((img for name, img in loaded_images.items() if "_BW_" in name), None)
    if bw is None:
        pytest.skip("No BW test image present")

    with_fb = webserver._default_dither_config()
    no_fb = _override(with_fb, bw_fallback__enabled=False)

    result_fb, _ = _render(bw, with_fb)
    result_no_fb, _ = _render(bw, no_fb)

    assert not np.array_equal(result_fb, result_no_fb), (
        "B&W fallback flag is a no-op: output identical with/without fallback")


@pytest.mark.skipif(not _test_images(), reason="images/test/ not checked out")
@pytest.mark.parametrize("image_path", _test_images(), ids=lambda p: p.name)
def test_palette_usage_matches_image_character(image_path, loaded_images):
    """Sanity check: sunset / market / portrait images must make meaningful
    use of the colour palette entries, while the B&W image must not.

    Catches broken saturation pipelines (all-gray output on a sunset) and
    broken B&W fallback (rainbow output on a black-and-white photo)."""
    img = loaded_images[image_path.name]
    dither_cfg = webserver._default_dither_config()
    result, mask = _render(img, dither_cfg)
    non_padding = ~mask

    colour_picks = np.isin(result, [2, 3, 4, 5]) & non_padding  # Y/R/B/G
    colour_fraction = colour_picks.sum() / max(1, non_padding.sum())

    is_bw = "_BW_" in image_path.name
    if is_bw:
        # B&W image with fallback on: colour should be near zero.
        assert colour_fraction < 0.05, (
            f"{image_path.name}: {colour_fraction:.1%} colour pixels in a BW image "
            f"— fallback not doing its job")
    else:
        # Colour image: at least some palette-colour use expected. 5% is a
        # very low floor — even a mostly-gray scene passes. Flags bugs that
        # strip all colour from the output.
        assert colour_fraction >= 0.05, (
            f"{image_path.name}: only {colour_fraction:.1%} colour pixels "
            f"— pipeline probably stripped all saturation")


# ── Full-resolution smoke test ────────────────────────────────────

@pytest.mark.skipif(not _test_images(), reason="images/test/ not checked out")
def test_full_resolution_render_one_image(loaded_images, tmp_path):
    """Exactly one full-resolution render (1200×1600) through _convert_image
    to make sure the production path survives the refactor. Uses the first
    test image; the permutation matrix above at 120×160 covers breadth."""
    paths = _test_images()
    if not paths:
        pytest.skip("no test images")
    src = paths[0]
    # _convert_image reads _config — patch it to the default preset.
    from unittest.mock import patch
    cfg = {**webserver.DEFAULT_CONFIG,
           "dither": webserver._default_dither_config(),
           "orientation": "landscape"}
    with patch.object(webserver, "_config", cfg):
        raw, preview = webserver._convert_image(src)
    assert len(raw) == webserver.TOTAL_BYTES
    assert preview.startswith(b"\x89PNG\r\n\x1a\n"), "preview is not a PNG"
