"""Backward-compatible re-export shim.

All dither logic has moved to ``dither_constrained`` (streaming) and
``dither_unconstrained`` (full-canvas reference).  This shim keeps existing
imports working without change.  New code should import directly from the
appropriate module.
"""
from hokku_server.dither_constrained import (  # noqa: F401
    PALETTE_LAB,
    PrepStripe,
    DiffusionKernel,
    UInt8Array,
    FloatArray,
    CanvasLike,
    adaptive_saturate,
    build_rgb_lut,
    build_rgb_lut_hue_aware,
    dither,
    dither_with_prep,
    lut_and_scale_for_dither_config,
    linear_to_xyz,
    noop_dither,
    rgb_to_lab,
    srgb_to_linear,
    xyz_to_lab,
    _cached_euclidean_lut,
    _cached_hue_aware_lut,
)
from hokku_server.dither_config import AlgorithmName, DitherConfig, LutName  # noqa: F401

# DEFAULT_STRIPE_H is an internal constant of dither_constrained; it's exposed
# here only for the memory-budget test that documents the stripe size.
from hokku_server.dither_constrained import _DEFAULT_STRIPE_H as DEFAULT_STRIPE_H  # noqa: F401
