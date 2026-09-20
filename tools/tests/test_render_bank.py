"""Regression tests for the per-image tuning tools.

Every case here is a bug that actually happened during a long unattended run and
that was invisible until something downstream misbehaved — which is the reason
they are worth pinning. Each one produced plausible-looking output while being
wrong.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import config_space
import param_search
import render_bank
from cam_compare import block_mean
from color_validate_photos import lab_img, srgb_img_to_xyz
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS


@pytest.fixture(scope="module")
def base():
    return PRESET_IMAGE_CONFIGS["default_general"]


class TestIntFieldCoercion:
    """`prepare_usm_amount` is declared int; a float reaches PIL and raises.

    The dataclass does not coerce and the constraint table only checks ranges,
    so a float passed validation and then died inside UnsharpMask with
    "TypeError: 'float' object cannot be interpreted as an integer". Every render
    of that knob failed and the whole sharpening dimension silently vanished from
    a sensitivity sweep that still printed a full table.
    """

    def test_int_fields_discovered_from_annotations(self):
        assert "prepare_usm_amount" in config_space.INT_FIELDS

    def test_float_is_coerced_to_int(self, base):
        cfg = config_space.set_one(base, "prepare_usm_amount", 110.0)
        assert isinstance(cfg.prepare_usm_amount, int)
        assert cfg.prepare_usm_amount == 110

    def test_float_fields_are_left_alone(self, base):
        cfg = config_space.set_one(base, "prepare_gamma", 0.91)
        assert isinstance(cfg.prepare_gamma, float)
        assert cfg.prepare_gamma == pytest.approx(0.91)

    def test_every_numeric_knob_survives_its_own_range(self, base):
        """A knob whose extremes cannot be set is a knob the search cannot use."""
        for name, kind, spec in config_space.KNOBS:
            if kind != "num":
                continue
            low, high, _step = spec
            for value in (low, (low + high) / 2, high):
                cfg = config_space.set_one(base, name, value)
                got = config_space.get(cfg, name)
                assert got == pytest.approx(value, abs=1.0), f"{name}={value} -> {got}"


class TestKnobRoundTrip:
    def test_vector_round_trip_is_identity(self, base):
        assert config_space.from_vector(base, config_space.to_vector(base)) == base

    def test_neighbours_differ_from_the_source(self, base):
        for name, _kind, _spec in config_space.KNOBS:
            for cfg in config_space.neighbours(base, name):
                assert config_space.get(cfg, name) != config_space.get(base, name)


class TestChromaVsSourceOnGreyscale:
    """The metric divides by the source's mean chroma, which is ~0 for B&W.

    A search dry run scored one greyscale photograph at +490 where every other
    image scored +0.7..4.5, and stored session metrics held 174..1013 against a
    normal ~0.5. Two of sixteen session images are effectively greyscale, so left
    alone this would have dominated a preference fit.
    """

    @staticmethod
    def _measure(canvas_rgb):
        display = DISPLAY_REGISTRY["huessen_epf1301"]
        model = render_bank.load_model()
        block = render_bank.BLOCK
        src_lab = lab_img(block_mean(srgb_img_to_xyz(canvas_rgb), block))
        full_lab = render_bank.rgb_to_lab(np.asarray(canvas_rgb, dtype=np.float64))
        full_c = np.hypot(full_lab[..., 1], full_lab[..., 2])
        full_hue = np.arctan2(full_lab[..., 2], full_lab[..., 1])
        reference = {
            "rect": None,  # a synthetic canvas has no letterbox to crop away
            "canvas": canvas_rgb,
            "src_lab": src_lab,
            "ref_lab": render_bank.adapted_reference(src_lab),
            "masks": render_bank._region_masks(src_lab, model["ceiling"]),
            "everywhere": np.ones(src_lab.shape[:2], bool),
            "full_lab": full_lab,
            "full_c": full_c,
            "full_hue": full_hue,
            "neutral": full_c < 10.0,
            "saturated": full_c > 25.0,
            "warm": (full_hue > np.radians(-40.0)) & (full_hue < np.radians(70.0)),
            # A 64x64 synthetic gradient contains no faces, so both face masks
            # are empty. Stated explicitly rather than omitted: measure() should
            # fail loudly on a malformed reference, not quietly skip metrics.
            "face": np.zeros(src_lab.shape[:2], bool),
            "face_full": np.zeros(canvas_rgb.shape[:2], bool),
        }
        idx = np.zeros(canvas_rgb.shape[:2], dtype=np.uint8)
        idx[:, ::2] = 1  # alternate black and white ink
        return render_bank.measure(reference, idx, display, model, block=block)

    def test_omitted_for_a_neutral_source(self):
        grey = np.repeat(np.linspace(0, 255, 64, dtype=np.uint8).reshape(1, -1, 1), 64, axis=0)
        canvas = np.repeat(grey, 3, axis=2)
        assert "chroma_vs_source" not in self._measure(canvas)

    def test_present_and_finite_for_a_coloured_source(self):
        canvas = np.zeros((64, 64, 3), dtype=np.uint8)
        canvas[..., 0] = 200
        canvas[..., 2] = 40
        metrics = self._measure(canvas)
        assert "chroma_vs_source" in metrics
        assert np.isfinite(metrics["chroma_vs_source"])
        assert metrics["chroma_vs_source"] < 10.0

    def test_chroma_use_never_explodes(self):
        """The same divide-by-nothing trap, on the other chroma metric."""
        canvas = np.full((64, 64, 3), 250, dtype=np.uint8)
        assert self._measure(canvas)["chroma_use"] < 10.0


class TestCacheKeyIncludesTheDisplay:
    """A LUT arm and the baseline must not share a cache row.

    The correction LUT is selected by the *display*, not by the ImageConfig, and
    every LUT arm in this project is rendered through a variant display with its
    own `model_id`. The cache keyed on `(image, config_slug, div)` only, so
    `gamut_50` and `baseline` on one photograph collided: the second one measured
    returned the first one's numbers and reported success. Most of the arms rated
    in the campaign are LUT swaps, so a preference fit over them would have been
    fitted to duplicated rows without anything looking wrong.
    """

    def test_two_displays_do_not_collide(self, tmp_path, base):
        conn = render_bank.open_cache(tmp_path / "bank.sqlite")
        slug = base.cache_slug()
        render_bank.cache_put(conn, "img", slug, 1, "huessen_epf1301", {"yn_dC": 1.0})
        render_bank.cache_put(conn, "img", slug, 1, "huessen_gamut50", {"yn_dC": 2.0})

        assert render_bank.cache_get(conn, "img", slug, 1, "huessen_epf1301") == {"yn_dC": 1.0}
        assert render_bank.cache_get(conn, "img", slug, 1, "huessen_gamut50") == {"yn_dC": 2.0}
        conn.close()

    def test_a_row_is_not_served_to_another_display(self, tmp_path, base):
        conn = render_bank.open_cache(tmp_path / "bank.sqlite")
        slug = base.cache_slug()
        render_bank.cache_put(conn, "img", slug, 1, "huessen_epf1301", {"yn_dC": 1.0})

        assert render_bank.cache_get(conn, "img", slug, 1, "huessen_red100") is None
        conn.close()

    def test_the_metric_code_still_invalidates(self, base):
        """`bank_version()` must stay in the key beside the display."""
        key = render_bank.cache_key(base.cache_slug(), "huessen_epf1301")
        assert render_bank.bank_version() in key
        assert "huessen_epf1301" in key


class TestTrustRegion:
    """The search must not optimise its way outside the evidence.

    The first full turn of the loop did exactly that: the objective correctly
    learned from 832 ratings that less saturation was wanted, had nothing telling
    it where to stop, and produced renders judged "so overlit it's useless".
    Nine of seventeen notes called the tuned version washed out; none said that
    of the baseline it was beating.
    """

    def _region(self, tmp_path, weights, rows, quantile=0.02, top=20):
        path = tmp_path / "corpus.csv"
        pd.DataFrame(rows).to_csv(path, index=False)
        return param_search.TrustRegion(path, weights, quantile, top)

    def test_a_metric_outside_the_judged_range_is_refused(self, tmp_path):
        region = self._region(
            tmp_path, {"contrast_ratio": -2.0}, {"contrast_ratio": np.linspace(0.4, 1.0, 100)}
        )
        bands = region.widen_for({"contrast_ratio": 0.7})
        assert param_search.TrustRegion.allows(bands, {"contrast_ratio": 0.7})
        assert not param_search.TrustRegion.allows(bands, {"contrast_ratio": 0.1})

    def test_the_baseline_is_always_a_legal_starting_point(self, tmp_path):
        """A live config already outside the judged range must still be searchable."""
        region = self._region(
            tmp_path, {"contrast_ratio": -2.0}, {"contrast_ratio": np.linspace(0.4, 1.0, 100)}
        )
        bands = region.widen_for({"contrast_ratio": 0.2})  # below anything judged
        assert param_search.TrustRegion.allows(bands, {"contrast_ratio": 0.2})
        assert not param_search.TrustRegion.allows(bands, {"contrast_ratio": 0.15})

    def test_the_contrast_family_is_bounded_whatever_its_leverage(self, tmp_path):
        """Leverage is |weight| x numeric range, which under-ranks a ratio.

        `contrast_ratio` spans about 0.4-1.0 against a dE metric's 40 units, so
        ranking by leverage alone dropped the one metric that had actually been
        observed to break the picture.
        """
        region = self._region(
            tmp_path,
            {"contrast_ratio": -2.0, "yn_de00": -2.0},
            {
                "contrast_ratio": np.linspace(0.4, 1.0, 100),
                "yn_de00": np.linspace(5.0, 45.0, 100),
            },
            top=1,
        )
        assert "yn_de00" in region.bands  # wins on leverage
        assert "contrast_ratio" in region.bands  # pinned by name regardless

    def test_a_metric_the_corpus_never_measured_is_not_bounded(self, tmp_path):
        region = self._region(
            tmp_path, {"invented": 5.0}, {"contrast_ratio": np.linspace(0.4, 1.0, 100)}
        )
        assert "invented" not in region.bands

    def test_a_missing_measurement_does_not_refuse_a_candidate(self, tmp_path):
        """Region metrics vanish when their mask is empty; that is not a violation."""
        region = self._region(
            tmp_path, {"contrast_ratio": -2.0}, {"contrast_ratio": np.linspace(0.4, 1.0, 100)}
        )
        bands = region.widen_for({"contrast_ratio": 0.7})
        assert param_search.TrustRegion.allows(bands, {})
        assert param_search.TrustRegion.allows(bands, {"contrast_ratio": float("nan")})
