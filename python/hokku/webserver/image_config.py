"""ImageConfig dataclass and its JSON helper."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Literal, get_args, get_origin, get_type_hints

from hokku.webserver.dither_config import DitherConfig

logger = logging.getLogger(__name__)

AdaptiveSaturateSpace = Literal["off", "cielab", "oklab"]
DrcSpace = Literal["cielab", "oklab"]


@dataclass(frozen=True)
class ImageConfig:
    """How to convert a source image to palette indices.

    Orientation is *not* stored here — it lives on the per-screen
    ScreenConfig and is passed explicitly to the render functions.
    """

    dither: DitherConfig
    prepare_autocontrast_cutoff: float
    prepare_gamma: float
    prepare_brightness: float
    prepare_contrast: float
    color_enhance: float
    adaptive_saturate_space: AdaptiveSaturateSpace
    saturate_max_enhance: float
    saturate_low_chroma_thresh: float  # CIELAB chroma units
    saturate_high_chroma_thresh: float  # CIELAB chroma units
    saturate_low_chroma_thresh_oklab: float  # OKLAB chroma units (panel inks 0.09–0.18)
    saturate_high_chroma_thresh_oklab: float
    scale_chroma: bool
    adaptive_vivid: bool
    vivid_chroma_low: float  # CIELAB chroma units
    vivid_chroma_high: float  # CIELAB chroma units
    vivid_chroma_low_oklab: float  # OKLAB chroma units
    vivid_chroma_high_oklab: float
    drc_l_space: DrcSpace  # space for L compression
    drc_chroma_space: DrcSpace  # space for chroma scaling
    prepare_midtone: float
    clahe_clip_limit: float
    clahe_keepout_feather: float  # sigma = min(canvas_w, canvas_h) * this; 0 = hard edge
    prepare_usm_radius: float
    prepare_usm_amount: int
    dither_noise: float

    def cache_slug(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:14]


def _bw_safe_image_config(cfg: ImageConfig) -> ImageConfig:
    """Return *cfg* with saturation boosters disabled (safe for B&W images)."""
    return replace(
        cfg,
        color_enhance=1.05,
        adaptive_saturate_space="off",
        adaptive_vivid=False,
        scale_chroma=False,
    )


# NOTE: there used to be a _LENIENT_DEFAULTS table here, listing the fields a
# stored config was allowed to omit. It existed because the parser reset a
# pipeline wholesale when any field was missing, so every field added to
# ImageConfig had to be hand-registered in a second place or it would silently
# wipe people's tuning on upgrade. That is what happened: clahe_keepout_feather
# was added without being listed, and from then on the shipped
# config.json.example (and any config predating that field) reset all three
# pipelines to the fallback preset. Merging onto the defaults makes the table
# unnecessary — a missing field is simply a field that keeps its default.


class ImageConfigError(ValueError):
    """A caller-supplied ImageConfig blob was rejected.

    ``errors`` holds every problem found, not just the first, so a UI can list
    them all instead of making the user resubmit once per mistake.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


