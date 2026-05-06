"""Flask application: routes, pool sync, and process entrypoint."""
import os
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime
from io import BytesIO
from pathlib import Path

from flask import Flask, abort, jsonify, make_response, redirect, render_template, request, send_file
from PIL import Image
from werkzeug.utils import secure_filename
from pillow_heif import register_heif_opener

from webserver.config import AppConfig, DEFAULT_CONFIG
from webserver.display_constants import (
    PANEL_BYTES,
    TOTAL_BYTES,
    VISUAL_H,
    VISUAL_W,
)
from webserver.image import (
    IMAGE_EXTENSIONS,
    PRESET_DISPLAY_IMAGE_CONFIGS,
    merge_display_image,
    merge_image,
    prepare_panel_canvas,
    preset_name_for_display,
    image_config_from_legacy_fat_dither_dict,
)
from webserver.image_manager import ImageManager
from webserver.serve_data import ServeDataStore, pick_next_image, _save_database
from webserver.screen_headers import _parse_battery_header, _parse_frame_state
from webserver.telemetry import ScreenTelemetry
from webserver.time import (
    DEBUG_FAST_REFRESH_SECONDS,
    calculate_sleep_seconds as _calculate_sleep_seconds,
    format_duration_human,
)
from webserver.watcher import DirectoryWatcher

register_heif_opener()

_pkg_root = Path(__file__).resolve().parent.parent
_template_dirs = [
    str(_pkg_root / "templates"),
    "/usr/share/hokku-server/templates",
]
_template_folder = next((d for d in _template_dirs if os.path.isdir(d)), str(_pkg_root / "templates"))
app = Flask(__name__, template_folder=_template_folder)

_lock = threading.Lock()
_pool = {}
_last_served = {"key": None, "name": None, "served_at": None}
_converting_count = 0
_converting_name = None
_converting_total = 0
_converting_done = 0
_config = AppConfig()
_database = {"serve_data": {}}

_sync_lock = threading.Lock()
_sync_state_lock = threading.Lock()
_sync_pending = False

def _get_upload_dir():
    return Path(_config.upload_dir)


def _get_cache_dir():
    return Path(_config.cache_dir)


_image_manager: ImageManager | None = None
_serve_store = ServeDataStore(_get_cache_dir)
_telemetry = ScreenTelemetry()


def _reload_image_manager() -> None:
    global _image_manager
    _image_manager = ImageManager(_config)


def _get_image_manager() -> ImageManager:
    if _image_manager is None:
        _reload_image_manager()
    assert _image_manager is not None
    return _image_manager


def _list_images():
    upload_dir = _get_upload_dir()
    if not upload_dir.exists():
        return []
    return sorted(
        [f for f in upload_dir.iterdir() if f.suffix.lower() in IMAGE_EXTENSIONS and f.is_file()],
        key=lambda p: p.name.lower(),
    )


def _read_cached_binary(img_path, content_hash):
    del content_hash  # pool metadata only; paths are stem + display slug
    return _get_image_manager().read_panel_bin(Path(img_path))


def _read_cached_preview(img_path, content_hash):
    del content_hash
    return _get_image_manager().read_preview_png(Path(img_path))


def _clear_cache_files():
    _get_image_manager().clear_managed_caches()


def _pick_next_image(pool, db):
    return pick_next_image(pool, db, _last_served)


def _prepare_canvas(img):
    return prepare_panel_canvas(img, _config.display)


def _thumb_path_for(img_path):
    return _get_image_manager().path_thumb_jpg(Path(img_path))


def _ensure_thumbnail(img_path):
    return _get_image_manager().ensure_thumb_jpg(Path(img_path))


def _record_screen_call(screen_name, screen_ip, sleep_seconds, served_name=None,
                        battery_mv=None, frame_state=None):
    _telemetry.record(
        _database, screen_name, screen_ip, sleep_seconds,
        served_name=served_name, battery_mv=battery_mv, frame_state=frame_state,
    )


def _busy_retry_seconds():
    return min(300, _calculate_sleep_seconds(_config))


