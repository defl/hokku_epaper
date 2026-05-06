"""Hokku image server package (Spectra 6 e-ink pipeline + Flask API)."""
from webserver import dither
from webserver import image
from webserver.image import PRESET_DISPLAY_IMAGE_CONFIGS

PRESET_DITHER_ALGORITHMS = PRESET_DISPLAY_IMAGE_CONFIGS
from webserver.image import IMAGE_EXTENSIONS
from webserver.config import AppConfig, DEFAULT_CONFIG
from webserver.display_constants import (
    FULL_W,
    PANEL_BYTES,
    PANEL_H,
    PANEL_W,
    PALETTE_MEASURED_RGB,
    PALETTE_NIBBLE,
    PALETTE_PREVIEW_RGB,
    TOTAL_BYTES,
    VISUAL_H,
    VISUAL_W,
)
from webserver.flask_app import app
from webserver.screen_headers import (
    BATTERY_MV_EMPTY,
    BATTERY_MV_FULL,
    _battery_percent,
    _parse_battery_header,
    _parse_frame_state,
)
from webserver.serve_data import _load_database, _save_database
from webserver.time import (
    DEBUG_FAST_REFRESH_SECONDS,
    calculate_sleep_seconds as _calculate_sleep_seconds,
    format_duration_human,
)


def main():
    from webserver.flask_app import main as run
    return run()
