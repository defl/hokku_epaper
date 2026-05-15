"""DitherConfig dataclass — algorithm, LUT, and scan-order settings."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal

AlgorithmName = Literal["floyd_steinberg", "atkinson", "stucki", "noop"]
LutName = Literal["euclidean", "hue_aware", "bw"]


@dataclass(frozen=True)
class DitherConfig:
    """Error-diffusion algorithm, palette LUT, scan order."""

    algorithm: AlgorithmName
    lut_name: LutName
    serpentine: bool
    hue_cutoff_deg: float
    neutral_chroma: float

    def cache_slug(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:14]
