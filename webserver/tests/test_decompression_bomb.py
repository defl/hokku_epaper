"""Decompression-bomb guards: PNG with huge declared dimensions must be
rejected before its pixel buffer is materialized.

The Raspberry Pi has limited RAM; an attacker (or a well-meaning user who
drags a 20000x20000 PNG onto the upload UI) could exhaust memory and OOM-kill
the server. These tests verify three layers of defense:

1. open_image_for_render() refuses oversized files before convert("RGB").
2. /hokku/api/upload returns the file in "skipped" with a clear reason.
3. PIL's Image.MAX_IMAGE_PIXELS is set to the project cap.
"""
from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image

from hokku_server import image_renderer
from hokku_server.app_state import AppState, build_manager
from hokku_server.flask_app import create_app
from hokku_server.image_classifier import ImageClassifier
from hokku_server.image_renderer import (
    MAX_IMAGE_PIXELS,
    MAX_SOURCE_LONG,
    MAX_SOURCE_SHORT,
    MAX_UPLOAD_PIXELS,
    open_image_for_render,
)
from hokku_server.serve_scheduler import ServeScheduler


def _crc(chunk_type: bytes, data: bytes) -> bytes:
    return struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)


def _make_bomb_png(width: int, height: int) -> bytes:
    """Forge a PNG whose IHDR declares (width, height) but whose IDAT is empty.

    The file is a few hundred bytes on disk but PIL's header parse reports
    the full declared dimensions — exactly the shape of a real decompression
    bomb. The IDAT is malformed (won't decode), which is fine: our guards run
    on header dims and must reject the file before any decode is attempted.
    """
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + _crc(b"IHDR", ihdr_data)
    idat_data = zlib.compress(b"")
    idat = struct.pack(">I", len(idat_data)) + b"IDAT" + idat_data + _crc(b"IDAT", idat_data)
    iend = struct.pack(">I", 0) + b"IEND" + _crc(b"IEND", b"")
    return sig + ihdr + idat + iend


def test_pil_max_image_pixels_is_capped():
    """The module sets a project-wide PIL cap below the default ~89 MP."""
    assert Image.MAX_IMAGE_PIXELS == MAX_IMAGE_PIXELS
    assert MAX_IMAGE_PIXELS <= 50_000_000


def test_bomb_png_header_reports_dimensions(tmp_path: Path):
    """Sanity-check the test fixture: the forged PNG really does declare its size.

    Use a size between MAX_IMAGE_PIXELS and 2*MAX_IMAGE_PIXELS so PIL's own
    check (which trips at 2x) doesn't fire — we want to verify *our* code
    can read the header and catch it.
    """
    bomb = tmp_path / "bomb.png"
    bomb.write_bytes(_make_bomb_png(8_000, 8_000))  # 64 MP, between 40 and 80
    with Image.open(bomb) as probe:
        assert probe.size == (8_000, 8_000)


def test_open_image_for_render_rejects_bomb(tmp_path: Path):
    """Our explicit dim check fires before convert, on a bomb just above the cap."""
    bomb = tmp_path / "bomb.png"
    bomb.write_bytes(_make_bomb_png(8_000, 8_000))  # 64 MP > 40 MP cap
    with pytest.raises(ValueError, match="too large"):
        open_image_for_render(bomb)


def test_open_image_for_render_rejects_massive_bomb(tmp_path: Path):
    """A bomb above 2x the cap is rejected by PIL at Image.open() itself."""
    bomb = tmp_path / "bomb.png"
    bomb.write_bytes(_make_bomb_png(20_000, 20_000))  # 400 MP
    with pytest.raises((ValueError, Image.DecompressionBombError)):
        open_image_for_render(bomb)


def _build_client(app_config):
    clf = ImageClassifier(app_config)
    mgr = build_manager(app_config, clf)
    sch = ServeScheduler(mgr)
    state = AppState(app_config, clf, mgr, sch)
    app = create_app(state)
    return app.test_client(), state


def test_api_upload_rejects_bomb(app_config):
    """/hokku/api/upload must put the bomb in skipped[] with a 'too large' reason."""
    client, state = _build_client(app_config)
    try:
        bomb_bytes = _make_bomb_png(8_000, 8_000)
        rv = client.post(
            "/hokku/api/upload",
            data={"file": (io.BytesIO(bomb_bytes), "bomb.png")},
            content_type="multipart/form-data",
        )
        assert rv.status_code == 200
        body = rv.get_json()
        assert body["saved"] == []
        assert len(body["skipped"]) == 1
        assert "too large" in body["skipped"][0]["reason"].lower()
        # The file must not have been written to disk.
        assert not (Path(app_config.upload_dir) / "bomb.png").exists()
    finally:
        try:
            state.manager.shutdown()
        except Exception:
            pass


def test_api_upload_accepts_normal_image(tmp_path: Path, app_config, make_test_image):
    """A normal small image still uploads successfully (regression check)."""
    src = make_test_image(tmp_path / "good.png", size=(40, 30))
    client, state = _build_client(app_config)
    try:
        with src.open("rb") as fh:
            rv = client.post(
                "/hokku/api/upload",
                data={"file": (fh, "good.png")},
                content_type="multipart/form-data",
            )
        assert rv.status_code == 200, rv.get_data(as_text=True)
        body = rv.get_json()
        assert body["saved"] == ["good.png"], body
        assert body["skipped"] == []
    finally:
        try:
            state.manager.shutdown()
        except Exception:
            pass


def test_upload_pixel_cap_matches_image_cap():
    """Upload-time cap should not exceed the decode-time cap — otherwise an
    uploaded image could be accepted at upload but fail at render."""
    assert MAX_UPLOAD_PIXELS <= MAX_IMAGE_PIXELS


def test_open_image_shrinks_when_oversize_in_both_dirs(tmp_path: Path):
    """An image that exceeds 2x screen in BOTH directions gets shrunk."""
    # Build a real image just over the 2x-both threshold but under MAX_IMAGE_PIXELS.
    # 4000x3000 = 12 MP, > (3200, 2400) in both dims.
    src = tmp_path / "oversize_both.png"
    Image.new("RGB", (4000, 3000), (100, 50, 20)).save(src)
    img = open_image_for_render(src)
    try:
        long_side = max(img.size)
        short_side = min(img.size)
        assert long_side <= MAX_SOURCE_LONG
        assert short_side <= MAX_SOURCE_SHORT
        # Aspect ratio is preserved (within rounding).
        assert abs(img.size[0] / img.size[1] - 4000 / 3000) < 0.01
    finally:
        img.close()


def test_open_image_does_not_shrink_tall_thin_portrait(tmp_path: Path):
    """A portrait that exceeds 2x screen on only one axis keeps its detail.

    The "both directions" rule means we don't aggressively shrink an image
    whose short side is already at or below 2x screen — it might be a tall
    panorama or magazine page where the short-side detail matters.
    """
    # 1500 wide x 5000 tall — long side > MAX_SOURCE_LONG (3200) but short
    # side (1500) is below MAX_SOURCE_SHORT (2400). 7.5 MP, under cap.
    src = tmp_path / "tall_thin.png"
    Image.new("RGB", (1500, 5000), (40, 40, 80)).save(src)
    img = open_image_for_render(src)
    try:
        # The general long-side cap still applies — long edge clamped to
        # _MAX_SOURCE_LONG_SIDE = 3200. We just verify the BOTH rule didn't
        # also force the short edge below 2x screen short.
        assert img.size[0] >= 900, (
            "short axis was over-shrunk; the 2x-both rule should not "
            "have triggered for an image that is only oversized on one axis"
        )
    finally:
        img.close()
