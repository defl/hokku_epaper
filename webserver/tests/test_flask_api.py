"""Comprehensive tests for all Flask API routes.

Covers routes that test_integration.py does not exercise:
  /hokku/api/upload              POST  — every test image incl. the oversized bomb
  /hokku/api/image/<name>        DELETE
  /hokku/api/image/<name>/retry  POST
  /hokku/api/show_next/<name>    POST
  /hokku/api/status              GET   — all fields, including face_bboxes not has_face
  /hokku/api/config              GET + POST
  /hokku/api/dither/preview      POST
  /hokku/api/thumbnail/<name>    GET
  /hokku/api/original/<name>     GET
  /hokku/api/dithered/<name>     GET
  /hokku/api/clear_cache         POST
  /hokku/api/scrub               POST
  /hokku/api/classifier/clear    POST
  /hokku/api/screens/<name>      DELETE
  /                              GET   — redirect
  /hokku/ui                      GET   — HTML
"""
from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from hokku_server.app_config import AppConfig
from hokku_server.app_state import AppState, build_manager
from hokku_server.flask_app import create_app
from hokku_server.image_classifier import ImageClassifier
from hokku_server.serve_scheduler import ServeScheduler
from hokku_server.presets import PRESET_IMAGE_CONFIGS
from hokku_server.image_renderer import MAX_UPLOAD_PIXELS

# ── paths ─────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_IMAGES_DIR = _REPO_ROOT / "images" / "test"

# All files in images/test/ — each will be uploaded in a parametrized test.
# CREDITS.md is deliberately included to verify it is rejected on extension.
# synth_black_10000x10000.png is the intentional "bomb" (100 M px > 40 M cap).
_ALL_TEST_FILES: list[Path] = sorted(
    p for p in _TEST_IMAGES_DIR.iterdir() if p.is_file()
)

# Files expected to land in "skipped" rather than "saved".
_EXPECTED_SKIP: dict[str, str] = {
    "CREDITS.md": "unsupported extension",
    "NewTux.svg": "unsupported extension",
    "synth_black_10000x10000.png": "too large",
}


# ── fixtures ──────────────────────────────────────────────────────────────────

def _make_state(config: AppConfig) -> AppState:
    clf = ImageClassifier(config)
    mgr = build_manager(config, clf)
    sch = ServeScheduler(mgr)
    return AppState(config, clf, mgr, sch)


@pytest.fixture
def bare_state(app_config: AppConfig) -> AppState:
    """AppState with no images uploaded."""
    return _make_state(app_config)


@pytest.fixture
def bare_client(bare_state: AppState, tmp_path: Path):
    """Flask test client backed by an empty upload directory."""
    app = create_app(bare_state, config_path=tmp_path / "cfg.json", template_folder=None)
    app.config["TESTING"] = True
    return app.test_client(), bare_state


@pytest.fixture
def synced_client(app_config: AppConfig, tmp_path: Path):
    """Flask test client with one small image already uploaded and dithered."""
    src = _TEST_IMAGES_DIR / "grayscale_linear_bar_1200x300.png"
    assert src.exists(), f"Test image missing: {src}"
    state = _make_state(app_config)
    dest = Path(app_config.upload_dir) / src.name
    shutil.copy(src, dest)
    state.manager.sync()
    state.manager.wait_for_idle()
    app = create_app(state, config_path=tmp_path / "cfg.json", template_folder=None)
    app.config["TESTING"] = True
    return app.test_client(), state, src.name


def _upload_bytes(client, data: bytes, filename: str):
    """POST multipart/form-data to /hokku/api/upload."""
    return client.post(
        "/hokku/api/upload",
        data={"file": (io.BytesIO(data), filename)},
        content_type="multipart/form-data",
    )


# ── /hokku/api/upload — every test image ──────────────────────────────────────

