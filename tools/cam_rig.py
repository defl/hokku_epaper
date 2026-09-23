#!/usr/bin/env python3
"""The camera rig: take a still of the panel from the Raspberry Pi over SSH.

The rig is a Pi 4 with an OV5647 on an M12 varifocal, aimed at the bench panel,
running a fullscreen live preview so the framing can be set by eye. That preview
owns the sensor — libcamera hands the camera to one process at a time — so every
capture has to stop it and put it back. ``hokku-cam-shot`` on the Pi does that
part; this module drives it and brings the file home.

Exposure is pinned rather than left on auto, and that is the whole point of this
module existing instead of a one-line ssh call. Auto-exposure and auto-white-
balance re-decide on every frame, so two captures minutes apart get different
gains and are not comparable to each other, let alone to a colorimeter reading.
The defaults below are the settled recipe for this bench:

  * ``--gain 1`` with a long shutter instead of the ~4x gain auto picks. The
    panel does not move, so exposure time is free and the sensor noise it buys
    back is not. Measured: FocusFoM 23697 -> 30409 for the same scene.
  * ``--awbgains`` frozen at the values AWB converged on under this room's
    dim-to-warm lamp, so the warm cast is removed identically every time.
  * ``--sharpness 0`` and denoise off, because both are spatial operators that
    invent local colour — fatal when the thing being measured is a dither
    pattern a few pixels across.

Change the lamp and every one of those numbers is stale; re-measure with an
auto-exposure frame and update AWB_GAINS.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

import dng

DEFAULT_HOST = "defl@hokku-cam.local"
DEFAULT_KEY = Path.home() / ".ssh" / "hokku_cam"

# Set against the PANEL SHOWING WHITE, which is the brightest thing this rig will
# ever photograph, by tools/cam_calibrate.py exposure.
#
# The shutter was once taken from an auto-exposure frame of a dark portrait and
# was 5x too long: a white panel came back 98 % clipped and every bright chart
# patch pinned at 255, which looks exactly like a modelling failure and is not
# one. Exposure is set from the brightest case, never from a representative one.
#
# The AWB gains no longer affect any measurement — those come from the raw
# Bayer data, which is captured before white balance exists. They are kept
# only so the JPEG used for registration looks sane to a feature detector, and
# they are the values AWB converged on under the 5116 K bench lamp.
#
# 52 ms until 2026-09-18, when the lamp was changed and the camera reframed.
# Briefly 200 ms (lamp at about a quarter), then 90 ms after a further lamp
# adjustment: white at 0.84 of full scale, nothing clipped. Captures either side
# of a lamp change are on different calibrations; earlier ones are kept in
# build/camcal/calib_20260906 and calib_20260918_dim1.
AWB_GAINS = (1.4375, 1.2751)
SHUTTER_US = 40_000
GAIN = 1.0
SENSOR_W, SENSOR_H = 2592, 1944


@dataclass(frozen=True)
class Shot:
    local_path: Path
    metadata: dict


def _ssh_base(host: str, key: Path) -> list[str]:
    return [
        "ssh",
        "-i",
        str(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        host,
    ]


def capture(
    local_path: Path,
    *,
    host: str = DEFAULT_HOST,
    key: Path = DEFAULT_KEY,
    shutter_us: int = SHUTTER_US,
    gain: float = GAIN,
    awb_gains: tuple[float, float] | None = AWB_GAINS,
    settle_ms: int = 2000,
    raw: bool = False,
    extra: tuple[str, ...] = (),
    verbose: bool = True,
) -> Shot:
    """Take one still with pinned exposure and copy it back. Returns its metadata.

    ``awb_gains=None`` leaves AWB on — only useful for re-measuring the lamp,
    never for a capture that will be compared against another.
    """
    remote = f"/tmp/{local_path.name}"  # noqa: S108 — a path on the Pi, not this host
    args = [
        "-t",
        str(settle_ms),
        "--width",
        str(SENSOR_W),
        "--height",
        str(SENSOR_H),
        "--gain",
        str(gain),
        "--shutter",
        str(shutter_us),
        "--sharpness",
        "0",
        "--denoise",
        "cdn_off",
        "--metadata",
        "-",
        "--metadata-format",
        "json",
    ]
    if awb_gains is not None:
        args += ["--awbgains", f"{awb_gains[0]},{awb_gains[1]}"]
    if raw:
        args += ["--raw"]
    args += list(extra)

    remote_cmd = (
        "hokku-cam-shot " + shlex.quote(remote) + " " + " ".join(shlex.quote(a) for a in args)
    )
    cmd = [*_ssh_base(host, key), remote_cmd]
    if verbose:
        print(f"  snap -> {remote} (gain {gain}, {shutter_us / 1000:.0f} ms)", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"capture failed ({proc.returncode}):\n{proc.stderr[-2000:]}")

    meta = _parse_metadata(proc.stdout)

    _scp_from_pi(host, key, remote, local_path)
    # Only the JPEG. When raw=True the DNG is still on the Pi and has not been
    # copied yet — capture_linear fetches it next and cleans it up itself.
    _remote_rm(host, key, [remote])
    return Shot(local_path=local_path, metadata=meta)


def _remote_rm(host: str, key: Path, paths: list[str]) -> None:
    """Best-effort delete on the Pi. Never fatal: the frame is already local."""
    quoted = " ".join(shlex.quote(x) for x in paths)
    try:
        subprocess.run(
            [*_ssh_base(host, key), f"rm -f {quoted}"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception as exc:  # cleanup is advisory — the frame is already local
        print(f"  (could not clean up on {host}: {type(exc).__name__})", flush=True)


def _parse_metadata(stdout: str) -> dict:
    """Pull the JSON object rpicam-still prints to stdout out of its log noise."""
    start = stdout.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(stdout)):
            if stdout[i] == "{":
                depth += 1
            elif stdout[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(stdout[start : i + 1])
                    except json.JSONDecodeError:
                        break
        start = stdout.find("{", start + 1)
    return {}


def summarise(meta: dict) -> str:
    keys = ("ExposureTime", "AnalogueGain", "Lux", "ColourTemperature", "FocusFoM", "ColourGains")
    return "  ".join(f"{k}={meta[k]}" for k in keys if k in meta)


# --------------------------------------------------------------------------
# Geometry: mapping the photograph back onto panel pixels.
#
# Registration is against the render that was displayed, not against a dedicated
# calibration frame. The rig is framed tight — the camera sees roughly the middle
# half of the panel and no edge of it — so there is no panel outline to find and
# corner fiducials fall outside the view. What there IS, always, is perfect prior
# knowledge of the displayed content, which makes feature matching the natural
# method rather than a fallback: match the photograph against the expected
# render and the homography comes out with a few hundred inliers.
#
# Only geometry is taken from that match. Colour is never fitted against the
# expected image, which would make any later colour comparison circular.
#
# Nothing here models lens distortion, so an M12 varifocal leaves a few pixels of
# error toward the frame edges. Sampling block-averages well past that; it is the
# reason not to trust the extreme border.
# --------------------------------------------------------------------------

MIN_INLIERS = 40


def content_homography(
    photo_bgr: np.ndarray, expected_rgb: np.ndarray, *, verbose: bool = True
) -> tuple[np.ndarray, dict]:
    """Homography taking PHOTO pixel coords -> PANEL visual pixel coords.

    Both sides are blurred before detection. The panel image is a dither, so at
    full resolution the strongest features are individual ink dots — which are
    aliased differently in the photograph and match each other by luck. Blurring
    to roughly the scale the eye integrates at leaves the structure that actually
    corresponds.
    """

    def prep(img: np.ndarray, gray_from: int) -> np.ndarray:
        g = cv2.cvtColor(img, gray_from)
        return cv2.GaussianBlur(g, (0, 0), 1.2)

    photo = prep(photo_bgr, cv2.COLOR_BGR2GRAY)
    panel = prep(expected_rgb, cv2.COLOR_RGB2GRAY)

    sift = cv2.SIFT_create(nfeatures=6000)  # type: ignore[attr-defined]  # present at runtime, absent from the cv2 stubs
    kp_photo, desc_photo = sift.detectAndCompute(photo, None)
    kp_panel, desc_panel = sift.detectAndCompute(panel, None)
    if desc_photo is None or desc_panel is None:
        raise RuntimeError("no features detected — is the panel actually showing the render?")

    matches = cv2.BFMatcher(cv2.NORM_L2).knnMatch(desc_photo, desc_panel, k=2)
    good = [m for m, n in matches if m.distance < 0.75 * n.distance]
    if len(good) < MIN_INLIERS:
        raise RuntimeError(f"only {len(good)} candidate matches — registration failed")

    src = np.array([kp_photo[m.queryIdx].pt for m in good], np.float32).reshape(-1, 1, 2)
    dst = np.array([kp_panel[m.trainIdx].pt for m in good], np.float32).reshape(-1, 1, 2)
    homography, mask = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
    inliers = int(mask.sum()) if mask is not None else 0
    if homography is None or inliers < MIN_INLIERS:
        raise RuntimeError(f"registration failed — {inliers} inliers")

    h, w = photo.shape
    corners = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32).reshape(-1, 1, 2)
    seen = cv2.perspectiveTransform(corners, homography).reshape(4, 2)
    info = {
        "matches": len(good),
        "inliers": inliers,
        "photo_corners_in_panel": seen.tolist(),
        "visible_bbox": [
            float(seen[:, 0].min()),
            float(seen[:, 1].min()),
            float(seen[:, 0].max()),
            float(seen[:, 1].max()),
        ],
    }
    if verbose:
        x0, y0, x1, y1 = info["visible_bbox"]
        print(f"  registered: {inliers}/{len(good)} inliers")
        print(f"  panel seen: x {x0:.0f}..{x1:.0f}, y {y0:.0f}..{y1:.0f}")
    return homography, info


def rectify(photo_bgr: np.ndarray, homography: np.ndarray, display) -> np.ndarray:
    """Warp a photograph into panel visual pixel space. Returns RGB uint8.

    Pixels the camera never saw come back black; ``visible_mask`` says which.
    """
    warped = cv2.warpPerspective(
        photo_bgr,
        homography,
        (display.visual_w, display.visual_h),
        flags=cv2.INTER_AREA,
        borderMode=cv2.BORDER_CONSTANT,
    )
    return cv2.cvtColor(warped, cv2.COLOR_BGR2RGB)


def visible_mask(photo_shape: tuple[int, int], homography: np.ndarray, display) -> np.ndarray:
    """Boolean (visual_h, visual_w): True where the photograph actually covers the panel."""
    h, w = photo_shape[:2]
    ones = np.full((h, w), 255, np.uint8)
    warped = cv2.warpPerspective(
        ones,
        homography,
        (display.visual_w, display.visual_h),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
    )
    # Erode so the soft warp boundary and lens falloff at the very edge are out.
    return cv2.erode(warped, np.ones((17, 17), np.uint8)) > 127


# --------------------------------------------------------------------------
# Colour: turning what the camera recorded into what a meter would have said.
#
# The photograph is not a measurement. Between the ink and the JPEG sit a warm
# 2500 K lamp, the sensor's own spectral sensitivities, a fixed white balance,
# a colour-correction matrix and a tone curve. All of them are deterministic
# once the capture settings are pinned, which is what makes them fittable: show
# the panel patches whose CIELAB a colorimeter already measured, photograph
# them, and solve for the mapping between the two.
#
# The fit therefore answers "what would the ColorMunki have said about this
# pixel", under D65, from a photograph taken under a domestic lamp. That is the
# only form in which a photograph can be compared against a source image.
#
# It is valid for exactly the lighting, geometry and capture settings it was
# fitted under. Move the lamp and it is silently wrong, which is why the fit
# file records the capture metadata it was built from.
# --------------------------------------------------------------------------


def poly_features(lin: np.ndarray) -> np.ndarray:
    """Linear camera RGB (...,3) -> (...,11) polynomial terms.

    Second order with cross terms plus rgb and a constant. Enough to absorb a
    colour matrix, per-channel gain and the mild nonlinearity the ISP leaves
    behind; small enough that a few dozen patches do not overfit it. There is no
    physical claim here — it is a regression, and it is only ever evaluated
    inside the gamut it was fitted on, which for a 41 %-of-sRGB panel is narrow.
    """
    r, g, b = lin[..., 0], lin[..., 1], lin[..., 2]
    ones = np.ones_like(r)
    return np.stack([r, g, b, r * g, r * b, g * b, r * r, g * g, b * b, r * g * b, ones], axis=-1)


def fit_camera_to_xyz(lin_rgb: np.ndarray, xyz_pct: np.ndarray) -> np.ndarray:
    """Least-squares (11,3) coefficients mapping camera RGB to measured XYZ percent."""
    feats = poly_features(lin_rgb)
    coeffs, *_ = np.linalg.lstsq(feats, np.asarray(xyz_pct, dtype=np.float64), rcond=None)
    return coeffs


def apply_camera_fit(linear: np.ndarray, coeffs: np.ndarray) -> np.ndarray:
    """Linear camera RGB (...,3) -> XYZ percent, via a fitted mapping.

    The input must be in the SAME units the fit was solved in — reflectance
    percent over 100, as produced by ``apply_photometric``. This function once
    gamma-decoded its input, left over from when captures were JPEGs, and kept
    doing so after the fit moved to linear raw. Nothing failed loudly: the
    calibration builds its features directly and so never touched this path,
    while every measurement quietly came back as a constant negative L*.

    ``check_fit_sanity`` exists because of that.
    """
    return poly_features(np.asarray(linear, dtype=np.float64)) @ np.asarray(coeffs, np.float64)


def check_fit_sanity(coeffs: np.ndarray, white_y: float, black_y: float) -> None:
    """Assert the fit maps the panel's own white and black back to themselves.

    Two points whose answer is known independently, checked before the fit is
    used on anything. A units mismatch between calibration and application is
    invisible in every other number a run prints.
    """
    for name, refl, want_y in (("white", white_y, white_y), ("black", black_y, black_y)):
        xyz = apply_camera_fit(np.array([[refl, refl, refl]]) / 100.0, coeffs)[0]
        if not (0.3 * want_y < xyz[1] < 3.0 * want_y):
            raise RuntimeError(
                f"colour fit is not in the expected units: a neutral {name} patch at "
                f"{refl:.2f} % reflectance maps to Y={xyz[1]:.2f}, expected near {want_y:.2f}"
            )


def _smooth_field(linear: np.ndarray, cells: int = 48) -> np.ndarray:
    """A robust, smooth version of a uniform frame: lighting shape, nothing else.

    Reduced to a coarse grid of block MEDIANS, cells far below the frame median
    dropped as occluders rather than lighting, holes filled from their neighbours,
    then smoothed back up. A plain blur instead of this produced a 5.3x spike
    under an object resting on the panel and stamped it into every later capture.
    """
    h, w = linear.shape[:2]
    gh, gw = cells, round(cells * w / h)
    by, bx = h // gh, w // gw
    coarse = np.median(linear[: gh * by, : gw * bx].reshape(gh, by, gw, bx, 3), axis=(1, 3))

    for c in range(3):
        plane = coarse[:, :, c].astype(np.float32)
        good = plane > 0.5 * np.median(plane)
        for _ in range(max(gh, gw)):
            if good.all():
                break
            filled = cv2.dilate(np.where(good, plane, 0), np.ones((3, 3)))
            counts = cv2.dilate(good.astype(np.float32), np.ones((3, 3)))
            plane = np.where(good, plane, filled / np.maximum(counts, 1))
            good = good | (counts > 0)
        coarse[:, :, c] = cv2.GaussianBlur(plane, (0, 0), 2.0)

    return cv2.resize(coarse.astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)


def photometric_fields(
    white_linear: np.ndarray,
    black_linear: np.ndarray,
    white_y: float,
    black_y: float,
    cells: int = 48,
) -> tuple[np.ndarray, np.ndarray]:
    """Separate the two things that vary across the frame: illumination and glare.

    A flat field alone is the wrong shape for this rig. The panel sits in a light
    tent, so its glossy lamination reflects the bright walls back at the camera as
    a veil — and a veil is ADDITIVE, while a flat field is multiplicative. No
    amount of dividing removes an added term.

    Two frames make them separable, because the panel can show two surfaces of
    KNOWN reflectance. Writing i(x,y) for illumination and g(x,y) for glare:

        white_frame = g + Yw * i
        black_frame = g + Yb * i

    with Yw and Yb the campaign's own measured luminance factors for white and
    black ink. Two equations, two unknowns, per pixel:

        i = (white_frame - black_frame) / (Yw - Yb)
        g =  black_frame - Yb * i

    Subtracting the whole black frame instead would be the obvious shortcut and
    is wrong: it would drive black ink to zero when the meter says it reflects
    1.24 %, biasing every dark patch.

    This assumes glare does not depend on what the panel is showing. It is not
    exactly true — a bright panel bounces more light back into the tent, which
    returns — but it is a far better approximation than ignoring the term.
    """
    white = _smooth_field(white_linear, cells)
    black = _smooth_field(black_linear, cells)
    illum = np.maximum(white - black, 1e-6) / (white_y - black_y)
    glare = black - black_y * illum
    return illum, np.maximum(glare, 0.0)


def apply_photometric(linear: np.ndarray, illum: np.ndarray, glare: np.ndarray) -> np.ndarray:
    """Camera linear RGB -> reflectance percent, flat across the frame.

    Output is in the campaign's own units: a patch reading 36.55 here is one the
    meter would have called white ink. That makes the colour fit a mapping
    between two things measured in the same currency, rather than between camera
    counts and physical units at once.
    """
    return (linear - glare) / illum


# --------------------------------------------------------------------------
# The linear path.
#
# Geometry comes from the JPEG and photometry from the DNG, which sounds like
# having it both ways and is actually the point. SIFT wants an 8-bit image with
# a tone curve on it, because that is what its gradients were designed around,
# and registration is a question about structure that the ISP cannot get wrong.
# Colour is the opposite: every ISP stage is a nonlinearity between the ink and
# the number, and the whole reason for reading raw is to have none of them.
#
# The two are the same field of view, the raw simply binned 2x1 per axis, so one
# homography serves both once it is scaled.
# --------------------------------------------------------------------------

RAW_SCALE = 2  # JPEG pixels per binned-raw pixel


@dataclass(frozen=True)
class LinearShot:
    raw: dng.Raw
    jpeg_bgr: np.ndarray
    metadata: dict
    dng_path: Path
    jpeg_path: Path


def _scp_from_pi(host: str, key: Path, remote: str, dest: Path, attempts: int = 3) -> None:
    """Copy one file off the Pi, and prove it arrived whole.

    Two traps, both of which fail *silently* with exit status 0:

    **The local destination must not contain shell/glob metacharacters.** On
    Windows, `scp` writes nothing when the local path contains brackets, and
    still exits 0. Measured: the same remote file copies to `plain.dng` (10 MB)
    and to `_t(2).dng` (no file), same command otherwise. Library filenames like
    `IMG_4179(2).HEIC` flow into capture names, so this is routine rather than
    exotic. The copy therefore lands on a sanitised temporary name and is
    renamed afterwards.

    **Do not "fix" it by shell-quoting the remote path.** OpenSSH 9+ speaks SFTP
    by default and passes the path through literally, so quotes become part of
    the name. Measured on OpenSSH 10.2 with a file called `sp ace(2).txt`: the
    raw path succeeds, a single-quoted path fails with "No such file or
    directory", and legacy `-O` plus quoting fails with a protocol error.

    Size is checked against what the Pi reports, because `rpicam-still` writes
    the DNG after the command that copies the JPEG returns, so a copy can also
    race a file that is still being written.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    safe = dest.parent / f".scp_{abs(hash(dest.name)) % (10**12)}{dest.suffix}"
    last = ""
    for attempt in range(attempts):
        safe.unlink(missing_ok=True)
        remote_size = _remote_size(host, key, remote)
        proc = subprocess.run(
            [  # noqa: S607 — scp comes from PATH
                "scp",
                "-i",
                str(key),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=no",
                f"{host}:{remote}",
                str(safe),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode != 0:
            last = f"scp failed ({proc.returncode}): {proc.stderr.strip()[:300]}"
        elif remote_size <= 0:
            last = f"could not stat {remote} on {host}"
        elif not safe.exists():
            last = "scp reported success but wrote no file"
        elif safe.stat().st_size != remote_size:
            last = f"truncated: {safe.stat().st_size} of {remote_size} bytes"
        else:
            safe.replace(dest)
            return
        if attempt < attempts - 1:
            time.sleep(2.0)
    safe.unlink(missing_ok=True)
    raise RuntimeError(f"{remote}: {last}")


def _remote_size(host: str, key: Path, path: str) -> int:
    """Size of a file on the Pi, or 0 if it cannot be read."""
    proc = subprocess.run(
        [*_ssh_base(host, key), f"stat -c %s {shlex.quote(path)}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        return int(proc.stdout.strip())
    except (ValueError, AttributeError):
        return 0


def capture_linear(
    local_path: Path,
    *,
    host: str = DEFAULT_HOST,
    key: Path = DEFAULT_KEY,
    **kwargs,
) -> LinearShot:
    """One capture, returned as linear sensor RGB plus the JPEG for registration."""
    shot = capture(local_path, host=host, key=key, raw=True, **kwargs)
    dng_remote = f"/tmp/{local_path.stem}.dng"  # noqa: S108 — a path on the Pi
    dng_local = local_path.with_suffix(".dng")

    # The DNG goes through the same guarded copy: sanitised local name, size
    # checked against the Pi, retried. See _scp_from_pi for why both matter.
    _scp_from_pi(host, key, dng_remote, dng_local)
    _remote_rm(host, key, [dng_remote])

    jpeg = cv2.imread(str(local_path))
    if jpeg is None:
        raise RuntimeError(f"cannot read {local_path}")
    return LinearShot(
        raw=dng.read_dng(dng_local),
        jpeg_bgr=jpeg,
        metadata=shot.metadata,
        dng_path=dng_local,
        jpeg_path=local_path,
    )


def load_linear(local_path: Path) -> LinearShot:
    """Rebuild a LinearShot from files already on disk.

    Re-solving a calibration from frames that are already captured costs no panel
    time, and 20 seconds of e-paper refresh per frame is the expensive part of
    every run here.
    """
    jpeg = cv2.imread(str(local_path))
    if jpeg is None:
        raise RuntimeError(f"cannot read {local_path}")
    return LinearShot(
        raw=dng.read_dng(local_path.with_suffix(".dng")),
        jpeg_bgr=jpeg,
        metadata={},
        dng_path=local_path.with_suffix(".dng"),
        jpeg_path=local_path,
    )


def scale_homography(homography: np.ndarray, scale: int = RAW_SCALE) -> np.ndarray:
    """Re-express a JPEG-pixel -> panel homography as binned-raw-pixel -> panel."""
    upscale = np.diag([float(scale), float(scale), 1.0])
    return homography @ upscale


def rectify_linear(rgb: np.ndarray, homography: np.ndarray, display) -> np.ndarray:
    """Warp linear float RGB into panel visual pixel space, staying linear."""
    return cv2.warpPerspective(
        rgb,
        homography,
        (display.visual_w, display.visual_h),
        flags=cv2.INTER_AREA,
        borderMode=cv2.BORDER_CONSTANT,
    )
