"""System smoke test: the real server boots and serves.

Launches the actual entrypoint — ``python -m hokku.webserver <config>`` — as a
subprocess against the committed ``images/test/`` set and a temp cache dir on a
free port, then checks that ``/hokku/api/status`` responds and a rendered panel
frame is served. This catches "the server process doesn't even start / bind /
render" regressions that the in-process Flask-test-client tests can't see.

Marked ``slow`` (real process boot + numba warmup + a render pass); it still runs
in the default CI suite (only ``time_intensive``/``serial`` are excluded there).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from hokku.webserver.app_config import AppConfig

_REPO = Path(__file__).resolve().parents[2]
_TEST_IMAGES = _REPO / "images" / "test"


def test_committed_dev_config_is_valid():
    """The committed test_server/config.json parses and points at real images, so
    ``hokku-server test_server/config.json`` (and the boot test below) actually work.
    Fast + hermetic — no subprocess, so it guards the committed file on every run."""
    cfg = AppConfig.load(_REPO / "test_server" / "config.json")
    assert cfg.port == 8080
    # upload_dir is repo-relative ("images/test") — resolve it from the repo root.
    upload = _REPO / cfg.upload_dir
    assert upload.is_dir(), f"committed upload_dir does not resolve: {upload}"
    assert any(upload.iterdir()), "committed upload_dir has no images"
    assert cfg.image_config_default is not None  # defaults materialised from the schema


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get_json(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


@pytest.mark.slow
def test_server_boots_and_serves(tmp_path):
    assert _TEST_IMAGES.is_dir(), f"missing committed test images: {_TEST_IMAGES}"
    cache = tmp_path / "cache"
    cache.mkdir()
    port = _free_port()
    config = {
        "version": 7,
        "upload_dir": str(_TEST_IMAGES),
        "cache_dir": str(cache),
        "port": port,
        "poll_interval_seconds": 2,
        "debug_fast_refresh": True,
        # image configs fall back to AppConfig defaults; no secrets, no mDNS.
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))

    # Ensure `hokku` is importable whether or not it's pip-installed in this env.
    env = {**os.environ, "PYTHONPATH": str(_REPO / "python")}
    log_path = tmp_path / "server.log"
    base = f"http://127.0.0.1:{port}"

    with open(log_path, "w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "hokku.webserver", str(config_path)],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(_REPO),
        )

    def _log_tail() -> str:
        return log_path.read_text(encoding="utf-8", errors="replace")[-2000:]

    try:
        # 1) The process boots and the status endpoint responds.
        status = None
        deadline = time.time() + 90
        while time.time() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"server exited early (rc={proc.returncode}):\n{_log_tail()}")
            try:
                status = _get_json(f"{base}/hokku/api/status")
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                time.sleep(1)
        assert status is not None, f"server never came up:\n{_log_tail()}"
        assert "bundled_firmware_versions" in status

        # 2) It renders + serves a real panel frame (960 KB for the reference model).
        served = False
        deadline = time.time() + 150
        while time.time() < deadline:
            if proc.poll() is not None:
                raise AssertionError(
                    f"server died while rendering (rc={proc.returncode}):\n{_log_tail()}"
                )
            req = urllib.request.Request(
                f"{base}/hokku/screen/",
                headers={"X-Screen-Name": "smoke", "X-Screen-Model": "huessen_epf1301"},
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    if len(r.read()) == 960000:
                        served = True
                        break
            except urllib.error.HTTPError as e:
                if e.code != 503:  # 503 = still converting; keep waiting
                    raise
            time.sleep(2)
        assert served, f"server booted but never served a frame:\n{_log_tail()}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
