#!/usr/bin/env python3
"""The parameter space a per-image search is allowed to move in.

`ImageConfig` has 31 fields and the three shipped presets differ in 10 of them.
That is the current model's entire vocabulary. This declares a wider one, as data
rather than as code, so the search, the candidate generator for judging sessions,
and the fitted per-image model all move in exactly the same space and cannot
quietly disagree about what is tunable.

Three rules shaped the list:

**Only knobs that change the picture.** Fields that merely re-express another
(the CIELAB/OKLAB threshold twins) are pinned to the space their partner selects,
because searching both halves independently wastes dimensions on settings that
can never both be live.

**Ranges bracket the shipped presets, and go past them.** A range that stops at
the current value can only confirm what is already there. Where a measurement
says the current value is suspect the range deliberately extends well beyond it —
`clahe_clip_limit` reaching 3.5 when the live server runs 1.75, for instance.

**Ordered where ordering is real.** Continuous knobs carry a step so a
coordinate-descent neighbour means "one notch", and categorical knobs list
alternatives with no implied order. A search over a categorical treated as a
number would interpolate between two dither algorithms, which is meaningless.

The intent is that a fitted model outputs a point in this space, so ``to_vector``
and ``from_vector`` round-trip: the same encoding trains the model and drives the
renderer.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from hokku.webserver.dither_config import DitherConfig
from hokku.webserver.image_config import ImageConfig

# ── the knobs ────────────────────────────────────────────────────────────────
#
# (path, kind, spec). ``path`` is "field" or "dither.field". For "num" the spec
# is (low, high, step); for "cat" it is the tuple of allowed values.

KNOBS: tuple[tuple[str, str, tuple], ...] = (
    # Dither. The algorithm and the palette LUT together decide how out-of-gamut
    # colour is paid for, which is the panel's dominant failure: a red wall
    # renders with 28 % blue ink under the shipped pair.
    ("dither.algorithm", "cat", ("atkinson", "floyd_steinberg", "stucki")),
    (
        "dither.lut_name",
        "cat",
        (
            "hue_aware",
            "hue_aware_weighted",
            "oklab_hue_aware",
            "cam16ucs_hue_aware",
            "euclidean_weighted",
        ),
    ),
    ("dither.serpentine", "cat", (True, False)),
    # The hue gate. Measured black ink is chromatic (C* 15.5), so at the shipped
    # neutral_chroma of 8 black stops being exempt from the gate and shadows can
    # be pushed to green, which is three times lighter. The range spans both
    # sides of that cliff.
    ("dither.hue_cutoff_deg", "num", (60.0, 130.0, 5.0)),
    ("dither.neutral_chroma", "num", (4.0, 20.0, 2.0)),
    # Tone. Everything here happens before the dither sees the picture.
    ("prepare_autocontrast_cutoff", "num", (0.0, 3.0, 0.25)),
    ("prepare_gamma", "num", (0.70, 1.15, 0.03)),
    ("prepare_brightness", "num", (0.90, 1.15, 0.025)),
    ("prepare_contrast", "num", (0.90, 1.35, 0.025)),
    ("prepare_midtone", "num", (0.90, 1.15, 0.02)),
    # Local contrast. 0 disables. The live server runs 1.75 while the repo
    # default is 0.0, so the truth is already contested and the range is wide.
    ("clahe_clip_limit", "num", (0.0, 3.5, 0.25)),
    # Sharpening — the direct lever on the detail loss that drew the loudest
    # complaint (measured 0.86x source modulation on the shipping config).
    ("prepare_usm_radius", "num", (0.6, 2.0, 0.1)),
    ("prepare_usm_amount", "num", (0.0, 220.0, 10.0)),
    # Colour.
    ("color_enhance", "num", (0.90, 1.60, 0.05)),
    ("adaptive_saturate_space", "cat", ("off", "cielab", "oklab")),
    ("saturate_max_enhance", "num", (1.00, 1.60, 0.05)),
    ("adaptive_vivid", "cat", (True, False)),
    ("scale_chroma", "cat", (True, False)),
    ("drc_l_space", "cat", ("cielab", "oklab")),
    ("drc_chroma_space", "cat", ("cielab", "oklab")),
    # Noise breaks up the banding error diffusion leaves on smooth gradients.
    ("dither_noise", "num", (0.0, 4.0, 0.5)),
)

KNOB_NAMES: tuple[str, ...] = tuple(k[0] for k in KNOBS)

# Threshold pairs that mirror one another across colour spaces. Searching both
# independently would spend dimensions on whichever half is inert, so each pair
# moves together, scaled by the ratio between the shipped defaults.
_MIRRORED = {
    "saturate_low_chroma_thresh": ("saturate_low_chroma_thresh_oklab", 0.005),
    "saturate_high_chroma_thresh": ("saturate_high_chroma_thresh_oklab", 0.005),
    "vivid_chroma_low": ("vivid_chroma_low_oklab", 0.005),
    "vivid_chroma_high": ("vivid_chroma_high_oklab", 0.005),
}


def _int_fields() -> frozenset[str]:
    """Config fields declared `int`, so a knob never hands them a float.

    ImageConfig is a plain dataclass and does not coerce, and its constraint
    table only checks ranges — so `prepare_usm_amount = 110.0` is accepted here
    and then raises `TypeError: 'float' object cannot be interpreted as an
    integer` deep inside PIL's UnsharpMask. That silently removed the whole
    sharpening knob from a sensitivity sweep (72 of 1176 renders, every one of
    them that knob's). Read from the annotations rather than hard-coded, so a
    new int field is handled without anyone remembering this.
    """
    names = set()
    for cls in (ImageConfig, DitherConfig):
        for field, annotation in getattr(cls, "__annotations__", {}).items():
            if annotation in ("int", int):
                names.add(field)
    return frozenset(names)


INT_FIELDS = _int_fields()


def _coerce(path: str, value):
    return round(float(value)) if path.split(".")[-1] in INT_FIELDS else value


def get(cfg: ImageConfig, path: str):
    obj = cfg.dither if path.startswith("dither.") else cfg
    return getattr(obj, path.split(".")[-1])


def set_one(cfg: ImageConfig, path: str, value) -> ImageConfig:
    """A copy of *cfg* with one knob changed, mirrored twins kept consistent."""
    value = _coerce(path, value)
    if path.startswith("dither."):
        return replace(cfg, dither=replace(cfg.dither, **{path.split(".")[-1]: value}))
    updates = {path: value}
    if path in _MIRRORED:
        twin, scale = _MIRRORED[path]
        updates[twin] = float(value) * scale
    return replace(cfg, **updates)


def _quantise(value: float, spec: tuple) -> float:
    """Snap onto the knob's grid, anchored at the range's low end.

    Anchoring at zero instead put the shipped defaults off-grid: `prepare_gamma`
    ships at 0.88 with a step of 0.03, and 0.88/0.03 rounds to 0.87. Coordinate
    descent starting from the baseline would then move that knob before it had
    evaluated anything, and report it as a change nobody chose.
    """
    low, high, step = spec
    snapped = low + round((value - low) / step) * step
    # Round off the float dust: 0.70 + 6 * 0.03 is 0.8799999999999999, which is
    # not equal to the shipped 0.88 and prints as noise in a config diff.
    return float(np.clip(round(snapped, 10), low, high))


def neighbours(cfg: ImageConfig, knob: str) -> list[ImageConfig]:
    """Every one-notch move along one knob — the unit of coordinate descent."""
    kind, spec = next((k, s) for name, k, s in KNOBS if name == knob)
    current = get(cfg, knob)
    if kind == "cat":
        return [set_one(cfg, knob, v) for v in spec if v != current]
    low, high, step = spec
    out = []
    for candidate in (float(current) - step, float(current) + step):
        if low - 1e-9 <= candidate <= high + 1e-9:
            value = _quantise(candidate, spec)
            if abs(value - float(current)) > 1e-9:
                out.append(set_one(cfg, knob, value))
    return out


def sample(base: ImageConfig, rng: np.random.Generator, knobs: int | None = None) -> ImageConfig:
    """A random point in the space, or a random move in *knobs* dimensions.

    Perturbing a few knobs from a known-good base beats sampling all of them
    independently: the space is 21-dimensional and almost all of it renders
    badly, so uniform sampling spends a judging session on candidates nobody
    would ever ship.
    """
    chosen = KNOB_NAMES if knobs is None else rng.choice(np.array(KNOB_NAMES), knobs, replace=False)
    cfg = base
    for knob in chosen:
        kind, spec = next((k, s) for name, k, s in KNOBS if name == str(knob))
        if kind == "cat":
            cfg = set_one(cfg, str(knob), spec[int(rng.integers(len(spec)))])
        else:
            low, high, step = spec
            steps = round((high - low) / step)
            cfg = set_one(
                cfg, str(knob), _quantise(low + step * int(rng.integers(steps + 1)), spec)
            )
    return cfg


def to_vector(cfg: ImageConfig) -> np.ndarray:
    """Encode a config for model fitting: numbers scaled to 0..1, categories as index."""
    out = []
    for name, kind, spec in KNOBS:
        value = get(cfg, name)
        if kind == "cat":
            out.append(float(spec.index(value)))
        else:
            low, high, _step = spec
            out.append((float(value) - low) / (high - low))
    return np.array(out, dtype=float)


def from_vector(base: ImageConfig, vector: np.ndarray) -> ImageConfig:
    """Inverse of ``to_vector``, quantised back onto the grid the search uses."""
    cfg = base
    for value, (name, kind, spec) in zip(vector, KNOBS, strict=True):
        if kind == "cat":
            cfg = set_one(cfg, name, spec[int(np.clip(round(value), 0, len(spec) - 1))])
        else:
            low, high, _step = spec
            cfg = set_one(cfg, name, _quantise(low + float(value) * (high - low), spec))
    return cfg


def describe(cfg: ImageConfig, base: ImageConfig) -> str:
    """Just the knobs that differ from *base* — the readable form of a candidate."""
    parts = [
        f"{name.split('.')[-1]}={get(cfg, name)}"
        for name in KNOB_NAMES
        if get(cfg, name) != get(base, name)
    ]
    return ", ".join(parts) if parts else "(baseline)"


def main() -> int:
    from hokku.webserver.presets import PRESET_IMAGE_CONFIGS  # noqa: PLC0415 — CLI only

    base = PRESET_IMAGE_CONFIGS["default_general"]
    size = 1
    print(f"  {len(KNOBS)} knobs")
    for name, kind, spec in KNOBS:
        if kind == "cat":
            n = len(spec)
        else:
            low, high, step = spec
            n = round((high - low) / step) + 1
        size *= n
        print(f"    {name:34s} {kind}  {n:3d} values   now={get(base, name)}")
    print(f"\n  grid size {size:.3e} — far too large to enumerate, hence descent not sweep")
    rng = np.random.default_rng(0)
    for i in range(3):
        print(f"  sample {i}: {describe(sample(base, rng, knobs=3), base)}")
    round_trip = from_vector(base, to_vector(base))
    print(f"  round-trip identical: {round_trip == base}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
