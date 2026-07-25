"""Decode-path correctness + peak-memory guards for ``open_image_for_render``.

``open_image_for_render`` is the single choke point every render and every
face-detect decode funnels through, and it is where the 464 MB Pi appliance
was OOM-killed: a big JPEG or a HEIF panorama used to be decoded, EXIF-
transposed, and RGB-converted all at *source* resolution, holding three-plus
full-frame buffers alive at once before ``thumbnail`` shrank anything. The fix
shrinks first (see ``_shrink_to_source_bbox``), so transpose + convert run on
the small image and peak RAM is one transient decode buffer.

Two kinds of test here:

* Correctness (fast, default suite): shrinking first must NOT lose the EXIF
  orientation or the transparency-onto-white flatten, and must still bound
  output dimensions.
* Peak memory: a default-suite guard on the opaque path (the OOM path — must
  run in CI, unlike the ``time_intensive`` full-render budget tests that CI
  skips) plus a broader ``time_intensive`` matrix.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

import hokku.webserver.image_renderer as ir
from hokku.webserver.image_renderer import (
    _MAX_SOURCE_LONG_SIDE,
    DECODE_BUDGET_PIXELS,
    MAX_SOURCE_LONG,
    MAX_SOURCE_SHORT,
    decoded_pixels_exceed_budget,
    open_image_for_render,
)
from tests._memory_helpers import peak_rss_decode_subprocess

_ORIENTATION_TAG = 0x0112  # EXIF Orientation
_ORIENTATION_ROTATE_270 = 6  # display transform that swaps width/height


# ──────────────────────────────────────────────────────────────────────
# Correctness — the reorder must preserve orientation + transparency
# ──────────────────────────────────────────────────────────────────────


def _save_with_orientation(path: Path, size: tuple[int, int], value: int) -> Path:
    img = Image.new("RGB", size, (120, 30, 30))
    exif = img.getexif()
    exif[_ORIENTATION_TAG] = value
    img.save(path, exif=exif)
    return path


def test_exif_orientation_applied_on_small_image(tmp_path: Path) -> None:
    """A landscape source tagged rotate-270 must come back portrait (dims swapped).

    Guards the reorder: ``exif_transpose`` now runs AFTER ``thumbnail``, relying
    on ``thumbnail`` preserving ``img.info['exif']``. If that ever regresses the
    orientation is silently dropped and the dimensions would NOT swap.
    """
    src = _save_with_orientation(tmp_path / "o.jpg", (400, 200), _ORIENTATION_ROTATE_270)
    img = open_image_for_render(src)
    try:
        assert img.size == (200, 400), (
            f"orientation not applied — expected swapped (200, 400), got {img.size}"
        )
    finally:
        img.close()


def test_exif_orientation_applied_after_shrink(tmp_path: Path) -> None:
    """Orientation must survive the shrink-first path too.

    4000×2000 tagged rotate-270: shrinks (long side clamped to
    ``_MAX_SOURCE_LONG_SIDE``) THEN transposes, so the result is portrait and
    bounded.
    """
    src = _save_with_orientation(tmp_path / "big.jpg", (4000, 2000), _ORIENTATION_ROTATE_270)
    img = open_image_for_render(src)
    try:
        w, h = img.size
        assert h > w, f"orientation not applied after shrink — got landscape {img.size}"
        assert max(img.size) <= _MAX_SOURCE_LONG_SIDE
        # 2:1 source, transposed → 1:2 result, aspect preserved within rounding.
        assert abs((h / w) - 2.0) < 0.02
    finally:
        img.close()


def test_transparency_flattened_onto_white(tmp_path: Path) -> None:
    """A transparent region must become WHITE, not black (regression: fb1fdd4)."""
    src = tmp_path / "alpha.png"
    # Transparent (alpha 0) with black RGB in the top-left quadrant; opaque red
    # elsewhere. A naive convert("RGB") would leave the transparent area black.
    img = Image.new("RGBA", (200, 200), (200, 0, 0, 255))
    for y in range(100):
        for x in range(100):
            img.putpixel((x, y), (0, 0, 0, 0))
    img.save(src)

    out = open_image_for_render(src)
    try:
        assert out.mode == "RGB"
        transparent_px = out.getpixel((10, 10))
        assert isinstance(transparent_px, tuple)
        r, g, b = transparent_px[0], transparent_px[1], transparent_px[2]
        assert r > 240 and g > 240 and b > 240, (
            f"transparent region should flatten to white, got ({r}, {g}, {b})"
        )
        # The opaque red region is untouched.
        opaque_px = out.getpixel((150, 150))
        assert isinstance(opaque_px, tuple) and opaque_px[0] > 150
    finally:
        out.close()


def test_wide_panorama_clamped_on_long_side_only(tmp_path: Path) -> None:
    """A wide panorama (oversized on the long axis only) keeps short-axis detail.

    Mirrors the tall-thin portrait case but landscape — this is the exact shape
    (a HEIF panorama) that OOM'd. The long side clamps to
    ``_MAX_SOURCE_LONG_SIDE``; the short side is NOT force-shrunk below 2× screen.
    """
    src = tmp_path / "pano.png"
    Image.new("RGB", (6000, 1500), (30, 60, 30)).save(src)  # 9 MP, under the budget
    img = open_image_for_render(src)
    try:
        assert max(img.size) <= _MAX_SOURCE_LONG_SIDE
        # long side clamped to the cap; short side scaled by the same factor,
        # NOT force-shrunk below MAX_SOURCE_SHORT (that's the tall/wide rule).
        expected_short = round(1500 * _MAX_SOURCE_LONG_SIDE / 6000)
        assert img.size[1] >= expected_short - 5, "short axis over-shrunk on a panorama"
        assert abs(img.size[0] / img.size[1] - 6000 / 1500) < 0.02
    finally:
        img.close()


@pytest.mark.parametrize("mode", ["P", "L"])
def test_oversize_non_rgb_opaque_modes(tmp_path: Path, mode: str) -> None:
    """Oversized palette (P) and grayscale (L) sources decode to bounded RGB.

    The common path reduce()/thumbnails BEFORE converting; palette indices can't
    be resampled, so P must be converted first. Guards that branch — an oversized
    P image would otherwise corrupt colours or error in reduce().
    """
    src = tmp_path / f"big_{mode}.png"
    base = Image.new("RGB", (4000, 3000), (30, 140, 90))
    base.convert(mode).save(src)
    img = open_image_for_render(src)
    try:
        assert img.mode == "RGB"
        assert max(img.size) <= MAX_SOURCE_LONG
        assert min(img.size) <= MAX_SOURCE_SHORT
    finally:
        img.close()


def test_oversize_rgba_still_bounded(tmp_path: Path) -> None:
    """The alpha path (flatten-then-shrink) still bounds output dimensions."""
    src = tmp_path / "big_alpha.png"
    Image.new("RGBA", (4000, 3000), (10, 20, 200, 255)).save(src)
    img = open_image_for_render(src)
    try:
        assert img.mode == "RGB"
        assert max(img.size) <= MAX_SOURCE_LONG
        assert min(img.size) <= MAX_SOURCE_SHORT
    finally:
        img.close()


# ──────────────────────────────────────────────────────────────────────
# Decode budget — un-draftable large images are refused, draftable ones aren't
# ──────────────────────────────────────────────────────────────────────


def _make_solid_png(path: Path, size: tuple[int, int]) -> Path:
    """A solid-colour PNG: tiny on disk, but decodes to the full W×H×3 buffer."""
    Image.new("RGB", size, (77, 133, 199)).save(path, "PNG")
    return path


def _over_budget_dims() -> tuple[int, int]:
    """A landscape size whose pixel count exceeds the decode budget but stays
    under the bomb cap, so the budget check (not the bomb guard) is exercised."""
    long = int((DECODE_BUDGET_PIXELS * 1.5 * 1.5) ** 0.5)  # 1.5:1, ~1.5x budget
    return (long, int(long / 1.5))


def test_budget_rejects_large_png(tmp_path: Path) -> None:
    """A PNG over the decode budget is refused (no shrink-on-load possible)."""
    w, h = _over_budget_dims()
    assert w * h > DECODE_BUDGET_PIXELS and w * h < 40_000_000  # budget, not bomb
    src = _make_solid_png(tmp_path / "over.png", (w, h))
    with pytest.raises(ValueError, match="decode budget"):
        open_image_for_render(src)


@pytest.mark.parametrize(
    ("w", "h", "is_jpeg", "expected"),
    [
        (10334, 3769, False, True),  # Albi 38.9 MP HEIF — the OOM culprit
        (6000, 4000, False, True),  # 24 MP PNG
        (4562, 7027, True, False),  # Actress 32 MP JPEG — drafts under budget
        (4160, 6240, True, False),  # string_ensemble 26 MP JPEG — drafts under
        (3024, 4032, False, False),  # tree.heic 12 MP HEIF — under budget
        (40, 30, False, False),  # tiny
    ],
)
def test_decoded_pixels_exceed_budget(w: int, h: int, is_jpeg: bool, expected: bool) -> None:
    """The dims-only gate mirrors open_image_for_render's accept/reject.

    This is what lets the pipeline refuse an image BEFORE any phase (thumbnail /
    classify / render) decodes it — a JPEG passes on its post-draft size, every
    other format on its full size.
    """
    assert decoded_pixels_exceed_budget(w, h, is_jpeg=is_jpeg) is expected


_TEST_IMAGES_DIR = Path(__file__).resolve().parent.parent.parent / "images" / "test"


def test_real_panorama_over_budget_is_rejected() -> None:
    """The actual 38.9 MP HEIF panorama that OOM-killed the Pi is now refused.

    ``Albi_Panorama_Sunset_Panini_General.heif`` is 10334×3769 — a genuine HEIF
    with no shrink-on-load, so it would decode to ~300 MB. It must be rejected
    cleanly (ValueError, no decode) rather than crash the server. This is the
    regression guard tied to the real reported failure.
    """
    albi = _TEST_IMAGES_DIR / "Albi_Panorama_Sunset_Panini_General.heif"
    if not albi.is_file():
        pytest.skip("bundled Albi panorama fixture missing")
    with pytest.raises(ValueError, match="decode budget"):
        open_image_for_render(albi)


def test_real_large_jpegs_still_render(tmp_path: Path) -> None:
    """The two big JPEGs from the OOM set decode fine via draft (not rejected).

    Actress (4562×7027, 32 MP) and string_ensemble (4160×6240, 26 MP) exceed the
    pixel budget on paper, but libjpeg shrink-on-loads their long side to ~6–8 MP,
    so they must be ACCEPTED — the budget must not over-reject draftable sources.
    """
    for name in ("Actress_Anna_Unterberger-2.jpg", "string_ensemble_concert.jpeg"):
        src = _TEST_IMAGES_DIR / name
        if not src.is_file():
            pytest.skip(f"bundled fixture {name} missing")
        img = open_image_for_render(src)  # must NOT raise
        try:
            assert img.mode == "RGB"
            assert max(img.size) <= _MAX_SOURCE_LONG_SIDE
        finally:
            img.close()


def test_budget_accepts_large_jpeg_via_draft(tmp_path: Path) -> None:
    """A JPEG over the *pixel* budget is ACCEPTED — draft shrink-on-loads it.

    This is the whole point of checking the budget AFTER draft: the same
    dimensions that get a PNG refused let a JPEG through, because libjpeg decodes
    it at reduced scale to well under the budget.
    """
    w, h = _over_budget_dims()
    src = tmp_path / "over.jpg"
    Image.new("RGB", (w, h), (90, 140, 60)).save(src, "JPEG", quality=85)
    img = open_image_for_render(src)  # must NOT raise
    try:
        assert max(img.size) <= _MAX_SOURCE_LONG_SIDE
        assert img.mode == "RGB"
    finally:
        img.close()


# ──────────────────────────────────────────────────────────────────────
# The OOM guard. The CI-runnable one is DETERMINISTIC (no byte thresholds — a
# single MB bound can't be tight on both Windows peak_wset and Linux ru_maxrss):
# it proves the expensive full-frame ops run AFTER the shrink, which is the exact
# property whose loss caused the OOM. The byte-based peak tests are
# time_intensive leak-catchers with generous, cross-platform bounds.
# ──────────────────────────────────────────────────────────────────────

_MB = 1024 * 1024
# ~15 MP, just under the 16 MP budget — the largest opaque source we accept.
_MAX_OPAQUE = (4800, 3100)


def test_transpose_runs_after_shrink(monkeypatch, tmp_path: Path) -> None:
    """``exif_transpose`` must receive the SHRUNK image, not the full-res source.

    This is the memory fix's core invariant and the deterministic CI guard: if a
    refactor ever moves the transpose (or the RGB convert next to it) back ahead
    of the shrink, it runs on the full-frame buffer again — the amplification
    that OOM-killed the Pi. Platform-independent: no bytes measured.
    """
    src = _make_solid_png(tmp_path / "big.png", _MAX_OPAQUE)  # oversized both axes
    seen: list[tuple[int, int]] = []
    real = ir.ImageOps.exif_transpose

    def spy(im, *a, **k):
        seen.append(im.size)
        return real(im, *a, **k)

    monkeypatch.setattr(ir.ImageOps, "exif_transpose", spy)
    open_image_for_render(src).close()

    assert seen, "exif_transpose was not called"
    assert max(seen[0]) <= _MAX_SOURCE_LONG_SIDE, (
        f"exif_transpose ran on {seen[0]} (full-res source) — the shrink-first "
        f"ordering has regressed and the decode will amplify RAM again"
    )


@pytest.mark.time_intensive
@pytest.mark.parametrize(
    "size",
    [(4800, 3100), (3100, 4800), (7800, 1900)],  # landscape, portrait, wide panorama
)
def test_decode_peak_leak_check(tmp_path: Path, size: tuple[int, int]) -> None:
    """Informational leak-catcher: the largest accepted opaque sources must
    decode well under a generous, cross-platform bound.

    The bound is deliberately loose (peak_wset on Windows and ru_maxrss on Linux
    differ by tens of MB), so this only trips on a gross regression or leak. The
    deterministic guard above is what pins the shrink-first ordering.
    """
    assert size[0] * size[1] <= DECODE_BUDGET_PIXELS
    src = _make_solid_png(tmp_path / f"m_{size[0]}x{size[1]}.png", size)
    delta, _baseline = peak_rss_decode_subprocess(src)
    delta_mb = delta / _MB
    print(f"\n  {size[0]}x{size[1]} PNG decode peak = {delta_mb:.1f} MB")
    assert delta < 140 * _MB, (
        f"{size} decode peak {delta_mb:.1f} MB exceeds 140 MB — gross amplification/leak"
    )