@pytest.mark.parametrize("img_path", _ALL_TEST_FILES, ids=lambda p: p.name)
def test_upload_each_test_file(bare_client, img_path: Path):
    """Upload every file in images/test/.

    Known-bad files must appear in 'skipped' with a reason; all valid images
    must appear in 'saved'.  The endpoint must always return HTTP 200 — errors
    are reported in the JSON body, never as 4xx/5xx.
    """
    client, _ = bare_client
    data = img_path.read_bytes()
    resp = _upload_bytes(client, data, img_path.name)

    assert resp.status_code == 200, f"Upload returned {resp.status_code}: {resp.data[:200]}"
    body = resp.get_json()
    assert "saved" in body and "skipped" in body

    expected_reason = _EXPECTED_SKIP.get(img_path.name)
    if expected_reason:
        names_skipped = [s["name"] for s in body["skipped"]]
        assert img_path.name in names_skipped, (
            f"{img_path.name!r} should be in skipped; got saved={body['saved']}, "
            f"skipped={body['skipped']}"
        )
        # Reason string must contain the expected keyword.
        skip_entry = next(s for s in body["skipped"] if s["name"] == img_path.name)
        assert expected_reason in skip_entry["reason"], (
            f"Skip reason for {img_path.name!r} should mention {expected_reason!r}: "
            f"{skip_entry['reason']!r}"
        )
    else:
        assert img_path.name in body["saved"], (
            f"{img_path.name!r} should be saved; got saved={body['saved']}, "
            f"skipped={body['skipped']}"
        )
        assert body["skipped"] == []


