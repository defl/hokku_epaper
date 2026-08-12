"""ImageManager: hashed filenames, sync, retry, scrub, db survival.

Every test runs twice (once against SingleThreadedImageManager, once against
MultiThreadedImageManager) via the parametrised ``image_manager_factory``
fixture in conftest.py. After ``mgr.sync()`` we always call
``mgr.wait_for_idle()`` so the multi-threaded variant's callbacks have
landed before assertions; on the single-threaded variant ``wait_for_idle``
is a no-op.
"""

from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image as _Image

from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.app_config import AppConfig
from hokku.webserver.bounding_box import BoundingBox
from hokku.webserver.image_classifier import ImageClassifierDecision
from hokku.webserver.image_manager_abstract import AbstractImageManager
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS
from hokku.webserver.screen_image_config import ScreenImageConfig
from tests._helpers import make_declared_size_png

_HUESSEN = DISPLAY_REGISTRY["huessen_epf1301"]
TOTAL_BYTES = _HUESSEN.total_bytes


def test_hash_name_stable():
    assert AbstractImageManager._hash_name("photo.jpg") == AbstractImageManager._hash_name(
        "photo.jpg"
    )
    assert AbstractImageManager._hash_name("photo.jpg") != AbstractImageManager._hash_name(
        "photo.png"
    )


