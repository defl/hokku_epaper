"""Flask application factory and route handlers.

Routes only — no module-level mutable globals. Live state lives in the
AppState instance passed to ``create_app()``. All route handlers read
``state.manager`` / ``state.scheduler`` / ``state.config`` at the start of
each request so they automatically pick up a hot-reloaded config.
"""
from __future__ import annotations

import io
import time as _time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from PIL import Image, UnidentifiedImageError

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
from pillow_heif import register_heif_opener
from werkzeug.utils import secure_filename

import os
import psutil
import subprocess
try:
    from importlib.metadata import version as _pkg_version, PackageNotFoundError
except ImportError:  # py<3.8 — not expected
    _pkg_version = None
    PackageNotFoundError = Exception

from hokku_server.app_state import AppState
from hokku_server.app_config import AppConfig
from hokku_server.display import TOTAL_BYTES, VISUAL_H, VISUAL_W
from hokku_server.dither_streaming_numba import NumbaStreamingDither
from hokku_server.image_config import _image_config_from_dict
from hokku_server.image_renderer import IMAGE_EXTENSIONS, ImageRenderer, MAX_UPLOAD_BYTES, MAX_UPLOAD_PIXELS, open_image_for_render
from hokku_server.presets import PRESET_IMAGE_CONFIGS, PRESET_META
from hokku_server.screen_headers import parse_battery_header, parse_frame_state
from hokku_server.time_utils import calculate_sleep_seconds, format_duration_human


register_heif_opener()


def _resolve_template_folder(override: str | None) -> str:
    if override:
        return override
    pkg_root = Path(__file__).resolve().parent.parent
    candidates = [
        pkg_root / "templates",
        Path("/usr/share/hokku-server/templates"),
    ]
    for c in candidates:
        if c.is_dir():
            return str(c)
    return str(candidates[0])  # default; flask will error if missing


