"""Wire-format pack/unpack roundtrip + palette sanity for the Display class."""

import numpy as np
import pytest

from hokku.screens.huessen_epf1301.display import HuessenEpf1301Display

_HUESSEN = HuessenEpf1301Display()


def test_constants():
    # Two half-panels make up the full byte count.
    assert (_HUESSEN.total_bytes // 2) * 2 == _HUESSEN.total_bytes
    assert _HUESSEN.panel_w * _HUESSEN.panel_h // 2 == _HUESSEN.total_bytes
    assert _HUESSEN.total_bytes == 960_000
    assert len(_HUESSEN.palette_measured_rgb) == 6
    assert len(_HUESSEN.palette_preview_rgb) == 6
    assert len(_HUESSEN.palette_nibble) == 6


def test_pack_unpack_roundtrip():
    rng = np.random.default_rng(0)
    idx = rng.integers(0, 6, size=(_HUESSEN.panel_h, _HUESSEN.panel_w), dtype=np.uint8)
    raw = _HUESSEN.indices_to_panel_bytes(idx)
    assert len(raw) == _HUESSEN.total_bytes
    back = _HUESSEN.panel_bytes_to_indices(raw)
    np.testing.assert_array_equal(idx, back)


def test_pack_wrong_shape_rejects():
    with pytest.raises(ValueError):
        _HUESSEN.indices_to_panel_bytes(np.zeros((10, 10), dtype=np.uint8))


def test_unpack_bad_nibble_rejects():
    # nibble 0xF is not in palette_nibble
    bad = bytes([0xFF] * _HUESSEN.total_bytes)
    with pytest.raises(ValueError):
        _HUESSEN.panel_bytes_to_indices(bad)


def test_unpack_wrong_size_rejects():
    with pytest.raises(ValueError):
        _HUESSEN.panel_bytes_to_indices(bytes([0x00] * (_HUESSEN.total_bytes - 1)))


def test_preview_rgb_shape():
    rng = np.random.default_rng(0)
    idx = rng.integers(0, 6, size=(40, 60), dtype=np.uint8)
    rgb = _HUESSEN.indices_to_preview_rgb(idx)
    assert rgb.shape == (40, 60, 3)
    assert rgb.dtype == np.uint8