# Value constraints for the fields where an out-of-range number either crashes a
# pipeline stage or silently produces nonsense. Fields absent here are still
# type-checked; they simply have no meaningful bound. Keyed by field name across
# both ImageConfig and DitherConfig — the names do not collide.
_CONSTRAINTS: dict[str, tuple[Callable[[float], bool], str]] = {
    # PIL's autocontrast takes this percentage off *each* end of the histogram,
    # so 50 or more would clip the whole range away.
    "prepare_autocontrast_cutoff": (lambda v: 0.0 <= v < 50.0, "must be >= 0 and < 50"),
    # Exponents: zero or negative inverts or flattens the curve into garbage.
    "prepare_gamma": (lambda v: v > 0.0, "must be > 0"),
    "prepare_midtone": (lambda v: v > 0.0, "must be > 0"),
    # PIL enhancement factors: 1.0 is "unchanged", 0 is the degenerate image.
    "prepare_brightness": (lambda v: v > 0.0, "must be > 0"),
    "prepare_contrast": (lambda v: v > 0.0, "must be > 0"),
    "color_enhance": (lambda v: v > 0.0, "must be > 0"),
    "saturate_max_enhance": (lambda v: v > 0.0, "must be > 0"),
    # Chroma thresholds are distances in Lab/OKLAB space — never negative.
    "saturate_low_chroma_thresh": (lambda v: v >= 0.0, "must be >= 0"),
    "saturate_high_chroma_thresh": (lambda v: v >= 0.0, "must be >= 0"),
    "saturate_low_chroma_thresh_oklab": (lambda v: v >= 0.0, "must be >= 0"),
    "saturate_high_chroma_thresh_oklab": (lambda v: v >= 0.0, "must be >= 0"),
    "vivid_chroma_low": (lambda v: v >= 0.0, "must be >= 0"),
    "vivid_chroma_high": (lambda v: v >= 0.0, "must be >= 0"),
    "vivid_chroma_low_oklab": (lambda v: v >= 0.0, "must be >= 0"),
    "vivid_chroma_high_oklab": (lambda v: v >= 0.0, "must be >= 0"),
    "neutral_chroma": (lambda v: v >= 0.0, "must be >= 0"),
    "clahe_clip_limit": (lambda v: v >= 0.0, "must be >= 0 (0 disables CLAHE)"),
    # Feather is a fraction of the canvas's short edge.
    "clahe_keepout_feather": (lambda v: 0.0 <= v <= 1.0, "must be between 0 and 1"),
    "prepare_usm_radius": (lambda v: v >= 0.0, "must be >= 0"),
    "prepare_usm_amount": (lambda v: v >= 0, "must be >= 0 (0 disables sharpening)"),
    "dither_noise": (lambda v: v >= 0.0, "must be >= 0 (0 disables noise)"),
    # A hue angle, in degrees.
    "hue_cutoff_deg": (lambda v: 0.0 <= v <= 360.0, "must be between 0 and 360"),
}


def _check_value(name: str, value: Any, hint: Any, path: str, errors: list[str]) -> None:
    """Type- and range-check one field, appending to *errors* rather than raising."""
    where = f"{path}.{name}"

    if get_origin(hint) is Literal:
        allowed = get_args(hint)
        if value not in allowed:
            errors.append(f"{where}: {value!r} is not one of {', '.join(map(repr, allowed))}")
        return

    # bool is a subclass of int in Python, so both directions need the explicit
    # check: True must not pass as a number, and 1 must not pass as a flag.
    if hint is bool:
        if not isinstance(value, bool):
            errors.append(f"{where}: must be true or false, got {value!r}")
        return
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{where}: must be a whole number, got {value!r}")
            return
    elif hint is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{where}: must be a number, got {value!r}")
            return
    else:  # pragma: no cover — a field type this function doesn't know about
        errors.append(f"{where}: unsupported field type {hint!r}")
        return

    constraint = _CONSTRAINTS.get(name)
    if constraint is not None and not constraint[0](value):
        errors.append(f"{where}: {constraint[1]}, got {value!r}")


def _check_exact_keys(blob: dict, expected: set[str], path: str, errors: list[str]) -> None:
    """Require *blob* to carry exactly *expected*. Typos must not pass silently."""
    for missing in sorted(expected - blob.keys()):
        errors.append(f"{path}.{missing}: required")
    for unknown in sorted(blob.keys() - expected):
        errors.append(f"{path}.{unknown}: unknown field")


def image_config_from_dict_strict(
    blob: Any,
    *,
    field_path: str = "image_config",
) -> ImageConfig:
    """Build an ImageConfig from a caller-supplied blob, rejecting anything odd.

    This is the parser for data arriving over the API. It is deliberately the
    opposite of ``_image_config_from_dict``, which merges onto defaults so an
    older stored config keeps loading: here every field must be present and
    valid, and an unrecognised key is an error rather than something to ignore.

    The difference matters because the two have opposite failure modes. Quietly
    keeping a default is right for a config file written by a previous version;
    it is wrong for a request, where it would answer 200 while discarding the
    setting the user actually asked for — a typo'd field name, an invalid
    ``lut_name`` or a nonsensical gamma would all appear to succeed and then
    either do nothing or fail later inside a render worker.

    Raises:
        ImageConfigError: listing every problem found.
    """
    errors: list[str] = []
    if not isinstance(blob, dict):
        raise ImageConfigError([f"{field_path}: must be an object, got {type(blob).__name__}"])

    image_hints = get_type_hints(ImageConfig)
    dither_hints = get_type_hints(DitherConfig)
    image_names = {f.name for f in fields(ImageConfig)}
    dither_names = {f.name for f in fields(DitherConfig)}

    _check_exact_keys(blob, image_names, field_path, errors)

    dither_blob = blob.get("dither")
    dither_path = f"{field_path}.dither"
    if "dither" in blob and not isinstance(dither_blob, dict):
        errors.append(f"{dither_path}: must be an object, got {type(dither_blob).__name__}")
        dither_blob = None
    elif isinstance(dither_blob, dict):
        _check_exact_keys(dither_blob, dither_names, dither_path, errors)
        for name in sorted(dither_names & dither_blob.keys()):
            _check_value(name, dither_blob[name], dither_hints[name], dither_path, errors)

    for name in sorted((image_names & blob.keys()) - {"dither"}):
        _check_value(name, blob[name], image_hints[name], field_path, errors)

    if errors:
        raise ImageConfigError(errors)

    assert isinstance(dither_blob, dict)  # no errors means every key checked out
    return ImageConfig(
        dither=DitherConfig(**{n: dither_blob[n] for n in dither_names}),
        **{n: blob[n] for n in image_names - {"dither"}},
    )