def test_register_and_convert(app_config: AppConfig, image_manager_factory, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    make_test_image(upload / "b.png")

    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()

    records = mgr.list()
    assert [r.name for r in records] == ["a.png", "b.png"]
    assert all(r.convert_status == "ok" for r in records)
    assert all(r.original_sha1 for r in records)
    expected_slug = ScreenImageConfig(
        image_config=app_config.image_config_default,
        orientation=Orientation.LANDSCAPE,
        crop_to_fill_threshold=app_config.crop_to_fill_threshold,
    ).cache_slug()
    assert all(
        r.slug_for("huessen_epf1301", Orientation.LANDSCAPE) == expected_slug for r in records
    )


def test_over_budget_image_is_failed_without_decoding(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    """An over-budget source is marked 'failed' at ingest and NO phase decodes it.

    This is the OOM guard: a 38.9 MP HEIF panorama (here a forged 24 MP PNG that
    declares its size in the header but never decodes) would crash the Pi if
    Phase 1 (thumbnail) or Phase 2 (classify) tried to load it at full res. The
    dimension-reading choke point refuses it up front — reported with no width,
    so it drops out of the thumbnail/classify/render phases entirely — while a
    normal image alongside it still converts. If the gate regressed, sync() would
    try to thumbnail the forged PNG and raise on its malformed IDAT.
    """
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "good.png")
    # 6000x4000 = 24 MP > the 16 MP decode budget, but < the 40 MP bomb cap.
    (upload / "toobig.png").write_bytes(make_declared_size_png(6000, 4000))

    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()

    good = mgr.status("good.png")
    toobig = mgr.status("toobig.png")
    assert good is not None and good.convert_status == "ok"
    assert toobig is not None
    assert toobig.convert_status == "failed", "over-budget image should be failed, not decoded"
    assert toobig.image_width is None, "over-budget image must be gated out of every decode phase"
    assert toobig.convert_error and "too large" in toobig.convert_error.lower()
    # No thumbnail was materialised for it (no decode was attempted).
    assert mgr.thumbnail_jpg("toobig.png") is None


def test_panel_bytes_after_sync(app_config: AppConfig, image_manager_factory, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    raw = mgr.panel_bytes_for_orientation("a.png", Orientation.LANDSCAPE)
    assert raw is not None and len(raw) == TOTAL_BYTES


def test_preview_after_sync(app_config: AppConfig, image_manager_factory, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    png = mgr.preview_png("a.png")
    assert png is not None and png.startswith(b"\x89PNG")


def test_thumbnail_jpg(app_config: AppConfig, image_manager_factory, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    jpg = mgr.thumbnail_jpg("a.png")
    assert jpg is not None and jpg[:3] == b"\xff\xd8\xff"  # JPEG SOI


def test_add_existing_raises(app_config: AppConfig, image_manager_factory):
    mgr = image_manager_factory(app_config)
    mgr.add("hello.png", _tiny_png_bytes())
    with pytest.raises(FileExistsError):
        mgr.add("hello.png", _tiny_png_bytes())


def test_remove_missing_raises(app_config: AppConfig, image_manager_factory):
    mgr = image_manager_factory(app_config)
    with pytest.raises(FileNotFoundError):
        mgr.remove("nope.png")


def test_remove_clears_cache(app_config: AppConfig, image_manager_factory, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    rec = mgr.status("a.png")
    assert rec is not None
    panel_path = (
        Path(app_config.cache_dir)
        / "images"
        / f"{rec.name_hash}_{rec.slug_for('huessen_epf1301', Orientation.LANDSCAPE)}_panel.bin.zst"
    )
    assert panel_path.exists()

    mgr.remove("a.png")
    assert not panel_path.exists()
    assert mgr.status("a.png") is None
    assert not (upload / "a.png").exists()


def test_db_survives_restart(app_config: AppConfig, image_manager_factory, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    mgr.shutdown()  # final DB flush
    rec = mgr.status("a.png")

    mgr2 = image_manager_factory(app_config)
    rec2 = mgr2.status("a.png")
    assert rec2 == rec


def _synced(app_config: AppConfig, image_manager_factory, make_test_image, name: str = "a.png"):
    """A manager with one converted image, ready to be overridden."""
    make_test_image(Path(app_config.upload_dir) / name)
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    return mgr


def test_set_overrides_queues_a_rerender(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    mgr = _synced(app_config, image_manager_factory, make_test_image)
    before = mgr.status("a.png")
    assert before is not None and before.convert_status == "ok"
    assert before.slugs  # a render happened

    assert mgr.set_overrides("a.png", image_config=PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"])

    rec = mgr.status("a.png")
    assert rec is not None
    assert rec.image_config == PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"]
    # Pending is the trigger — _reconcile_with_disk() only slug-checks "ok"
    # records, so it is the backstop, not what queues the work.
    assert rec.convert_status == "pending"
    # Cleared slugs stop a polling screen being served the pre-override render.
    assert rec.slugs == {}


def test_override_changes_the_render(app_config: AppConfig, image_manager_factory, make_test_image):
    """End to end: the new slug is different and the picture re-renders under it."""
    mgr = _synced(app_config, image_manager_factory, make_test_image)
    old_slug = mgr.status("a.png").slug_for("huessen_epf1301", Orientation.LANDSCAPE)

    mgr.set_overrides("a.png", image_config=PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"])
    mgr.sync()
    mgr.wait_for_idle()

    rec = mgr.status("a.png")
    assert rec is not None
    assert rec.convert_status == "ok"
    new_slug = rec.slug_for("huessen_epf1301", Orientation.LANDSCAPE)
    assert new_slug is not None and new_slug != old_slug
    assert mgr.panel_bytes_for_orientation("a.png", Orientation.LANDSCAPE) is not None


def test_crop_only_override_changes_the_slug(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    """The crop override has to reach the cache key, not just the renderer."""
    mgr = _synced(app_config, image_manager_factory, make_test_image)
    old_slug = mgr.status("a.png").slug_for("huessen_epf1301", Orientation.LANDSCAPE)

    assert mgr.set_overrides("a.png", crop_to_fill_threshold=0.42)
    mgr.sync()
    mgr.wait_for_idle()

    rec = mgr.status("a.png")
    assert rec is not None
    assert rec.image_config is None  # pipeline still automatic
    assert rec.slug_for("huessen_epf1301", Orientation.LANDSCAPE) != old_slug


def test_clearing_an_override_restores_the_automatic_render(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    """Going back to automatic must land on the original slug again."""
    mgr = _synced(app_config, image_manager_factory, make_test_image)
    auto_slug = mgr.status("a.png").slug_for("huessen_epf1301", Orientation.LANDSCAPE)

    mgr.set_overrides("a.png", image_config=PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"])
    mgr.sync()
    mgr.wait_for_idle()
    assert mgr.status("a.png").slug_for("huessen_epf1301", Orientation.LANDSCAPE) != auto_slug

    assert mgr.set_overrides("a.png", image_config=None)
    mgr.sync()
    mgr.wait_for_idle()

    assert mgr.status("a.png").slug_for("huessen_epf1301", Orientation.LANDSCAPE) == auto_slug


def test_set_overrides_leaves_unmentioned_fields_alone(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    """Three-valued arguments: absent means leave, None means clear."""
    mgr = _synced(app_config, image_manager_factory, make_test_image)
    mgr.set_overrides(
        "a.png",
        image_config=PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"],
        crop_to_fill_threshold=0.3,
    )

    mgr.set_overrides("a.png", crop_to_fill_threshold=None)  # clear only the crop

    rec = mgr.status("a.png")
    assert rec is not None
    assert rec.crop_to_fill_threshold is None
    assert rec.image_config == PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"]


def test_set_overrides_is_a_noop_when_nothing_changes(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    """A redundant clear must not throw away a perfectly good render."""
    mgr = _synced(app_config, image_manager_factory, make_test_image)

    assert mgr.set_overrides("a.png", image_config=None) is False

    rec = mgr.status("a.png")
    assert rec is not None
    assert rec.convert_status == "ok"
    assert rec.slugs  # untouched


def test_set_overrides_unknown_image_raises(app_config: AppConfig, image_manager_factory):
    mgr = image_manager_factory(app_config)
    with pytest.raises(FileNotFoundError):
        mgr.set_overrides("nope.png", crop_to_fill_threshold=0.5)


def test_effective_decision_reports_the_override(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    mgr = _synced(app_config, image_manager_factory, make_test_image)
    assert mgr.effective_decision("nope.png") is None

    auto = mgr.effective_decision("a.png")
    assert auto is not None

    mgr.set_overrides("a.png", image_config=PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"])
    after = mgr.effective_decision("a.png")

    assert after is not None
    assert after.image_config == PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"]
    assert auto.image_config != after.image_config


def test_overrides_survive_clear_caches(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    mgr = _synced(app_config, image_manager_factory, make_test_image)
    mgr.set_overrides(
        "a.png", image_config=PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"], crop_to_fill_threshold=0.2
    )

    mgr.clear_caches()

    rec = mgr.status("a.png")
    assert rec is not None
    assert rec.image_config == PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"]
    assert rec.crop_to_fill_threshold == pytest.approx(0.2)


def test_overrides_survive_a_content_change(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    """Re-saving a picture keeps the tuning done for it — it is the same picture."""
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png", color=(255, 0, 0))
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    mgr.set_overrides("a.png", image_config=PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"])

    make_test_image(upload / "a.png", color=(0, 0, 255))
    mgr.sync()
    mgr.wait_for_idle()

    rec = mgr.status("a.png")
    assert rec is not None
    assert rec.image_config == PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"]


def test_overrides_survive_a_restart(app_config: AppConfig, image_manager_factory, make_test_image):
    mgr = _synced(app_config, image_manager_factory, make_test_image)
    mgr.set_overrides("a.png", crop_to_fill_threshold=0.35)
    mgr.shutdown()

    mgr2 = image_manager_factory(app_config)

    rec = mgr2.status("a.png")
    assert rec is not None
    assert rec.crop_to_fill_threshold == pytest.approx(0.35)


def test_deleting_the_image_drops_the_override(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    upload = Path(app_config.upload_dir)
    mgr = _synced(app_config, image_manager_factory, make_test_image)
    mgr.set_overrides("a.png", image_config=PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"])

    mgr.remove("a.png")
    make_test_image(upload / "a.png")
    mgr.sync()
    mgr.wait_for_idle()

    rec = mgr.status("a.png")
    assert rec is not None
    assert rec.image_config is None


def test_overrides_are_salvaged_across_a_db_version_wipe(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    """A future _DB_VERSION bump must cost a re-render, not the user's tuning.

    Everything derived is meant to be discarded by the wipe; the overrides are
    the only thing in that file nothing can reconstruct.
    """
    mgr = _synced(app_config, image_manager_factory, make_test_image)
    mgr.set_overrides(
        "a.png", image_config=PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"], crop_to_fill_threshold=0.2
    )
    mgr.shutdown()

    db_path = Path(app_config.cache_dir) / "image_manager.json"
    db = json.loads(db_path.read_text())
    db["version"] = 99  # a version this build does not understand
    db_path.write_text(json.dumps(db))

    mgr2 = image_manager_factory(app_config)
    mgr2.sync()
    mgr2.wait_for_idle()

    rec = mgr2.status("a.png")
    assert rec is not None
    assert rec.image_config == PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"]
    assert rec.crop_to_fill_threshold == pytest.approx(0.2)


def test_salvaged_override_for_a_deleted_file_is_forgotten(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    """An override belongs to its file. No file, no override to restore."""
    mgr = _synced(app_config, image_manager_factory, make_test_image)
    mgr.set_overrides("a.png", crop_to_fill_threshold=0.2)
    mgr.shutdown()

    db_path = Path(app_config.cache_dir) / "image_manager.json"
    db = json.loads(db_path.read_text())
    db["version"] = 99
    db_path.write_text(json.dumps(db))
    (Path(app_config.upload_dir) / "a.png").unlink()

    mgr2 = image_manager_factory(app_config)
    mgr2.sync()
    mgr2.wait_for_idle()

    assert mgr2.status("a.png") is None
    assert mgr2._salvaged_overrides == {}


def test_override_replaces_the_classifier_choice(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    rec = mgr.status("a.png")
    assert rec is not None

    chosen = PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"]
    overridden = replace(rec, image_config=chosen)
    decision = mgr._decision_for_record(upload / "a.png", overridden)

    assert decision.image_config == chosen


def test_crop_override_is_independent_of_the_pipeline_override(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    """Each override applies on its own; neither implies the other."""
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    rec = mgr.status("a.png")
    assert rec is not None

    auto = mgr._decision_for_record(upload / "a.png", rec)
    crop_only = mgr._decision_for_record(
        upload / "a.png", replace(rec, crop_to_fill_threshold=0.42)
    )

    assert crop_only.crop_to_fill_threshold == pytest.approx(0.42)
    assert crop_only.image_config == auto.image_config  # pipeline untouched


def test_override_keeps_the_classifier_observations(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    """An override replaces the pipeline choice, not the detection results.

    The face keep-out boxes and face-aware crop anchors come from detection, so
    they have to survive an override or a portrait would lose its skin-tone
    protection the moment someone hand-picked a dither for it.
    """
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    rec = mgr.status("a.png")
    assert rec is not None

    bboxes = (BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4),)
    stub = ImageClassifierDecision(
        image_config=PRESET_IMAGE_CONFIGS["atkinson_hue_aware"],
        crop_to_fill_threshold=0.1,
        clahe_keepout_bboxes=bboxes,
        face_crop_bboxes=bboxes,
    )
    with patch.object(mgr._classifier, "decision_for", return_value=stub):
        decision = mgr._decision_for_record(
            upload / "a.png",
            replace(rec, image_config=PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"]),
        )

    assert decision.image_config == PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"]
    assert decision.clahe_keepout_bboxes == bboxes
    assert decision.face_crop_bboxes == bboxes


def test_no_override_leaves_the_decision_untouched(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    rec = mgr.status("a.png")
    assert rec is not None

    direct = mgr._classifier.decision_for(upload / "a.png", rec.original_sha1)
    assert mgr._decision_for_record(upload / "a.png", rec) == direct


def test_retire_does_not_flush(app_config: AppConfig, image_manager_factory, make_test_image):
    """retire() leaves the DB file alone — its successor already owns it.

    Diverging _records first proves the file is untouched rather than merely
    rewritten with identical content.
    """
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    db_path = Path(app_config.cache_dir) / "image_manager.json"

    mgr._records.clear()
    mgr.retire()

    assert "a.png" in json.loads(db_path.read_text())["images"]


def test_writes_frozen_after_retire(app_config: AppConfig, image_manager_factory, make_test_image):
    """A retired manager cannot write, even when asked directly.

    This is what stops a hot-reload's outgoing manager — whose render callbacks
    keep firing after the swap — from reverting the live manager's file.
    """
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    db_path = Path(app_config.cache_dir) / "image_manager.json"

    mgr.retire()
    mgr._records.clear()
    mgr._save_db()  # a late render callback
    mgr.shutdown()  # and an explicit teardown afterwards

    assert "a.png" in json.loads(db_path.read_text())["images"]


def test_disk_change_detected(app_config: AppConfig, image_manager_factory, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png", color=(255, 0, 0))
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    sha_before = mgr.status("a.png").original_sha1

    # Replace contents
    make_test_image(upload / "a.png", color=(0, 0, 255))
    mgr.sync()
    mgr.wait_for_idle()
    sha_after = mgr.status("a.png").original_sha1
    assert sha_before != sha_after


def test_orphan_scrubbed(app_config: AppConfig, image_manager_factory, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()

    # Plant an orphan file in cache
    orphan = Path(app_config.cache_dir) / "images" / "deadbeefdeadbe_xx_panel.bin.zst"
    orphan.write_bytes(b"x" * TOTAL_BYTES)

    mgr.sync()
    mgr.wait_for_idle()
    assert not orphan.exists()


def test_retry_on_failed(app_config: AppConfig, image_manager_factory):
    mgr = image_manager_factory(app_config)
    # Create a corrupt "image" (non-image bytes with an image extension)
    src = Path(app_config.upload_dir) / "broken.png"
    src.write_bytes(b"not actually a png")

    mgr.sync()
    mgr.wait_for_idle()
    rec = mgr.status("broken.png")
    assert rec is not None and rec.convert_status == "failed"

    # Retry while still broken — stays failed
    mgr.retry("broken.png")
    mgr.sync()
    mgr.wait_for_idle()
    assert mgr.status("broken.png").convert_status == "failed"


def test_clear_caches_marks_pending(app_config: AppConfig, image_manager_factory, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    assert mgr.status("a.png").convert_status == "ok"

    mgr.clear_caches()
    assert mgr.status("a.png").convert_status == "pending"
    assert mgr.panel_bytes_for_orientation("a.png", Orientation.LANDSCAPE) is None

    mgr.sync()
    mgr.wait_for_idle()
    assert mgr.status("a.png").convert_status == "ok"


def test_inflight_prevents_double_submission(
    app_config: AppConfig, image_manager_factory, make_test_image, monkeypatch
):
    """sync() must not submit an image that is already converted."""
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")

    mgr = image_manager_factory(app_config)
    submitted: list[str] = []
    original = mgr._dispatch_render

    def counting(name, expected_slug, model, orientation, render_args, t0, *, update_status=True):
        submitted.append(name)
        return original(
            name, expected_slug, model, orientation, render_args, t0, update_status=update_status
        )

    monkeypatch.setattr(mgr, "_dispatch_render", counting)

    mgr.sync()  # submits and completes a.png
    mgr.wait_for_idle()
    mgr.sync()  # a.png is now 'ok'; should NOT resubmit
    mgr.wait_for_idle()

    # Both orientations are dispatched per image; the second sync() must not re-submit.
    assert submitted == ["a.png", "a.png"], (
        f"a.png should be submitted exactly twice (both orientations), got {submitted}"
    )


def test_two_images_both_succeed(app_config: AppConfig, image_manager_factory, make_test_image):
    """Two pending images both finish successfully."""
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "x.png")
    make_test_image(upload / "y.png")
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    assert mgr.status("x.png").convert_status == "ok"
    assert mgr.status("y.png").convert_status == "ok"


def test_worker_error_marks_failed_sibling_unaffected(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    """A failing render marks that image 'failed'; sibling images succeed."""
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "good.png")
    (upload / "bad.png").write_bytes(b"not-an-image")  # will fail PIL open

    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    assert mgr.status("good.png").convert_status == "ok"
    assert mgr.status("bad.png").convert_status == "failed"


def _tiny_png_bytes() -> bytes:
    """Smallest valid 1x1 PNG."""
    buf = BytesIO()
    _Image.new("RGB", (1, 1), (0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


# ── Conversion-progress correctness ──────────────────────────────────────────


def test_progress_total_not_doubled_by_concurrent_sync(
    request, app_config: AppConfig, image_manager_factory, make_test_image
):
    """Two back-to-back sync() calls must not double-count the total.

    Regression test for the race where a second sync() fired during the
    classify phase and saw all images still pending (not yet inflight), adding
    them to the total a second time.  Pre-reserving inflight in the first
    sync() lock prevents this.

    Only meaningful for the multi-threaded variant: SingleThreadedImageManager
    renders inline and synchronously, so the first sync() always completes
    before the second starts — progress resets correctly to (0,0) and there
    is no double-count risk.
    """
    if request.node.callspec.params.get("image_manager_factory") == "single":
        pytest.skip("Race condition only applies to the multi-threaded pool")

    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    make_test_image(upload / "b.png")
    make_test_image(upload / "c.png")

    mgr = image_manager_factory(app_config)
    # First sync dispatches all 3 images and marks them inflight.
    mgr.sync()
    # Second sync runs before the first batch is done (simulates watcher
    # firing while workers are still running).
    mgr.sync()
    mgr.wait_for_idle()

    prog = mgr.conversion_progress()
    # Total must be 3, not 6.
    assert prog.total == 3, f"expected total=3, got {prog.total}"
    assert prog.done == 3, f"expected done=3, got {prog.done}"


def test_progress_done_reaches_total_after_sync(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    """After a batch completes, done must equal total so converting clears."""
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "x.png")
    make_test_image(upload / "y.png")

    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()

    prog = mgr.conversion_progress()
    assert prog.done == prog.total, f"badge would never clear: done={prog.done} total={prog.total}"


def test_clear_caches_resets_progress(
    app_config: AppConfig, image_manager_factory, make_test_image
):
    """clear_caches() resets the progress counter so the next batch starts fresh.

    Without the reset, stale done/total values from the previous batch carry
    over and the new batch's done count can never reach the accumulated total,
    keeping 'converting=1' forever.
    """
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    make_test_image(upload / "b.png")

    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()

    # Progress after first batch: done=2, total=2.
    prog = mgr.conversion_progress()
    assert prog.done == 2 and prog.total == 2

    # Clear and re-convert.
    mgr.clear_caches()
    prog_after_clear = mgr.conversion_progress()
    assert prog_after_clear.done == 0 and prog_after_clear.total == 0, (
        f"progress not reset after clear_caches: {prog_after_clear}"
    )

    mgr.sync()
    mgr.wait_for_idle()

    prog_final = mgr.conversion_progress()
    assert prog_final.total == 2, f"expected total=2, got {prog_final.total}"
    assert prog_final.done == prog_final.total, (
        f"done never reached total after re-convert: {prog_final}"
    )
