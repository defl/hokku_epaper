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

from hokku.screens import huessen_epf1301
from hokku.webserver.app_config import AppConfig
from hokku.webserver.app_state import AppState
from hokku.webserver.display import FULL_W, PANEL_H, TOTAL_BYTES, VISUAL_H, VISUAL_W
from hokku.webserver.dither_streaming_numba import NumbaStreamingDither
from hokku.webserver.image_abc import transform_bboxes_to_canvas_norm
from hokku.webserver.image_config import _image_config_from_dict
from hokku.webserver.image_record import ConvertStatus
from hokku.webserver.image_renderer import (
    IMAGE_EXTENSIONS,
    MAX_UPLOAD_PIXELS,
    SVG_PROBE_DIMS,
    ImageRenderer,
    open_image_for_render,
)
from hokku.webserver.mdns import _get_local_ip
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS, PRESET_META
from hokku.webserver.screen_headers import (
    parse_battery_header,
    parse_config_state,
    parse_firmware_build,
    parse_firmware_version,
    parse_frame_state,
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


def _busy_retry_seconds(config: AppConfig) -> int:
    return min(300, calculate_sleep_seconds(config))


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
    # Bundled merged firmware for the "Flash a screen" feature (None if absent).
    firmware_path = huessen_epf1301.merged_firmware_file()
    _fw_hdr = huessen_epf1301.release_app_header()
    bundled_firmware_version: str | None = (
        _fw_hdr[48:80].split(b"\x00")[0].decode("ascii", errors="replace").strip()
        if _fw_hdr
        else None
    ) or None

    # ── Firmware-facing ────────────────────────────────────────

    @app.route("/hokku/screen/", strict_slashes=False, methods=["GET", "POST"])
    def serve_binary():
        manager = state.manager
        scheduler = state.scheduler
        config = state.config

        screen_name = request.headers.get("X-Screen-Name", "unnamed")
        screen_ip = request.remote_addr or "unknown"
        battery_mv = parse_battery_header(request.headers.get("X-Battery-mV"))
        frame_state = parse_frame_state(request.headers.get("X-Frame-State"))
        fw_version = parse_firmware_version(request.headers.get("X-Firmware-Version"))
        fw_build = parse_firmware_build(request.headers.get("X-Firmware-Build"))

        # A device that reports the bundled version has finished any update —
        # drop a stale OTA-migration error if one was recorded.
        if fw_version and fw_version == bundled_firmware_version:
            scheduler.clear_ota_error(screen_name)
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
            )
            if converting:
                msg, status, label = "Converting images, try again shortly", 503, "Converting"
            else:
                msg, status, label = "No images in upload directory", 404, "No images"
            resp = make_response(msg, status)
            resp.headers["X-Sleep-Seconds"] = str(sleep_seconds)
            logger.debug("%s: %s told to retry in %ss", label, screen_name, sleep_seconds)
            return resp

        binary = manager.panel_bytes_for_orientation(chosen, cfg.orientation)
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
            )
            resp = make_response("Cached binary missing, try again shortly", 503)
            resp.headers["X-Sleep-Seconds"] = str(sleep_seconds)
            return resp

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
        )
        logger.debug("Serving: %s to %s (sleep_seconds=%s)", chosen, screen_name, sleep_seconds)

        response = make_response(binary)
        response.headers["Content-Type"] = "application/octet-stream"
        response.headers["X-Sleep-Seconds"] = str(sleep_seconds)
        response.headers["X-Server-Time-Epoch"] = str(int(_time.time()))
        response.headers["Content-Disposition"] = "attachment; filename=hokku.bin"

        # Manual OTA: if the user toggled "update on next refresh" for this screen
        # and it is OTA-capable and we actually have firmware to ship, tell it to
        # update (it will ignore this image body and fetch firmware/config). The
        # flag is consumed one-shot here so the device gets exactly one signal.
        if (
            ota_capable
            and bundled_firmware_version
            and scheduler.is_ota_pending(screen_name)
            and scheduler.take_ota_pending(screen_name)
        ):
            response.headers["X-Firmware-Update"] = bundled_firmware_version
            logger.info("Signalling OTA to %s (-> %s)", screen_name, bundled_firmware_version)
        return response

    @app.route("/hokku/firmware.bin", methods=["GET"])
    def serve_firmware_image():
        """Serve the OTA-flashable app image (sliced from the bundled merged bin).

        The device streams this straight into its inactive OTA slot."""
        app_image = huessen_epf1301.release_app_image()
        if not app_image:
            logger.error("OTA firmware.bin requested but no bundled firmware found")
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
        path uses. A migration refusal is a should-never-happen bug: we record it
        on the screen and return 422 so the device aborts and stays put."""
        screen_name = request.headers.get("X-Screen-Name")
        current = parse_config_state(request.headers.get("X-Config-State"))
        if not screen_name:
            screen_name = (current or {}).get("screen_name") or "unnamed"
        if current is None:
            logger.error("firmware-config from %s: missing/invalid X-Config-State", screen_name)
            return make_response("missing X-Config-State", 400)

        migrated = huessen_epf1301.migrate_config(current)
        if migrated is None:
            msg = f"cannot migrate config to schema v{huessen_epf1301.CONFIG_VERSION}"
            state.scheduler.record_ota_error(screen_name, msg)
            return make_response(msg, 422)

        url_override = state.scheduler.get_screen_config(screen_name).server_url_override
        if url_override:
            migrated["image_url"] = url_override
            logger.info("Applying server URL override for %s: %s", screen_name, url_override)

        try:
            nvs_image = huessen_epf1301.build_nvs_binary(migrated)
        except huessen_epf1301.NvsToolUnavailable as e:
            logger.error("firmware-config: NVS generator unavailable: %s", e)
            return make_response("NVS generator unavailable on server", 503)
        except (RuntimeError, OSError) as e:
            msg = f"failed to build NVS image: {e}"
            state.scheduler.record_ota_error(screen_name, msg)
            return make_response(msg, 500)

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
        return render_template("index.html", visual_w=VISUAL_W, visual_h=VISUAL_H)

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
                            "reason": f"image too large; cap {MAX_UPLOAD_PIXELS:,} px",
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
                        "reason": f"image too large ({w}x{h}); cap {MAX_UPLOAD_PIXELS:,} px",
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
        """Toggle the one-shot 'update firmware on next refresh' flag for a screen.

        Body: {"enabled": bool}. The actual OTA is signalled to the device on its
        next poll (only if it is OTA-capable). Available regardless of version so
        the user can also re-flash the same firmware."""
        if not bundled_firmware_version:
            return jsonify({"error": "no bundled firmware available on the server"}), 409
        body = request.get_json(silent=True) or {}
        enabled = body.get("enabled", True)
        if not isinstance(enabled, bool):
            return jsonify({"error": "enabled must be a boolean"}), 400
        state.scheduler.set_ota_pending(name, enabled)
        return jsonify({"ok": True, "ota_pending": enabled})

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
        for sname, t in scheduler.screens().items():
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
                "ip": t.ip,
                "request_count": t.request_count,
                "last_seen": (
                    datetime.fromtimestamp(t.last_seen_at).isoformat(timespec="seconds")
                    if t.last_seen_at
                    else None
                ),
                "last_sleep_seconds": t.last_sleep_seconds,
                "last_served": t.last_served,
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
                "bundled_firmware_version": bundled_firmware_version,
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
                "server_time": datetime.now().isoformat(timespec="seconds"),
                "panel": {"visual_w": VISUAL_W, "visual_h": VISUAL_H, "total_bytes": TOTAL_BYTES},
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

        Body: {name: str, image: ImageConfig dict}. Returns PNG bytes.
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
        try:
            path = state.manager.original_path(name)
        except FileNotFoundError:
            logger.info("Dither preview: image %r not found", name)
            return jsonify({"error": f"image {name!r} not found"}), 404
        try:
            cfg = _image_config_from_dict(image_blob)
        except (TypeError, ValueError) as e:
            logger.info("Dither preview: invalid image config: %s", e)
            return jsonify({"error": f"invalid image config: {e}"}), 400

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

        # Render in the image's native orientation. NEUTRAL/unknown falls back
        # to LANDSCAPE since the renderer needs a concrete orientation.
        render_orientation = Orientation.LANDSCAPE
        if record is not None and record.convert_status == ConvertStatus.OK:
            native = record.native_orientation
            if native != Orientation.NEUTRAL:
                render_orientation = native

        logger.debug("Preview: %r", name)
        with open_image_for_render(path) as img:
            orig_w, orig_h = img.size
            png = ImageRenderer(NumbaStreamingDither()).render_preview_png(
                img,
                cfg,
                render_orientation,
                clahe_keepout_bboxes_norm=keepout,
            )
        logger.debug("Preview done: %r", name)

        canvas_bboxes = transform_bboxes_to_canvas_norm(
            face_bboxes_orig,
            orig_w,
            orig_h,
            render_orientation,
            FULL_W,
            PANEL_H,
            state.config.crop_to_fill_threshold,
        )

        resp = _png_response(png)
        resp.headers["X-Face-Bboxes"] = json.dumps([list(b) for b in canvas_bboxes])
        return resp

    # ── API: flash a screen over USB ───────────────────────────

    @app.route("/hokku/api/flash/devices")
    def api_flash_devices():
        """Scan serial ports for connected screens and report their state.

        Refused (409) while a flash is in progress — scanning resets the device
        and contends for the serial port.
        """
        if firmware_path is None:
            logger.error("Flash scan requested but no bundled firmware available")
            return jsonify({"error": "no bundled firmware available on this server"}), 503
        if state.flash_jobs.is_busy():
            logger.warning("Flash scan rejected: a flash is already in progress")
            return jsonify({"error": "a flash is in progress", "busy": True}), 409
        return jsonify({"devices": huessen_epf1301.scan_devices(), "busy": False})

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

    @app.route("/hokku/api/flash/start", methods=["POST"])
    def api_flash_start():
        """Begin flashing firmware + NVS config to a connected screen."""
        if firmware_path is None:
            logger.error("Flash start requested but no bundled firmware available")
            return jsonify({"error": "no bundled firmware available on this server"}), 503
        if not huessen_epf1301.nvs_tool_available():
            logger.error("Flash start requested but esp-idf-nvs-partition-gen is not installed")
            return jsonify(
                {"error": "NVS generator not installed (esp-idf-nvs-partition-gen)"}
            ), 503

        body = request.get_json(silent=True) or {}
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

        config: dict = {
            "wifi_ssid1": wifi_ssid1,
            "wifi_pass1": body.get("wifi_pass1") or "",
            "image_url": image_url,
            "wifi_order": int(body.get("wifi_order") or 0),
        }
        ssid2 = (body.get("wifi_ssid2") or "").strip()
        if ssid2:
            config["wifi_ssid2"] = ssid2
            config["wifi_pass2"] = body.get("wifi_pass2") or ""
        if screen_name:
            config["screen_name"] = screen_name

        job_id = state.flash_jobs.start(port, config, firmware_path)
        if job_id is None:
            logger.warning("Flash start rejected: a flash is already in progress")
            return jsonify({"error": "a flash is already in progress"}), 409

        # Persist WiFi credentials so the form pre-fills next time.
        wifi_pass1 = body.get("wifi_pass1") or ""
        wifi_pass2 = body.get("wifi_pass2") or ""
        if config_path:
            cfg = state.config
            if (
                wifi_ssid1 != cfg.flash_wifi_ssid
                or wifi_pass1 != cfg.flash_wifi_pass
                or ssid2 != cfg.flash_wifi_ssid2
                or wifi_pass2 != cfg.flash_wifi_pass2
            ):
                try:
                    new_cfg = replace(
                        cfg,
                        flash_wifi_ssid=wifi_ssid1,
                        flash_wifi_pass=wifi_pass1,
                        flash_wifi_ssid2=ssid2,
                        flash_wifi_pass2=wifi_pass2,
                    )
                    new_cfg.save(config_path)
                    with state._lock:
                        state.config = new_cfg
                except Exception as e:
                    logger.warning("Could not persist WiFi credentials to config: %s", e)

        return jsonify({"job_id": job_id})

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
