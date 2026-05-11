"""Integration tests: full request/response cycle through the Flask app.

Each test wires up a real ImageManager + ServeScheduler + AppState + Flask
app over a temp directory, uploads a real source image from images/test/,
runs sync() to dither it (noop kernel → fast), then exercises the
/hokku/screen/ endpoint and verifies that:

  - the response is 200 with the correct Content-Type
  - the binary payload is exactly TOTAL_BYTES long
  - all expected firmware headers are present
  - the payload contains only valid palette indices (0–5)

A second test class spins up a real Werkzeug HTTP server so that
tools.screen_sim.fetch_screen() (the real urllib path) can be exercised
end-to-end, including --download mode.
"""
from __future__ import annotations

import shutil
import socket
import threading
import time
from pathlib import Path

import pytest

from hokku_server.app_state import AppState, build_manager
from hokku_server.app_config import AppConfig
from hokku_server.display import TOTAL_BYTES, panel_bytes_to_indices
from hokku_server.flask_app import create_app
from hokku_server.image_classifier import ImageClassifier
from hokku_server.serve_scheduler import ServeScheduler

# Path to a real source image (small enough to be fast with noop dither).
_TEST_IMAGE = (
    Path(__file__).resolve().parent.parent.parent
    / "images" / "test" / "Fitz_Roy_1.jpg"
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_state(config: AppConfig) -> AppState:
    clf = ImageClassifier(config)
    mgr = build_manager(config, clf)
    sch = ServeScheduler(mgr)
    return AppState(config, clf, mgr, sch)


def _upload_and_sync(state: AppState, src: Path) -> str:
    """Copy *src* into upload_dir and run sync(). Returns the filename."""
    dest = Path(state.config.upload_dir) / src.name
    shutil.copy(src, dest)
    state.manager.sync()
    state.manager.wait_for_idle()  # inline pool fires callbacks synchronously, but flush DB
    return src.name


def _free_port() -> int:
    """Find an unused TCP port on localhost."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Flask test-client tests ───────────────────────────────────────────────────

@pytest.fixture
def live_state(app_config: AppConfig) -> AppState:
    return _make_state(app_config)


@pytest.fixture
def live_client(live_state: AppState, tmp_path: Path):
    """Flask test client with a real image already dithered."""
    name = _upload_and_sync(live_state, _TEST_IMAGE)
    app = create_app(live_state, config_path=tmp_path / "config.json", template_folder=None)
    app.config["TESTING"] = True
    return app.test_client(), live_state, name


def test_serve_binary_status_200(live_client):
    client, _, _ = live_client
    resp = client.get(
        "/hokku/screen/",
        headers={"X-Screen-Name": "test-screen"},
    )
    assert resp.status_code == 200


def test_serve_binary_content_type(live_client):
    client, _, _ = live_client
    resp = client.get(
        "/hokku/screen/",
        headers={"X-Screen-Name": "test-screen"},
    )
    assert resp.headers["Content-Type"] == "application/octet-stream"


def test_serve_binary_exact_size(live_client):
    client, _, _ = live_client
    resp = client.get(
        "/hokku/screen/",
        headers={"X-Screen-Name": "test-screen"},
    )
    assert len(resp.data) == TOTAL_BYTES, (
        f"Expected {TOTAL_BYTES} bytes, got {len(resp.data)}"
    )


def test_serve_binary_valid_palette_indices(live_client):
    """Every nibble in the payload must map to a known palette index (0–5)."""
    client, _, _ = live_client
    resp = client.get(
        "/hokku/screen/",
        headers={"X-Screen-Name": "test-screen"},
    )
    assert resp.status_code == 200
    indices = panel_bytes_to_indices(resp.data)
    assert indices.min() >= 0
    assert indices.max() <= 5


def test_serve_binary_x_sleep_seconds_header(live_client):
    client, _, _ = live_client
    resp = client.get(
        "/hokku/screen/",
        headers={"X-Screen-Name": "test-screen"},
    )
    assert "X-Sleep-Seconds" in resp.headers
    assert int(resp.headers["X-Sleep-Seconds"]) > 0


def test_serve_binary_x_server_time_epoch_header(live_client):
    client, _, _ = live_client
    resp = client.get(
        "/hokku/screen/",
        headers={"X-Screen-Name": "test-screen"},
    )
    assert "X-Server-Time-Epoch" in resp.headers
    epoch = int(resp.headers["X-Server-Time-Epoch"])
    # Sanity: epoch is somewhere in the 2020s.
    assert epoch > 1_600_000_000


def test_serve_binary_content_disposition_header(live_client):
    client, _, _ = live_client
    resp = client.get(
        "/hokku/screen/",
        headers={"X-Screen-Name": "test-screen"},
    )
    cd = resp.headers.get("Content-Disposition", "")
    assert "hokku.bin" in cd


def test_serve_binary_firmware_headers_forwarded(live_client):
    """Battery and frame-state headers from the 'firmware' should be recorded
    without causing an error response."""
    client, state, _ = live_client
    resp = client.get(
        "/hokku/screen/",
        headers={
            "X-Screen-Name": "fw-screen",
            "X-Battery-mV": "3800",
            "X-Frame-State": "USB_AWAKE",
        },
    )
    assert resp.status_code == 200
    # The screen should now appear in scheduler telemetry.
    screens = state.scheduler.screens()
    assert "fw-screen" in screens
    assert screens["fw-screen"].battery_mv == 3800


def test_clear_cache_triggers_immediate_reconversion(live_client):
    """/api/clear_cache must start rendering without waiting for the next watcher tick.

    With _InlineRenderPool, sync() is effectively synchronous, so by the time
    the POST response arrives the images should already be converted again.
    """
    client, state, _ = live_client
    # Verify images are already converted after initial upload+sync.
    prog_before = state.manager.conversion_progress()
    assert prog_before.done == prog_before.total

    # Clear — this marks everything pending.
    resp = client.post("/hokku/api/clear_cache")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    # Because _InlineRenderPool fires callbacks synchronously, the sync()
    # called inside api_clear_cache() re-converts everything inline.
    # wait_for_idle() flushes the _DbSaver timer so the DB is up-to-date.
    state.manager.wait_for_idle()
    prog_after = state.manager.conversion_progress()
    assert prog_after.done == prog_after.total, (
        "Images should be reconverted immediately after /api/clear_cache, "
        "not waiting for the next watcher tick"
    )


def test_api_status_200_and_shape(live_client):
    """/api/status must return 200 with the expected top-level keys.

    Regression for AttributeError when flask_app.py referenced a field that
    was removed from the Observations dataclass (e.g. has_face after face
    detection was dropped).  Any attribute access on obs that doesn't exist
    will blow up here rather than silently on the Pi.
    """
    client, _, name = live_client
    resp = client.get("/hokku/api/status")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.data[:200]}"

    data = resp.get_json()
    assert data is not None, "Response was not valid JSON"

    # Top-level structure
    assert "upload_files" in data
    assert "failed_files" in data
    assert "serve_data" in data

    # Per-file entries: check every field referenced in flask_app is present
    # and that no stale fields from removed features are referenced.
    for entry in data["upload_files"]:
        assert "name" in entry
        assert "dithered" in entry
        assert "is_bw" in entry          # classifier observation — must exist
        assert "has_face" not in entry   # removed with face detection


def test_serve_binary_no_images_returns_404_or_503(app_config: AppConfig, tmp_path: Path):
    """With no images uploaded the endpoint must return 503 (not 200)."""
    state = _make_state(app_config)
    # Do NOT upload any images.
    state.manager.sync()
    app = create_app(state, config_path=tmp_path / "config.json", template_folder=None)
    app.config["TESTING"] = True
    client = app.test_client()
    resp = client.get("/hokku/screen/", headers={"X-Screen-Name": "empty"})
    assert resp.status_code in (404, 503)
    assert "X-Sleep-Seconds" in resp.headers


# ── Real HTTP server + screen_sim.fetch_screen() ─────────────────────────────

@pytest.fixture
def http_server(app_config: AppConfig, tmp_path: Path):
    """Spin up a real Werkzeug HTTP server in a background thread.

    Yields (base_url, state) and tears down the server after the test.
    """
    from werkzeug.serving import make_server

    state = _make_state(app_config)
    _upload_and_sync(state, _TEST_IMAGE)

    app = create_app(state, config_path=tmp_path / "config.json", template_folder=None)

    port = _free_port()
    server = make_server("127.0.0.1", port, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Give the server a moment to accept connections.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)

    yield f"http://127.0.0.1:{port}", state

    server.shutdown()


def test_fetch_screen_returns_correct_size(http_server):
    """fetch_screen() over a real HTTP connection returns TOTAL_BYTES."""
    from tools.screen_sim import fetch_screen

    base_url, _ = http_server
    data, headers = fetch_screen(base_url, "sim-screen")
    assert len(data) == TOTAL_BYTES


def test_fetch_screen_headers_present(http_server):
    """fetch_screen() returns the expected firmware-facing headers."""
    from tools.screen_sim import fetch_screen

    base_url, _ = http_server
    _, headers = fetch_screen(base_url, "sim-screen")
    assert "x-sleep-seconds" in headers
    assert "x-server-time-epoch" in headers
    assert "content-type" in headers
    assert headers["content-type"] == "application/octet-stream"


def test_fetch_screen_valid_palette_indices(http_server):
    """Payload from the real server contains only valid palette nibbles."""
    from tools.screen_sim import fetch_screen

    base_url, _ = http_server
    data, _ = fetch_screen(base_url, "sim-screen")
    indices = panel_bytes_to_indices(data)
    assert indices.min() >= 0
    assert indices.max() <= 5


def test_fetch_screen_download_mode(http_server, tmp_path: Path):
    """Saving the binary to disk via fetch_screen() + write reproduces the
    exact bytes that a real screen would receive."""
    from tools.screen_sim import fetch_screen

    base_url, _ = http_server
    data, _ = fetch_screen(base_url, "dl-screen")

    out = tmp_path / "downloaded.bin"
    out.write_bytes(data)

    assert out.exists()
    assert out.stat().st_size == TOTAL_BYTES
    assert out.read_bytes() == data


def test_fetch_screen_battery_header_forwarded(http_server):
    """X-Battery-mV sent by the sim reaches the server's telemetry."""
    from tools.screen_sim import fetch_screen

    base_url, state = http_server
    fetch_screen(base_url, "battery-sim", battery_mv=3750)
    screens = state.scheduler.screens()
    assert "battery-sim" in screens
    assert screens["battery-sim"].battery_mv == 3750