def _convert_and_store(img_path, content_hash):
    m = _get_image_manager()
    try:
        if m.panel_cache_hit(Path(img_path)):
            print(f"  Cache hit: {img_path.name}")
        else:
            def on_begin(name: str) -> None:
                global _converting_count, _converting_name
                with _lock:
                    _converting_count += 1
                    _converting_name = name

            def on_end() -> None:
                global _converting_count, _converting_name
                with _lock:
                    _converting_count -= 1
                    if _converting_count == 0:
                        _converting_name = None

            m.materialize_panel_cache(
                Path(img_path),
                on_convert_begin=on_begin,
                on_convert_end=on_end,
            )

        with _lock:
            _pool[str(img_path)] = {"hash": content_hash}
        print(f"  Pool: {img_path.name} ready ({len(_pool)} total)")
    except Exception as e:
        print(f"  Error converting {img_path.name}: {e}")


def _sync_pool():
    global _sync_pending
    with _sync_state_lock:
        if not _sync_lock.acquire(blocking=False):
            _sync_pending = True
            print("  Sync already running, queued rerun")
            return
        _sync_pending = False
    try:
        while True:
            _sync_pool_inner()
            with _sync_state_lock:
                if not _sync_pending:
                    break
                _sync_pending = False
    finally:
        _sync_lock.release()


def _sync_pool_inner():
    global _converting_total, _converting_done
    m = _get_image_manager()
    m.refresh_image_files()
    image_paths = list(m.upload_paths)
    current_paths = set()

    for img_path in image_paths:
        if not _thumb_path_for(img_path).exists():
            _ensure_thumbnail(img_path)

    to_convert = []
    for img_path in image_paths:
        key = str(img_path)
        current_paths.add(key)
        content_hash = m.sha1_hex(img_path)
        with _lock:
            existing = _pool.get(key)
            if existing and existing["hash"] == content_hash:
                continue
        to_convert.append((img_path, content_hash))

    with _lock:
        _converting_total = len(to_convert)
        _converting_done = 0

    for img_path, content_hash in to_convert:
        print(f"  Processing: {img_path.name}")
        _convert_and_store(img_path, content_hash)
        with _lock:
            _converting_done += 1

    with _lock:
        _converting_total = 0
        _converting_done = 0

    with _lock:
        for key in list(_pool.keys()):
            if key not in current_paths:
                del _pool[key]
                print(f"  Pool: removed deleted file {key}")


_BROWSER_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif"}


@app.route("/hokku/screen/", strict_slashes=False)
def serve_binary():
    screen_name = request.headers.get("X-Screen-Name", "unnamed")
    screen_ip = request.remote_addr or "unknown"
    battery_mv = _parse_battery_header(request.headers.get("X-Battery-mV"))
    frame_state = _parse_frame_state(request.headers.get("X-Frame-State"))

    serve_log = None

    with _lock:
        if not _pool:
            sleep_seconds = _busy_retry_seconds()
            _record_screen_call(screen_name, screen_ip, sleep_seconds,
                                battery_mv=battery_mv, frame_state=frame_state)
            _save_database(_get_cache_dir(), _database)
            status = 503 if _converting_count > 0 else 404
            msg = "Converting images, try again shortly" if status == 503 else "No images in upload directory"
            resp = make_response(msg, status)
            resp.headers["X-Sleep-Seconds"] = str(sleep_seconds)
            print(f"  Busy: {screen_name} told to retry in {sleep_seconds}s (status={status})")
            return resp

        key = _pick_next_image(_pool, _database)
        if key is None:
            sleep_seconds = _busy_retry_seconds()
            _record_screen_call(screen_name, screen_ip, sleep_seconds,
                                battery_mv=battery_mv, frame_state=frame_state)
            _save_database(_get_cache_dir(), _database)
            resp = make_response("No images available", 404)
            resp.headers["X-Sleep-Seconds"] = str(sleep_seconds)
            return resp

        entry = _pool[key]
        content_hash = entry["hash"]

        sleep_seconds = _calculate_sleep_seconds(_config)
        _record_screen_call(screen_name, screen_ip, sleep_seconds,
                            served_name=Path(key).name, battery_mv=battery_mv,
                            frame_state=frame_state)
        _save_database(_get_cache_dir(), _database)

        served_name = Path(key).name
        sd = _database["serve_data"]
        idx = sd[served_name]["show_index"]
        pool_names = [Path(k).name for k in _pool]
        at_idx = sum(1 for n in pool_names if sd[n]["show_index"] == idx)
        higher = [sd[n]["show_index"] for n in pool_names if sd[n]["show_index"] > idx]
        next_up = min(higher) if higher else None
        serve_log = (idx, at_idx - 1, next_up)

    binary = _read_cached_binary(Path(key), content_hash)
    if binary is None:
        resp = make_response("Cached binary missing, try again shortly", 503)
        resp.headers["X-Sleep-Seconds"] = str(_busy_retry_seconds())
        return resp

    _last_served["key"] = key
    _last_served["name"] = Path(key).name
    _last_served["served_at"] = datetime.now().isoformat(timespec="seconds")

    log_idx, log_peers, log_next = serve_log
    next_s = str(log_next) if log_next is not None else "-"
    print(
        f"  Serving: {Path(key).name} to {screen_name} (sleep_seconds={sleep_seconds}) "
        f"show_index={log_idx} peers_at_index={log_peers} next_up={next_s}"
    )

    response = make_response(binary)
    response.headers["Content-Type"] = "application/octet-stream"
    response.headers["X-Sleep-Seconds"] = str(sleep_seconds)
    response.headers["X-Server-Time-Epoch"] = str(int(time.time()))
    response.headers["Content-Disposition"] = "attachment; filename=hokku.bin"
    return response


