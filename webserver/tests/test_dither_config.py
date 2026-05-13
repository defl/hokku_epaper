"""DitherConfig: round-trip and cache_slug stability."""
from __future__ import annotations

from dataclasses import asdict

from hokku_server.dither_config import DitherConfig


def _default() -> DitherConfig:
    return DitherConfig(
        algorithm="floyd_steinberg",
        lut_name="euclidean",
        serpentine=False,
        hue_cutoff_deg=95.0,
        neutral_chroma=8.0,
    )


def test_default_roundtrip():
    cfg = _default()
    d = asdict(cfg)
    assert DitherConfig(**d) == cfg


def test_non_default_roundtrip():
    cfg = DitherConfig(
        algorithm="atkinson",
        lut_name="hue_aware",
        serpentine=True,
        hue_cutoff_deg=60.0,
        neutral_chroma=12.5,
    )
    d = asdict(cfg)
    assert DitherConfig(**d) == cfg


def test_cache_slug_stable():
    cfg = _default()
    assert cfg.cache_slug() == cfg.cache_slug()
    assert cfg.cache_slug() == DitherConfig(**asdict(cfg)).cache_slug()


def test_cache_slug_changes_on_algorithm():
    a = _default()
    b = DitherConfig(**{**asdict(a), "algorithm": "atkinson"})
    assert a.cache_slug() != b.cache_slug()


def test_cache_slug_changes_on_lut_name():
    a = _default()
    b = DitherConfig(**{**asdict(a), "lut_name": "hue_aware"})
    assert a.cache_slug() != b.cache_slug()


def test_cache_slug_changes_on_serpentine():
    a = _default()
    b = DitherConfig(**{**asdict(a), "serpentine": True})
    assert a.cache_slug() != b.cache_slug()


def test_cache_slug_changes_on_hue_cutoff():
    a = _default()
    b = DitherConfig(**{**asdict(a), "hue_cutoff_deg": 50.0})
    assert a.cache_slug() != b.cache_slug()


def test_cache_slug_length():
    assert len(_default().cache_slug()) == 14