def parse_crop_to_fill_threshold(
    value: Any, *, field_path: str = "crop_to_fill_threshold"
) -> float:
    """Validate a crop-to-fill threshold, a fraction of the image's long edge.

    Raises:
        ImageConfigError: if it is not a number in [0, 1].
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ImageConfigError([f"{field_path}: must be a number, got {value!r}"])
    if not 0.0 <= value <= 1.0:
        raise ImageConfigError([f"{field_path}: must be between 0 and 1, got {value!r}"])
    return float(value)


def _image_config_from_dict(
    blob: Any,
    *,
    field_path: str = "image_config",
    default: ImageConfig | None = None,
) -> ImageConfig:
    """Build an ImageConfig by merging a stored JSON object onto the defaults.

    Fields present in *blob* win; fields absent keep their value from
    *default*. A config that predates a field therefore keeps every setting it
    does carry, instead of being discarded.

    This used to reset the whole pipeline to the fallback preset if a single
    field was missing, which silently wiped tuning on upgrade — the entire
    shipped example config was being reset that way. See the note where
    _LENIENT_DEFAULTS used to live.

    Args:
        blob:       The dict (or None) to parse.
        field_path: Used in log/error messages to identify which config field is bad.
        default:    Base to merge onto. Defaults to the fallback preset; callers
                    should pass the default for *their* pipeline so a sparse
                    B&W or face blob falls back to B&W or face values rather
                    than to the generic default pipeline.
    """
    from hokku.webserver.presets import (  # noqa: PLC0415 — deferred to break circular import
        FALLBACK_PRESET,
        PRESET_IMAGE_CONFIGS,
    )

    base = default if default is not None else PRESET_IMAGE_CONFIGS[FALLBACK_PRESET]

    if blob is None:
        return base
    if not isinstance(blob, dict):
        raise ValueError(f"config['{field_path}'] must be an object")

    # Renames still need handling explicitly — a renamed field is not the same
    # thing as a missing one, and dropping it would lose a real setting.
    blob = dict(blob)  # shallow copy — don't mutate the caller's dict
    if "adaptive_saturate_space" not in blob and "use_adaptive_saturate" in blob:
        blob["adaptive_saturate_space"] = "cielab" if blob["use_adaptive_saturate"] else "off"
    blob.pop("use_adaptive_saturate", None)  # tolerate either presence; ignore now

    dither_blob = blob.get("dither")
    if dither_blob is not None and not isinstance(dither_blob, dict):
        raise ValueError(f"config['{field_path}']['dither'] must be an object")
    dither_blob = dither_blob or {}
    dither_kwargs = {
        f.name: (dither_blob[f.name] if f.name in dither_blob else getattr(base.dither, f.name))
        for f in fields(DitherConfig)
    }
    dither = DitherConfig(**dither_kwargs)

    image_kwargs: dict[str, Any] = {"dither": dither}
    filled_from_default: list[str] = []
    for f in fields(ImageConfig):
        if f.name == "dither":
            continue
        if f.name in blob:
            image_kwargs[f.name] = blob[f.name]
        else:
            image_kwargs[f.name] = getattr(base, f.name)
            filled_from_default.append(f.name)

    if filled_from_default:
        # Say so. The old reset was completely silent, which is why a shipped
        # example config could be discarded on every single load unnoticed.
        logger.info(
            "config['%s']: %d field(s) absent, kept defaults: %s",
            field_path,
            len(filled_from_default),
            ", ".join(sorted(filled_from_default)),
        )

    return ImageConfig(**image_kwargs)
