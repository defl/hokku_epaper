"""The firmware library: bundled + downloaded artifacts, channels, and pinning.

The server ships bundled firmware in the ``.deb`` (offline-first — see
``firmware_paths.BUNDLED_FIRMWARE_DIRS``). This module layers an *optional*,
user-managed set of firmware downloaded from GitHub Releases on top of that,
plus a per-model *pin* so a specific version — including a beta — can be chosen
deliberately.

It is a thin **override** over the existing bundled resolution in
:mod:`hokku.screens.firmware_registry`. When nothing is downloaded and nothing is
pinned it defers entirely to that registry, so a box that never touches the new
UI behaves exactly as before (serves the bundled firmware that shipped in the
package). Only a download or a pin changes what is served — and:

  * the **effective** version (what a screen is offered over OTA / USB) is the
    pinned version if set and present, otherwise the bundled default, which a
    newer **stable** download supersedes; and
  * a **beta** is never the effective version unless it is explicitly pinned.

The stable/beta channel is a property of firmware **downloaded from GitHub**
(taken from its release's pre-release flag). Bundled firmware carries the neutral
``bundled`` channel — its upstream maturity isn't knowable at runtime, so it is
never labelled "stable" (that would be a claim we can't back up).

Downloaded artifacts live in ``config.firmware_dir`` (``/var/lib/hokku/firmware``
on a package install) next to a per-file ``<name>.meta.json`` sidecar recording
its release channel and tag, and a single ``selection.json`` holding the pins.
Nothing here reaches the network — that is :mod:`hokku.webserver.firmware_github`,
called only on an explicit user action.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from hokku.common.firmware_paths import version_key
from hokku.screens import firmware_registry
from hokku.webserver.filesystem import atomic_write_json

logger = logging.getLogger(__name__)

CHANNEL_STABLE = "stable"
CHANNEL_BETA = "beta"
#: Bundled firmware ships inside the installed server package. Its upstream
#: maturity (stable vs beta) is NOT knowable at runtime — a GitHub release's
#: pre-release flag describes the *appliance* release, not the firmware — so we do
#: not claim a stable/beta channel for it. It is simply the shipped default. The
#: stable/beta distinction applies only to firmware downloaded from GitHub.
CHANNEL_BUNDLED = "bundled"

_SELECTION_FILE = "selection.json"


@dataclass(frozen=True)
class FirmwareVariant:
    """One firmware artifact available for a model."""

    model_id: str
    version: str
    channel: str  # CHANNEL_STABLE | CHANNEL_BETA | CHANNEL_BUNDLED
    source: str  # "downloaded" | "bundled"
    path: Path | None = None  # concrete file (always set for downloaded)
    tag: str | None = None  # GitHub release tag, for downloaded variants


class FirmwareStore:
    """Read/write view over one server's firmware library.

    Cheap to construct (a couple of globs + one small JSON read), so route
    handlers build a fresh instance per request and it always reflects the
    on-disk state, mirroring how the rest of the webserver reads live config.
    """

    def __init__(self, download_dir: Path) -> None:
        self.download_dir = download_dir
        self.selection_path = download_dir / _SELECTION_FILE

    # ── discovery ────────────────────────────────────────────────

    def downloaded_variants(self, model_id: str) -> list[FirmwareVariant]:
        """Every artifact for *model_id* in the writable download dir."""
        out: list[FirmwareVariant] = []
        for version, path in firmware_registry.list_model_download_files(
            model_id, self.download_dir
        ):
            channel, tag = self._read_sidecar(path)
            out.append(
                FirmwareVariant(model_id, version, channel, "downloaded", path=path, tag=tag)
            )
        return out

    def _bundled_variants(self, model_id: str) -> list[FirmwareVariant]:
        """Every bundled artifact for *model_id* (dir-scan, plus the registry's
        reported highest so a single installed file with no dir-scan hit — or a
        test that stubs the version — is still represented). Channel is
        ``bundled`` — we don't claim its upstream stable/beta maturity."""
        out: list[FirmwareVariant] = []
        seen: set[str] = set()
        for version, path in firmware_registry.list_bundled_firmware(model_id):
            out.append(FirmwareVariant(model_id, version, CHANNEL_BUNDLED, "bundled", path=path))
            seen.add(version)
        reported = firmware_registry.firmware_version_for(model_id)
        if reported and reported not in seen:
            out.append(FirmwareVariant(model_id, reported, CHANNEL_BUNDLED, "bundled"))
        return out

    def variants(self, model_id: str) -> list[FirmwareVariant]:
        """Every available variant for *model_id*, de-duplicated by version and
        ordered highest version first. Downloaded artifacts win over a bundled
        one of the same version (identical bytes, but downloads carry metadata)."""
        seen: dict[str, FirmwareVariant] = {}
        for v in self.downloaded_variants(model_id) + self._bundled_variants(model_id):
            seen.setdefault(v.version, v)
        return sorted(seen.values(), key=lambda v: version_key(v.version), reverse=True)

    def all_variants(self) -> dict[str, list[FirmwareVariant]]:
        return {m: self.variants(m) for m in firmware_registry.known_models()}

    # ── selection ────────────────────────────────────────────────

    def _pins(self) -> dict[str, str]:
        if not self.selection_path.exists():
            return {}
        try:
            with open(self.selection_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Ignoring unreadable firmware selection %s: %s", self.selection_path, e)
            return {}
        pins = data.get("pins") if isinstance(data, dict) else None
        return {str(k): str(v) for k, v in pins.items()} if isinstance(pins, dict) else {}

    def pinned(self, model_id: str) -> str | None:
        """The version pinned for *model_id*, or None if unpinned."""
        return self._pins().get(model_id)

    def set_pin(self, model_id: str, version: str | None) -> None:
        """Pin *model_id* to *version*, or clear the pin when *version* is None."""
        pins = self._pins()
        if version is None:
            pins.pop(model_id, None)
        else:
            pins[model_id] = version
        self.download_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.selection_path, {"pins": pins})

    # ── effective version (what gets served) ─────────────────────

    def _override(self, model_id: str) -> FirmwareVariant | None:
        """The variant to serve *instead of* the bundled default, or None to keep
        deferring to the registry's bundled resolution.

        Returns non-None only when a pin or a downloaded artifact genuinely
        changes the outcome — this is what keeps the no-download/no-pin path a
        byte-for-byte pass-through to the registry."""
        downloaded = self.downloaded_variants(model_id)
        bundled_ver = firmware_registry.firmware_version_for(model_id)
        pin = self.pinned(model_id)

        if pin:
            if pin == bundled_ver:
                return None  # pinned to the bundled default → nothing to override
            for v in downloaded:
                if v.version == pin:
                    return v
            for version, path in firmware_registry.list_bundled_firmware(model_id):
                if version == pin:  # pinned to an older bundled build
                    return FirmwareVariant(model_id, version, CHANNEL_BUNDLED, "bundled", path=path)
            logger.warning(
                "Pinned firmware %s for %s not present; using bundled default", pin, model_id
            )
            return None

        # No pin: a downloaded STABLE build overrides only if strictly newer than
        # the bundled default. Betas never win here (pin-only).
        best: FirmwareVariant | None = None
        for v in downloaded:
            if v.channel != CHANNEL_STABLE:
                continue
            if best is None or version_key(v.version) > version_key(best.version):
                best = v
        if best and (bundled_ver is None or version_key(best.version) > version_key(bundled_ver)):
            return best
        return None

    def effective(self, model_id: str | None) -> FirmwareVariant | None:
        """The variant that will be served for *model_id* (for display), or None
        when the model has no firmware at all."""
        if not model_id:
            return None
        ov = self._override(model_id)
        if ov is not None:
            return ov
        bundled_ver = firmware_registry.firmware_version_for(model_id)
        if not bundled_ver:
            return None
        # Prefer a concrete bundled path when we have one (lets the UI show source).
        for v in self._bundled_variants(model_id):
            if v.version == bundled_ver:
                return v
        return FirmwareVariant(model_id, bundled_ver, CHANNEL_BUNDLED, "bundled")

    def effective_version(self, model_id: str | None) -> str | None:
        ov = self._override(model_id) if model_id else None
        if ov is not None:
            return ov.version
        return firmware_registry.firmware_version_for(model_id)

    def effective_versions(self) -> dict[str, str | None]:
        return {m: self.effective_version(m) for m in firmware_registry.known_models()}

    def effective_app_image(self, model_id: str | None) -> bytes | None:
        """The OTA image bytes for the effective version of *model_id*, or None.

        A download/pin override is read straight from its file; otherwise this
        defers to the registry's bundled ``release_app_image_for`` unchanged."""
        ov = self._override(model_id) if model_id else None
        if ov is not None and ov.path is not None:
            return firmware_registry.app_image_from_file(model_id, ov.path)
        return firmware_registry.release_app_image_for(model_id)

    # ── downloads ────────────────────────────────────────────────

    def add_download(
        self, model_id: str, version: str, data: bytes, *, channel: str, tag: str | None
    ) -> FirmwareVariant:
        """Write a downloaded artifact + its sidecar into the library.

        Validates the bytes as a real image for *model_id* before committing —
        a corrupt or mismatched download is written to a temp file, rejected,
        and removed, never entering the library. Raises ValueError on an unknown
        model or a file that fails validation."""
        name = firmware_registry.artifact_name(model_id, version)
        if name is None:
            raise ValueError(f"unknown model_id {model_id!r}")
        self.download_dir.mkdir(parents=True, exist_ok=True)
        path = self.download_dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        # Validate the staged bytes (extension-agnostic — tmp ends in .tmp; the
        # final name already carries the right extension by construction).
        if not firmware_registry.validate_firmware_content(model_id, tmp):
            tmp.unlink(missing_ok=True)
            raise ValueError(f"downloaded {name} is not a valid {model_id} firmware image")
        tmp.replace(path)
        atomic_write_json(
            self._sidecar_path(path),
            {"channel": channel, "tag": tag, "version": version, "model_id": model_id},
        )
        return FirmwareVariant(model_id, version, channel, "downloaded", path=path, tag=tag)

    # ── sidecar helpers ──────────────────────────────────────────

    @staticmethod
    def _sidecar_path(path: Path) -> Path:
        return path.with_name(path.name + ".meta.json")

    def _read_sidecar(self, path: Path) -> tuple[str, str | None]:
        """Return ``(channel, tag)`` for a downloaded file. A missing or malformed
        sidecar is treated as a stable, tag-less artifact (conservative: it will
        not be hidden as a beta)."""
        sc = self._sidecar_path(path)
        if not sc.exists():
            return CHANNEL_STABLE, None
        try:
            with open(sc) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return CHANNEL_STABLE, None
        channel = data.get("channel")
        if channel not in (CHANNEL_STABLE, CHANNEL_BETA):
            channel = CHANNEL_STABLE
        tag = data.get("tag")
        return channel, (str(tag) if tag else None)
