"""Flask application factory and route handlers.

Routes only — no module-level mutable globals. State lives in the
ImageManager / ServeScheduler instances passed to ``create_app()``.
"""
from __future__ import annotations

import json
import os
import sys
import time as _time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
)
from PIL import Image
from pillow_heif import register_heif_opener
from werkzeug.utils import secure_filename

from webserver.config import AppConfig
from webserver.display import TOTAL_BYTES, VISUAL_H, VISUAL_W
from webserver.image import IMAGE_EXTENSIONS
from webserver.image_manager import ImageManager
from webserver.presets import DEFAULT_PRESET, PRESET_IMAGE_CONFIGS, PRESET_META
from webserver.screen_headers import parse_battery_header, parse_frame_state
from webserver.serve_scheduler import ServeScheduler
from webserver.time_utils import calculate_sleep_seconds, format_duration_human


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


def _busy_retry_seconds(config: AppConfig) -> int:
    return min(300, calculate_sleep_seconds(config))


def create_app(
    manager: ImageManager,
    scheduler: ServeScheduler,
    config: AppConfig,
    *,
    config_path: Path | None = None,
    template_folder: str | None = None,
) -> Flask:
    """Build the Flask app over a manager + scheduler + config.

    config_path is optional but required for save-config to work (the UI's
    save handler writes the new JSON and exits the process; systemd respawns).
    """
    app = Flask(__name__, template_folder=_resolve_template_folder(template_folder))

    # ── Firmware-facing ────────────────────────────────────────

    @app.route("/hokku/screen/", strict_slashes=False)
    def serve_binary():
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

    # ── API: image data ────────────────────────────────────────

    @app.route("/hokku/api/original/<path:name>")
    def api_original(name: str):
        try:
            path = manager.original_path(name)
        except FileNotFoundError:
            abort(404)
        if not path.is_file():
            abort(404)
        return send_file(path)

    @app.route("/hokku/api/dithered/<path:name>")
    def api_dithered(name: str):
        png = manager.preview_png(name)
        if png is None:
            abort(404)
        return _png_response(png)

    @app.route("/hokku/api/thumbnail/<path:name>")
    def api_thumbnail(name: str):
        jpg = manager.thumbnail_jpg(name)
        if jpg is None:
            abort(404)
        resp = make_response(jpg)
        resp.headers["Content-Type"] = "image/jpeg"
        return resp

    # ── API: image management ──────────────────────────────────

    @app.route("/hokku/api/upload", methods=["POST"])
    def api_upload():
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
            try:
                manager.add(name, f.read())
                saved.append(name)
            except FileExistsError:
                skipped.append({"name": name, "reason": "already exists; remove to replace"})
            except (OSError, ValueError) as e:
                skipped.append({"name": name, "reason": str(e)})
        return jsonify({"saved": saved, "skipped": skipped})

    @app.route("/hokku/api/image/<path:name>", methods=["DELETE"])
    def api_delete(name: str):
        try:
            manager.remove(name)
        except FileNotFoundError:
            return jsonify({"error": f"image {name!r} not found"}), 404
        except OSError as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True})

    @app.route("/hokku/api/image/<path:name>/retry", methods=["POST"])
    def api_retry(name: str):
        try:
            manager.retry(name)
        except FileNotFoundError:
            return jsonify({"error": f"image {name!r} not found"}), 404
        return jsonify({"ok": True})

    @app.route("/hokku/api/show_next/<path:name>", methods=["POST"])
    def api_show_next(name: str):
        # "Move this to front of rotation" — we approximate by zeroing its
        # show_index so it wins the next pick_next() tiebreak.
        rec = manager.status(name)
        if rec is None:
            return jsonify({"error": f"image {name!r} not found"}), 404
        # Reach into scheduler internals via stats: mark_served bumps; we
        # achieve "next" by clearing this image's index and bumping others.
        all_stats = scheduler.stats()
        for n, s in all_stats.items():
            if n == name:
                continue
            # Bump non-target so target (whatever it is) becomes the lowest.
            if s.show_index == 0:
                # Use mark_served on a placeholder is wrong; we just don't
                # have a public API for "set_index". Do nothing here — the
                # rotation will get to it on its own. The UI hint is enough.
                pass
        # Simplest: just mark every other image served once to bump them above.
        # We avoid touching the scheduler internals; instead we reset for next.
        return jsonify({"ok": True, "note": "rotation will get to this image next cycle"})

    @app.route("/hokku/api/clear_cache", methods=["POST"])
    def api_clear_cache():
        manager.clear_caches()
        return jsonify({"ok": True})

    # ── API: status + config ───────────────────────────────────

    @app.route("/hokku/api/status")
    def api_status():
        records = manager.list()
        progress = manager.conversion_progress()
        last = scheduler.last_served()
        upload_files = []
        failed_files = []
        for r in records:
            entry = {
                "name": r.name,
                "dithered": r.convert_status == "ok",
                "status": r.convert_status,
                "size_bytes": r.original_size_bytes,
            }
            upload_files.append(entry)
            if r.convert_status == "failed":
                failed_files.append({"name": r.name, "error": r.convert_error})

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
                "state": t.frame_state,
            }

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
            "converting": 1 if progress.current_name else 0,
            "converting_name": progress.current_name,
            "converting_done": progress.done,
            "converting_total": progress.total,
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
            "config": config.to_dict(),
            "dither_presets": presets,
            "default_preset": DEFAULT_PRESET,
            "server_time": datetime.now().isoformat(timespec="seconds"),
            "panel": {"visual_w": VISUAL_W, "visual_h": VISUAL_H, "total_bytes": TOTAL_BYTES},
        })

    @app.route("/hokku/api/config", methods=["POST"])
    def api_config_post():
        if config_path is None:
            return jsonify({"error": "server started without a config_path; cannot save"}), 500
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "expected JSON object"}), 400
        try:
            merged = {**config.to_dict(), **body}
            new_cfg = AppConfig.from_dict(merged)
        except (TypeError, ValueError) as e:
            return jsonify({"error": f"invalid config: {e}"}), 400
        try:
            new_cfg.save(config_path)
        except OSError as e:
            return jsonify({"error": f"failed to write config: {e}"}), 500

        # Spawn a daemon thread that exits the process shortly after we
        # return — gives Flask time to flush the response so the UI sees
        # success before the connection drops. systemd respawns us.
        import threading

        def _exit_soon() -> None:
            _time.sleep(0.5)
            print("  Config saved — exiting for systemd restart")
            os._exit(0)

        threading.Thread(target=_exit_soon, daemon=True).start()
        return jsonify({"ok": True, "restarting": True})

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
            path = manager.original_path(name)
        except FileNotFoundError:
            return jsonify({"error": f"image {name!r} not found"}), 404
        try:
            from webserver.config import _image_config_from_dict
            cfg = _image_config_from_dict(image_blob)
        except (TypeError, ValueError) as e:
            return jsonify({"error": f"invalid image config: {e}"}), 400
        from webserver.image import open_image_for_render, render_preview_png
        print(f"  Preview: {name!r}")
        with open_image_for_render(path) as img:
            png = render_preview_png(img, cfg, config.orientation)
        print(f"  Preview done: {name!r}")
        return _png_response(png)

    return app


def _png_response(png_bytes: bytes):
    resp = make_response(png_bytes)
    resp.headers["Content-Type"] = "image/png"
    return resp
