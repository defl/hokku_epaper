"""Flask application factory and route handlers.

Routes only — no module-level mutable globals. Live state lives in the
AppState instance passed to ``create_app()``. All route handlers read
``state.manager`` / ``state.scheduler`` / ``state.config`` at the start of
each request so they automatically pick up a hot-reloaded config.
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import threading
import time as _time
from dataclasses import asdict, replace
from datetime import datetime
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

import pillow_jxl  # noqa: F401 — PIL plugin registration
import psutil
from flask import (
    Flask,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
)
from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from hokku.screens import firmware_registry, huessen_epf1301
from hokku.screens.bigme_f7 import bootstrap as bigme_bootstrap
from hokku.screens.bigme_f7 import firmware as bigme_firmware
from hokku.screens.flasher_registry import esp32_screen, esp32_screens
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver import firmware_github
from hokku.webserver.app_config import AppConfig
from hokku.webserver.app_state import AppState
from hokku.webserver.dither_streaming_numba import NumbaStreamingDither
from hokku.webserver.firmware_library import FirmwareStore
from hokku.webserver.image_abc import transform_bboxes_to_canvas_norm
from hokku.webserver.image_classifier import Observations
from hokku.webserver.image_config import (
    ImageConfigError,
    image_config_from_dict_strict,
    parse_crop_to_fill_threshold,
)
from hokku.webserver.image_record import ConvertStatus, ImageRecord
from hokku.webserver.image_renderer import (
    IMAGE_EXTENSIONS,
    MAX_UPLOAD_PIXELS,
    SVG_PROBE_DIMS,
    ImageRenderer,
    format_megapixels,
    open_image_for_render,
)
from hokku.webserver.mdns import _get_local_ip
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS, PRESET_META
from hokku.webserver.resource_budget import (
    MIN_MEMORY_BUDGET_MB,
    compute_budget,
    memory_budget_too_low,
)
from hokku.webserver.resource_limits import detect_memory_limit_bytes
from hokku.webserver.screen_headers import (
    parse_battery_header,
    parse_cal_ppm,
    parse_config_state,
    parse_firmware_build,
    parse_firmware_version,
    parse_frame_state,
    parse_mac_header,
    parse_screen_model,
)
from hokku.webserver.time_utils import calculate_sleep_seconds, format_duration_human

logger = logging.getLogger(__name__)

register_heif_opener()


def _resolve_template_folder(override: str | None) -> str:
    if override:
        return override
    pkg_dir = Path(__file__).resolve().parent  # python/hokku/webserver/
    candidates = [
        pkg_dir / "templates",
        Path("/usr/share/hokku-server/templates"),
    ]
    for c in candidates:
        if c.is_dir():
            return str(c)
    return str(candidates[0])  # default; flask will error if missing


def _resolve_static_folder() -> Path:
    """Locate the directory that holds /hokku/static/* assets.

    Dev: python/hokku/webserver/static/. Installed: /usr/share/hokku-server/static/.
    """
    pkg_dir = Path(__file__).resolve().parent  # python/hokku/webserver/
    candidates = [
        pkg_dir / "static",
        Path("/usr/share/hokku-server/static"),
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


def _read_git_describe() -> tuple[str, str | None]:
    """Returns (version string, full commit hash or None).

    Prefers git describe (dev tree); falls back to installed package metadata.
    """
    try:
        # python/hokku/webserver/flask_app.py -> repo root is parents[3]
        repo_root = Path(__file__).resolve().parents[3]
        describe = (
            subprocess.check_output(
                ["git", "describe", "--tags", "--always"],  # noqa: S607 — git on PATH is expected
                cwd=str(repo_root),
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            .decode("ascii")
            .strip()
        )
        commit = (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],  # noqa: S607
                cwd=str(repo_root),
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            .decode("ascii")
            .strip()
        )
        if describe:
            return describe, commit or None
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        return _pkg_version("hokku-server"), None
    except Exception:
        return "unknown", None


_REPO_URL = "https://github.com/defl/hokku_epaper"

# How many times to re-signal a pending firmware UPGRADE before giving up and
# recording an error (guards against a genuinely-bad image looping forever). Each
# attempt is one device poll; a transient failure (e.g. a dropped download) just
# retries on the next poll and self-heals well within this budget.
OTA_MAX_ATTEMPTS = 5

# Bound concurrent NVS-partition-generator subprocesses spawned by the
# unauthenticated /firmware-config endpoint. Without this, a burst of OTA polls
# (or a hostile LAN client looping the endpoint) could fork unbounded Python
# subprocesses and exhaust the appliance's CPU/PID budget, starving the real
# image-serving path. Excess requests get a 503 and retry on the next device poll.
OTA_NVS_MAX_CONCURRENT_BUILDS = 4
_nvs_build_slots = threading.BoundedSemaphore(OTA_NVS_MAX_CONCURRENT_BUILDS)

# Bound concurrent one-off dither previews. Each decodes the full source image —
# up to the decode budget, tens of MB — and that decode, not the dither,
# dominates the cost. Unbounded, a client looping the endpoint or a compare grid
# fired in parallel would hold one decoded image per request thread and starve
# the screen-serving path. Excess requests get a 503 and can retry.
PREVIEW_MAX_CONCURRENT = 2
_preview_slots = threading.BoundedSemaphore(PREVIEW_MAX_CONCURRENT)

# Preview canvas bounds, in pixels on the long edge. The default matches
# ImageRenderer's own; the floor keeps a caller from asking for something too
# small to judge. Note that lowering it speeds up the resize and the dither but
# not the decode, so the saving is real but modest.
_PREVIEW_MAX_SIDE_PX = 800
_PREVIEW_MIN_SIDE_PX = 200

# Upper byte-bounds on the USB-flash config string fields (in addition to the
# 64-byte screen_name cap enforced inline). SSID: 802.11 max; PSK: WPA2 max; URL:
# the firmware's server-URL buffer. Rejected with 400 before the NVS generator.
_FLASH_FIELD_MAX_BYTES = {
    "wifi_ssid1": 32,
    "wifi_ssid2": 32,
    "wifi_pass1": 64,
    "wifi_pass2": 64,
    "image_url": 256,
}


def _busy_retry_seconds(config: AppConfig) -> int:
    return min(300, calculate_sleep_seconds(config))


def _request_sync(state: AppState) -> None:
    """Ask for a sync as soon as possible, without blocking this request.

    Deliberately not ``manager.sync()``: on the default single-threaded manager
    that renders inline, so the caller would hold one of the server's four
    request threads for the length of a full conversion — seconds on a Pi. Waking
    the watcher hands the work to the thread that owns the sync cadence and
    returns immediately, which is why these endpoints answer "queued" rather
    than "done".

    Falls back to an inline sync when there is no watcher (tests, embedded use),
    where being synchronous is what the caller wants anyway.
    """
    if state.watcher is not None:
        state.watcher.wake()
    else:
        state.manager.sync()


def _pipeline_label(rec: ImageRecord, obs: Observations | None) -> str:
    """Which pipeline a picture renders through, for the UI to show as a chip.

    Mirrors the dispatch order in ImageClassifier plus the override that
    outranks it, so the UI can say *why* a picture looks the way it does
    without re-deriving the policy.
    """
    if rec.image_config is not None:
        return "override"
    if obs is not None and obs.is_bw:
        return "bw"
    if obs is not None and obs.face_bboxes:
        return "face"
    return "default"


def create_app(
    state: AppState,
    *,
    config_path: Path | None = None,
    template_folder: str | None = None,
) -> Flask:
    """Build the Flask app over an AppState.

    config_path is optional but required for save-config to work.
    """
    app = Flask(__name__, template_folder=_resolve_template_folder(template_folder))

    static_root = _resolve_static_folder()
    git_describe, git_hash = _read_git_describe()
    # Legacy scalar bundled-firmware version for the status view (the huessen
    # reference model). Per-model versions come from bundled_firmware_versions();
    # the flash endpoints resolve firmware per requested model at flash time.
    _fw_hdr = huessen_epf1301.release_app_header()
    bundled_firmware_version: str | None = (
        _fw_hdr[48:80].split(b"\x00")[0].decode("ascii", errors="replace").strip()
        if _fw_hdr
        else None
    ) or None

    def _firmware_store() -> FirmwareStore:
        """A live FirmwareStore over the current config's download dir.

        Built per request (cheap: a couple of globs + a small JSON read) so it
        always reflects on-disk downloads/pins, mirroring how handlers read
        ``state.config`` fresh each request."""
        return FirmwareStore(Path(state.config.firmware_dir))

    # ── Firmware-facing ────────────────────────────────────────

    @app.route("/hokku/screen/", strict_slashes=False, methods=["GET", "POST"])
    def serve_binary():
        manager = state.manager
        scheduler = state.scheduler
        config = state.config

        screen_name = request.headers.get("X-Screen-Name", "unnamed")
        screen_ip = request.remote_addr or "unknown"

        # Screens self-identify their hardware model, but the header only exists
        # from firmware 1.2.9 onwards. Anything older predates multi-screen
        # support entirely, and back then huessen_epf1301 was the only model
        # there was — so a *missing* header means huessen, not a broken screen.
        # Rejecting it would strand every pre-1.2.9 frame on a 400 with no way
        # back: that firmware has no OTA, so it could never be told to update
        # and would need a USB reflash just to start showing photos again.
        # A header that is present but unrecognised is still a real error.
        # (Matches the firmware/config endpoints below, which already default.)
        raw_screen_model = request.headers.get("X-Screen-Model")
        if raw_screen_model is None:
            screen_model = "huessen_epf1301"
        else:
            screen_model = parse_screen_model(raw_screen_model)
            if screen_model is None:
                resp = make_response("Unknown X-Screen-Model", 400)
                resp.headers["X-Sleep-Seconds"] = str(_busy_retry_seconds(config))
                return resp

        battery_mv = parse_battery_header(request.headers.get("X-Battery-mV"))
        frame_state = parse_frame_state(request.headers.get("X-Frame-State"))
        fw_version = parse_firmware_version(request.headers.get("X-Firmware-Version"))
        fw_build = parse_firmware_build(request.headers.get("X-Firmware-Build"))
        # X-Screen-Mac is the durable per-device key; cal_ppm (in the frame-state
        # JSON) is the device's current drift correction, folded into the pinned
        # long-term mean that seeds cold-start devices.
        screen_mac = parse_mac_header(request.headers.get("X-Screen-Mac"))
        cal_ppm = parse_cal_ppm(frame_state.get("cal_ppm")) if frame_state else None

        def _add_cal_seed(resp):
            """Attach the MAC-pinned drift seed to every response; the device
            decides whether to adopt it (based on the sample count)."""
            mean_ppm, samples = scheduler.cal_seed_for(screen_name, screen_mac)
            resp.headers["X-Sleep-Cal-PPM"] = str(mean_ppm)
            resp.headers["X-Sleep-Cal-N"] = str(samples)
            return resp

        # A device that reports its model's bundled version has finished any
        # update — drop a stale OTA-migration error and, if an UPGRADE was
        # pending, clear it (the retry succeeded). Re-flash requests are one-shot
        # and clear themselves when signalled, so leave those alone.
        model_fw_version = _firmware_store().effective_version(screen_model)
        if fw_version and fw_version == model_fw_version:
            scheduler.clear_ota_error(screen_name)
            if scheduler.is_ota_pending(screen_name) and not scheduler.is_ota_reflash(screen_name):
                scheduler.clear_ota_pending(screen_name)
        # OTA capability is advertised in the frame-state JSON ("ota":1). Only
        # OTA-capable firmware understands X-Firmware-Update + config migration,
        # so we never signal an update to a pre-OTA screen (would be a no-op).
        ota_capable = bool(frame_state and frame_state.get("ota"))

        # POST body carries the firmware log (plain text); GET has no body.
        screen_log: str | None = None
        if request.method == "POST":
            raw = request.get_data()
            if raw:
                screen_log = raw.decode("utf-8", errors="replace")

        cfg = scheduler.get_screen_config(screen_name)
        pick_orientation = cfg.orientation if cfg.filter_by_orientation else Orientation.NEUTRAL
        chosen = scheduler.pick_next(orientation=pick_orientation)
        sleep_seconds = calculate_sleep_seconds(config) if chosen else _busy_retry_seconds(config)

        if chosen is None:
            progress = manager.conversion_progress()
            converting = progress.total > 0
            scheduler.record_screen_call(
                screen_name,
                screen_ip,
                sleep_seconds,
                None,
                battery_mv,
                frame_state,
                log=screen_log,
                firmware_version=fw_version,
                firmware_build=fw_build,
                screen_model=screen_model,
                mac=screen_mac,
                cal_ppm=cal_ppm,
            )
            if converting:
                msg, status, label = "Converting images, try again shortly", 503, "Converting"
            else:
                msg, status, label = "No images in upload directory", 404, "No images"
            resp = make_response(msg, status)
            resp.headers["X-Sleep-Seconds"] = str(sleep_seconds)
            logger.debug("%s: %s told to retry in %ss", label, screen_name, sleep_seconds)
            return _add_cal_seed(resp)

        binary = manager.panel_bytes_for_model_orientation(chosen, screen_model, cfg.orientation)
        if binary is None:
            # Cache missing (not yet rendered for this orientation) — tell screen to retry.
            sleep_seconds = _busy_retry_seconds(config)
            scheduler.record_screen_call(
                screen_name,
                screen_ip,
                sleep_seconds,
                None,
                battery_mv,
                frame_state,
                log=screen_log,
                firmware_version=fw_version,
                firmware_build=fw_build,
                screen_model=screen_model,
                mac=screen_mac,
                cal_ppm=cal_ppm,
            )
            resp = make_response("Cached binary missing, try again shortly", 503)
            resp.headers["X-Sleep-Seconds"] = str(sleep_seconds)
            return _add_cal_seed(resp)

        scheduler.mark_served(chosen)
        scheduler.record_screen_call(
            screen_name,
            screen_ip,
            sleep_seconds,
            chosen,
            battery_mv,
            frame_state,
            log=screen_log,
            firmware_version=fw_version,
            firmware_build=fw_build,
            screen_model=screen_model,
            mac=screen_mac,
            cal_ppm=cal_ppm,
        )
        logger.debug("Serving: %s to %s (sleep_seconds=%s)", chosen, screen_name, sleep_seconds)

        response = make_response(binary)
        response.headers["Content-Type"] = "application/octet-stream"
        response.headers["X-Sleep-Seconds"] = str(sleep_seconds)
        response.headers["X-Server-Time-Epoch"] = str(int(_time.time()))
        response.headers["Content-Disposition"] = "attachment; filename=hokku.bin"
        _add_cal_seed(response)

        # Manual OTA: if the user toggled "update on next refresh" and the screen
        # is OTA-capable with firmware to ship, tell it to update (it ignores this
        # image body and fetches the firmware). A same-version RE-FLASH is one-shot
        # (success can't be confirmed by version). An UPGRADE is re-signalled every
        # poll until the device reports the target version, so a dropped download
        # self-heals — capped at OTA_MAX_ATTEMPTS to bound a genuinely-bad image.
        if ota_capable and model_fw_version and scheduler.is_ota_pending(screen_name):
            if scheduler.is_ota_reflash(screen_name):
                if scheduler.take_ota_pending(screen_name):
                    response.headers["X-Firmware-Update"] = model_fw_version
                    logger.info(
                        "Signalling OTA re-flash to %s [%s] (-> %s)",
                        screen_name,
                        screen_model,
                        model_fw_version,
                    )
            elif fw_version != model_fw_version:
                attempts = scheduler.note_ota_signal(screen_name)
                if attempts > OTA_MAX_ATTEMPTS:
                    scheduler.clear_ota_pending(screen_name)
                    scheduler.record_ota_error(
                        screen_name,
                        f"OTA to {model_fw_version} did not take after {OTA_MAX_ATTEMPTS} attempts",
                    )
                else:
                    response.headers["X-Firmware-Update"] = model_fw_version
                    logger.info(
                        "Signalling OTA to %s [%s] (-> %s) attempt %d/%d",
                        screen_name,
                        screen_model,
                        model_fw_version,
                        attempts,
                        OTA_MAX_ATTEMPTS,
                    )
        return response

    @app.route("/hokku/firmware.bin", methods=["GET"])
    def serve_firmware_image():
        """Serve the OTA firmware image for the requesting screen's model.

        The model is taken from the ``?model=`` query param (or the
        ``X-Screen-Model`` header). The device streams this straight into its
        inactive OTA slot — the byte format is per-model (huessen: sliced ESP
        app; bigme_f7: the full AWIH image, verbatim).

        Back-compat: existing huessen devices fetch this with no model param, so
        an absent model defaults to the huessen reference model."""
        model_arg = request.args.get("model") or request.headers.get("X-Screen-Model")
        model = parse_screen_model(model_arg) if model_arg else "huessen_epf1301"
        app_image = _firmware_store().effective_app_image(model)
        if not app_image:
            logger.error("OTA firmware.bin requested for model %r but no firmware found", model)
            abort(404)
        resp = make_response(app_image)
        resp.headers["Content-Type"] = "application/octet-stream"
        resp.headers["Content-Disposition"] = "attachment; filename=hokku-app.bin"
        return resp

    @app.route("/hokku/firmware-config", methods=["GET"])
    def serve_firmware_config():
        """Return a flashable NVS partition image for the OTA target firmware.

        The device sends its current config as an X-Config-State JSON header; we
        map it forward into the new firmware's NVS schema (``migrate_config``) and
        build the partition image with the same ``build_nvs_binary`` the USB setup
        path uses. The NVS schema is per-model (an ESP32-S3 board); the model comes
        from ``X-Screen-Model`` and defaults to huessen for existing devices that
        predate the header. A migration refusal is a should-never-happen bug: we
        record it on the screen and return 422 so the device aborts and stays put."""
        model_arg = request.headers.get("X-Screen-Model")
        model = parse_screen_model(model_arg) if model_arg else "huessen_epf1301"
        screen = esp32_screen(model)
        if screen is None:
            # Only ESP32-S3 boards use the NVS firmware-config round-trip (the F7
            # keeps config device-local). An unknown/non-NVS model is a bug.
            logger.error("firmware-config requested for model %r with no NVS config path", model)
            abort(404)

        screen_name = request.headers.get("X-Screen-Name")
        current = parse_config_state(request.headers.get("X-Config-State"))
        if not screen_name:
            screen_name = (current or {}).get("screen_name") or "unnamed"
        if current is None:
            logger.error("firmware-config from %s: missing/invalid X-Config-State", screen_name)
            return make_response("missing X-Config-State", 400)

        migrated = screen.migrate_config(current)
        if migrated is None:
            msg = f"cannot migrate config to schema v{screen.CONFIG_VERSION}"
            state.scheduler.record_ota_error(screen_name, msg)
            return make_response(msg, 422)

        url_override = state.scheduler.get_screen_config(screen_name).server_url_override
        if url_override:
            migrated["image_url"] = url_override
            logger.info("Applying server URL override for %s: %s", screen_name, url_override)

        if not _nvs_build_slots.acquire(blocking=False):
            logger.warning("firmware-config: NVS build slots exhausted, refusing %s", screen_name)
            return make_response("server busy building NVS images, retry shortly", 503)
        try:
            nvs_image = screen.build_nvs_binary(migrated)
        except screen.NvsToolUnavailable as e:
            logger.error("firmware-config: NVS generator unavailable: %s", e)
            return make_response("NVS generator unavailable on server", 503)
        except (RuntimeError, OSError) as e:
            # Log the full generator error (temp paths, stderr) server-side only;
            # the client/status gets a generic message to avoid leaking internals.
            logger.error("firmware-config: NVS build failed for %s: %s", screen_name, e)
            msg = "failed to build NVS image"
            state.scheduler.record_ota_error(screen_name, msg)
            return make_response(msg, 500)
        finally:
            _nvs_build_slots.release()

        resp = make_response(nvs_image)
        resp.headers["Content-Type"] = "application/octet-stream"
        resp.headers["Content-Disposition"] = "attachment; filename=hokku-nvs.bin"
        logger.info("Served migrated NVS image to %s (%d bytes)", screen_name, len(nvs_image))
        return resp

    # ── Web GUI ────────────────────────────────────────────────

    @app.route("/")
    def root():
        return redirect("/hokku/ui")

    @app.route("/hokku/ui")
    def web_gui():
        ref = DISPLAY_REGISTRY["huessen_epf1301"]
        return render_template("index.html", visual_w=ref.visual_w, visual_h=ref.visual_h)

    @app.route("/hokku/static/<path:filename>")
    def static_asset(filename: str):
        # send_from_directory rejects path-traversal automatically.
        return send_from_directory(static_root, filename)

    # ── API: image data ────────────────────────────────────────

    @app.route("/hokku/api/original/<path:name>")
    def api_original(name: str):
        manager = state.manager
        try:
            path = manager.original_path(name)
        except FileNotFoundError:
            abort(404)
        if not path.is_file():
            abort(404)
        return send_file(path)

    @app.route("/hokku/api/dithered/<path:name>")
    def api_dithered(name: str):
        png = state.manager.preview_png(name)
        if png is None:
            abort(404)
        return _png_response(png)

    @app.route("/hokku/api/thumbnail/<path:name>")
    def api_thumbnail(name: str):
        jpg = state.manager.thumbnail_jpg(name)
        if jpg is None:
            abort(404)
        resp = make_response(jpg)
        resp.headers["Content-Type"] = "image/jpeg"
        return resp

    # ── API: image management ──────────────────────────────────

    @app.route("/hokku/api/upload", methods=["POST"])
    def api_upload():
        manager = state.manager
        files = request.files.getlist("file") or request.files.getlist("files")
        if not files:
            logger.info("Upload rejected: no files in request")
            return jsonify({"error": "No files in upload"}), 400
        saved, skipped = [], []
        for f in files:
            if not f or not f.filename:
                continue
            name = secure_filename(f.filename)
            if not name:
                skipped.append({"name": f.filename, "reason": "invalid filename"})
                continue
            ext = Path(name).suffix.lower()
            if ext not in IMAGE_EXTENSIONS:
                skipped.append({"name": name, "reason": f"unsupported extension {ext}"})
                continue
            data = f.read()
            # Header-only dimension probe — cheap and safe even for bomb PNGs.
            # Reject before writing to disk so we never decode a giant buffer.
            # SVG: skip PIL probe; resvg always renders within screen resolution
            # so the pixel budget is never exceeded.
            if ext == ".svg":
                w, h = SVG_PROBE_DIMS
            else:
                try:
                    with Image.open(io.BytesIO(data)) as probe:
                        w, h = probe.size
                except Image.DecompressionBombError:
                    skipped.append(
                        {
                            "name": name,
                            "reason": f"image too large; limit {format_megapixels(MAX_UPLOAD_PIXELS)}",
                        }
                    )
                    continue
                except (UnidentifiedImageError, OSError) as e:
                    logger.error("Unreadable image %r: %s: %s", name, type(e).__name__, e)
                    skipped.append({"name": name, "reason": "unreadable image"})
                    continue
            if w * h > MAX_UPLOAD_PIXELS:
                skipped.append(
                    {
                        "name": name,
                        "reason": f"image too large: {format_megapixels(w * h)} ({w}x{h}); "
                        f"limit {format_megapixels(MAX_UPLOAD_PIXELS)}",
                    }
                )
                continue
            try:
                manager.add(name, data)
                saved.append(name)
            except FileExistsError:
                skipped.append({"name": name, "reason": "already exists; remove to replace"})
            except (OSError, ValueError) as e:
                logger.error("Error adding %r: %s: %s", name, type(e).__name__, e)
                skipped.append({"name": name, "reason": str(e)})
        return jsonify({"saved": saved, "skipped": skipped})

    @app.route("/hokku/api/image/<path:name>", methods=["DELETE"])
    def api_delete(name: str):
        try:
            state.manager.remove(name)
        except FileNotFoundError:
            logger.info("Delete: image %r not found", name)
            return jsonify({"error": f"image {name!r} not found"}), 404
        except OSError as e:
            logger.error("Delete failed for %r: %s", name, e)
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True})

    @app.route("/hokku/api/image/<path:name>/retry", methods=["POST"])
    def api_retry(name: str):
        try:
            state.manager.retry(name)
        except FileNotFoundError:
            logger.info("Retry: image %r not found", name)
            return jsonify({"error": f"image {name!r} not found"}), 404
        return jsonify({"ok": True})

    @app.route("/hokku/api/image/<path:name>/config", methods=["GET"])
    def api_image_config_get(name: str):
        """This picture's overrides, and what it actually renders with.

        ``effective`` is what the details UI opens its editor on, so tuning
        starts from the picture as it looks now rather than from an arbitrary
        preset. Cheap by construction: no detection is run.
        """
        rec = state.manager.status(name)
        if rec is None:
            logger.info("Image config: image %r not found", name)
            return jsonify({"error": f"image {name!r} not found"}), 404

        decision = state.manager.effective_decision(name)
        obs = state.classifier.observations_for(rec.original_sha1) if rec.original_sha1 else None
        return jsonify(
            {
                "overrides": {
                    "image_config": asdict(rec.image_config) if rec.image_config else None,
                    "crop_to_fill_threshold": rec.crop_to_fill_threshold,
                },
                "effective": {
                    "image_config": asdict(decision.image_config),
                    "crop_to_fill_threshold": decision.crop_to_fill_threshold,
                }
                if decision is not None
                else None,
                "pipeline": _pipeline_label(rec, obs),
            }
        )

    @app.route("/hokku/api/image/<path:name>/config", methods=["PATCH"])
    def api_image_config_patch(name: str):
        """Set or clear this picture's dither and/or crop override.

        PATCH because the two fields are independent: a key that is absent is
        left alone, and an explicit null clears that one override. So the same
        route covers setting either, clearing either, and doing both at once.

        Nothing is written unless the whole body validates — a rejected request
        must leave the picture exactly as it was.
        """
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            logger.info("Image config: expected JSON object, got %r", type(body).__name__)
            return jsonify({"error": "expected JSON object"}), 400

        unknown = body.keys() - {"image_config", "crop_to_fill_threshold"}
        if unknown:
            return jsonify(
                {"error": f"unknown field(s): {', '.join(sorted(unknown))}"},
            ), 400

        rec = state.manager.status(name)
        if rec is None:
            logger.info("Image config: image %r not found", name)
            return jsonify({"error": f"image {name!r} not found"}), 404
        if rec.image_width is None:
            return jsonify(
                {"error": f"image {name!r} cannot be rendered, so it cannot be configured"}
            ), 409

        changes: dict[str, Any] = {}
        try:
            if "image_config" in body:
                raw = body["image_config"]
                changes["image_config"] = (
                    None if raw is None else image_config_from_dict_strict(raw)
                )
            if "crop_to_fill_threshold" in body:
                raw = body["crop_to_fill_threshold"]
                changes["crop_to_fill_threshold"] = (
                    None if raw is None else parse_crop_to_fill_threshold(raw)
                )
        except ImageConfigError as e:
            logger.info("Image config: rejected override for %r: %s", name, e)
            return jsonify({"error": str(e), "errors": e.errors}), 400

        try:
            queued = state.manager.set_overrides(name, **changes)
        except FileNotFoundError:
            return jsonify({"error": f"image {name!r} not found"}), 404

        if queued:
            _request_sync(state)
        return jsonify({"ok": True, "queued": queued})

    @app.route("/hokku/api/show_next/<path:name>", methods=["POST"])
    def api_show_next(name: str):
        rec = state.manager.status(name)
        if rec is None:
            logger.info("Show next: image %r not found", name)
            return jsonify({"error": f"image {name!r} not found"}), 404
        if rec.convert_status != ConvertStatus.OK:
            logger.warning("Show next: image %r not ready (status: %s)", name, rec.convert_status)
            return jsonify(
                {"error": f"image {name!r} is not ready (status: {rec.convert_status})"}
            ), 409
        try:
            state.scheduler.set_next(name)
        except ValueError as e:
            logger.warning("Show next: scheduler rejected %r: %s", name, e)
            return jsonify({"error": str(e)}), 409
        return jsonify({"ok": True, "next_image": name})

    @app.route("/hokku/api/clear_cache", methods=["POST"])
    def api_clear_cache():
        state.manager.clear_caches()
        state.manager.sync()  # kick off reconversion immediately
        return jsonify({"ok": True})

    @app.route("/hokku/api/classifier/clear", methods=["POST"])
    def api_classifier_clear():
        """Wipe all cached classifier observations (is_bw / has_face) and trigger re-sync.

        Deletes image_classifier.json and kicks off immediate re-classification.
        Already-rendered panel .bin files are NOT touched — they are keyed by
        ScreenImageConfig slug and remain valid unless the classification result changes.
        """
        state.classifier.clear_cache()
        state.manager.sync()
        return jsonify({"ok": True})

    @app.route("/hokku/api/screens/<string:name>", methods=["DELETE"])
    def api_screen_delete(name: str):
        """Remove a screen's telemetry and serve-stats records.

        The screen will re-appear automatically the next time it connects.
        """
        state.scheduler.remove_screen(name)
        return jsonify({"ok": True})

    @app.route("/hokku/api/screens/<string:name>/config", methods=["PATCH"])
    def api_screen_config(name: str):
        """Patch per-screen config (orientation and/or orientation filter)."""
        body = request.get_json(silent=True) or {}
        current = state.scheduler.get_screen_config(name)
        updates: dict = {}

        if "orientation" in body:
            raw = body.get("orientation")
            if raw not in ("landscape", "portrait"):
                logger.info("Screen config %r: invalid orientation %r", name, raw)
                return jsonify({"error": "orientation must be 'landscape' or 'portrait'"}), 400
            updates["orientation"] = Orientation(raw)

        if "filter_by_orientation" in body:
            val = body.get("filter_by_orientation")
            if not isinstance(val, bool):
                logger.info(
                    "Screen config %r: filter_by_orientation must be bool, got %r", name, val
                )
                return jsonify({"error": "filter_by_orientation must be a boolean"}), 400
            updates["filter_by_orientation"] = val

        if "server_url_override" in body:
            val = body.get("server_url_override")
            if not isinstance(val, str):
                return jsonify({"error": "server_url_override must be a string"}), 400
            updates["server_url_override"] = val.strip()

        if updates:
            state.scheduler.set_screen_config(name, replace(current, **updates))
            state.manager.sync()
        return jsonify({"ok": True})

    @app.route("/hokku/api/screens/<string:name>/update", methods=["POST"])
    def api_screen_update(name: str):
        """Toggle the 'update firmware on next refresh' flag for a screen.

        Body: {"enabled": bool}. Signalled to the device on its next poll (only if
        OTA-capable). An upgrade retries until the device reports the new version;
        a same-version re-flash is one-shot. Available regardless of version so the
        user can also re-flash the current firmware."""
        model = state.scheduler.get_screen_model(name)
        # Block only when there is nothing to serve for this screen: its own
        # model has no firmware, and (for a screen that hasn't reported a model
        # yet) the server has no firmware at all. Uses the *effective* version
        # (pin/highest-stable) so a re-flash vs upgrade is judged against what
        # will actually be served.
        store = _firmware_store()
        model_version = store.effective_version(model)
        has_firmware = model_version if model else any(store.effective_versions().values())
        if not has_firmware:
            return jsonify({"error": "no bundled firmware available for this screen"}), 409
        body = request.get_json(silent=True) or {}
        enabled = body.get("enabled", True)
        if not isinstance(enabled, bool):
            return jsonify({"error": "enabled must be a boolean"}), 400
        # Re-flash vs upgrade: if the device already runs the target version, the
        # update can't be confirmed by a version change, so it's a one-shot.
        current = state.scheduler.get_screen_firmware_version(name)
        reflash = bool(model_version and current and current == model_version)
        state.scheduler.set_ota_pending(name, enabled, reflash=reflash)
        return jsonify({"ok": True, "ota_pending": enabled})

    # ── API: firmware library (bundled + downloaded + pins) ────────

    @app.route("/hokku/api/firmware/library")
    def api_firmware_library():
        """The local firmware library: per-model available versions plus which is
        effective (served) and which is pinned. No network access."""
        store = _firmware_store()
        models = {}
        for model in firmware_registry.known_models():
            eff = store.effective(model)
            models[model] = {
                "effective": eff.version if eff else None,
                "effective_channel": eff.channel if eff else None,
                "effective_source": eff.source if eff else None,
                "pinned": store.pinned(model),
                "available": [
                    {
                        "version": v.version,
                        "channel": v.channel,
                        "source": v.source,
                        "tag": v.tag,
                    }
                    for v in store.variants(model)
                ],
            }
        return jsonify(
            {
                "repo": state.config.firmware_github_repo,
                "models": models,
            }
        )

    @app.route("/hokku/api/firmware/remote")
    def api_firmware_remote():
        """List firmware downloadable from GitHub Releases. On demand only.

        Prereleases (betas) are excluded unless ``?prereleases=1``. This is the
        only route that reaches the internet, and only because the user asked."""
        cfg = state.config
        include_pre = request.args.get("prereleases") == "1"
        try:
            remote = firmware_github.available_firmware(
                cfg.firmware_github_repo, include_prereleases=include_pre
            )
        except firmware_github.FirmwareFetchError as e:
            return jsonify({"error": str(e)}), 502
        store = _firmware_store()
        have = {m: {v.version for v in store.variants(m)} for m in firmware_registry.known_models()}
        items = [
            {
                "model": r.model_id,
                "version": r.version,
                "tag": r.tag,
                "channel": r.channel,
                "prerelease": r.prerelease,
                "size": r.size,
                "downloaded": r.version in have.get(r.model_id, set()),
            }
            for r in remote
        ]
        return jsonify({"releases": items, "include_prereleases": include_pre})

    @app.route("/hokku/api/firmware/fetch", methods=["POST"])
    def api_firmware_fetch():
        """Download one firmware asset from GitHub into the library.

        Body: ``{"model": <id>, "tag": <release-tag>}`` (or ``"version"``). The
        download is validated as a real image for the model before it is admitted
        (a bad asset is rejected, not stored). Does NOT change what is served —
        the user must pin it separately."""
        cfg = state.config
        body = request.get_json(silent=True) or {}
        model = body.get("model")
        tag = body.get("tag")
        version = body.get("version")
        if model not in firmware_registry.known_models():
            return jsonify({"error": "unknown model"}), 400
        if not tag and not version:
            return jsonify({"error": "tag or version required"}), 400
        # Include prereleases so a beta can be fetched by its exact tag/version.
        try:
            remote = firmware_github.available_firmware(
                cfg.firmware_github_repo, include_prereleases=True
            )
        except firmware_github.FirmwareFetchError as e:
            return jsonify({"error": str(e)}), 502
        match = next(
            (r for r in remote if r.model_id == model and (r.tag == tag or r.version == version)),
            None,
        )
        if match is None:
            return jsonify({"error": "no matching firmware asset in that release"}), 404
        try:
            data = firmware_github.download(match)
        except firmware_github.FirmwareFetchError as e:
            return jsonify({"error": str(e)}), 502
        try:
            variant = _firmware_store().add_download(
                match.model_id, match.version, data, channel=match.channel, tag=match.tag
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        logger.info(
            "Downloaded firmware %s %s (%s) from %s",
            model,
            variant.version,
            variant.channel,
            match.tag,
        )
        return jsonify(
            {"ok": True, "model": model, "version": variant.version, "channel": variant.channel}
        )

    @app.route("/hokku/api/firmware/select", methods=["POST"])
    def api_firmware_select():
        """Pin a model to a specific version, or clear the pin.

        Body: ``{"model": <id>, "version": <ver>|null}``. Clearing reverts the
        model to the highest-stable default. A pin must reference a version that
        is actually present in the library."""
        store = _firmware_store()
        body = request.get_json(silent=True) or {}
        model = body.get("model")
        version = body.get("version")
        if model not in firmware_registry.known_models():
            return jsonify({"error": "unknown model"}), 400
        if version is not None:
            if not isinstance(version, str):
                return jsonify({"error": "version must be a string or null"}), 400
            available = {v.version for v in store.variants(model)}
            if version not in available:
                return jsonify({"error": "version not present in the firmware library"}), 404
        store.set_pin(model, version)
        return jsonify({"ok": True, "model": model, "pinned": version})

    @app.route("/hokku/api/scrub", methods=["POST"])
    def api_scrub():
        """Remove stale-slug panel/preview files immediately (preserves thumbs)."""
        state.manager.scrub_stale_cache()
        return jsonify({"ok": True})

    # ── API: status + config ───────────────────────────────────

    @app.route("/hokku/api/status")
    def api_status():
        manager = state.manager
        scheduler = state.scheduler

        # Re-derive the resource budget for display (cheap; reads cgroup/psutil).
        _budget = compute_budget(state.config.memory_budget_mb)

        records = manager.list()
        progress = manager.conversion_progress()
        last = scheduler.last_served()
        classifier = state.classifier
        upload_files = []
        failed_files = []
        for r in records:
            obs = classifier.observations_for(r.original_sha1) if r.original_sha1 else None
            entry = {
                "name": r.name,
                "dithered": r.convert_status == ConvertStatus.OK,
                "status": r.convert_status,
                "error": r.convert_error,
                "size_bytes": r.original_size_bytes,
                "image_width": r.image_width,
                "image_height": r.image_height,
                "dimension_unit": "pt" if Path(r.name).suffix.lower() == ".svg" else "px",
                "native_orientation": r.native_orientation.value
                if r.convert_status == ConvertStatus.OK
                else None,
                "last_conversion_seconds": r.last_conversion_seconds,
                "is_bw": obs.is_bw if obs else None,
                "face_bboxes": [[b.x, b.y, b.w, b.h] for b in obs.face_bboxes]
                if (obs and obs.face_bboxes)
                else [],
                # Enough for the grid chip and for the modal to decide whether
                # it is showing an override. Deliberately NOT the ImageConfig
                # itself: this runs for the whole library on every poll, and a
                # 24-field blob per picture would be a few hundred KB a poll on
                # a large library. The editor fetches the full config for the
                # one picture it is opening.
                "has_image_config_override": r.image_config is not None,
                "crop_to_fill_threshold": r.crop_to_fill_threshold,
                "pipeline": _pipeline_label(r, obs),
            }
            upload_files.append(entry)
            if r.convert_status == ConvertStatus.FAILED:
                failed_files.append(
                    {"name": r.name, "error": r.convert_error, "size_bytes": r.original_size_bytes}
                )

        ready_count = sum(1 for r in records if r.convert_status == ConvertStatus.OK)
        serve_data: dict[str, dict] = {}
        for n, s in scheduler.stats().items():
            serve_data[n] = {
                "show_index": s.show_index,
                "last_request": (
                    datetime.fromtimestamp(s.last_served_at).isoformat(timespec="seconds")
                    if s.last_served_at
                    else None
                ),
                "total_show_count": s.total_show_count,
                "total_show_minutes": s.total_show_minutes,
                "total_show_formatted": format_duration_human(s.total_show_minutes),
            }

        screens_payload: dict[str, dict] = {}
        screen_peek_orientations: set[Orientation] = set()
        for _sid, t in scheduler.screens().items():
            sname = t.name
            next_update_at = None
            if t.last_seen_at and t.last_sleep_seconds:
                next_update_at = datetime.fromtimestamp(
                    t.last_seen_at + t.last_sleep_seconds
                ).isoformat(timespec="seconds")
            scfg = scheduler.get_screen_config(sname)
            peek_orientation = (
                scfg.orientation if scfg.filter_by_orientation else Orientation.NEUTRAL
            )
            screen_peek_orientations.add(peek_orientation)
            screens_payload[sname] = {
                "mac": t.mac,
                "cal_ppm": t.cal_ppm,
                "cal_mean_ppm": (round(t.cal_mean_ppm, 1) if t.cal_mean_ppm is not None else None),
                "cal_samples": t.cal_samples,
                "ip": t.ip,
                "request_count": t.request_count,
                "last_seen": (
                    datetime.fromtimestamp(t.last_seen_at).isoformat(timespec="seconds")
                    if t.last_seen_at
                    else None
                ),
                "last_sleep_seconds": t.last_sleep_seconds,
                "last_served": t.last_served,
                "screen_model": t.screen_model,
                "battery_mv": t.battery_mv,
                "battery_percent": t.battery_percent,
                "battery_seen_at": (
                    datetime.fromtimestamp(t.battery_seen_at).isoformat(timespec="seconds")
                    if t.battery_seen_at
                    else None
                ),
                "next_update_at": next_update_at,
                "state": t.frame_state,
                "orientation": scfg.orientation,
                "filter_by_orientation": scfg.filter_by_orientation,
                "server_url_override": scfg.server_url_override,
                "last_log": t.last_log or None,
                "last_log_at": (
                    datetime.fromtimestamp(t.last_log_at).isoformat(timespec="seconds")
                    if t.last_log_at
                    else None
                ),
                "firmware_version": t.firmware_version,
                "firmware_build": t.firmware_build,
                "ota_pending": scheduler.is_ota_pending(sname),
                "ota_capable": bool(t.frame_state and t.frame_state.get("ota")),
                "ota_error": t.ota_error,
                "ota_error_at": (
                    datetime.fromtimestamp(t.ota_error_at).isoformat(timespec="seconds")
                    if t.ota_error_at
                    else None
                ),
            }

        disk = manager.cache_disk_info()
        return jsonify(
            {
                "server_time": datetime.now().isoformat(timespec="seconds"),
                "upload_size": len(records),
                "pool_size": ready_count,
                "pool_files": [r.name for r in records if r.convert_status == ConvertStatus.OK],
                "upload_files": upload_files,
                "failed_files": failed_files,
                "serve_data": serve_data,
                "screens": screens_payload,
                "last_served": last[0] if last else None,
                "converting": 1 if progress.current_name or progress.done < progress.total else 0,
                "converting_name": progress.current_name,
                "converting_done": progress.done,
                "converting_total": progress.total,
                "converting_eta_seconds": manager.estimate_remaining_seconds(),
                "next_images": {
                    o.value: scheduler.peek_next(orientation=o)
                    for o in screen_peek_orientations
                    if scheduler.peek_next(orientation=o) is not None
                },
                "cache_used_bytes": disk["cache_used_bytes"],
                "disk_free_bytes": disk["disk_free_bytes"],
                "image_worker_count_resolved": state.manager.resolved_worker_count,
                "cpu_cores": os.cpu_count(),
                "memory_available_gb": round(psutil.virtual_memory().available / 1e9, 1),
                # Cgroup-aware resource picture (honours docker --memory / --cpus /
                # systemd MemoryMax, unlike cpu_cores / memory_available_gb which
                # report the host). memory_limit_mb is what's physically detected;
                # memory_budget_effective_mb is after the memory_budget_mb cap.
                "cpu_limit": _budget.cpu_count,
                "memory_limit_mb": round(detect_memory_limit_bytes() / (1024 * 1024)),
                "memory_budget_effective_mb": round(_budget.memory_bytes / (1024 * 1024)),
                "memory_budget_auto": _budget.memory_auto,
                "decode_budget_pixels": _budget.decode_budget_pixels,
                "under_provisioned": _budget.under_provisioned,
                "bundled_firmware_version": bundled_firmware_version,
                # Per-model EFFECTIVE firmware versions (pin, else highest stable
                # across bundled + downloaded), keyed by model id. Kept under the
                # historical key so the GUI's outdated-firmware comparison targets
                # what will actually be served. Equals the bundled version when
                # nothing has been downloaded or pinned.
                "bundled_firmware_versions": _firmware_store().effective_versions(),
            }
        )

    @app.route("/hokku/api/config", methods=["GET"])
    def api_config_get():
        presets = {}
        for name, p in PRESET_IMAGE_CONFIGS.items():
            meta = PRESET_META.get(name, {})
            presets[name] = {
                **asdict(p),
                "label": meta.get("label", name),
                "description": meta.get("description", ""),
            }
        return jsonify(
            {
                "config": state.config.to_dict(),
                "config_defaults": AppConfig().to_dict(),
                "dither_presets": presets,
                # jsonify sorts object keys, so the catalog's own order does not
                # survive the wire — without this the dropdown would be
                # alphabetical and the three shipped defaults would not lead it.
                "dither_preset_order": list(PRESET_IMAGE_CONFIGS),
                "server_time": datetime.now().isoformat(timespec="seconds"),
                # Per-model panel geometry, keyed by model id — auto-includes any
                # model added to the registry.  The legacy "panel" key mirrors the
                # reference model so older UI code keeps working.
                "panels": {
                    mid: {
                        "visual_w": d.visual_w,
                        "visual_h": d.visual_h,
                        "total_bytes": d.total_bytes,
                    }
                    for mid, d in DISPLAY_REGISTRY.items()
                },
                "panel": {
                    "visual_w": DISPLAY_REGISTRY["huessen_epf1301"].visual_w,
                    "visual_h": DISPLAY_REGISTRY["huessen_epf1301"].visual_h,
                    "total_bytes": DISPLAY_REGISTRY["huessen_epf1301"].total_bytes,
                },
                "git_describe": git_describe,
                "commit_url": f"{_REPO_URL}/commit/{git_hash}" if git_hash else None,
                "repo_url": _REPO_URL,
                # True only on appliance images where hokku-installer is installed.
                # The UI uses this to show/hide the "Reset to installer" button.
                "installer_available": Path("/var/lib/hokku-installer").is_dir(),
            }
        )

    @app.route("/hokku/api/config", methods=["POST"])
    def api_config_post():
        if config_path is None:
            logger.error("Config save attempted but server has no config_path")
            return jsonify({"error": "server started without a config_path; cannot save"}), 500
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            logger.info("Config save: expected JSON object, got %r", type(body).__name__)
            return jsonify({"error": "expected JSON object"}), 400
        try:
            merged = {**state.config.to_dict(), **body}
            new_cfg = AppConfig.from_dict(merged)
        except (TypeError, ValueError) as e:
            logger.info("Config save: invalid config: %s", e)
            return jsonify({"error": f"invalid config: {e}"}), 400
        if memory_budget_too_low(new_cfg.memory_budget_mb):
            msg = (
                f"Memory budget too low: {new_cfg.memory_budget_mb} MB. "
                f"Minimum is {MIN_MEMORY_BUDGET_MB} MB — below this the server can't "
                f"decode images. Leave the field empty for automatic detection."
            )
            logger.info("Config save rejected: %s", msg)
            return jsonify({"error": msg}), 400
        try:
            new_cfg.save(config_path)
        except OSError as e:
            logger.error("Failed to write config to %s: %s", config_path, e)
            return jsonify({"error": f"failed to write config: {e}"}), 500
        try:
            state.reload(new_cfg)
        except ValueError as e:
            logger.error("Config reload failed after save: %s", e)
            return jsonify({"error": f"reload failed: {e}"}), 400
        return jsonify({"ok": True, "restarting": False})

    @app.route("/hokku/api/dither/preview", methods=["POST"])
    def api_dither_preview():
        """Render a one-off dithered preview for a given image + image_config.

        Body: {name: str, image: ImageConfig dict, clahe_keepout?: bool,
        face_aware_crop?: bool, crop_to_fill_threshold?: float,
        max_side_px?: int}. Returns PNG bytes. ``clahe_keepout``,
        ``face_aware_crop`` and ``crop_to_fill_threshold`` default to the saved
        config when omitted; ``max_side_px`` trades preview detail for speed and
        is what the compare-presets grid uses for its thumbnails.
        The ``X-Face-Bboxes`` response header carries face bboxes already
        transformed into the rendered preview's coordinate space (JSON list
        of [x, y, w, h] tuples, each normalised 0..1 against the preview
        image).
        """
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            logger.info("Dither preview: expected JSON object, got %r", type(body).__name__)
            return jsonify({"error": "expected JSON object"}), 400
        name = body.get("name")
        image_blob = body.get("image")
        if not name or not isinstance(image_blob, dict):
            logger.info("Dither preview: missing name or image in body")
            return jsonify({"error": "expected {name, image}"}), 400
        # Which screen model to preview for. Defaults to the reference model;
        # an explicit value must be a known model.
        screen_model = str(body.get("screen_model", "huessen_epf1301"))
        if screen_model not in DISPLAY_REGISTRY:
            return jsonify({"error": f"unknown screen_model {screen_model!r}"}), 400
        preview_display = DISPLAY_REGISTRY[screen_model]
        try:
            path = state.manager.original_path(name)
        except FileNotFoundError:
            logger.info("Dither preview: image %r not found", name)
            return jsonify({"error": f"image {name!r} not found"}), 404
        # Strict, unlike the stored-config path: a preview the user cannot trust
        # to be the config they typed is worse than no preview. The lenient
        # parser would keep a default for a misspelled knob and render something
        # subtly different from what is on screen.
        try:
            cfg = image_config_from_dict_strict(image_blob, field_path="image")
        except ImageConfigError as e:
            logger.info("Dither preview: invalid image config: %s", e)
            return jsonify({"error": str(e), "errors": e.errors}), 400

        crop_threshold = state.config.crop_to_fill_threshold
        if body.get("crop_to_fill_threshold") is not None:
            try:
                crop_threshold = parse_crop_to_fill_threshold(body["crop_to_fill_threshold"])
            except ImageConfigError as e:
                return jsonify({"error": str(e), "errors": e.errors}), 400

        max_side_px = _PREVIEW_MAX_SIDE_PX
        if body.get("max_side_px") is not None:
            raw_side = body["max_side_px"]
            if isinstance(raw_side, bool) or not isinstance(raw_side, int):
                return jsonify({"error": "max_side_px must be a whole number"}), 400
            max_side_px = max(_PREVIEW_MIN_SIDE_PX, min(_PREVIEW_MAX_SIDE_PX, raw_side))

        # Look up cached face bboxes (original-image normalised) so we can map
        # them onto the rendered preview's coordinate space below.
        records = {r.name: r for r in state.manager.list()}
        record = records.get(name)
        face_bboxes_orig: tuple = ()
        if record and record.original_sha1:
            obs = state.classifier.observations_for(record.original_sha1)
            if obs and obs.face_bboxes:
                face_bboxes_orig = obs.face_bboxes

        use_clahe_keepout = body.get(
            "clahe_keepout", state.config.classifier_face_detect_clahe_keepout
        )
        keepout = face_bboxes_orig if (face_bboxes_orig and use_clahe_keepout) else None

        use_face_aware_crop = body.get(
            "face_aware_crop", state.config.classifier_face_aware_crop_enabled
        )
        crop_anchor = face_bboxes_orig if (face_bboxes_orig and use_face_aware_crop) else None

        # Render in the image's native orientation. NEUTRAL/unknown falls back
        # to LANDSCAPE since the renderer needs a concrete orientation.
        render_orientation = Orientation.LANDSCAPE
        if record is not None and record.convert_status == ConvertStatus.OK:
            native = record.native_orientation
            if native != Orientation.NEUTRAL:
                render_orientation = native

        # Previews decode the full source image, which dominates their cost and
        # is bounded only by the decode budget. Without a cap, a client looping
        # this endpoint (or a compare grid firing in parallel) would hold one
        # decoded image per request thread and starve the screen-serving path.
        if not _preview_slots.acquire(blocking=False):
            logger.info("Dither preview: refused, %d already rendering", PREVIEW_MAX_CONCURRENT)
            return jsonify({"error": "busy rendering previews, retry shortly"}), 503
        try:
            logger.debug("Preview: %r", name)
            with open_image_for_render(path) as img:
                orig_w, orig_h = img.size
                png = ImageRenderer(
                    NumbaStreamingDither(preview_display), preview_display
                ).render_preview_png(
                    img,
                    cfg,
                    render_orientation,
                    max_side_px=max_side_px,
                    crop_to_fill_threshold=crop_threshold,
                    clahe_keepout_bboxes_norm=keepout,
                    crop_anchor_bboxes_norm=crop_anchor,
                )
            logger.debug("Preview done: %r", name)
        finally:
            _preview_slots.release()

        # Same threshold the render above used. These two used to disagree: the
        # render took the 0.0 default because the argument was positional, so it
        # always letterboxed, while the overlay was computed for a cover-cropped
        # canvas the returned PNG did not have.
        canvas_bboxes = transform_bboxes_to_canvas_norm(
            face_bboxes_orig,
            orig_w,
            orig_h,
            render_orientation,
            preview_display.panel_w,
            preview_display.panel_h,
            crop_threshold,
            panel_rotated=preview_display.panel_rotated,
            crop_anchor_bboxes_norm=crop_anchor,
        )

        resp = _png_response(png)
        resp.headers["X-Face-Bboxes"] = json.dumps([list(b) for b in canvas_bboxes])
        return resp

    # ── API: flash a screen over USB ───────────────────────────

    @app.route("/hokku/api/flash/devices")
    def api_flash_devices():
        """Scan serial ports for connected screens and report their state.

        Refused (409) while a flash OR another scan is in progress — scanning
        resets the device and drives the same serial port as a flash. The scan
        holds the single serial slot for its whole duration so a flash cannot
        start mid-scan.

        The optional ``?model=`` selects which ESP32 screen's release the device's
        "up to date?" freshness is judged against (the two boards enumerate
        identically; only the version/config comparison is model-specific). It
        defaults to the huessen reference model.
        """
        if not any(s.merged_firmware_file() for s in esp32_screens()):
            logger.error("Flash scan requested but no bundled ESP32 firmware available")
            return jsonify({"error": "no bundled firmware available on this server"}), 503
        if not state.flash_jobs.begin_scan():
            logger.warning("Flash scan rejected: the serial port is busy")
            return jsonify({"error": "a flash is in progress", "busy": True}), 409
        try:
            screen = esp32_screen(request.args.get("model")) or huessen_epf1301
            # Leave the scanned screens in the bootloader. Booting one starts a
            # full panel repaint (~30-60s), and a scan is nearly always the step
            # right before a flash — which would then interrupt that paint
            # mid-refresh and wedge the panel controller. The panel keeps showing
            # its last image meanwhile (e-paper holds without power), and
            # arm_deferred_boot puts it back to work if no flash follows.
            devices = screen.scan_devices(boot_after=False)
        finally:
            state.flash_jobs.end_scan()
        state.flash_jobs.arm_deferred_boot(
            screen, [d["port"] for d in devices if d.get("is_esp32")]
        )
        # Classify non-ESP32 ports the UI knows how to guide: a CH340 bridge is a
        # Bigme F7 (XR872), which is USB-flashed by a different (vendor-tool)
        # procedure, not esptool — so the UI shows F7 guidance instead of the
        # generic "not an ESP32-S3" warning.
        #
        # is_usb distinguishes a plausible screen (any USB serial device, which
        # has a VID) from the host's own on-board UARTs (e.g. the Pi's
        # /dev/ttyS0, /dev/ttyAMA0 — no VID). A screen is always USB-attached, so
        # the UI groups the non-USB host ports away as "not a screen".
        for d in devices:
            d["is_bigme_f7"] = (d.get("vid"), d.get("pid")) == (0x1A86, 0x7523)
            d["is_usb"] = d.get("vid") is not None
        return jsonify({"devices": devices, "busy": False})

    @app.route("/hokku/api/flash/server_url")
    def api_flash_server_url():
        """The image-server URL a freshly-flashed screen should poll: this Pi.

        Prefers the mDNS hostname (``<name>.local``) when mDNS is enabled —
        the URL stays valid even if the Pi's IP address changes.
        """
        port = state.config.port
        hostname = state.config.mdns_hostname
        if hostname:
            url = f"http://{hostname}.local:{port}/hokku/screen/"
            return jsonify(
                {"address": f"{hostname}.local", "via": "mdns", "port": port, "url": url}
            )
        ip = _get_local_ip()
        return jsonify(
            {"address": ip, "via": "ip", "port": port, "url": f"http://{ip}:{port}/hokku/screen/"}
        )

    def _remember_flash_wifi(ssid1: str, pass1: str, ssid2: str | None, pass2: str | None) -> None:
        """Persist the Wi-Fi credentials a flash just used so the form pre-fills.

        Shared by every flash route — the same network provisions every screen,
        whatever the model. ``ssid2``/``pass2`` are ``None`` for models without a
        fallback network (the F7): those keep whatever was remembered before
        instead of being cleared by a flash that never asked for them."""
        if not config_path:
            return
        cfg = state.config
        fields = {"flash_wifi_ssid": ssid1, "flash_wifi_pass": pass1}
        if ssid2 is not None:
            fields["flash_wifi_ssid2"] = ssid2
            fields["flash_wifi_pass2"] = pass2 or ""
        if all(getattr(cfg, k) == v for k, v in fields.items()):
            return
        try:
            new_cfg = replace(cfg, **fields)
            new_cfg.save(config_path)
            with state._lock:
                state.config = new_cfg
        except Exception as e:
            logger.warning("Could not persist WiFi credentials to config: %s", e)

    @app.route("/hokku/api/flash/start", methods=["POST"])
    def api_flash_start():
        """Begin flashing firmware + NVS config to a connected screen.

        The target ESP32-S3 model comes from the request body (the two boards share
        a USB VID:PID, so the operator's choice, not the scan, picks the flash
        layout); it defaults to huessen for older UIs that omit it."""
        body = request.get_json(silent=True) or {}
        screen_model = (body.get("screen_model") or "huessen_epf1301").strip()
        screen = esp32_screen(screen_model)
        if screen is None:
            logger.info("Flash start: unknown ESP32 model %r", screen_model)
            return jsonify({"error": f"unknown ESP32 screen model {screen_model!r}"}), 400
        model_firmware = screen.merged_firmware_file()
        if model_firmware is None:
            logger.error("Flash start requested but no bundled firmware for %s", screen_model)
            return jsonify({"error": f"no bundled firmware for {screen_model} on this server"}), 503
        if not screen.nvs_tool_available():
            logger.error("Flash start requested but esp-idf-nvs-partition-gen is not installed")
            return jsonify(
                {"error": "NVS generator not installed (esp-idf-nvs-partition-gen)"}
            ), 503

        port = (body.get("port") or "").strip()
        wifi_ssid1 = (body.get("wifi_ssid1") or "").strip()
        image_url = (body.get("image_url") or "").strip()
        if not port or not wifi_ssid1 or not image_url:
            logger.info(
                "Flash start: missing required fields (port=%r ssid=%r url=%r)",
                port,
                wifi_ssid1,
                image_url,
            )
            return jsonify({"error": "port, wifi_ssid1 and image_url are required"}), 400

        screen_name = (body.get("screen_name") or "").strip()
        if len(screen_name.encode("utf-8")) > 64:
            logger.info(
                "Flash start: screen_name too long (%d bytes)", len(screen_name.encode("utf-8"))
            )
            return jsonify({"error": "screen_name must be <= 64 bytes"}), 400

        # NVS string fields are bounded by the firmware's config buffers; reject
        # over-long values up front (like screen_name) instead of letting them
        # fail deep in the NVS generator with an opaque error.
        for field, limit in _FLASH_FIELD_MAX_BYTES.items():
            if len((body.get(field) or "").encode("utf-8")) > limit:
                logger.info("Flash start: %s exceeds %d bytes", field, limit)
                return jsonify({"error": f"{field} must be <= {limit} bytes"}), 400

        try:
            wifi_order = int(body.get("wifi_order") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "wifi_order must be an integer"}), 400

        config: dict = {
            "wifi_ssid1": wifi_ssid1,
            "wifi_pass1": body.get("wifi_pass1") or "",
            "image_url": image_url,
            "wifi_order": wifi_order,
        }
        ssid2 = (body.get("wifi_ssid2") or "").strip()
        if ssid2:
            config["wifi_ssid2"] = ssid2
            config["wifi_pass2"] = body.get("wifi_pass2") or ""
        if screen_name:
            config["screen_name"] = screen_name

        job_id = state.flash_jobs.start(port, config, model_firmware, screen_model=screen_model)
        if job_id is None:
            logger.warning("Flash start rejected: a flash is already in progress")
            return jsonify({"error": "a flash is already in progress"}), 409

        # Persist WiFi credentials so the form pre-fills next time.
        _remember_flash_wifi(
            wifi_ssid1, body.get("wifi_pass1") or "", ssid2, body.get("wifi_pass2") or ""
        )

        return jsonify({"job_id": job_id})

    @app.route("/hokku/api/flash/start_f7", methods=["POST"])
    def api_flash_start_f7():
        """Bootstrap a fresh Bigme F7 (XR872) into Hokku firmware over USB.

        Catches the mask-BROM (the operator power-cycles with a USB replug + power
        press) and writes slot 0 via the same validated ``flash_slot`` — bootloader
        and OEM slot 1 untouched. Wi-Fi/config are provisioned afterward over the
        device console, not here. Progress polls the shared ``/flash/status``."""
        if not bigme_bootstrap.tooling_available():
            logger.error("F7 bootstrap requested but tools/ flash primitives are absent")
            return jsonify({"error": "Bigme F7 flash tooling is not available on this server"}), 503
        image = bigme_firmware.firmware_image_file()
        if image is None:
            logger.error("F7 bootstrap requested but no bundled xr_system.img is present")
            return jsonify({"error": "no bundled Bigme F7 firmware on this server"}), 503

        body = request.get_json(silent=True) or {}
        port = (body.get("port") or "").strip()
        if not port:
            return jsonify({"error": "port is required"}), 400

        # Optional provisioning: written over the console after the flash. The F7
        # console tokenizer splits on whitespace, so these must be single tokens.
        ssid = (body.get("wifi_ssid1") or "").strip()
        psk = body.get("wifi_pass1") or ""
        name = (body.get("screen_name") or "").strip()
        # F7 firmware >= 1.2.2 resolves mDNS `.local` (lwIP 2.1.2), so pass the URL
        # through unchanged like the ESP32 — no LAN-IP pinning needed.
        server_url = (body.get("image_url") or "").strip()
        for label, val in (("Wi-Fi name", ssid), ("Wi-Fi password", psk), ("screen name", name)):
            if val and not bigme_bootstrap.console_safe_token(val):
                return jsonify(
                    {"error": f"{label} can't contain spaces on the Bigme F7 (console limit)"}
                ), 400
        provision = None
        if ssid or name or server_url:
            provision = {"ssid": ssid, "psk": psk, "name": name, "server_url": server_url}

        job_id = state.flash_jobs.start_f7(port, image, provision)
        if job_id is None:
            logger.warning("F7 bootstrap rejected: a flash is already in progress")
            return jsonify({"error": "a flash is already in progress"}), 409

        # Persist WiFi credentials so the form pre-fills next time. The F7 has no
        # fallback network, so the remembered second network is left alone.
        if ssid:
            _remember_flash_wifi(ssid, psk, None, None)

        return jsonify({"job_id": job_id})

    @app.route("/hokku/api/flash/cancel", methods=["POST"])
    def api_flash_cancel():
        """Ask the running flash job to stop (only the F7 catch loop is cancellable)."""
        cancelled = state.flash_jobs.cancel()
        return jsonify({"cancelled": cancelled})

    @app.route("/hokku/api/flash/status")
    def api_flash_status():
        """Poll the current/last flash job (progress log + state)."""
        return jsonify(state.flash_jobs.status() or {"state": "idle"})

    @app.route("/hokku/api/reset-to-installer", methods=["POST"])
    def api_reset_to_installer():
        """Re-enter the first-boot installer AP mode.

        Only available on appliance images (hokku-installer installed).
        Removes the setup sentinel and schedules a system reboot.
        """
        installer_dir = Path("/var/lib/hokku-installer")
        if not installer_dir.is_dir():
            return jsonify({"error": "hokku-installer is not installed on this system"}), 404

        sentinel = installer_dir / "setup_complete"
        try:
            sentinel.unlink(missing_ok=True)
        except OSError as exc:
            logger.error("Could not remove installer sentinel: %s", exc)
            return jsonify({"error": f"could not remove sentinel: {exc}"}), 500

        logger.info("Installer sentinel removed — rebooting into installer AP mode")
        threading.Timer(2.0, lambda: subprocess.run(["systemctl", "reboot"], check=False)).start()  # noqa: S607
        return jsonify({"status": "rebooting"})

    @app.errorhandler(Exception)
    def _unhandled_exception(exc: Exception):
        if isinstance(exc, HTTPException):
            return exc
        logger.error("Unhandled exception in %s %s", request.method, request.path, exc_info=True)
        return jsonify({"error": "internal server error"}), 500

    return app


def _png_response(png_bytes: bytes):
    resp = make_response(png_bytes)
    resp.headers["Content-Type"] = "image/png"
    return resp
