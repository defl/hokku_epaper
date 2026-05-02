"""Orientation, cache keys, and dither / color pipeline helpers."""
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

import webserver


class TestOrientation:
    def _make_image(self, w, h, color=(128, 64, 32)):
        return Image.new("RGB", (w, h), color)

    def test_landscape_canvas_dimensions(self):
        img = self._make_image(800, 600)
        with patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG, orientation="landscape")):
            canvas, mask = webserver.flask_app._prepare_canvas(img)
        assert canvas.size == (webserver.FULL_W, webserver.PANEL_H)
        assert mask.shape == (webserver.PANEL_H, webserver.FULL_W)

    def test_portrait_canvas_dimensions(self):
        img = self._make_image(600, 800)
        with patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG, orientation="portrait")):
            canvas, mask = webserver.flask_app._prepare_canvas(img)
        assert canvas.size == (webserver.FULL_W, webserver.PANEL_H)
        assert mask.shape == (webserver.PANEL_H, webserver.FULL_W)

    def test_landscape_padding_mask_pillarbox(self):
        img = self._make_image(600, 800)
        with patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG, orientation="landscape")):
            canvas, mask = webserver.flask_app._prepare_canvas(img)
        assert mask.any(), "Should have some padding pixels"
        assert not mask.all(), "Should have some image pixels"

    def test_portrait_padding_mask_letterbox(self):
        img = self._make_image(800, 600)
        with patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG, orientation="portrait")):
            canvas, mask = webserver.flask_app._prepare_canvas(img)
        assert mask.any(), "Should have some padding pixels"
        assert not mask.all(), "Should have some image pixels"

    def test_landscape_exact_fit_no_padding(self):
        img = self._make_image(1600, 1200)
        with patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG, orientation="landscape")):
            canvas, mask = webserver.flask_app._prepare_canvas(img)
        assert not mask.any(), "Exact 4:3 landscape should have no padding"

    def test_portrait_exact_fit_no_padding(self):
        img = self._make_image(1200, 1600)
        with patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG, orientation="portrait")):
            canvas, mask = webserver.flask_app._prepare_canvas(img)
        assert not mask.any(), "Exact 3:4 portrait should have no padding"

    def test_padding_forced_white_landscape(self):
        img = self._make_image(100, 100, color=(50, 50, 50))
        with patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG, orientation="landscape")):
            canvas, mask = webserver.flask_app._prepare_canvas(img)
            r = webserver.PRESET_DITHER_ALGORITHMS["floyd_steinberg"]
            canvas_array = webserver.image.compress_dynamic_range(
                np.array(canvas, dtype=np.float32),
                scale_chroma=r.scale_chroma,
                adaptive_vivid=r.adaptive_vivid,
                vivid_chroma_low=r.vivid_chroma_low,
                vivid_chroma_high=r.vivid_chroma_high,
            )
            canvas_img = Image.fromarray(canvas_array.astype(np.uint8))
            lut, grid_scale = webserver.dither._cached_euclidean_lut()
            result_idx = webserver.dither.floyd_steinberg_dither(
                canvas_img,
                lut,
                grid_scale,
                False,
            )
            result_idx[mask] = 1
        assert (result_idx[mask] == 1).all(), "All padding pixels should be white in landscape"

    def test_padding_forced_white_portrait(self):
        img = self._make_image(100, 100, color=(50, 50, 50))
        with patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG, orientation="portrait")):
            canvas, mask = webserver.flask_app._prepare_canvas(img)
            r = webserver.PRESET_DITHER_ALGORITHMS["floyd_steinberg"]
            canvas_array = webserver.image.compress_dynamic_range(
                np.array(canvas, dtype=np.float32),
                scale_chroma=r.scale_chroma,
                adaptive_vivid=r.adaptive_vivid,
                vivid_chroma_low=r.vivid_chroma_low,
                vivid_chroma_high=r.vivid_chroma_high,
            )
            canvas_img = Image.fromarray(canvas_array.astype(np.uint8))
            lut, grid_scale = webserver.dither._cached_euclidean_lut()
            result_idx = webserver.dither.floyd_steinberg_dither(
                canvas_img,
                lut,
                grid_scale,
                False,
            )
            result_idx[mask] = 1
        assert (result_idx[mask] == 1).all(), "All padding pixels should be white in portrait"

    def test_pool_entries_are_metadata_only(self):
        entry = {"hash": "abc123"}
        assert "binary" not in entry
        assert "preview_png" not in entry

    def test_cache_orientation_splits_storage_dir_not_key_string(self):
        path = Path("/images/test.jpg")
        content_hash = "abcdef123456"
        with patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG, orientation="landscape")):
            key_l = webserver.flask_app._cache_key(path, content_hash)
            dir_l = webserver.flask_app._disk_cache.cache_dir
        with patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG, orientation="portrait")):
            key_p = webserver.flask_app._cache_key(path, content_hash)
            dir_p = webserver.flask_app._disk_cache.cache_dir
        assert key_l == key_p, "Same dither → same filename key; orientation uses different dc_ subfolder"
        assert dir_l != dir_p
        assert key_l.endswith(f"_{webserver._CACHE_VERSION}")

    def test_cache_key_differs_by_dither_algorithm(self):
        path = Path("/images/test.jpg")
        content_hash = "abcdef123456"
        keys = {}
        for name in webserver.PRESET_DITHER_ALGORITHMS:
            preset = webserver.PRESET_DITHER_ALGORITHMS[name]
            img_cfg = replace(
                preset,
                dither=replace(preset.dither, serpentine=False),
            )
            with patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG, image=img_cfg)):
                keys[name] = webserver.flask_app._cache_key(path, content_hash)
        assert len(set(keys.values())) == len(keys), f"Cache keys collided: {keys}"

    def test_cache_key_algorithm_override(self):
        path = Path("/images/test.jpg")
        content_hash = "abcdef123456"
        floyd = webserver.PRESET_DITHER_ALGORITHMS["floyd_steinberg"]
        atkinson = webserver.PRESET_DITHER_ALGORITHMS["atkinson"]
        with patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG, image=floyd)):
            key_active = webserver.flask_app._cache_key(path, content_hash)
            key_other = webserver.flask_app._cache_key(path, content_hash, image=atkinson)
        assert key_active != key_other
        assert "floyd_steinberg" not in key_active
        assert "atkinson" not in key_other

    def test_cache_key_differs_by_serpentine(self):
        path = Path("/images/test.jpg")
        content_hash = "abcdef123456"
        base_preset = webserver.PRESET_DITHER_ALGORITHMS["atkinson_hue_aware"]
        with patch.object(
            webserver.flask_app, "_config",
            replace(
                webserver.DEFAULT_CONFIG,
                image=replace(base_preset, dither=replace(base_preset.dither, serpentine=False)),
            ),
        ):
            key_off = webserver.flask_app._cache_key(path, content_hash)
        with patch.object(
            webserver.flask_app, "_config",
            replace(
                webserver.DEFAULT_CONFIG,
                image=replace(base_preset, dither=replace(base_preset.dither, serpentine=True)),
            ),
        ):
            key_on = webserver.flask_app._cache_key(path, content_hash)
        assert key_off != key_on

    def test_cache_key_serpentine_override(self):
        path = Path("/images/test.jpg")
        content_hash = "abcdef123456"
        base_preset = webserver.PRESET_DITHER_ALGORITHMS["atkinson_hue_aware"]
        with patch.object(
            webserver.flask_app, "_config",
            replace(
                webserver.DEFAULT_CONFIG,
                image=replace(base_preset, dither=replace(base_preset.dither, serpentine=True)),
            ),
        ):
            k_default = webserver.flask_app._cache_key(path, content_hash)
            k = webserver.flask_app._cache_key(
                path,
                content_hash,
                image=replace(base_preset, dither=replace(base_preset.dither, serpentine=False)),
            )
        assert k != k_default

    def test_default_is_atkinson_hue_aware(self):
        assert webserver.DEFAULT_CONFIG.image == webserver.PRESET_DITHER_ALGORITHMS["atkinson_hue_aware"]
        assert "atkinson_hue_aware" in webserver.PRESET_DITHER_ALGORITHMS
        assert "floyd_steinberg_hue_aware" in webserver.PRESET_DITHER_ALGORITHMS

    def test_floyd_steinberg_hue_aware_is_floyd_with_hue_lut(self):
        r = webserver.PRESET_DITHER_ALGORITHMS["floyd_steinberg_hue_aware"]
        assert r.dither.algorithm == "floyd_steinberg"
        assert r.dither.lut_name == "hue_aware"

    def test_preset_image_configs_embed_expected_dither(self):
        for name, image_cfg in webserver.PRESET_DITHER_ALGORITHMS.items():
            assert image_cfg is webserver.PRESET_DITHER_ALGORITHMS[name]
            assert isinstance(image_cfg.color_enhance, float)
            assert isinstance(image_cfg.scale_chroma, bool)
            assert isinstance(image_cfg.use_adaptive_saturate, bool)
            assert isinstance(image_cfg.adaptive_vivid, bool)
            off = replace(
                image_cfg,
                dither=replace(image_cfg.dither, serpentine=False),
            )
            assert off.dither.serpentine is False
        ahu = webserver.PRESET_DITHER_ALGORITHMS["atkinson_hue_aware"]
        r_ahu = replace(ahu, dither=replace(ahu.dither, serpentine=True))
        assert r_ahu.use_adaptive_saturate is True
        assert r_ahu.adaptive_vivid is True
        assert r_ahu.scale_chroma is False
        assert r_ahu.dither.serpentine is True
        r_fs = webserver.PRESET_DITHER_ALGORITHMS["floyd_steinberg"]
        assert r_fs.use_adaptive_saturate is False
        assert r_fs.adaptive_vivid is False
        r_stucki_h = webserver.PRESET_DITHER_ALGORITHMS["stucki_hue_aware"]
        assert webserver.dither._DITHER_FN[r_stucki_h.dither.algorithm] is webserver.dither.stucki_dither
        assert r_stucki_h.use_adaptive_saturate is True
        assert r_stucki_h.adaptive_vivid is True
        r_stucki = webserver.PRESET_DITHER_ALGORITHMS["stucki"]
        assert webserver.dither._DITHER_FN[r_stucki.dither.algorithm] is webserver.dither.stucki_dither
        assert r_stucki.use_adaptive_saturate is False

    def test_floyd_steinberg_hue_aware_config_persists(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"dither_algorithm": "floyd_steinberg_hue_aware"}, f)
            temp_path = f.name
        try:
            with patch.dict(os.environ, {"HOKKU_CONFIG": temp_path}):
                config = webserver.AppConfig.load_from_file()
            assert config.image == webserver.PRESET_DITHER_ALGORITHMS["floyd_steinberg_hue_aware"]
        finally:
            os.unlink(temp_path)

    def test_cache_key_includes_version(self):
        path = Path("/images/test.jpg")
        key = webserver.flask_app._cache_key(path, "abcdef123456")
        assert webserver._CACHE_VERSION in key

    def test_is_near_grayscale_detects_bw(self):
        bw = Image.new("RGB", (400, 300))
        pixels = bw.load()
        for y in range(300):
            for x in range(400):
                v = (x * 255) // 400
                pixels[x, y] = (v, v, v)
        assert webserver.image.is_near_grayscale(bw) is True

        color = Image.new("RGB", (400, 300), (200, 40, 40))
        assert webserver.image.is_near_grayscale(color) is False

    def test_compress_dynamic_range_scale_chroma_flag(self):
        img = np.full((8, 8, 3), [220, 40, 40], dtype=np.float32)
        out_lonly = webserver.image.compress_dynamic_range(
            img, scale_chroma=False, adaptive_vivid=False, vivid_chroma_low=5.0, vivid_chroma_high=15.0,
        )
        out_vivid = webserver.image.compress_dynamic_range(
            img, scale_chroma=True, adaptive_vivid=False, vivid_chroma_low=5.0, vivid_chroma_high=15.0,
        )
        lab_lonly = webserver.dither.rgb_to_lab(out_lonly)
        lab_vivid = webserver.dither.rgb_to_lab(out_vivid)
        chroma_lonly = np.sqrt(lab_lonly[..., 1] ** 2 + lab_lonly[..., 2] ** 2).mean()
        chroma_vivid = np.sqrt(lab_vivid[..., 1] ** 2 + lab_vivid[..., 2] ** 2).mean()
        assert chroma_vivid < chroma_lonly, f"vivid={chroma_vivid:.1f} should be < lonly={chroma_lonly:.1f}"

    def test_adaptive_vivid_preserves_saturated_chroma(self):
        saturated = np.full((8, 8, 3), [220, 40, 40], dtype=np.float32)
        neutral = np.full((8, 8, 3), [200, 195, 198], dtype=np.float32)

        out_sat_adaptive = webserver.image.compress_dynamic_range(
            saturated, scale_chroma=False, adaptive_vivid=True, vivid_chroma_low=5.0, vivid_chroma_high=15.0,
        )
        out_sat_uniform = webserver.image.compress_dynamic_range(
            saturated, scale_chroma=True, adaptive_vivid=False, vivid_chroma_low=5.0, vivid_chroma_high=15.0,
        )
        lab_ad = webserver.dither.rgb_to_lab(out_sat_adaptive)
        lab_un = webserver.dither.rgb_to_lab(out_sat_uniform)
        chr_ad = np.sqrt(lab_ad[..., 1] ** 2 + lab_ad[..., 2] ** 2).mean()
        chr_un = np.sqrt(lab_un[..., 1] ** 2 + lab_un[..., 2] ** 2).mean()
        assert chr_ad > chr_un, f"adaptive kept less chroma: ad={chr_ad:.1f}, un={chr_un:.1f}"

        out_neu_adaptive = webserver.image.compress_dynamic_range(
            neutral, scale_chroma=False, adaptive_vivid=True, vivid_chroma_low=5.0, vivid_chroma_high=15.0,
        )
        lab_neu_in = webserver.dither.rgb_to_lab(neutral)
        lab_neu_out = webserver.dither.rgb_to_lab(out_neu_adaptive)
        chr_in = np.sqrt(lab_neu_in[..., 1] ** 2 + lab_neu_in[..., 2] ** 2).mean()
        chr_out = np.sqrt(lab_neu_out[..., 1] ** 2 + lab_neu_out[..., 2] ** 2).mean()
        assert chr_out <= chr_in + 0.5, f"neutral amplified: in={chr_in:.2f}, out={chr_out:.2f}"

    def test_adaptive_saturate_gated_by_source_chroma(self):
        saturated = np.full((8, 8, 3), [220, 40, 40], dtype=np.float64)
        neutral = np.full((8, 8, 3), [200, 200, 200], dtype=np.float64)

        sat_before = np.sqrt(np.sum((webserver.dither.rgb_to_lab(saturated)[..., 1:]) ** 2, axis=-1)).mean()
        sat_after = np.sqrt(np.sum((webserver.dither.rgb_to_lab(webserver.dither.adaptive_saturate(
            saturated, 1.3, 5.0, 15.0,
        ))[..., 1:]) ** 2, axis=-1)).mean()
        assert sat_after > sat_before, f"saturated should be boosted: before={sat_before:.1f} after={sat_after:.1f}"

        neu_before = np.sqrt(np.sum((webserver.dither.rgb_to_lab(neutral)[..., 1:]) ** 2, axis=-1)).mean()
        neu_after = np.sqrt(np.sum((webserver.dither.rgb_to_lab(webserver.dither.adaptive_saturate(
            neutral, 1.3, 5.0, 15.0,
        ))[..., 1:]) ** 2, axis=-1)).mean()
        assert abs(neu_after - neu_before) < 0.5, f"neutral should be unchanged: before={neu_before:.2f} after={neu_after:.2f}"
