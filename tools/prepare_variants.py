#!/usr/bin/env python3
"""Bench-only variants of the prepare stage that ImageConfig cannot express.

**Autocontrast.** `_apply_prepare_enhancements` starts with
``ImageOps.autocontrast(canvas, cutoff=...)``, which stretches R, G and B
*independently* — an automatic white balance. Measured on the 30 test
photographs rendered as production does, that one call shifts mean b* by up to
+13 (yellow) and a* by up to -17, depending on each photograph's channel ranges:
+10.5 b* on the baby photograph whose note reads "skin nearly all yellow", +8 on
"all 3 have yellow skin". White letterbox bars pin every channel's top end at
255, so whether a photograph has bars also changes its cast — which is how the
same letterbox test turned one photograph yellow with its bars and another
yellow without them.

Pillow can do the same stretch without the cast: ``preserve_tone=True`` computes
it on luminance and applies it to all channels alike. These modes let a capture
plan test that, and "off", without touching shipped code.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator
from pathlib import Path

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from PIL import ImageOps

import hokku.webserver.image_abc as image_abc

_REAL = ImageOps.autocontrast
AUTOCONTRAST_MODES = {
    "per_channel": _REAL,  # production
    "preserve_tone": lambda im, **kw: _REAL(im, preserve_tone=True, **kw),
    "off": lambda im, **_kw: im,
}


@contextlib.contextmanager
def autocontrast_as(mode: str | None) -> Iterator[None]:
    """Run the enclosed rendering with ``mode`` autocontrast; None is production.

    ``image_abc`` calls ``ImageOps.autocontrast`` through the module attribute,
    so replacing that attribute is enough. It is PIL's own module object, so the
    swap is process-wide for its duration: single-threaded use only.
    """
    if mode is None or mode == "per_channel":
        yield
        return
    previous = image_abc.ImageOps.autocontrast
    image_abc.ImageOps.autocontrast = AUTOCONTRAST_MODES[mode]
    try:
        yield
    finally:
        image_abc.ImageOps.autocontrast = previous