@app.route("/")
def root():
    return redirect("/hokku/ui")


@app.route("/hokku/ui")
def web_gui():
    return render_template("index.html")


@app.route("/hokku/api/status")
def api_status():
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(_config.timezone)
        now_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _lock:
        pool_files = sorted(Path(k).name for k in _pool.keys())
        pool_set = set(pool_files)
        serve_data = _database.get("serve_data", {})

        all_upload = sorted(p.name for p in _list_images())
        upload_files = [{"name": n, "dithered": n in pool_set} for n in all_upload]

        enriched = {}
        for fname in pool_files:
            entry = serve_data.get(fname, {})
            enriched[fname] = {
                "show_index": entry.get("show_index", 0),
                "last_request": entry.get("last_request"),
                "total_show_count": entry.get("total_show_count", 0),
                "total_show_minutes": entry.get("total_show_minutes", 0.0),
                "total_show_formatted": format_duration_human(entry.get("total_show_minutes", 0.0)),
            }

        screens = _database.get("screens", {})

        return jsonify({
            "pool_files": pool_files,
            "upload_files": upload_files,
            "pool_size": len(_pool),
            "upload_size": len(upload_files),
            "serve_data": enriched,
            "screens": screens,
            "last_served": _last_served["name"],
            "converting": _converting_count,
            "converting_name": _converting_name,
            "converting_total": _converting_total,
            "converting_done": _converting_done,
            "server_time": now_str,
            "config": {
                "timezone": _config.timezone,
                "refresh_image_at_time": _config.refresh_image_at_time,
                "port": _config.port,
                "poll_interval_seconds": _config.poll_interval_seconds,
                "orientation": _config.display.orientation,
                "debug_fast_refresh": _config.debug_fast_refresh,
                "debug_fast_refresh_seconds": DEBUG_FAST_REFRESH_SECONDS,
                "image": asdict(_config.display.image),
                "image_preset": preset_name_for_display(_config.display),
                "upload_dir": str(_get_upload_dir()),
                "cache_dir": str(_get_cache_dir()),
            },
        })


@app.route("/hokku/api/original/<filename>")
def api_original(filename):
    img_path = _get_upload_dir() / filename
    if not img_path.exists() or not img_path.is_file():
        abort(404)
    if img_path.suffix.lower() in _BROWSER_IMAGE_EXTS:
        return send_file(img_path)
    try:
        from PIL import ImageOps
        img = Image.open(img_path)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=92)
        buf.seek(0)
        return send_file(buf, mimetype="image/jpeg")
    except Exception:
        return send_file(img_path)


@app.route("/hokku/api/thumbnail/<filename>")
def api_thumbnail(filename):
    img_path = _get_upload_dir() / filename
    if not img_path.exists() or not img_path.is_file():
        abort(404)
    thumb_path = _ensure_thumbnail(img_path)
    if thumb_path is None:
        abort(500)
    return send_file(thumb_path, mimetype="image/jpeg")


