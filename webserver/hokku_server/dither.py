"""Backward-compatible re-export shim.

All dither logic lives in ``dither_streaming`` (StreamingDither),
``dither_unconstrained`` (UnconstrainedDither), ``dither_numba`` (NumbaDither),
and ``dither_abc`` (AbstractDither + type aliases).  This shim keeps existing
imports working without change.  New code should import directly.
"""
from hokku_server.dither_abc import (  # noqa: F401
    AbstractDither,
    CanvasLike,
    DiffusionKernel,
    FloatArray,
    PrepStripe,
    UInt8Array,
    _DEFAULT_STRIPE_H,
)
from hokku_server.dither_config import AlgorithmName, DitherConfig, LutName  # noqa: F401
from hokku_server.dither_numba import NumbaDither  # noqa: F401
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
from hokku_server.dither_unconstrained import UnconstrainedDither  # noqa: F401

DEFAULT_STRIPE_H = _DEFAULT_STRIPE_H