def test_upload_no_files_returns_400(bare_client):
    client, _ = bare_client
    resp = client.post("/hokku/api/upload", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400


def test_upload_duplicate_is_skipped(bare_client):
    """Uploading the same image twice: second upload must be in skipped."""
    client, _ = bare_client
    img = _TEST_IMAGES_DIR / "grayscale_linear_bar_1200x300.png"
    data = img.read_bytes()
    r1 = _upload_bytes(client, data, img.name)
    assert img.name in r1.get_json()["saved"]

    r2 = _upload_bytes(client, data, img.name)
    body2 = r2.get_json()
    assert r2.status_code == 200
    assert img.name in [s["name"] for s in body2["skipped"]]
    assert "already exists" in body2["skipped"][0]["reason"]


def test_upload_bad_extension_is_skipped(bare_client):
    client, _ = bare_client
    resp = _upload_bytes(client, b"not an image", "photo.xyz")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["saved"] == []
    assert body["skipped"][0]["reason"].startswith("unsupported extension")


# ── /hokku/api/image/<name> DELETE ────────────────────────────────────────────

def test_delete_existing_image(bare_client):
    client, state = bare_client
    img = _TEST_IMAGES_DIR / "grayscale_linear_bar_1200x300.png"
    _upload_bytes(client, img.read_bytes(), img.name)

    resp = client.delete(f"/hokku/api/image/{img.name}")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_delete_missing_image_returns_404(bare_client):
    client, _ = bare_client
    resp = client.delete("/hokku/api/image/does_not_exist.jpg")
    assert resp.status_code == 404


# ── /hokku/api/image/<name>/retry POST ────────────────────────────────────────

def test_retry_existing_image_returns_ok(bare_client):
    client, _ = bare_client
    img = _TEST_IMAGES_DIR / "grayscale_linear_bar_1200x300.png"
    _upload_bytes(client, img.read_bytes(), img.name)
    resp = client.post(f"/hokku/api/image/{img.name}/retry")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_retry_missing_image_returns_404(bare_client):
    client, _ = bare_client
    resp = client.post("/hokku/api/image/ghost.jpg/retry")
    assert resp.status_code == 404


# ── /hokku/api/show_next/<name> POST ──────────────────────────────────────────

def test_show_next_ready_image(synced_client):
    client, state, name = synced_client
    resp = client.post(f"/hokku/api/show_next/{name}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["next_image"] == name


def test_show_next_missing_image_returns_404(synced_client):
    client, _, _ = synced_client
    resp = client.post("/hokku/api/show_next/no_such_file.jpg")
    assert resp.status_code == 404


def test_show_next_not_ready_returns_409(bare_client):
    """Uploading without syncing leaves status != 'ok' → 409."""
    client, _ = bare_client
    img = _TEST_IMAGES_DIR / "grayscale_linear_bar_1200x300.png"
    _upload_bytes(client, img.read_bytes(), img.name)
    # No sync → convert_status is 'pending' or 'converting'
    resp = client.post(f"/hokku/api/show_next/{img.name}")
    assert resp.status_code == 409


# ── /hokku/api/status GET ─────────────────────────────────────────────────────

def test_status_empty_upload(bare_client):
    client, _ = bare_client
    resp = client.get("/hokku/api/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["upload_size"] == 0
    assert data["pool_size"] == 0
    assert data["upload_files"] == []


def test_status_top_level_keys(synced_client):
    client, _, _ = synced_client
    data = client.get("/hokku/api/status").get_json()
    for key in (
        "server_time", "upload_size", "pool_size", "pool_files",
        "upload_files", "failed_files", "serve_data", "screens",
        "last_served", "converting", "converting_name", "converting_done",
        "converting_total", "converting_eta_seconds", "next_image",
        "cache_used_bytes", "disk_free_bytes", "image_worker_count_resolved",
        "cpu_cores", "memory_available_gb",
    ):
        assert key in data, f"Missing key {key!r} in /api/status response"


def test_status_upload_file_fields(synced_client):
    """Every entry in upload_files must have the expected per-file fields,
    including face_bboxes (not the removed has_face)."""
    client, _, name = synced_client
    data = client.get("/hokku/api/status").get_json()
    entries = {e["name"]: e for e in data["upload_files"]}
    assert name in entries
    entry = entries[name]
    for field in (
        "name", "dithered", "status", "error",
        "size_bytes", "image_width", "image_height",
        "last_conversion_seconds", "is_bw", "face_bboxes",
    ):
        assert field in entry, f"Missing field {field!r} in upload_file entry"
    assert "has_face" not in entry, (
        "has_face was removed — use face_bboxes instead; "
        "the flask_app still references it"
    )
    assert isinstance(entry["face_bboxes"], list)


def test_status_ready_image_is_in_pool(synced_client):
    client, _, name = synced_client
    data = client.get("/hokku/api/status").get_json()
    assert name in data["pool_files"]
    assert data["pool_size"] >= 1


# ── /hokku/api/config GET ─────────────────────────────────────────────────────

def test_config_get_returns_200(bare_client):
    client, _ = bare_client
    assert client.get("/hokku/api/config").status_code == 200


def test_config_get_top_level_keys(bare_client):
    client, _ = bare_client
    data = client.get("/hokku/api/config").get_json()
    for key in ("config", "config_defaults", "dither_presets", "panel", "version"):
        assert key in data, f"Missing key {key!r} in /api/config response"


def test_config_get_panel_dims(bare_client):
    client, _ = bare_client
    panel = client.get("/hokku/api/config").get_json()["panel"]
    assert panel["visual_w"] > 0
    assert panel["visual_h"] > 0
    assert panel["total_bytes"] > 0


def test_config_get_presets_non_empty(bare_client):
    client, _ = bare_client
    presets = client.get("/hokku/api/config").get_json()["dither_presets"]
    assert len(presets) > 0
    # Each preset must have label and description.
    for name, p in presets.items():
        assert "label" in p, f"Preset {name!r} missing 'label'"
        assert "description" in p, f"Preset {name!r} missing 'description'"


# ── /hokku/api/config POST ────────────────────────────────────────────────────

def test_config_post_valid_field(bare_client, tmp_path):
    client, _ = bare_client
    resp = client.post(
        "/hokku/api/config",
        json={"poll_interval_seconds": 30},
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_config_post_non_json_returns_400(bare_client):
    client, _ = bare_client
    resp = client.post("/hokku/api/config", data="not json", content_type="text/plain")
    assert resp.status_code == 400


def test_config_post_invalid_nested_config_returns_400(bare_client):
    """Passing a string where AppConfig expects a nested ImageConfig dict must 400."""
    client, _ = bare_client
    resp = client.post("/hokku/api/config", json={"image_config_default": "not_a_dict"})
    assert resp.status_code == 400


def test_config_post_without_config_path_returns_500(bare_state: AppState):
    """create_app with config_path=None makes POST /api/config return 500."""
    app = create_app(bare_state, config_path=None, template_folder=None)
    app.config["TESTING"] = True
    client = app.test_client()
    resp = client.post("/hokku/api/config", json={"poll_interval_seconds": 10})
    assert resp.status_code == 500


# ── /hokku/api/dither/preview POST ───────────────────────────────────────────

def test_dither_preview_returns_png(synced_client):
    client, _, name = synced_client
    # Use the atkinson preset dict as the image config body.
    from dataclasses import asdict
    img_cfg = asdict(PRESET_IMAGE_CONFIGS["atkinson"])
    resp = client.post(
        "/hokku/api/dither/preview",
        json={"name": name, "image": img_cfg},
    )
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "image/png"
    assert resp.data[:4] == b"\x89PNG", "Response body is not a PNG"


def test_dither_preview_face_bboxes_header_present(synced_client):
    client, _, name = synced_client
    from dataclasses import asdict
    img_cfg = asdict(PRESET_IMAGE_CONFIGS["atkinson"])
    resp = client.post(
        "/hokku/api/dither/preview",
        json={"name": name, "image": img_cfg},
    )
    assert resp.status_code == 200
    assert "X-Face-Bboxes" in resp.headers
    bboxes = json.loads(resp.headers["X-Face-Bboxes"])
    assert isinstance(bboxes, list)


def test_dither_preview_missing_image_returns_404(bare_client):
    client, _ = bare_client
    from dataclasses import asdict
    resp = client.post(
        "/hokku/api/dither/preview",
        json={"name": "ghost.jpg", "image": asdict(PRESET_IMAGE_CONFIGS["atkinson"])},
    )
    assert resp.status_code == 404


def test_dither_preview_missing_name_returns_400(bare_client):
    client, _ = bare_client
    resp = client.post("/hokku/api/dither/preview", json={"image": {}})
    assert resp.status_code == 400


def test_dither_preview_non_json_body_returns_400(bare_client):
    client, _ = bare_client
    resp = client.post(
        "/hokku/api/dither/preview",
        data="not json",
        content_type="text/plain",
    )
    assert resp.status_code == 400


# ── /hokku/api/thumbnail/<name> GET ──────────────────────────────────────────

def test_thumbnail_existing_image_returns_jpeg(synced_client):
    client, _, name = synced_client
    resp = client.get(f"/hokku/api/thumbnail/{name}")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "image/jpeg"
    assert len(resp.data) > 0


def test_thumbnail_missing_image_returns_404(bare_client):
    client, _ = bare_client
    resp = client.get("/hokku/api/thumbnail/ghost.jpg")
    assert resp.status_code == 404


# ── /hokku/api/dithered/<name> GET ───────────────────────────────────────────

def test_dithered_existing_image_returns_png(synced_client):
    client, _, name = synced_client
    resp = client.get(f"/hokku/api/dithered/{name}")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "image/png"
    assert resp.data[:4] == b"\x89PNG"


def test_dithered_missing_image_returns_404(bare_client):
    client, _ = bare_client
    resp = client.get("/hokku/api/dithered/ghost.jpg")
    assert resp.status_code == 404


# ── /hokku/api/original/<name> GET ───────────────────────────────────────────

def test_original_existing_image_returns_file(synced_client):
    client, _, name = synced_client
    resp = client.get(f"/hokku/api/original/{name}")
    assert resp.status_code == 200
    assert len(resp.data) > 0


def test_original_missing_image_returns_404(bare_client):
    client, _ = bare_client
    resp = client.get("/hokku/api/original/ghost.jpg")
    assert resp.status_code == 404


# ── /hokku/api/clear_cache POST ──────────────────────────────────────────────

def test_clear_cache_returns_ok(bare_client):
    client, _ = bare_client
    resp = client.post("/hokku/api/clear_cache")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


# ── /hokku/api/scrub POST ────────────────────────────────────────────────────

def test_scrub_returns_ok(bare_client):
    client, _ = bare_client
    resp = client.post("/hokku/api/scrub")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


# ── /hokku/api/classifier/clear POST ─────────────────────────────────────────

def test_classifier_clear_returns_ok(bare_client):
    client, _ = bare_client
    resp = client.post("/hokku/api/classifier/clear")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


# ── /hokku/api/screens/<name> DELETE ─────────────────────────────────────────

def test_screen_delete_returns_ok(bare_client):
    """Deleting a screen that was never registered must still return ok."""
    client, _ = bare_client
    resp = client.delete("/hokku/api/screens/my-screen")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_screen_delete_removes_from_telemetry(bare_client):
    """After recording a screen call, deleting it removes it from telemetry."""
    client, state = bare_client
    # Simulate the screen checking in by hitting /hokku/screen/
    client.get("/hokku/screen/", headers={"X-Screen-Name": "frame-1"})
    assert "frame-1" in state.scheduler.screens()

    client.delete("/hokku/api/screens/frame-1")
    assert "frame-1" not in state.scheduler.screens()


# ── navigation ────────────────────────────────────────────────────────────────

def test_root_redirects_to_ui(bare_client):
    client, _ = bare_client
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (301, 302, 308)
    assert "/hokku/ui" in resp.headers["Location"]


def test_ui_returns_html(bare_client):
    client, _ = bare_client
    resp = client.get("/hokku/ui")
    assert resp.status_code == 200
    assert b"<!DOCTYPE html>" in resp.data or b"<html" in resp.data