@app.route("/hokku/api/dithered/<filename>")
def api_dithered(filename):
    with _lock:
        match = None
        for key, entry in _pool.items():
            if Path(key).name == filename:
                match = (key, entry["hash"])
                break
    if match is None:
        abort(404)
    key, content_hash = match
    preview = _read_cached_preview(Path(key), content_hash)
    if preview is None:
        abort(404)
    return send_file(BytesIO(preview), mimetype="image/png")


@app.route("/hokku/api/show_next/<filename>", methods=["POST"])
def api_show_next(filename):
    with _lock:
        serve_data = _database.get("serve_data", {})
        if filename not in serve_data:
            return jsonify({"error": "Image not found in database"}), 404

        min_idx = min(e["show_index"] for e in serve_data.values()) if serve_data else 0
        serve_data[filename]["show_index"] = min_idx - 1

        _save_database(_get_cache_dir(), _database)

    return jsonify({"status": "ok", "filename": filename, "new_show_index": min_idx - 1})


@app.route("/hokku/api/config", methods=["POST"])
def api_config():
    global _config
    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    changed = False
    prev_orientation = _config.display.orientation
    if "timezone" in data:
        _config.timezone = data["timezone"]
        changed = True
    if "refresh_image_at_time" in data:
        _config.refresh_image_at_time = data["refresh_image_at_time"]
        changed = True
    if "poll_interval_seconds" in data:
        val = int(data["poll_interval_seconds"])
        if val >= 1:
            _config.poll_interval_seconds = val
            changed = True

    if "orientation" in data:
        val = data["orientation"]
        if val in ("landscape", "portrait"):
            _config.display = merge_display_image(_config.display, {"orientation": val})
            changed = True

    if "debug_fast_refresh" in data:
        _config.debug_fast_refresh = bool(data["debug_fast_refresh"])
        changed = True

    dither_changed = False
    if "image" in data and isinstance(data["image"], dict):
        new_disp = merge_display_image(_config.display, {"image": data["image"]})
        if new_disp.image != _config.display.image:
            dither_changed = True
        _config.display = new_disp
        changed = True
    elif "dither" in data and isinstance(data["dither"], dict):
        if "prepare_autocontrast_cutoff" in data["dither"]:
            new_i = image_config_from_legacy_fat_dither_dict(data["dither"])
        else:
            new_i = merge_image(_config.display.image, {"dither": data["dither"]})
        if new_i != _config.display.image:
            dither_changed = True
        _config.display = replace(_config.display, image=new_i)
        changed = True
    else:
        preset_key = data.get("dither_preset") or data.get("dither_algorithm")
        if preset_key is not None:
            if preset_key not in PRESET_DISPLAY_IMAGE_CONFIGS:
                return jsonify({"error": f"Unknown dither preset: {preset_key}"}), 400
            serp = (
                bool(data["dither_serpentine"])
                if "dither_serpentine" in data
                else _config.display.image.dither.serpentine
            )
            preset = PRESET_DISPLAY_IMAGE_CONFIGS[preset_key]
            new_i = replace(
                preset.image,
                dither=replace(preset.image.dither, serpentine=serp),
            )
            new_disp = replace(_config.display, image=new_i)
            if new_disp.image != _config.display.image:
                dither_changed = True
            _config.display = new_disp
            changed = True
        elif "dither_serpentine" in data:
            val = bool(data["dither_serpentine"])
            new_i = replace(
                _config.display.image,
                dither=replace(_config.display.image.dither, serpentine=val),
            )
            if new_i != _config.display.image:
                dither_changed = True
            _config.display = replace(_config.display, image=new_i)
            changed = True

    if changed:
        _config.save_to_file()

    orientation_changed = _config.display.orientation != prev_orientation
    if orientation_changed:
        _clear_cache_files()
        with _lock:
            _pool.clear()
        _reload_image_manager()
        threading.Thread(target=_sync_pool, daemon=True).start()
    elif dither_changed:
        with _lock:
            _pool.clear()
        _reload_image_manager()
        threading.Thread(target=_sync_pool, daemon=True).start()

    return jsonify({"status": "ok", "config": {
        "timezone": _config.timezone,
        "refresh_image_at_time": _config.refresh_image_at_time,
        "poll_interval_seconds": _config.poll_interval_seconds,
        "orientation": _config.display.orientation,
        "debug_fast_refresh": _config.debug_fast_refresh,
        "image": asdict(_config.display.image),
        "image_preset": preset_name_for_display(_config.display),
    }})


