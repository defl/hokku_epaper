#!/usr/bin/env python3
"""Put one image on the glass with a chosen subset of the measured corrections.

The colour campaign produced three corrections and the pipeline applies all
three together, so "the measurements make it look worse" is a statement about
their sum and cannot be acted on as it stands. This renders any subset --- see
``panel_arms`` for what ``drc``, ``lut`` and ``palette`` each are --- through the
real production renderer, so an arm cannot drift from what the server would do.

Everything outside those three is held fixed across arms, including the whole
hand-tuned tonal chain, so a difference on the glass is attributable.

    # look at it without spending panel time
    python tools/correction_ab.py save --model huessen_epf1301 \
        --image images/test/Robert_De_Niro_KVIFF_portrait.jpg \
        --arm none --out build/camcal/deniro_none.png

    # put it on the glass and photograph it
    python tools/correction_ab.py shoot --model huessen_epf1301 --port COM4 \
        --bench-flip180 --image images/test/Robert_De_Niro_KVIFF_portrait.jpg \
        --arm all --outdir build/camcal
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

import cam_rig
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS
from panel_arms import arm_config, arm_display, arm_label, parse_arm, render_arm, to_visible
from send_frame import open_device, send_frame

# The three portraits. Skin is where the complaint lives, and a correction that
# helps flat swatches has already failed on faces once in this project.
HUMANS = (
    "images/test/Robert_De_Niro_KVIFF_portrait.jpg",
    "images/test/Actress_Anna_Unterberger-2.jpg",
    "images/test/Wayuu_woman_with_sad_face_in_the_market_buying.jpg",
)


def render(
    model: str, image: Path, arm: str, preset: str, cfg: object = None
) -> tuple[np.ndarray, object]:
    """(panel-memory index raster, the arm's Display).

    ``cfg`` overrides the named preset — used to render with a live server's own
    config, which is the only way to measure the pipeline that is actually
    deployed rather than the repo's current idea of it.
    """
    base = DISPLAY_REGISTRY[model]
    toggles = parse_arm(arm)
    display = arm_display(base, toggles)
    cfg = arm_config(cfg if cfg is not None else PRESET_IMAGE_CONFIGS[preset], toggles)
    img = Image.open(image).convert("RGB")
    idx = render_arm(display, img, cfg, base.panel_w, base.panel_h)
    return idx, display


def preview_rgb(idx: np.ndarray, display, base) -> Image.Image:
    """Index raster -> what it should look like, painted in the panel's real inks.

    Always the BASE panel's ink table, never the arm's. The arm's palette is a
    statement about which ink to pick; the ink's actual colour on glass does not
    change because the renderer was told something different about it, and
    painting a preview in the arm's own table would hide exactly the error the
    palette arm is being tested for.
    """
    vis = to_visible(idx, display)
    rgb = np.rint(base.palette_measured_rgb).clip(0, 255).astype(np.uint8)[vis]
    return Image.fromarray(rgb)


def push(
    idx: np.ndarray,
    display,
    base,
    port: str,
    flip180: bool,
    timeout: float,
    interactive: bool = False,
) -> bool:
    """Upload one frame. Set *interactive* for anything but a one-off poke.

    USB-interactive mode pins the device to the console instead of letting it
    return to its own schedule. Without it a long run eventually collides with a
    scheduled refresh — which costs a panel update plus a reboot, so the port
    disappears and the upload fails. That is exactly what happened partway
    through a 64-frame capture: nine consecutive uploads failed after the device
    wandered back onto its own schedule. The mode is RAM-only and any reset
    clears it, so it is asserted per upload rather than once at the start.
    """
    panel = np.rot90(idx, k=2) if flip180 else idx
    data = base.indices_to_panel_bytes(panel)
    print(f"opening {port} ({base.model_id})...", flush=True)
    s = open_device(port, base.model_id, timeout_s=timeout, interactive=interactive)
    if s is None:
        return False
    try:
        return send_frame(s, data, f"arm={display.model_id}")
    finally:
        s.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("save", "push", "shoot"):
        p = sub.add_parser(name)
        p.add_argument("--model", default="huessen_epf1301", choices=sorted(DISPLAY_REGISTRY))
        p.add_argument("--image", type=Path, required=True)
        p.add_argument("--arm", required=True, help="none | all | drc+lut+palette subset")
        p.add_argument("--preset", default="default_general", choices=sorted(PRESET_IMAGE_CONFIGS))
        if name == "save":
            p.add_argument("--out", type=Path, required=True)
        else:
            p.add_argument("--port", required=True)
            p.add_argument("--console-timeout", type=float, default=180.0)
            p.add_argument("--bench-flip180", action="store_true")
        if name == "shoot":
            p.add_argument("--outdir", type=Path, default=Path("build/camcal"))
            p.add_argument("--tag", default=None, help="filename stem (default: image__arm)")
            p.add_argument("--settle", type=int, default=2000)

    args = ap.parse_args(argv)
    base = DISPLAY_REGISTRY[args.model]
    label = arm_label(parse_arm(args.arm))
    print(f"  image {args.image.name}")
    print(f"  arm   {label}   (preset {args.preset})")

    idx, display = render(args.model, args.image, args.arm, args.preset)

    if args.cmd == "save":
        args.out.parent.mkdir(parents=True, exist_ok=True)
        preview_rgb(idx, display, base).save(args.out)
        print(f"  preview -> {args.out}")
        return 0

    if not push(idx, display, base, args.port, args.bench_flip180, args.console_timeout):
        print("upload failed")
        return 1

    if args.cmd == "push":
        return 0

    stem = args.tag or f"{args.image.stem[:24]}__{label}"
    args.outdir.mkdir(parents=True, exist_ok=True)
    preview_rgb(idx, display, base).save(args.outdir / f"{stem}__expected.png")
    shot = cam_rig.capture(args.outdir / f"{stem}__shot.jpg", settle_ms=args.settle)
    print(f"  {cam_rig.summarise(shot.metadata)}")
    print(f"  photo    -> {shot.local_path}")
    print(f"  expected -> {args.outdir / (stem + '__expected.png')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
