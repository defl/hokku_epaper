#!/usr/bin/env python3
"""Pull real images and the live render config off a running Hokku server.

The stock test images are chosen to stress specific failure modes, and they do
that well, but they are not what the panel spends its life showing. A finding can
be worth a fraction of a dE on a portrait and dominate a whole wall in a holiday
snap — this project has one of those already: error diffusion leaks hue-opposite
ink in proportion to how large and how flat an out-of-gamut area is, so a test
image with a small red garment understates it by a factor of three against a
photograph of a red wall.

So measurements should be run against the actual library. This fetches from the
server read-only: the image pool, an original, and the config the server is
really using — never the repo's idea of the defaults, which drift from a
deployed appliance and would silently measure the wrong pipeline.

Nothing here writes to the server.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from hokku.webserver.dither_config import DitherConfig
from hokku.webserver.image_config import ImageConfig

DEFAULT_BASE = "http://hokku.local:8080"
TIMEOUT_S = 60


@dataclass(frozen=True)
class ServerScreen:
    name: str
    model: str
    orientation: str
    firmware: str


def _get_json(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=TIMEOUT_S) as response:
        return json.load(response)


def status(base: str = DEFAULT_BASE) -> dict:
    return _get_json(base, "/hokku/api/status")


def screens(base: str = DEFAULT_BASE) -> list[ServerScreen]:
    """Every screen the server knows, with the model its renders are built for."""
    return [
        ServerScreen(
            name=name,
            model=s.get("screen_model") or "",
            orientation=s.get("orientation") or "",
            firmware=s.get("firmware_version") or "",
        )
        for name, s in status(base).get("screens", {}).items()
    ]


def pool(base: str = DEFAULT_BASE) -> list[str]:
    return list(status(base).get("pool_files", []))


def image_config(name: str = "image_config_default", base: str = DEFAULT_BASE) -> ImageConfig:
    """The live render config, as an ImageConfig the renderer accepts.

    Read from the server rather than from ``presets.py``. A deployed appliance's
    config diverges from the repo's defaults — this one still runs CLAHE at 1.75,
    which the branch turned off — and rendering with the wrong one measures a
    pipeline nobody is looking at.
    """
    raw = dict(_get_json(base, "/hokku/api/config")["config"][name])
    return ImageConfig(dither=DitherConfig(**raw.pop("dither")), **raw)


def fetch_original(name: str, dest_dir: Path, base: str = DEFAULT_BASE) -> Path:
    """Download one pool image, cached by name. Returns the local path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    local = dest_dir / name
    if local.exists() and local.stat().st_size > 0:
        return local
    url = f"{base}/hokku/api/original/" + urllib.parse.quote(name)
    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:
        local.write_bytes(response.read())
    return local


def fetch_dithered_indices(name: str, display, base: str = DEFAULT_BASE):
    """The server's OWN ink raster for an image, recovered from its preview PNG.

    The preview endpoint paints each ink in ``palette_preview_rgb``, which is a
    punchy stand-in and not what the glass shows — a wall of red ink with blue
    diffused through it previews as vivid red and reads as grey mud in person.
    Those six colours are distinct, so the mapping inverts exactly, which turns
    the preview into the one thing worth having: what the server actually decided
    to print, without re-rendering it here and hoping the two agree.
    """
    url = f"{base}/hokku/api/dithered/" + urllib.parse.quote(name)
    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:
        raw = response.read()
    png = np.array(Image.open(io.BytesIO(raw)).convert("RGB"), dtype=np.int16)
    palette = np.asarray(display.palette_preview_rgb, dtype=np.int16)
    dist = np.abs(png.reshape(-1, 3)[:, None, :] - palette[None, :, :]).sum(axis=2)
    exact = float(np.mean(dist.min(axis=1) == 0))
    if exact < 0.99:
        raise RuntimeError(f"only {100 * exact:.1f}% of preview pixels are palette colours")
    return np.argmin(dist, axis=1).astype(np.uint8).reshape(png.shape[:2])


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE
    st = status(base)
    print(f"  {base}")
    for s in screens(base):
        print(f"  screen {s.name!r}: {s.model}  {s.orientation}  fw {s.firmware}")
    print(f"  pool: {len(st.get('pool_files', []))} images, last served {st.get('last_served')!r}")
    cfg = image_config(base=base)
    print(
        f"  live default: dither={cfg.dither.lut_name}/{cfg.dither.algorithm} "
        f"neutral_chroma={cfg.dither.neutral_chroma} clahe={cfg.clahe_clip_limit} "
        f"saturate={cfg.adaptive_saturate_space}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
