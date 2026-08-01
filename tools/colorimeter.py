#!/usr/bin/env python3
"""Instrument backends for panel colour measurement.

Two kinds of meter reach this code and they differ in ways that matter:

**Reflective spectrophotometer** (X-Rite ColorMunki Photo / Design, i1Studio,
i1Pro, Calibrite ColorChecker Studio). Has its own lamp and a 45°/0° head, so it
illuminates the sample itself, self-calibrates against a built-in white tile, and
reports *absolute* colorimetry. Nothing external is needed and the 45/0 geometry
rejects the specular reflection off the panel's front lamination — which is the
hardest thing to get right in a DIY rig. This is the good case.

**Emissive colorimeter** (Calibrite ColorChecker Display, i1Display Pro). No
lamp. It can only report the light arriving at its aperture, so the sample must
be lit externally and every reading carries the lamp's brightness and colour
cast. Those are divided out against a white reference of known Lab elsewhere
(see color_read.normalise). Supported here only as the manual-paste path.

The white-point trap
--------------------
Reflective measurement is a *print* convention and instruments default to
**D50**. This codebase is sRGB/**D65** throughout — `dither_streaming.xyz_to_lab`
divides by the D65 white point, and every `palette_measured_rgb` value is an sRGB
triple. Feeding D50 XYZ into it silently shifts every anchor.

So readings are converted with a Bradford chromatic adaptation before anything
downstream sees them. Prefer telling the instrument to report D65 directly; the
adaptation is the fallback for instruments (or vendor software) that will only
emit D50.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

import numpy as np

# CIE standard illuminants, XYZ normalised to Y = 1.
D50_XYZ = np.array([0.96422, 1.00000, 0.82521])
D65_XYZ = np.array([0.95047, 1.00000, 1.08883])

# Bradford cone-response matrix — the standard basis for chromatic adaptation
# (ICC uses it for exactly this D50<->D65 problem).
_BRADFORD = np.array(
    [
        [0.8951, 0.2664, -0.1614],
        [-0.7502, 1.7135, 0.0367],
        [0.0389, -0.0685, 1.0296],
    ]
)

_NUM = r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?"


def bradford_adapt(xyz, src_white=D50_XYZ, dst_white=D65_XYZ) -> np.ndarray:
    """Chromatically adapt XYZ from *src_white* to *dst_white*.

    Adapting a white point to itself is the identity, and adapting the source
    white gives the destination white exactly — both pinned in the tests.
    """
    xyz = np.asarray(xyz, dtype=float)
    src_cone = _BRADFORD @ np.asarray(src_white, dtype=float)
    dst_cone = _BRADFORD @ np.asarray(dst_white, dtype=float)
    m = np.linalg.inv(_BRADFORD) @ np.diag(dst_cone / src_cone) @ _BRADFORD
    return xyz @ m.T


def parse_xyz(text: str) -> np.ndarray | None:
    """Pull an XYZ triple out of an instrument line, or None.

    Accepts a bare ``X Y Z`` triple and ArgyllCMS ``spotread`` result lines such
    as ``Result is XYZ: 24.31 25.55 21.37, D50 Lab: ...`` — the XYZ is taken and
    any Lab on the line ignored, because that Lab is against the instrument's
    own white point and we do our own adaptation.
    """
    text = text.strip()
    if not text:
        return None
    m = re.search(rf"XYZ:?\s*({_NUM})[,\s]+({_NUM})[,\s]+({_NUM})", text, re.IGNORECASE)
    if m:
        return np.array([float(m.group(i)) for i in (1, 2, 3)])
    nums = re.findall(_NUM, text)
    if len(nums) >= 3:
        return np.array([float(n) for n in nums[:3]])
    return None


def detect_xyz_scale(readings: list[np.ndarray]) -> float:
    """Return 100.0 if the readings look percent-scaled, else 1.0.

    Argyll reports reflective XYZ as percentages (white ~= 80-100); this
    codebase works with Y = 1.0 for a perfect diffuser. Deciding per reading
    would be wrong — a dark patch reads below 2 on either scale — so the whole
    set is judged together by its brightest member.
    """
    if not readings:
        return 1.0
    return 100.0 if max(float(np.asarray(r)[1]) for r in readings) > 2.0 else 1.0


@dataclass
class Reading:
    xyz_d65: np.ndarray  # adapted, Y = 1.0 for a perfect diffuser
    raw: np.ndarray  # exactly what the instrument said
    source: str  # "spotread" | "manual"


class Instrument:
    """A source of tristimulus readings."""

    name = "instrument"

    def prepare(self) -> None:
        """Calibrate / warm up. Called once before the first patch."""

    def read(self, label: str) -> Reading | None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class ManualInstrument(Instrument):
    """Operator reads the meter and pastes the numbers.

    Works with any instrument and any vendor software, and needs no driver
    swap — which on Windows means it keeps working when ArgyllCMS's USB driver
    would otherwise have to displace the X-Rite/Calibrite one.
    """

    name = "manual"

    def __init__(self, illuminant: str = "d50"):
        self.illuminant = illuminant

    def read(self, label: str) -> Reading | None:
        while True:
            text = input(f"  {label}: ")
            if not text.strip():
                return None  # skip
            raw = parse_xyz(text)
            if raw is None:
                print("    ! could not parse — expected three numbers, or a spotread line")
                continue
            return Reading(xyz_d65=raw.copy(), raw=raw, source=self.name)


class SpotreadInstrument(Instrument):
    """Drives ArgyllCMS ``spotread`` once per patch.

    One process per reading rather than a long-lived interactive session: the
    interactive prompt flow differs between Argyll versions and instruments, and
    a per-reading invocation is far easier to reason about and to recover from.
    The cost is the instrument re-initialising each time, which is seconds.

    ``extra_args`` exists because the exact flags depend on the Argyll build and
    the instrument. Reflective spot mode is spotread's default (``-e`` selects
    emissive), so the common case needs nothing; ``-y`` and illuminant options
    can be added here once the meter is in hand.
    """

    name = "spotread"

    def __init__(
        self,
        exe: str = "spotread",
        extra_args: list[str] | None = None,
        illuminant: str = "d50",
        timeout_s: float = 120.0,
    ):
        self.exe = exe
        self.extra_args = list(extra_args or [])
        self.illuminant = illuminant
        self.timeout_s = timeout_s

    def prepare(self) -> None:
        if shutil.which(self.exe) is None:
            raise SystemExit(
                f"'{self.exe}' not found on PATH.\n"
                "Install ArgyllCMS and make sure its bin/ is on PATH. On Windows the\n"
                "instrument also needs Argyll's USB driver, which displaces the\n"
                "X-Rite/Calibrite one — swap back if you want to use i1Profiler again."
            )

    def read(self, label: str) -> Reading | None:
        cmd = [self.exe, *self.extra_args]
        try:
            # spotread takes a reading per newline on stdin and exits on 'q'.
            proc = subprocess.run(
                cmd, input="\nq\n", capture_output=True, text=True, timeout=self.timeout_s
            )
        except subprocess.TimeoutExpired:
            print(f"    ! {self.exe} timed out after {self.timeout_s:.0f}s")
            return None
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        for line in out.splitlines():
            if "XYZ" in line.upper():
                raw = parse_xyz(line)
                if raw is not None:
                    return Reading(xyz_d65=raw.copy(), raw=raw, source=self.name)
        print(f"    ! no XYZ in {self.exe} output; last lines:")
        for line in out.strip().splitlines()[-4:]:
            print(f"      | {line}")
        return None


def finalise(readings: list[Reading], illuminant: str) -> None:
    """Scale to Y=1 and adapt to D65, in place, once the whole set is known.

    Deferred until every patch is read because the percent-vs-unit scale can
    only be judged from the brightest reading in the set.
    """
    if not readings:
        return
    scale = detect_xyz_scale([r.raw for r in readings])
    src_white = D50_XYZ if illuminant.lower() == "d50" else D65_XYZ
    for r in readings:
        xyz = np.asarray(r.raw, dtype=float) / scale
        r.xyz_d65 = bradford_adapt(xyz, src_white, D65_XYZ) if illuminant.lower() == "d50" else xyz


def make_instrument(kind: str, **kw) -> Instrument:
    if kind == "manual":
        return ManualInstrument(illuminant=kw.get("illuminant", "d50"))
    if kind == "spotread":
        return SpotreadInstrument(
            exe=kw.get("exe", "spotread"),
            extra_args=kw.get("extra_args"),
            illuminant=kw.get("illuminant", "d50"),
        )
    raise ValueError(f"unknown instrument {kind!r}")
