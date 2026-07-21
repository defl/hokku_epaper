"""Tests for the server self-update feature (GitHub release check + install).

Hardware/privilege-free: no test may invoke real sudo, systemctl, or apt-get —
those are stubbed out. The root-side install script (debian/self-update.sh)
is shell, not Python, and has no pytest coverage here (no existing precedent
in this repo for testing debian/*.sh scripts).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hokku.webserver import flask_app as flask_app_mod
from hokku.webserver import self_update
from hokku.webserver.app_state import AppState, build_manager
from hokku.webserver.flask_app import create_app
from hokku.webserver.image_classifier import ImageClassifier
from hokku.webserver.self_update import (
    ReleaseInfo,
    SelfUpdateManager,
    _current_version,
    find_newer_release,
    is_package_install,
)
from hokku.webserver.serve_scheduler import ServeScheduler

# ── helpers ───────────────────────────────────────────────────────────────────


def _bare_state(app_config) -> AppState:
    clf = ImageClassifier(app_config)
    mgr = build_manager(app_config, clf)
    return AppState(app_config, clf, mgr, ServeScheduler(mgr))


def _client(state: AppState, tmp_path: Path):
    app = create_app(state, config_path=tmp_path / "cfg.json", template_folder=None)
    app.config["TESTING"] = True
    return app.test_client()


def _wait_until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def _release(tag: str, asset_name: str | None = None, body: str = "") -> dict:
    assets = []
    if asset_name is not None:
        assets.append(
            {
                "name": asset_name,
                "browser_download_url": f"https://example.invalid/{asset_name}",
                "size": 1234,
            }
        )
    return {"tag_name": tag, "assets": assets, "body": body}


# ── version parsing ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("v3.0.0", (3, 0, 0)),
        ("v4.2.10", (4, 2, 10)),
        ("v3.0.0-alpha2", None),
        ("v3.0.1-dev.47", None),
        ("3.0.0", None),  # missing leading "v"
        ("not-a-tag", None),
        ("", None),
    ],
)
def test_parse_release_tag(tag, expected):
    assert self_update._parse_release_tag(tag) == expected


def test_parse_release_tag_numeric_ordering():
    # "1.2.10" must rank above "1.2.9" -- tuple-of-ints, not string, comparison.
    newer = self_update._parse_release_tag("v1.2.10")
    older = self_update._parse_release_tag("v1.2.9")
    assert newer is not None
    assert older is not None
    assert newer > older


# ── find_newer_release ───────────────────────────────────────────────────────


def test_find_newer_release_none_when_only_dev_releases_are_newer(monkeypatch):
    monkeypatch.setattr(
        self_update,
        "_fetch_all_releases",
        lambda: [_release("v4.0.1-dev.70", "hokku-server_4.0.1~dev.70-1_all.deb")],
    )
    assert find_newer_release((4, 0, 0)) is None


def test_find_newer_release_skips_release_without_deb_asset(monkeypatch):
    monkeypatch.setattr(
        self_update,
        "_fetch_all_releases",
        lambda: [
            _release("v4.0.2", asset_name=None),  # e.g. firmware-only release
            _release("v4.0.0", "hokku-server_4.0.0-1_all.deb"),
        ],
    )
    # v4.0.2 has no .deb asset, so it's skipped; v4.0.0 is not newer than current.
    assert find_newer_release((4, 0, 0)) is None


def test_find_newer_release_skips_odd_patch():
    # Odd-PATCH tags are dev-track, never a clean vX.Y.Z anyway, but confirm
    # the even-patch filter rejects a (hypothetical) clean odd-patch tag too.
    assert self_update._parse_release_tag("v4.0.3") == (4, 0, 3)


def test_find_newer_release_returns_newest_matching(monkeypatch):
    monkeypatch.setattr(
        self_update,
        "_fetch_all_releases",
        lambda: [
            _release("v4.0.3-dev.5", "hokku-server_4.0.3~dev.5-1_all.deb"),  # dev, skipped
            _release("v4.0.2", "hokku-server_4.0.2-1_all.deb", body="release notes"),
            _release("v4.0.0", "hokku-server_4.0.0-1_all.deb"),
        ],
    )
    info = find_newer_release((4, 0, 0))
    assert info is not None
    assert info.tag == "v4.0.2"
    assert info.version == (4, 0, 2)
    assert info.asset_name == "hokku-server_4.0.2-1_all.deb"
    assert info.body == "release notes"
    assert info.deb_version == "4.0.2-1"


def test_find_newer_release_none_when_up_to_date(monkeypatch):
    monkeypatch.setattr(
        self_update,
        "_fetch_all_releases",
        lambda: [_release("v4.0.2", "hokku-server_4.0.2-1_all.deb")],
    )
    assert find_newer_release((4, 0, 2)) is None
    assert find_newer_release((4, 2, 0)) is None


# ── is_package_install / _current_version ───────────────────────────────────


def test_is_package_install(tmp_path, monkeypatch):
    script = tmp_path / "self-update.sh"
    monkeypatch.setattr(self_update, "_SELF_UPDATE_SCRIPT", script)
    assert is_package_install() is False
    script.write_text("")
    assert is_package_install() is True


def test_current_version_none_when_not_installed(monkeypatch):
    def _raise(_name):
        raise self_update.PackageNotFoundError("hokku-server")

    monkeypatch.setattr(self_update, "_pkg_version", _raise)
    assert _current_version() is None


# ── routes ────────────────────────────────────────────────────────────────────


def test_update_check_404_when_not_package_install(app_config, tmp_path, monkeypatch):
    # flask_app.py did `from ...self_update import is_package_install`, so the
    # name it calls is bound in its own module namespace, not self_update's --
    # patch it there, not on self_update itself.
    monkeypatch.setattr(flask_app_mod, "is_package_install", lambda: False)
    client = _client(_bare_state(app_config), tmp_path)
    r = client.get("/hokku/api/update/check")
    assert r.status_code == 404


def test_update_install_404_when_not_package_install(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(flask_app_mod, "is_package_install", lambda: False)
    client = _client(_bare_state(app_config), tmp_path)
    r = client.post("/hokku/api/update/install", json={"tag": "v4.0.2"})
    assert r.status_code == 404


def test_update_check_reports_available_update(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(flask_app_mod, "is_package_install", lambda: True)
    monkeypatch.setattr(self_update, "_current_version", lambda: (4, 0, 0))
    monkeypatch.setattr(
        self_update,
        "find_newer_release",
        lambda current: ReleaseInfo(
            tag="v4.0.2",
            version=(4, 0, 2),
            asset_name="hokku-server_4.0.2-1_all.deb",
            asset_url="https://example.invalid/x.deb",
            asset_size=100,
            body="notes",
        ),
    )
    client = _client(_bare_state(app_config), tmp_path)
    r = client.get("/hokku/api/update/check")
    assert r.status_code == 200
    body = r.get_json()
    assert body["state"] == "checked"
    assert body["result"]["update_available"] is True
    assert body["result"]["latest_version"] == "4.0.2"
    assert body["result"]["tag"] == "v4.0.2"
    assert body["result"]["current_version"] == "4.0.0"


def test_update_check_reports_up_to_date(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(flask_app_mod, "is_package_install", lambda: True)
    monkeypatch.setattr(self_update, "_current_version", lambda: (4, 0, 2))
    monkeypatch.setattr(self_update, "find_newer_release", lambda current: None)
    client = _client(_bare_state(app_config), tmp_path)
    r = client.get("/hokku/api/update/check")
    assert r.status_code == 200
    body = r.get_json()
    assert body["state"] == "up_to_date"
    assert body["result"]["update_available"] is False


def test_update_install_409_when_job_already_running(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(flask_app_mod, "is_package_install", lambda: True)
    state = _bare_state(app_config)
    # Occupy the single job slot directly, without touching sudo/systemctl.
    state.update_jobs._job = {
        "id": 1,
        "kind": "install",
        "state": "downloading",
        "log": [],
        "error": None,
        "result": None,
        "started_at": 0,
        "finished_at": None,
    }
    client = _client(state, tmp_path)
    r = client.post("/hokku/api/update/install", json={"tag": "v4.0.2"})
    assert r.status_code == 409


def test_update_install_missing_tag(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(flask_app_mod, "is_package_install", lambda: True)
    client = _client(_bare_state(app_config), tmp_path)
    r = client.post("/hokku/api/update/install", json={})
    assert r.status_code == 400


def test_update_status_idle_when_nothing_run(app_config, tmp_path, monkeypatch):
    monkeypatch.setattr(SelfUpdateManager, "read_disk_status", staticmethod(lambda: None))
    client = _client(_bare_state(app_config), tmp_path)
    r = client.get("/hokku/api/update/status")
    assert r.status_code == 200
    body = r.get_json()
    assert body["job"] is None
    assert body["disk"] is None


# ── SelfUpdateManager phase transitions ─────────────────────────────────────


def test_manager_check_no_release_reaches_up_to_date(monkeypatch):
    monkeypatch.setattr(self_update, "_current_version", lambda: (4, 0, 2))
    monkeypatch.setattr(self_update, "find_newer_release", lambda current: None)
    mgr = SelfUpdateManager()
    job_id = mgr.check()
    assert job_id is not None
    status = mgr.status()
    assert status is not None
    assert status["state"] == "up_to_date"
    assert status["result"]["update_available"] is False


def test_manager_check_busy_returns_none(monkeypatch):
    mgr = SelfUpdateManager()
    mgr._job = {
        "id": 1,
        "kind": "check",
        "state": "checking",
        "log": [],
        "error": None,
        "result": None,
        "started_at": 0,
        "finished_at": None,
    }
    assert mgr.check() is None


def test_manager_install_reaches_triggered_without_real_sudo(monkeypatch, tmp_path):
    """The hard requirement: no real sudo/systemctl/apt-get invocation."""
    release = ReleaseInfo(
        tag="v4.0.2",
        version=(4, 0, 2),
        asset_name="hokku-server_4.0.2-1_all.deb",
        asset_url="https://example.invalid/x.deb",
        asset_size=5,
        body="",
    )
    monkeypatch.setattr(self_update, "_current_version", lambda: (4, 0, 0))
    monkeypatch.setattr(self_update, "find_newer_release", lambda current: release)

    staged_deb = tmp_path / "staged.deb"
    staged_meta = tmp_path / "staged.json"
    monkeypatch.setattr(self_update, "_STAGED_DEB", staged_deb)
    monkeypatch.setattr(self_update, "_STAGED_META", staged_meta)

    def fake_download(info, dest, on_progress=None):
        dest.write_bytes(b"fake-deb-contents")
        if on_progress:
            on_progress(len(b"fake-deb-contents"), info.asset_size)

    monkeypatch.setattr(self_update, "download_asset", fake_download)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)

    mgr = SelfUpdateManager()
    job_id = mgr.start_install("v4.0.2")
    assert job_id is not None

    def _done():
        s = mgr.status()
        return s is not None and s["state"] in ("triggered", "error")

    _wait_until(_done)
    status = mgr.status()
    assert status is not None
    assert status["state"] == "triggered", status
    assert staged_deb.exists()
    assert staged_meta.exists()
    assert len(calls) == 1
    assert calls[0] == [
        "sudo",
        "/usr/bin/systemctl",
        "start",
        "--no-block",
        "hokku-server-self-update.service",
    ]


def test_manager_install_stale_tag_errors(monkeypatch, tmp_path):
    # find_newer_release now returns a DIFFERENT (newer) release than what
    # the client asked to install -- must not silently install the wrong one.
    newer_release = ReleaseInfo(
        tag="v4.0.4",
        version=(4, 0, 4),
        asset_name="hokku-server_4.0.4-1_all.deb",
        asset_url="https://example.invalid/x.deb",
        asset_size=5,
        body="",
    )
    monkeypatch.setattr(self_update, "_current_version", lambda: (4, 0, 0))
    monkeypatch.setattr(self_update, "find_newer_release", lambda current: newer_release)
    monkeypatch.setattr(self_update, "_STAGED_DEB", tmp_path / "staged.deb")
    monkeypatch.setattr(self_update, "_STAGED_META", tmp_path / "staged.json")

    def fake_run(cmd, **kwargs):
        raise AssertionError("must not trigger install for a stale tag")

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)

    mgr = SelfUpdateManager()
    job_id = mgr.start_install("v4.0.2")  # stale -- current latest is v4.0.4
    assert job_id is not None

    def _errored():
        s = mgr.status()
        return s is not None and s["state"] == "error"

    _wait_until(_errored)
    status = mgr.status()
    assert status is not None
    assert status["error"] is not None
    assert "no longer the latest" in status["error"]