@app.route("/hokku/api/upload", methods=["POST"])
def api_upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files provided"}), 400

    upload_dir = _get_upload_dir()
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return jsonify({"error": f"Upload directory not writable ({upload_dir}): {e.strerror or e}"}), 500

    saved = []
    skipped = []
    for f in files:
        if not f or not f.filename:
            continue
        safe_name = secure_filename(f.filename)
        if not safe_name:
            skipped.append({"filename": f.filename, "reason": "invalid filename"})
            continue
        ext = Path(safe_name).suffix.lower()
        if ext not in IMAGE_EXTENSIONS:
            skipped.append({"filename": f.filename, "reason": f"unsupported type {ext}"})
            continue

        dest = upload_dir / safe_name
        if dest.exists():
            stem = dest.stem
            n = 1
            while dest.exists():
                dest = upload_dir / f"{stem}_{n}{ext}"
                n += 1

        try:
            f.save(dest)
        except OSError as e:
            print(f"  Upload error: {dest}: {e}")
            return jsonify({
                "error": f"Failed to save {dest.name}: {e.strerror or e}",
                "saved": saved,
                "skipped": skipped,
            }), 500
        saved.append(dest.name)
        print(f"  Upload: saved {dest.name}")

    if saved:
        _get_image_manager().refresh_image_files()
        threading.Thread(target=_sync_pool, daemon=True).start()

    return jsonify({"status": "ok", "saved": saved, "skipped": skipped})


@app.route("/hokku/api/image/<filename>", methods=["DELETE"])
def api_delete_image(filename):
    safe_name = secure_filename(filename)
    if not safe_name:
        return jsonify({"error": "Invalid filename"}), 400

    img_path = _get_upload_dir() / safe_name
    if not img_path.exists() or not img_path.is_file():
        return jsonify({"error": "Image not found"}), 404

    try:
        img_path.unlink()
    except OSError as e:
        print(f"  Delete error: {img_path}: {e}")
        return jsonify({"error": f"Failed to delete {safe_name}: {e.strerror or e}"}), 500
    print(f"  Delete: removed {safe_name}")

    _get_image_manager().refresh_image_files()
    threading.Thread(target=_sync_pool, daemon=True).start()

    return jsonify({"status": "ok", "deleted": safe_name})


@app.route("/hokku/api/clear_cache", methods=["POST"])
def api_clear_cache():
    _clear_cache_files()
    with _lock:
        _pool.clear()
    threading.Thread(target=_sync_pool, daemon=True).start()
    return jsonify({"status": "cache cleared, re-converting"})


@app.route("/hokku/api/time")
def api_time():
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(_config.timezone)
        now = datetime.now(tz)
    except Exception:
        now = datetime.now()
    return jsonify({
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": _config.timezone,
    })


def main():
    global _config, _database, _image_manager

    _config = AppConfig.load_from_file()
    upload_dir = _get_upload_dir()
    cache_dir = _get_cache_dir()
    upload_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    _database.clear()
    _database.update(_serve_store.load())

    _image_manager = None
    _reload_image_manager()

    port = _config.port
    poll = _config.poll_interval_seconds

    print(f"Hokku image server (full resolution: {VISUAL_W}x{VISUAL_H})")
    print(f"  Upload dir: {upload_dir}")
    print(f"  Cache dir:  {cache_dir}")
    print(f"  Timezone:   {_config.timezone}")
    print(f"  Refresh at: {_config.refresh_image_at_time}")
    print(f"  Poll interval: {poll}s")
    print(f"  Output: {TOTAL_BYTES} bytes per image ({PANEL_BYTES} per panel)")
    print(f"  Endpoints:")
    print(f"    GET /hokku/screen/      — 960K binary (fair rotation) + X-Sleep-Seconds header")
    print(f"    GET /hokku/ui           — Web GUI")

    images = _list_images()
    if images:
        print(f"  Found {len(images)} image(s), converting in background...")
    else:
        print(f"  No images found yet, waiting for uploads...")

    watcher = DirectoryWatcher(
        poll_interval_fn=lambda: _config.poll_interval_seconds,
        sync_fn=_sync_pool,
        sleep_fn=time.sleep,
    )
    threading.Thread(target=watcher.run_forever, daemon=True).start()

    print(f"  Starting server on port {port}...")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
