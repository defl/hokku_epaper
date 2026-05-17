"""Orientation enum shared across the image pipeline."""
from __future__ import annotations

import enum


class Orientation(str, enum.Enum):
    """Display orientation of the e-ink panel.

    ``str`` base class keeps ``Orientation.LANDSCAPE == "landscape"`` true and
    JSON serialisation producing plain strings, so existing comparisons and
    config files are unaffected.
    """

    LANDSCAPE = "landscape"
    PORTRAIT = "portrait"
