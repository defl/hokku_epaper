"""Checks on the rendered web UI.

index.html carries ~3000 lines of inline JavaScript that nothing else in the
suite executes, so a syntax error in it ships silently — the page loads, the
script dies on parse, and every control stops working. `node --check` catches
that in a second when a JS runtime is available.

The rest of the assertions pin the structural contract between the Python side
and the page: the elements the JS mounts into, and the fields it reads.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from hokku.webserver.app_config import AppConfig
from hokku.webserver.app_state import AppState, build_manager
from hokku.webserver.flask_app import create_app
from hokku.webserver.image_classifier import ImageClassifier
from hokku.webserver.serve_scheduler import ServeScheduler


@pytest.fixture
def rendered_ui(app_config: AppConfig, tmp_path: Path) -> str:
    clf = ImageClassifier(app_config)
    mgr = build_manager(app_config, clf)
    state = AppState(app_config, clf, mgr, ServeScheduler(mgr))
    app = create_app(state, config_path=tmp_path / "cfg.json")
    app.config["TESTING"] = True
    resp = app.test_client().get("/hokku/ui")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _inline_js(html: str) -> str:
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, "no inline <script> in the rendered page"
    return "\n".join(blocks)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_inline_javascript_parses(rendered_ui: str, tmp_path: Path):
    js = tmp_path / "ui.js"
    js.write_text(_inline_js(rendered_ui), encoding="utf-8")
    node = shutil.which("node")
    assert node is not None  # guarded by the skipif above

    result = subprocess.run([node, "--check", str(js)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "element_id",
    [
        # Every dither editor is generated into one of these by
        # mountDitherEditor(); if a mount point is renamed or lost, the whole
        # pipeline editor silently fails to appear.
        "default-editor-mount",
        "bw-editor-mount",
        "face-editor-mount",
        "imgcfg-editor-mount",
        # Per-picture override controls.
        "imgcfg-apply-btn",
        "imgcfg-auto-btn",
        "imgcfg-compare-btn",
        "imgcfg-crop",
        "imgcfg-compare-grid",
    ],
)
def test_ui_has_mount_points(rendered_ui: str, element_id: str):
    assert f'id="{element_id}"' in rendered_ui


def test_editor_markup_is_not_hand_duplicated(rendered_ui: str):
    """The three Config pipelines must come from the component, not the page.

    They used to be three hand-maintained copies and had already drifted apart.
    A literal id="dither-advanced-panel" in the template means someone pasted a
    fourth copy back in.
    """
    for pfx in ("dither", "bw", "face"):
        assert f'id="{pfx}-advanced-panel"' not in rendered_ui


def test_no_references_to_the_retired_state_variables(rendered_ui: str):
    """Editor state lives in ditherStates, keyed by panel id."""
    js = _inline_js(rendered_ui)
    for name in ("bwDitherState", "faceDitherState"):
        assert name not in js
    assert "ditherStates" in js


def test_collection_context_persists_and_serving_indicator_is_mounted(rendered_ui: str):
    """The page exposes the frame-serving status alongside the image stats."""
    assert 'id="stat-serving-collection"' in rendered_ui
    assert "Serving collection:" in rendered_ui
