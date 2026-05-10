"""Backward-compatibility shim for dither_constrained.

All logic has moved to ``dither_streaming`` (StreamingDither class) and
``dither_abc`` (type aliases).  Existing imports continue to resolve.
New code should import directly from the appropriate module.
"""
from hokku_server.dither_abc import (  # noqa: F401
    CanvasLike,
    DiffusionKernel,
    FloatArray,
    PrepStripe,
    UInt8Array,
    _DEFAULT_STRIPE_H,
)
from hokku_server.dither_config import AlgorithmName, DitherConfig, LutName  # noqa: F401
from hokku_server.dither_streaming import (  # noqa: F401
    PALETTE_LAB,
    StreamingDither,
    _cached_euclidean_lut,
    _cached_hue_aware_lut,
    adaptive_saturate,
    build_rgb_lut,
    build_rgb_lut_hue_aware,
    dither,
    dither_with_prep,
    linear_to_xyz,
    lut_and_scale_for_dither_config,
    noop_dither,
    rgb_to_lab,
    srgb_to_linear,
    xyz_to_lab,
)

DEFAULT_STRIPE_H = _DEFAULT_STRIPE_H
