"""Shared ESP32-S3 flash / OTA / NVS layer for hokku screens.

The two ESP32-S3 screens (``huessen_epf1301`` and ``seeedstudio_e1004``) share
the identical esptool flashing, NVS partition generation/parsing, ESP-IDF image
parsing, and device-detection logic — only a handful of per-screen values differ
(flash size, partition offsets, config-schema version, and the release-artifact
name derived from the model id). Those values live in an :class:`Esp32Spec`; the
functions in this package take a spec as their first argument. Each screen
package builds its spec and binds these functions into its public surface.

The XR872 ``bigme_f7`` screen does NOT use this layer (different SoC / BROM path).
"""

from __future__ import annotations

from hokku.screens.esp32.spec import Esp32Spec

__all__ = ["Esp32Spec"]
