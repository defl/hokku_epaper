"""Wire-format pack/unpack roundtrip + palette sanity."""
import numpy as np
import pytest

from hokku_server.display import (
    FULL_W,
    PALETTE_MEASURED_RGB,
    PALETTE_NIBBLE,
    PALETTE_PREVIEW_RGB,
    PANEL_BYTES,
    PANEL_H,
    TOTAL_BYTES,
    indices_to_panel_bytes,
    indices_to_preview_rgb,
    panel_bytes_to_indices,
)


def test_constants():
    assert PANEL_BYTES * 2 == TOTAL_BYTES
    assert FULL_W * PANEL_H // 2 == TOTAL_BYTES
    assert len(PALETTE_MEASURED_RGB) == 6
    assert len(PALETTE_PREVIEW_RGB) == 6
    assert len(PALETTE_NIBBLE) == 6


def test_pack_unpack_roundtrip():
    rng = np.random.default_rng(0)
    idx = rng.integers(0, 6, size=(PANEL_H, FULL_W), dtype=np.uint8)
    raw = indices_to_panel_bytes(idx)
    assert len(raw) == TOTAL_BYTES
    back = panel_bytes_to_indices(raw)
    np.testing.assert_array_equal(idx, back)


def test_pack_wrong_shape_rejects():
    with pytest.raises(ValueError):
        indices_to_panel_bytes(np.zeros((10, 10), dtype=np.uint8))


def test_unpack_bad_nibble_rejects():
    bad = bytes([0xFF] * TOTAL_BYTES)  # nibble 0xF is not in PALETTE_NIBBLE
    with pytest.raises(ValueError):
        panel_bytes_to_indices(bad)


def test_preview_rgb_shape():
    rng = np.random.default_rng(0)
    idx = rng.integers(0, 6, size=(40, 60), dtype=np.uint8)
    rgb = indices_to_preview_rgb(idx)
    assert rgb.shape == (40, 60, 3)
    assert rgb.dtype == np.uint8
