"""Backwards-compat shim — import from webserver.app_config instead."""
# ruff: noqa: F401
from webserver.app_config import (
    AppConfig,
    Orientation,
    _CURRENT_VERSION,
    _MIGRATIONS,
    _migrate,
)
from webserver.image_config import ImageConfig, _image_config_from_dict
