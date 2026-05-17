"""ImageManager: hashed filenames, sync, retry, scrub, db survival.

Every test runs twice (once against SingleThreadedImageManager, once against
MultiThreadedImageManager) via the parametrised ``image_manager_factory``
fixture in conftest.py. After ``mgr.sync()`` we always call
``mgr.wait_for_idle()`` so the multi-threaded variant's callbacks have
landed before assertions; on the single-threaded variant ``wait_for_idle``
is a no-op.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image as _Image

from hokku_server.app_config import AppConfig
from hokku_server.display import TOTAL_BYTES
from hokku_server.image_manager_abstract import AbstractImageManager
from hokku_server.screen_image_config import ScreenImageConfig


def test_hash_name_stable():
    assert AbstractImageManager._hash_name("photo.jpg") == AbstractImageManager._hash_name("photo.jpg")
    assert AbstractImageManager._hash_name("photo.jpg") != AbstractImageManager._hash_name("photo.png")


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
    from hokku_server.orientation import Orientation
    expected_slug = ScreenImageConfig(
        image_config=app_config.image_config_default,
        orientation=app_config.orientation,
        crop_to_fill_threshold=app_config.crop_to_fill_threshold,
    ).cache_slug()
    orientation = app_config.orientation
    if orientation == Orientation.LANDSCAPE:
        assert all(r.landscape_image_config_slug == expected_slug for r in records)
    else:
        assert all(r.portrait_image_config_slug == expected_slug for r in records)


def test_panel_bytes_after_sync(app_config: AppConfig, image_manager_factory, make_test_image):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    raw = mgr.panel_bytes("a.png")
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
    panel_path = Path(app_config.cache_dir) / "images" / f"{rec.name_hash}_{rec.slug(app_config.orientation)}_panel.bin.zst"
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
    assert mgr.panel_bytes("a.png") is None

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

    def counting(name, expected_slug, orientation, render_args, t0, *, update_status=True):
        submitted.append(name)
        return original(name, expected_slug, orientation, render_args, t0, update_status=update_status)

    monkeypatch.setattr(mgr, "_dispatch_render", counting)

    mgr.sync()   # submits and completes a.png
    mgr.wait_for_idle()
    mgr.sync()   # a.png is now 'ok'; should NOT resubmit
    mgr.wait_for_idle()

    # Both orientations are dispatched per image; the second sync() must not re-submit.
    assert submitted == ["a.png", "a.png"], f"a.png should be submitted exactly twice (both orientations), got {submitted}"


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
    assert prog.done == prog.total, (
        f"badge would never clear: done={prog.done} total={prog.total}"
    )


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
