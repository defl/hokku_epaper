"""Shared test helpers."""
from __future__ import annotations

import re
from pathlib import Path

from hokku_server.image_renderer import MAX_IMAGE_PIXELS


def is_oversize_fixture(p: Path) -> bool:
    """True if the filename encodes a pixel count above MAX_IMAGE_PIXELS.

    Synthetic oversize fixtures (e.g. ``synth_black_10000x10000.png``) exist
    purely to exercise the decompression-bomb guard; they are rejected at
    header-read time and must be skipped by tests that actually try to decode
    pixels from every image in ``images/test/``.
    """
    m = re.search(r"(\d+)x(\d+)", p.name.lower())
    if not m:
        return False
    try:
        return int(m.group(1)) * int(m.group(2)) > MAX_IMAGE_PIXELS
    except ValueError:
        return False
