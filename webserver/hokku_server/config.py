"""Backwards-compat shim — import from hokku_server.app_config instead."""

# ruff: noqa: F401
from hokku_server.app_config import (
    _CURRENT_VERSION,
    _MIGRATIONS,
    AppConfig,
    Orientation,
    _migrate,
)
from hokku_server.image_config import ImageConfig, _image_config_from_dict
