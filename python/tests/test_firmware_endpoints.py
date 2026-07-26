"""The firmware-library HTTP API: /library, /remote, /fetch, /select.

GitHub is stubbed at ``firmware_github.available_firmware`` / ``download`` so the
endpoint logic is exercised without the network. Bundled state is stubbed via
``firmware_registry``. The download path uses the Bigme F7 ``.img`` so the
admission validator (AWIH magic) accepts the stubbed bytes.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from hokku.screens import firmware_registry
from hokku.webserver import firmware_github
from hokku.webserver.app_config import AppConfig
from hokku.webserver.app_state import AppState, build_manager
from hokku.webserver.firmware_github import RemoteFirmware
from hokku.webserver.flask_app import create_app
from hokku.webserver.image_classifier import ImageClassifier
from hokku.webserver.serve_scheduler import ServeScheduler

MODEL = "bigme_f7"


def _client(config: AppConfig, tmp_path: Path):
    clf = ImageClassifier(config)
    mgr = build_manager(config, clf)
    state = AppState(config, clf, mgr, ServeScheduler(mgr))
    app = create_app(state, config_path=tmp_path / "cfg.json", template_folder=None)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def cfg(app_config, tmp_path):
    fw = tmp_path / "fwdir"
    return replace(app_config, firmware_dir=str(fw))


@pytest.fixture(autouse=True)
def bundled(monkeypatch):
    """Bigme F7 has a bundled 1.2.2; the ESP32 models have none (keeps it simple)."""
    monkeypatch.setattr(
        firmware_registry,
        "firmware_version_for",
        lambda m: "1.2.2" if m == MODEL else None,
    )
    monkeypatch.setattr(
        firmware_registry,
        "release_app_image_for",
        lambda m: b"BUNDLED" if m == MODEL else None,
    )
    monkeypatch.setattr(firmware_registry, "list_bundled_firmware", lambda m: [])


def _remote_list():
    return [
        RemoteFirmware(
            MODEL, "1.3.0", "v4.0.0", False, "hokku-bigme_f7-1.3.0.img", "https://x/b130", 1_000_000
        ),
        RemoteFirmware(
            MODEL,
            "1.4.0",
            "v4.1.0-beta1",
            True,
            "hokku-bigme_f7-1.4.0.img",
            "https://x/b140",
            1_050_000,
        ),
    ]


# ── /library ──────────────────────────────────────────────────────────────────


def test_library_reports_bundled_effective(cfg, tmp_path):
    body = _client(cfg, tmp_path).get("/hokku/api/firmware/library").get_json()
    assert body["repo"] == cfg.firmware_github_repo
    m = body["models"][MODEL]
    assert m["effective"] == "1.2.2"
    assert m["effective_channel"] == "stable"
    assert m["pinned"] is None


# ── /select ───────────────────────────────────────────────────────────────────


def test_select_pin_and_clear(cfg, tmp_path):
    client = _client(cfg, tmp_path)
    # Pin the bundled version (it is present in the library).
    r = client.post("/hokku/api/firmware/select", json={"model": MODEL, "version": "1.2.2"})
    assert r.status_code == 200 and r.get_json()["pinned"] == "1.2.2"
    assert (
        client.get("/hokku/api/firmware/library").get_json()["models"][MODEL]["pinned"] == "1.2.2"
    )
    # Clear it.
    r = client.post("/hokku/api/firmware/select", json={"model": MODEL, "version": None})
    assert r.status_code == 200 and r.get_json()["pinned"] is None


def test_select_unknown_model_400(cfg, tmp_path):
    r = _client(cfg, tmp_path).post(
        "/hokku/api/firmware/select", json={"model": "nope", "version": "1"}
    )
    assert r.status_code == 400


def test_select_version_not_present_404(cfg, tmp_path):
    r = _client(cfg, tmp_path).post(
        "/hokku/api/firmware/select", json={"model": MODEL, "version": "9.9.9"}
    )
    assert r.status_code == 404


# ── /remote ───────────────────────────────────────────────────────────────────


def test_remote_lists_stable_only_by_default(cfg, tmp_path, monkeypatch):
    seen = {}

    def _avail(repo, *, include_prereleases):
        seen["pre"] = include_prereleases
        return [r for r in _remote_list() if include_prereleases or not r.prerelease]

    monkeypatch.setattr(firmware_github, "available_firmware", _avail)
    body = _client(cfg, tmp_path).get("/hokku/api/firmware/remote").get_json()
    assert seen["pre"] is False
    versions = {r["version"] for r in body["releases"]}
    assert versions == {"1.3.0"}
    assert body["releases"][0]["downloaded"] is False


def test_remote_includes_prereleases_when_asked(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(
        firmware_github,
        "available_firmware",
        lambda repo, *, include_prereleases: _remote_list() if include_prereleases else [],
    )
    body = _client(cfg, tmp_path).get("/hokku/api/firmware/remote?prereleases=1").get_json()
    versions = {r["version"] for r in body["releases"]}
    assert versions == {"1.3.0", "1.4.0"}
    beta = next(r for r in body["releases"] if r["version"] == "1.4.0")
    assert beta["channel"] == "beta" and beta["prerelease"] is True


def test_remote_propagates_fetch_error(cfg, tmp_path, monkeypatch):
    def _boom(repo, *, include_prereleases):
        raise firmware_github.FirmwareFetchError("could not reach GitHub")

    monkeypatch.setattr(firmware_github, "available_firmware", _boom)
    r = _client(cfg, tmp_path).get("/hokku/api/firmware/remote")
    assert r.status_code == 502
    assert "GitHub" in r.get_json()["error"]


# ── /fetch ────────────────────────────────────────────────────────────────────


def test_fetch_unknown_model_400(cfg, tmp_path):
    r = _client(cfg, tmp_path).post(
        "/hokku/api/firmware/fetch", json={"model": "nope", "tag": "v1"}
    )
    assert r.status_code == 400


def test_fetch_no_match_404(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(
        firmware_github, "available_firmware", lambda repo, *, include_prereleases: _remote_list()
    )
    r = _client(cfg, tmp_path).post(
        "/hokku/api/firmware/fetch", json={"model": MODEL, "tag": "v9.9.9"}
    )
    assert r.status_code == 404


def test_fetch_downloads_and_adds_to_library(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(
        firmware_github, "available_firmware", lambda repo, *, include_prereleases: _remote_list()
    )
    monkeypatch.setattr(firmware_github, "download", lambda remote: b"AWIH" + b"\x00" * 200)
    client = _client(cfg, tmp_path)

    r = client.post("/hokku/api/firmware/fetch", json={"model": MODEL, "tag": "v4.1.0-beta1"})
    assert r.status_code == 200
    j = r.get_json()
    assert j["version"] == "1.4.0" and j["channel"] == "beta"

    # It now shows in the library as a downloaded beta — but is NOT effective yet.
    lib = client.get("/hokku/api/firmware/library").get_json()["models"][MODEL]
    assert lib["effective"] == "1.2.2"  # beta not auto-selected
    dl = {v["version"]: v for v in lib["available"]}
    assert dl["1.4.0"]["source"] == "downloaded" and dl["1.4.0"]["channel"] == "beta"

    # Pinning it makes it effective.
    client.post("/hokku/api/firmware/select", json={"model": MODEL, "version": "1.4.0"})
    lib = client.get("/hokku/api/firmware/library").get_json()["models"][MODEL]
    assert lib["effective"] == "1.4.0" and lib["pinned"] == "1.4.0"


def test_fetch_rejects_corrupt_download(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(
        firmware_github, "available_firmware", lambda repo, *, include_prereleases: _remote_list()
    )
    # No AWIH magic -> validation rejects it.
    monkeypatch.setattr(firmware_github, "download", lambda remote: b"GARBAGE")
    r = _client(cfg, tmp_path).post(
        "/hokku/api/firmware/fetch", json={"model": MODEL, "tag": "v4.0.0"}
    )
    assert r.status_code == 422
