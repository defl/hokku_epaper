"""Named :class:`~webserver.image.DisplayImageConfig` presets for the panel pipeline."""
from __future__ import annotations

from webserver.dither import DitherConfig
from webserver.image import DisplayImageConfig, ImageConfig

PRESET_DISPLAY_IMAGE_CONFIGS: dict[str, DisplayImageConfig] = {
    "floyd_steinberg": DisplayImageConfig(
        image=ImageConfig(
            dither=DitherConfig(
                algorithm="floyd_steinberg",  # type: ignore[arg-type]
                lut_name="euclidean",  # type: ignore[arg-type]
                serpentine=False,
                hue_cutoff_deg=95.0,
                neutral_chroma=8.0,
            ),
            prepare_autocontrast_cutoff=0.5,
            prepare_gamma=0.85,
            prepare_brightness=1.0,
            prepare_contrast=1.1,
            prepare_sharpness=1.3,
            color_enhance=1.2,
            use_adaptive_saturate=False,
            saturate_max_enhance=1.0,
            saturate_low_chroma_thresh=5.0,
            saturate_high_chroma_thresh=15.0,
            scale_chroma=False,
            adaptive_vivid=False,
            vivid_chroma_low=5.0,
            vivid_chroma_high=15.0,
        ),
        orientation="landscape",
    ),
    "floyd_steinberg_hue_aware": DisplayImageConfig(
        image=ImageConfig(
            dither=DitherConfig(
                algorithm="floyd_steinberg",  # type: ignore[arg-type]
                lut_name="hue_aware",  # type: ignore[arg-type]
                serpentine=False,
                hue_cutoff_deg=95.0,
                neutral_chroma=8.0,
            ),
            prepare_autocontrast_cutoff=0.5,
            prepare_gamma=0.85,
            prepare_brightness=1.0,
            prepare_contrast=1.1,
            prepare_sharpness=1.3,
            color_enhance=1.25,
            use_adaptive_saturate=True,
            saturate_max_enhance=1.25,
            saturate_low_chroma_thresh=5.0,
            saturate_high_chroma_thresh=15.0,
            scale_chroma=False,
            adaptive_vivid=True,
            vivid_chroma_low=5.0,
            vivid_chroma_high=15.0,
        ),
        orientation="landscape",
    ),
    "atkinson": DisplayImageConfig(
        image=ImageConfig(
            dither=DitherConfig(
                algorithm="atkinson",  # type: ignore[arg-type]
                lut_name="euclidean",  # type: ignore[arg-type]
                serpentine=False,
                hue_cutoff_deg=95.0,
                neutral_chroma=8.0,
            ),
            prepare_autocontrast_cutoff=0.5,
            prepare_gamma=0.85,
            prepare_brightness=1.0,
            prepare_contrast=1.1,
            prepare_sharpness=1.3,
            color_enhance=1.2,
            use_adaptive_saturate=False,
            saturate_max_enhance=1.0,
            saturate_low_chroma_thresh=5.0,
            saturate_high_chroma_thresh=15.0,
            scale_chroma=False,
            adaptive_vivid=False,
            vivid_chroma_low=5.0,
            vivid_chroma_high=15.0,
        ),
        orientation="landscape",
    ),
    "atkinson_hue_aware": DisplayImageConfig(
        image=ImageConfig(
            dither=DitherConfig(
                algorithm="atkinson",  # type: ignore[arg-type]
                lut_name="hue_aware",  # type: ignore[arg-type]
                serpentine=False,
                hue_cutoff_deg=95.0,
                neutral_chroma=8.0,
            ),
            prepare_autocontrast_cutoff=0.5,
            prepare_gamma=0.85,
            prepare_brightness=1.0,
            prepare_contrast=1.1,
            prepare_sharpness=1.3,
            color_enhance=1.25,
            use_adaptive_saturate=True,
            saturate_max_enhance=1.25,
            saturate_low_chroma_thresh=5.0,
            saturate_high_chroma_thresh=15.0,
            scale_chroma=False,
            adaptive_vivid=True,
            vivid_chroma_low=5.0,
            vivid_chroma_high=15.0,
        ),
        orientation="landscape",
    ),
    "stucki": DisplayImageConfig(
        image=ImageConfig(
            dither=DitherConfig(
                algorithm="stucki",  # type: ignore[arg-type]
                lut_name="euclidean",  # type: ignore[arg-type]
                serpentine=False,
                hue_cutoff_deg=95.0,
                neutral_chroma=8.0,
            ),
            prepare_autocontrast_cutoff=0.5,
            prepare_gamma=0.85,
            prepare_brightness=1.0,
            prepare_contrast=1.1,
            prepare_sharpness=1.3,
            color_enhance=1.2,
            use_adaptive_saturate=False,
            saturate_max_enhance=1.0,
            saturate_low_chroma_thresh=5.0,
            saturate_high_chroma_thresh=15.0,
            scale_chroma=False,
            adaptive_vivid=False,
            vivid_chroma_low=5.0,
            vivid_chroma_high=15.0,
        ),
        orientation="landscape",
    ),
    "stucki_hue_aware": DisplayImageConfig(
        image=ImageConfig(
            dither=DitherConfig(
                algorithm="stucki",  # type: ignore[arg-type]
                lut_name="hue_aware",  # type: ignore[arg-type]
                serpentine=False,
                hue_cutoff_deg=95.0,
                neutral_chroma=8.0,
            ),
            prepare_autocontrast_cutoff=0.5,
            prepare_gamma=0.85,
            prepare_brightness=1.0,
            prepare_contrast=1.1,
            prepare_sharpness=1.3,
            color_enhance=1.25,
            use_adaptive_saturate=True,
            saturate_max_enhance=1.25,
            saturate_low_chroma_thresh=5.0,
            saturate_high_chroma_thresh=15.0,
            scale_chroma=False,
            adaptive_vivid=True,
            vivid_chroma_low=5.0,
            vivid_chroma_high=15.0,
        ),
        orientation="landscape",
    ),
}