def _resolve_static_folder() -> Path:
    """Locate the directory that holds /hokku/static/* assets.

    Dev: <webserver_root>/static/. Installed: /usr/share/hokku-server/static/.
    """
    pkg_root = Path(__file__).resolve().parent.parent
    candidates = [
        pkg_root / "static",
        Path("/usr/share/hokku-server/static"),
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


def _read_version() -> str:
    if _pkg_version is not None:
        try:
            return _pkg_version("hokku-server")
        except PackageNotFoundError:
            pass
    # Fallback: read pyproject.toml
    try:
        pyproj = Path(__file__).resolve().parent.parent / "pyproject.toml"
        for line in pyproj.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("version"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return "unknown"


def _read_commit() -> str | None:
    try:
        repo_root = Path(__file__).resolve().parent.parent.parent
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode("ascii").strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


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
    app_version = _read_version()
    app_commit = _read_commit()

    # ── Firmware-facing ────────────────────────────────────────

    @app.route("/hokku/screen/", strict_slashes=False)
    def serve_binary():
        manager = state.manager
        scheduler = state.scheduler
        config = state.config

        screen_name = request.headers.get("X-Screen-Name", "unnamed")
        screen_ip = request.remote_addr or "unknown"
        battery_mv = parse_battery_header(request.headers.get("X-Battery-mV"))
        frame_state = parse_frame_state(request.headers.get("X-Frame-State"))

        chosen = scheduler.pick_next()
        sleep_seconds = (
            calculate_sleep_seconds(config) if chosen else _busy_retry_seconds(config)
        )

        if chosen is None:
            progress = manager.conversion_progress()
            converting = progress.total > 0
            scheduler.record_screen_call(
                screen_name, screen_ip, sleep_seconds, None, battery_mv, frame_state,
            )
            if converting:
                msg, status, label = "Converting images, try again shortly", 503, "Converting"
            else:
                msg, status, label = "No images in upload directory", 404, "No images"
            resp = make_response(msg, status)
            resp.headers["X-Sleep-Seconds"] = str(sleep_seconds)
            print(f"  {label}: {screen_name} told to retry in {sleep_seconds}s")
            return resp

        binary = manager.panel_bytes(chosen)
        if binary is None:
            # Cache disappeared between pick and read — tell screen to retry.
            sleep_seconds = _busy_retry_seconds(config)
            scheduler.record_screen_call(
                screen_name, screen_ip, sleep_seconds, None, battery_mv, frame_state,
            )
            resp = make_response("Cached binary missing, try again shortly", 503)
            resp.headers["X-Sleep-Seconds"] = str(sleep_seconds)
            return resp

        scheduler.mark_served(chosen)
        scheduler.record_screen_call(
            screen_name, screen_ip, sleep_seconds, chosen, battery_mv, frame_state,
        )
        print(f"  Serving: {chosen} to {screen_name} (sleep_seconds={sleep_seconds})")

        response = make_response(binary)
        response.headers["Content-Type"] = "application/octet-stream"
        response.headers["X-Sleep-Seconds"] = str(sleep_seconds)
        response.headers["X-Server-Time-Epoch"] = str(int(_time.time()))
        response.headers["Content-Disposition"] = "attachment; filename=hokku.bin"
        return response

    # ── Web GUI ────────────────────────────────────────────────

    @app.route("/")
    def root():
        return redirect("/hokku/ui")

    @app.route("/hokku/ui")
    def web_gui():
        return render_template("index.html")

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
            try:
                with Image.open(io.BytesIO(data)) as probe:
                    w, h = probe.size
            except Image.DecompressionBombError:
                skipped.append({
                    "name": name,
                    "reason": f"image too large; cap {MAX_UPLOAD_PIXELS:,} px",
                })
                continue
            except (UnidentifiedImageError, OSError):
                skipped.append({"name": name, "reason": "unreadable image"})
                continue
            if w * h > MAX_UPLOAD_PIXELS:
                skipped.append({
                    "name": name,
                    "reason": f"image too large ({w}x{h}); cap {MAX_UPLOAD_PIXELS:,} px",
                })
                continue
            try:
                manager.add(name, data)
                saved.append(name)
            except FileExistsError:
                skipped.append({"name": name, "reason": "already exists; remove to replace"})
            except (OSError, ValueError) as e:
                skipped.append({"name": name, "reason": str(e)})
        return jsonify({"saved": saved, "skipped": skipped})

    @app.route("/hokku/api/image/<path:name>", methods=["DELETE"])
    def api_delete(name: str):
        try:
            state.manager.remove(name)
        except FileNotFoundError:
            return jsonify({"error": f"image {name!r} not found"}), 404
        except OSError as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True})

    @app.route("/hokku/api/image/<path:name>/retry", methods=["POST"])
    def api_retry(name: str):
        try:
            state.manager.retry(name)
        except FileNotFoundError:
            return jsonify({"error": f"image {name!r} not found"}), 404
        return jsonify({"ok": True})

    @app.route("/hokku/api/show_next/<path:name>", methods=["POST"])
    def api_show_next(name: str):
        rec = state.manager.status(name)
        if rec is None:
            return jsonify({"error": f"image {name!r} not found"}), 404
        if rec.convert_status != "ok":
            return jsonify({"error": f"image {name!r} is not ready (status: {rec.convert_status})"}), 409
        try:
            state.scheduler.set_next(name)
        except ValueError as e:
            return jsonify({"error": str(e)}), 409
        return jsonify({"ok": True, "next_image": name})

    @app.route("/hokku/api/clear_cache", methods=["POST"])
    def api_clear_cache():
        state.manager.clear_caches()
        state.manager.sync()  # kick off reconversion immediately
        return jsonify({"ok": True})

    @app.route("/hokku/api/screens/<string:name>", methods=["DELETE"])
    def api_screen_delete(name: str):
        """Remove a screen's telemetry and serve-stats records.

        The screen will re-appear automatically the next time it connects.
        """
        state.scheduler.remove_screen(name)
        return jsonify({"ok": True})

    @app.route("/hokku/api/scrub", methods=["POST"])
    def api_scrub():
        """Remove stale-slug panel/preview files immediately (preserves thumbs)."""
        state.manager.scrub_stale_cache()
        return jsonify({"ok": True})

    @app.route("/hokku/api/classifier/clear", methods=["POST"])
    def api_classifier_clear():
        """Wipe all cached classifier observations (is_bw / has_face).

        Deletes image_classifier.json. The next sync will re-run detection
        on every image. Already-rendered panel .bin files are NOT touched —
        they are keyed by ScreenImageConfig slug and remain valid unless the
        classification result changes.
        """
        state.classifier.clear_cache()
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
                "dithered": r.convert_status == "ok",
                "status": r.convert_status,
                "error": r.convert_error,
                "size_bytes": r.original_size_bytes,
                "image_width": r.image_width,
                "image_height": r.image_height,
                "last_conversion_seconds": r.last_conversion_seconds,
                "is_bw": obs.is_bw if obs else None,
                "has_face": obs.has_face if obs else None,
            }
            upload_files.append(entry)
            if r.convert_status == "failed":
                failed_files.append({"name": r.name, "error": r.convert_error, "size_bytes": r.original_size_bytes})

        ready_count = sum(1 for r in records if r.convert_status == "ok")
        serve_data: dict[str, dict] = {}
        for n, s in scheduler.stats().items():
            serve_data[n] = {
                "show_index": s.show_index,
                "last_request": (
                    datetime.fromtimestamp(s.last_served_at).isoformat(timespec="seconds")
                    if s.last_served_at else None
                ),
                "total_show_count": s.total_show_count,
                "total_show_minutes": s.total_show_minutes,
                "total_show_formatted": format_duration_human(s.total_show_minutes),
            }

        screens_payload: dict[str, dict] = {}
        for sname, t in scheduler.screens().items():
            next_update_at = None
            if t.last_seen_at and t.last_sleep_seconds:
                next_update_at = datetime.fromtimestamp(
                    t.last_seen_at + t.last_sleep_seconds
                ).isoformat(timespec="seconds")
            screens_payload[sname] = {
                "ip": t.ip,
                "request_count": t.request_count,
                "last_seen": (
                    datetime.fromtimestamp(t.last_seen_at).isoformat(timespec="seconds")
                    if t.last_seen_at else None
                ),
                "last_sleep_seconds": t.last_sleep_seconds,
                "last_served": t.last_served,
                "battery_mv": t.battery_mv,
                "battery_percent": t.battery_percent,
                "battery_seen_at": (
                    datetime.fromtimestamp(t.battery_seen_at).isoformat(timespec="seconds")
                    if t.battery_seen_at else None
                ),
                "next_update_at": next_update_at,
                "state": t.frame_state,
            }

        disk = manager.cache_disk_info()
        return jsonify({
            "server_time": datetime.now().isoformat(timespec="seconds"),
            "upload_size": len(records),
            "pool_size": ready_count,
            "pool_files": [r.name for r in records if r.convert_status == "ok"],
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
            "next_image": scheduler.peek_next(),
            "cache_used_bytes": disk["cache_used_bytes"],
            "disk_free_bytes": disk["disk_free_bytes"],
            "image_worker_count_resolved": state.manager.resolved_worker_count,
            "cpu_cores": os.cpu_count(),
            "memory_available_gb": round(psutil.virtual_memory().available / 1e9, 1),
        })

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
        return jsonify({
            "config": state.config.to_dict(),
            "config_defaults": AppConfig().to_dict(),
            "dither_presets": presets,
            "server_time": datetime.now().isoformat(timespec="seconds"),
            "panel": {"visual_w": VISUAL_W, "visual_h": VISUAL_H, "total_bytes": TOTAL_BYTES},
            "version": app_version,
            "commit": app_commit,
            "repo_url": _REPO_URL,
        })

    @app.route("/hokku/api/config", methods=["POST"])
    def api_config_post():
        if config_path is None:
            return jsonify({"error": "server started without a config_path; cannot save"}), 500
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "expected JSON object"}), 400
        try:
            merged = {**state.config.to_dict(), **body}
            new_cfg = AppConfig.from_dict(merged)
        except (TypeError, ValueError) as e:
            return jsonify({"error": f"invalid config: {e}"}), 400
        try:
            new_cfg.save(config_path)
        except OSError as e:
            return jsonify({"error": f"failed to write config: {e}"}), 500
        try:
            state.reload(new_cfg)
        except ValueError as e:
            return jsonify({"error": f"reload failed: {e}"}), 400
        print("  Config saved and reloaded in-process")
        return jsonify({"ok": True, "restarting": False})

    @app.route("/hokku/api/dither/preview", methods=["POST"])
    def api_dither_preview():
        """Render a one-off dithered preview for a given image + image_config.

        Body: {name: str, image: ImageConfig dict}. Returns PNG bytes.
        """
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "expected JSON object"}), 400
        name = body.get("name")
        image_blob = body.get("image")
        if not name or not isinstance(image_blob, dict):
            return jsonify({"error": "expected {name, image}"}), 400
        try:
            path = state.manager.original_path(name)
        except FileNotFoundError:
            return jsonify({"error": f"image {name!r} not found"}), 404
        try:
            cfg = _image_config_from_dict(image_blob)
        except (TypeError, ValueError) as e:
            return jsonify({"error": f"invalid image config: {e}"}), 400
        print(f"  Preview: {name!r}")
        with open_image_for_render(path) as img:
            png = ImageRenderer(NumbaStreamingDither()).render_preview_png(img, cfg, state.config.orientation)
        print(f"  Preview done: {name!r}")
        return _png_response(png)

    return app


def _png_response(png_bytes: bytes):
    resp = make_response(png_bytes)
    resp.headers["Content-Type"] = "image/png"
    return resp
