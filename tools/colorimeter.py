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

import contextlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

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
    # Full reflectance curve, when the instrument gives one. This is the only
    # part of a measurement that CANNOT be reconstructed later: XYZ is a
    # projection of it through one observer and one illuminant, so a session
    # recorded without spectra can never answer "how does this look under
    # tungsten?" without going back to the hardware.
    spectrum: np.ndarray | None = None  # reflectance %, one per wavelength
    wavelengths: np.ndarray | None = None  # nm, parallel to spectrum


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

    Flags below are verified against a real ColorMunki Photo with ArgyllCMS
    3.5.0, not guessed:

      ``-O``  do ONE calibration-or-measurement and exit. This is the whole
              reason the driven path works: spotread's interactive loop reads
              the **console** directly, not stdin, so piped keystrokes never
              reach it. -O sidesteps the loop entirely.
      ``-N``  skip auto-calibration. The instrument keeps its calibration
              between invocations, so calibrate once up front and every
              subsequent read starts immediately.
      ``-i D65``  compute XYZ under D65 rather than spotread's D50 default.
              This is what lets us skip chromatic adaptation altogether —
              bradford_adapt() stays available but is not in the path.
              (Note spotread still *prints* "D50 Lab" without -w; that label
              is ignored, parse_xyz takes the XYZ.)

    **No trigger is needed.** Once the instrument has been calibrated once,
    ``-O`` measures immediately and exits — verified on hardware with two
    back-to-back reads agreeing to 0.03 dE. The operator only has to place the
    meter on a patch; the software does the rest. (The interactive prompt does
    accept "instrument switch or any other key", but -O never reaches it.)

    Because timing is ours, a caller can cheaply average several reads per
    patch — at that repeatability it is nearly free accuracy.
    """

    name = "spotread"

    #   ``-s``  print the spectrum with each reading. With a logfile this appends
    #           the full 380-730 nm reflectance curve (36 bands) to the same row,
    #           so it costs nothing per reading and needs no second file.
    DEFAULT_ARGS = ("-O", "-N", "-s", "-i", "D65")

    # Set when a read fails, so a caller can tell "this instrument needs the dial
    # rotated" apart from "one reading glitched". They need opposite responses:
    # the first must stop the run immediately, the second should be retried.
    last_error: str | None = None

    def __init__(
        self,
        exe: str = "spotread",
        extra_args: list[str] | None = None,
        illuminant: str = "d65",
        timeout_s: float = 120.0,
    ):
        self.exe = exe
        self.extra_args = list(extra_args) if extra_args is not None else list(self.DEFAULT_ARGS)
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
        # Log to a file rather than scraping stdout: with a logfile argument
        # spotread writes a clean tab-separated "reading X Y Z L* a* b*" row,
        # which is far more robust than parsing prose. Verified on hardware.
        self.last_error = None  # cleared per attempt; only the latest matters
        with tempfile.TemporaryDirectory() as td:
            logfile = Path(td) / "reading.txt"
            outfile = Path(td) / "stdout.txt"
            errfile = Path(td) / "stderr.txt"
            cmd = [self.exe, *self.extra_args, str(logfile)]
            try:
                # -O measures once and exits on its own — no trigger needed once
                # the instrument has been calibrated.
                #
                # Redirect to FILES, never pipes. capture_output=True makes
                # subprocess.run wait for the stdout/stderr pipes to close, and the
                # ColorMunki driver runs a background switch-monitor thread that can
                # keep those handles open after the measurement is done — so the
                # call blocks on a reading that already succeeded (observed on
                # hardware: the meter visibly took both readings, the second never
                # returned). The reading itself is parsed from `logfile`, so the
                # pipes were pure risk.
                with (
                    outfile.open("w", encoding="utf-8") as so,
                    errfile.open("w", encoding="utf-8") as se,
                ):
                    subprocess.run(
                        cmd,
                        stdout=so,
                        stderr=se,
                        stdin=subprocess.DEVNULL,
                        text=True,
                        timeout=self.timeout_s,
                    )
            except subprocess.TimeoutExpired:
                print(f"    ! {self.exe} timed out after {self.timeout_s:.0f}s")
                # The measurement may still have landed before the hang, so fall
                # through and try the logfile rather than discarding it.
                pass

            if logfile.exists():
                # Row layout with -s:
                #   header  "Reading X Y Z L* a* b* 380.000 390.000 ... 730.000"
                #   data    "1      X Y Z L* a* b* <36 reflectance values>"
                # Wavelengths come from the header rather than being assumed, so a
                # different instrument or band count parses correctly instead of
                # being silently mislabelled.
                waves: np.ndarray | None = None
                for line in logfile.read_text(encoding="utf-8", errors="replace").splitlines():
                    parts = line.split()
                    if not parts:
                        continue
                    if parts[0].lower().startswith("reading") and len(parts) > 7:
                        with contextlib.suppress(ValueError):
                            waves = np.array([float(v) for v in parts[7:]])
                        continue
                    if len(parts) >= 4 and parts[0].isdigit():
                        try:
                            xyz = np.array([float(v) for v in parts[1:4]])
                        except ValueError:
                            continue
                        spec = None
                        if len(parts) > 7:
                            with contextlib.suppress(ValueError):
                                spec = np.array([float(v) for v in parts[7:]])
                        if spec is not None and waves is not None and len(spec) != len(waves):
                            spec = None  # mismatched: do not guess an alignment
                        return Reading(
                            xyz_d65=xyz.copy(),
                            raw=xyz,
                            source=self.name,
                            spectrum=spec,
                            wavelengths=waves if spec is not None else None,
                        )

            # Read the console text BEFORE the temp dir is removed; the fallback
            # below needs it and the directory does not outlive this block.
            out = "\n".join(
                f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
                for f in (outfile, errfile)
            )

        # Fall back to stdout scraping if the logfile route produced nothing.
        for line in out.splitlines():
            if "XYZ" in line.upper():
                raw = parse_xyz(line)
                if raw is not None:
                    return Reading(xyz_d65=raw.copy(), raw=raw, source=self.name)
        # Classify the failure — and be SPECIFIC. Matching the bare word
        # "calibration" anywhere in the output is wrong: when a one-shot read
        # fails, spotread falls back to its interactive loop and prints a banner
        # containing "'k' to do a calibration". That made every transient USB
        # glitch indistinguishable from an expired calibration, and a caller that
        # stops on the latter would kill a healthy run on a single hiccup.
        lowered = out.lower()
        if any(
            p in lowered
            for p in (
                "needs a calibration",
                "calibration failed",
                "set instrument sensor to calibration position",
                "got abort or error from calibration",
            )
        ):
            self.last_error = "calibration"
        elif any(
            p in lowered
            for p in (
                "communication problem",
                "communications failure",
                "instrument initialisation failed",
            )
        ):
            # Retryable. Measured at ~2 failures per 114 reads with the next read
            # succeeding; a known ColorMunki/Argyll USB flakiness, not a fault.
            self.last_error = "transient"
        else:
            self.last_error = "unknown"
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
            timeout_s=kw.get("timeout_s", 120.0),
        )
    raise ValueError(f"unknown instrument {kind!r}")
