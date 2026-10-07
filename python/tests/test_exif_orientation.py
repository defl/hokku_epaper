"""EXIF orientation, end to end: every container x every orientation x every stage.

Orientation bugs recur in image pipelines because each stage (header probe,
decode, thumbnail, face detector, DB) reads the file its own way, and they only
disagree on inputs nobody tested. So this suite pins ONE invariant everywhere:
whatever the container and wherever it keeps the tag, every stage sees the
same upright picture that reaches the panel.

Fixtures (tests/_orientation_fixtures.py) are a four-colour quadrant picture
stored through the inverse of each orientation, so a stage that ignores the tag,
or applies the wrong transform, is caught by which colour lands where — not
just by the aspect ratio. The transform table comes from the EXIF spec and is
cross-checked here against OpenCV's independent JPEG implementation, so Pillow
is never its own oracle.

Issue #40: a phone portrait (sensor 4000x3000 + Orientation=6) was recorded as
landscape because the header probe read img.size without the tag.
"""

from __future__ import annotations

import io
import json
import shutil
import struct
import tracemalloc
import zlib
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pillow_avif  # noqa: F401 — PIL plugin registration
import pillow_jxl  # noqa: F401 — PIL plugin registration
import pytest
from PIL import ExifTags, Image, ImageOps

from hokku.webserver.app_config import AppConfig
from hokku.webserver.bounding_box import BoundingBox
from hokku.webserver.face_detect_abstract import load_image_resized
from hokku.webserver.image_classifier import ImageClassifier, _cv2_saw_render_frame
from hokku.webserver.image_manager_abstract import AbstractImageManager
from hokku.webserver.image_manager_single import SingleThreadedImageManager
from hokku.webserver.image_renderer import open_image_for_render
from hokku.webserver.orientation import Orientation
from hokku.webserver.presets import PRESET_IMAGE_CONFIGS
from tests._helpers import make_declared_size_png
from tests._orientation_fixtures import (
    FIXTURE_KINDS,
    ORIENTATIONS,
    UPRIGHT_SIZE,
    exif_bytes,
    stored_image,
    upright_mismatch,
    write_fixture,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_MATRIX = [(kind, o) for kind in FIXTURE_KINDS for o in ORIENTATIONS]
_MATRIX_IDS = [f"{kind}-o{o}" for kind, o in _MATRIX]


@pytest.fixture(scope="module")
def fixture_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("orientation")
    for kind, o in _MATRIX:
        write_fixture(kind, o, d)
    return d


def _path(fixture_dir: Path, kind: str, o: int) -> Path:
    (match,) = fixture_dir.glob(f"{kind}_o{o}.*")
    return match


# ── the oracle ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("o", ORIENTATIONS)
def test_spec_table_agrees_with_opencv_on_plain_jpeg(fixture_dir: Path, o: int):
    """The fixtures' orientation table matches an implementation that isn't Pillow.

    OpenCV applies JPEG EXIF orientation itself (libjpeg + its own transform
    code), so if this and the Pillow-based stages below all pass, both agree
    with the spec rather than merely with each other.
    """
    bgr = cv2.imread(str(_path(fixture_dir, "jpeg", o)))
    assert bgr is not None
    rgb = Image.fromarray(np.ascontiguousarray(bgr[:, :, ::-1]))
    assert upright_mismatch(rgb) is None, upright_mismatch(rgb)


# ── per-stage invariants across the whole matrix ─────────────────────────────


@pytest.mark.parametrize(("kind", "o"), _MATRIX, ids=_MATRIX_IDS)
def test_render_decode_is_upright(fixture_dir: Path, kind: str, o: int):
    """What reaches the dither — the ground truth every other stage must match."""
    with open_image_for_render(_path(fixture_dir, kind, o)) as img:
        assert img.size == UPRIGHT_SIZE
        assert upright_mismatch(img) is None, upright_mismatch(img)


@pytest.mark.parametrize(("kind", "o"), _MATRIX, ids=_MATRIX_IDS)
def test_recorded_dims_are_the_displayed_dims(fixture_dir: Path, kind: str, o: int):
    """The header probe (issue #40) reports the size the renderer produces."""
    w, h, err = AbstractImageManager._try_read_image_dims(_path(fixture_dir, kind, o))
    assert err is None
    assert (w, h) == UPRIGHT_SIZE


@pytest.mark.parametrize(("kind", "o"), _MATRIX, ids=_MATRIX_IDS)
def test_face_detector_sees_the_rendered_frame(fixture_dir: Path, kind: str, o: int):
    """Face boxes are fractions of the image the detector saw; the renderer
    applies them to its own decode. Both must be the same frame, and every
    format the renderer takes must reach the detector (cv2.imread, its old
    loader, missed XMP, post-IDAT PNG and AVIF orientation, and depending on
    the OpenCV build couldn't read HEIF/AVIF/JXL at all)."""
    loaded = load_image_resized(_path(fixture_dir, kind, o))
    assert loaded is not None
    rgb = Image.fromarray(np.ascontiguousarray(loaded[0][:, :, ::-1]))
    assert upright_mismatch(rgb) is None, upright_mismatch(rgb)


@pytest.mark.parametrize(("kind", "o"), _MATRIX, ids=_MATRIX_IDS)
def test_classifier_knows_where_old_cv2_face_boxes_were_wrong(fixture_dir: Path, kind: str, o: int):
    """Face boxes cached before the detector moved off cv2.imread are kept only
    where cv2 decoded the render frame. Checked against cv2 itself, so the
    header-only predicate can't drift from the loader it stands in for.

    Beyond JPEG/PNG/WebP, what cv2 reads depends on the build (the Linux wheel
    the Pi runs decodes AVIF, ignoring its orientation; the Windows one can't
    read it), so there the predicate only has to be safe: re-detecting a box
    that was right costs a re-render, keeping one that was wrong is the bug."""
    path = _path(fixture_dir, kind, o)
    bgr = cv2.imread(str(path))
    cv2_right = bgr is not None and (
        upright_mismatch(Image.fromarray(np.ascontiguousarray(bgr[:, :, ::-1]))) is None
    )
    keep = _cv2_saw_render_frame(path)
    if kind.startswith(("jpeg", "mpo", "png", "webp")):
        assert keep == cv2_right
    else:
        assert cv2_right or not keep


def test_classifier_redetects_only_faces_cv2_saw_in_the_wrong_frame(tmp_path: Path):
    """v1 caches: boxes from a frame cv2 got right survive (no re-render); a
    wrong frame, and "no face" in a format cv2 couldn't read, are detected
    again — once."""
    right = write_fixture("jpeg", 6, tmp_path)
    wrong = write_fixture("jpeg_xmp_only", 6, tmp_path)
    unreadable = write_fixture("heif", 6, tmp_path)
    old_box = {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}
    cache = tmp_path / "ca"
    cache.mkdir()
    (cache / "image_classifier.json").write_text(
        json.dumps(
            {
                "version": 1,
                "observations": {
                    "right": {"is_bw": None, "face_bboxes": [old_box]},
                    "wrong": {"is_bw": None, "face_bboxes": [old_box]},
                    "unreadable": {"is_bw": None, "face_bboxes": []},
                },
            }
        )
    )
    cfg = AppConfig(
        upload_dir=str(tmp_path), cache_dir=str(cache), classifier_face_detect_enabled=True
    )
    new_box = BoundingBox(x=0.5, y=0.5, w=0.1, h=0.1)
    detected: list[Path] = []

    class _Detector:
        def detect(self, path: Path) -> list[BoundingBox]:
            detected.append(path)
            return [new_box]

    def classify_all(clf: ImageClassifier) -> dict[str, tuple[BoundingBox, ...] | None]:
        clf._face_detector = _Detector()  # type: ignore[assignment]
        out = {}
        for sha, path in (("right", right), ("wrong", wrong), ("unreadable", unreadable)):
            clf.decision_for(path, sha)
            out[sha] = clf.observations_for(sha).face_bboxes
        return out

    got = classify_all(ImageClassifier(cfg))
    assert detected == [wrong, unreadable]
    assert got == {
        "right": (BoundingBox(**old_box),),
        "wrong": (new_box,),
        "unreadable": (new_box,),
    }
    detected.clear()
    assert classify_all(ImageClassifier(cfg)) == got
    assert detected == []


@pytest.mark.parametrize(("kind", "o"), _MATRIX, ids=_MATRIX_IDS)
def test_thumbnail_is_upright(tmp_path: Path, fixture_dir: Path, kind: str, o: int):
    """The web UI thumbnail: its own decode path (draft + exif_transpose, no
    open_image_for_render)."""
    thumb = tmp_path / "thumb.jpg"
    AbstractImageManager._materialize_thumbnail(_path(fixture_dir, kind, o), thumb)
    with Image.open(thumb) as img:
        assert upright_mismatch(img) is None, upright_mismatch(img)


# ── the manager: record, thumbnail, preview, filters ─────────────────────────

# A full render per fixture is ~0.4 s, so the end-to-end manager run takes one
# container per distinct way the orientation reaches Pillow (header EXIF, a
# trailing PNG chunk, TIFF's pre-oriented size, HEIF's container transform,
# XMP); the per-stage tests above cover every container.
_MANAGER_KINDS = ("jpeg", "png_exif_after_idat", "png_xmp_only", "tiff", "heif")
_MANAGER_MATRIX = [(kind, o) for kind in _MANAGER_KINDS for o in ORIENTATIONS]


@pytest.fixture(scope="module")
def synced_manager(tmp_path_factory, fixture_dir: Path):
    """One manager that has ingested the fixtures (a sync per case is too slow)."""
    base = PRESET_IMAGE_CONFIGS["atkinson_hue_aware"]
    root = tmp_path_factory.mktemp("orientation_mgr")
    (root / "up").mkdir()
    (root / "cache").mkdir()
    for kind, o in _MANAGER_MATRIX:
        f = _path(fixture_dir, kind, o)
        shutil.copy(f, root / "up" / f.name)
    cfg = AppConfig(
        upload_dir=str(root / "up"),
        cache_dir=str(root / "cache"),
        port=18080,
        poll_interval_seconds=1,
        image_config_default=replace(base, dither=replace(base.dither, algorithm="noop")),
        classifier_face_detect_enabled=False,
        classifier_bw_detect_enabled=False,
    )
    mgr = SingleThreadedImageManager(cfg)
    mgr.sync()
    mgr.wait_for_idle()
    yield mgr
    mgr.shutdown()


@pytest.mark.parametrize(
    ("kind", "o"), _MANAGER_MATRIX, ids=[f"{k}-o{o}" for k, o in _MANAGER_MATRIX]
)
def test_manager_records_orientation_and_serves_upright_images(
    synced_manager: AbstractImageManager, fixture_dir: Path, kind: str, o: int
):
    name = _path(fixture_dir, kind, o).name
    rec = synced_manager.status(name)
    assert rec is not None and rec.convert_status == "ok", rec and rec.convert_error
    assert (rec.image_width, rec.image_height) == UPRIGHT_SIZE
    assert rec.native_orientation == Orientation.LANDSCAPE
    assert rec.matches_orientation_filter(Orientation.LANDSCAPE)
    assert not rec.matches_orientation_filter(Orientation.PORTRAIT)

    thumb = synced_manager.thumbnail_jpg(name)
    assert thumb is not None
    assert upright_mismatch(Image.open(io.BytesIO(thumb))) is None

    # The rendered preview (what the panel shows) is colour-mapped by the
    # pipeline, so compare it with the same container's untagged render.
    preview = synced_manager.preview_png(name)
    reference = synced_manager.preview_png(_path(fixture_dir, kind, 1).name)
    assert preview is not None and reference is not None
    got = upright_mismatch(
        Image.open(io.BytesIO(preview)), reference=Image.open(io.BytesIO(reference))
    )
    assert got is None, got


# ── edges ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [0, 9, 255, 65535])
@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_out_of_range_orientation_is_ignored_consistently(tmp_path: Path, fmt: str, value: int):
    """A garbage tag (seen from buggy editors) means "no rotation" everywhere."""
    path = tmp_path / f"bad.{fmt.lower()}"
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = value
    stored_image(1).save(path, format=fmt, exif=exif.tobytes())

    w, h, err = AbstractImageManager._try_read_image_dims(path)
    assert err is None
    with open_image_for_render(path) as img:
        assert (w, h) == img.size == UPRIGHT_SIZE
        assert upright_mismatch(img) is None


@pytest.mark.parametrize(
    ("kind", "o", "upright"),
    [
        # 12 MP phone-sized JPEG: exercises libjpeg draft (shrink-on-load).
        ("jpeg", 6, (4000, 3000)),
        ("jpeg", 8, (4000, 3000)),
        # Big PNG: no draft, full decode then _shrink_to_source_bbox.
        ("png", 6, (3000, 2000)),
        ("png_exif_after_idat", 5, (3000, 2000)),
        ("tiff", 7, (3000, 2000)),
    ],
    ids=lambda v: str(v),
)
def test_large_images_through_the_shrink_paths(
    tmp_path: Path, kind: str, o: int, upright: tuple[int, int]
):
    """Draft/reduce/thumbnail run BEFORE exif_transpose (memory); orientation
    must survive them."""
    path = write_fixture(kind, o, tmp_path, size=upright)
    w, h, err = AbstractImageManager._try_read_image_dims(path)
    assert err is None
    assert (w, h) == upright
    with open_image_for_render(path) as img:
        assert upright_mismatch(img, upright) is None, upright_mismatch(img, upright)
        assert (img.size[0] > img.size[1]) == (upright[0] > upright[1])


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


@pytest.mark.parametrize(
    "chunk",
    [
        _chunk(b"eXIf", exif_bytes(6)),
        _chunk(
            b"iTXt",
            b"XML:com.adobe.xmp\0\0\0\0\0"
            b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description '
            b'tiff:Orientation="6"/></rdf:RDF></x:xmpmeta>',
        ),
    ],
    ids=["exif", "xmp"],
)
def test_png_orientation_after_idat_is_read_without_decoding(tmp_path: Path, chunk: bytes):
    """The header probe must find trailing metadata WITHOUT touching IDAT.

    The IDAT here is empty (see make_declared_size_png) — any decode raises, so
    this succeeding proves the chunk walk seeks past pixel data. Ingest must
    never decode: that is the OOM gate's whole premise.
    """
    raw = make_declared_size_png(400, 200)
    iend = raw.rindex(b"IEND") - 4
    path = tmp_path / "forged.png"
    path.write_bytes(raw[:iend] + chunk + raw[iend:])

    assert AbstractImageManager._try_read_image_dims(path) == (200, 400, None)


@pytest.mark.parametrize(
    "chunk",
    [
        # Multi-MB plain text: skipped by its declared length, never read.
        _chunk(b"tEXt", b"XML:com.adobe.xmp\0" + b" " * (5 * 1024 * 1024)),
        # Tiny zTXt that inflates to 64 MB: capped mid-inflate.
        _chunk(b"zTXt", b"XML:com.adobe.xmp\0\0" + zlib.compress(b" " * (64 * 1024 * 1024))),
    ],
    ids=["huge", "zip_bomb"],
)
def test_png_hostile_metadata_chunk_is_not_materialised(tmp_path: Path, chunk: bytes):
    """The header probe must not allocate what a hostile text chunk asks for."""
    raw = make_declared_size_png(400, 200)
    iend = raw.rindex(b"IEND") - 4
    path = tmp_path / "junk.png"
    path.write_bytes(raw[:iend] + chunk + raw[iend:])

    tracemalloc.start()
    try:
        dims = AbstractImageManager._try_read_image_dims(path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert dims == (400, 200, None)
    assert peak < 16 * 1024 * 1024, f"header probe allocated {peak / 1e6:.0f} MB"


# ── real camera files ─────────────────────────────────────────────────────────


def _real_rotated_photos() -> list[Path]:
    """iPhone JPEGs already in the repo (screen documentation), any tag != 1."""
    out = []
    for p in sorted((_REPO_ROOT / "images" / "screens").rglob("*.jp*g")):
        with Image.open(p) as img:
            if img.getexif().get(ExifTags.Base.Orientation, 1) != 1:
                out.append(p)
    return out


_REAL_PHOTOS = _real_rotated_photos()


def test_repo_has_real_rotated_photos():
    """Guard: if these get deleted the real-camera test silently stops testing."""
    orientations = set()
    for p in _REAL_PHOTOS:
        with Image.open(p) as img:
            orientations.add(img.getexif()[ExifTags.Base.Orientation])
    assert {3, 6, 8} <= orientations


@pytest.mark.parametrize("path", _REAL_PHOTOS, ids=lambda p: p.name)
def test_real_camera_photo_dims_match_render(path: Path):
    w, h, err = AbstractImageManager._try_read_image_dims(path)
    assert err is None and w and h
    with Image.open(path) as img:
        img.draft("RGB", (img.size[0] // 8, img.size[1] // 8))  # cheap full-frame view
        tw, th = ImageOps.exif_transpose(img).size
    assert (w > h) == (tw > th)
    assert abs(w / h - tw / th) < 0.01
    with open_image_for_render(path) as rendered:
        assert (w > h) == (rendered.size[0] > rendered.size[1])


# ── DB migration ─────────────────────────────────────────────────────────────


def test_v4_db_migration_corrects_only_the_old_bug(app_config: AppConfig, image_manager_factory):
    """Loading a pre-fix (v4) DB repairs swapped dims in place.

    The pre-fix reader stored raw sensor dims for EXIF-rotated JPEGs, but was
    already right for TIFF and HEIF (their size comes out oriented at open) —
    the migration must fix the first without double-swapping the others. Only
    the dims change: status and rendered slugs survive, so upgrading a library
    does not trigger a re-render.
    """
    upload = Path(app_config.upload_dir)
    for kind in ("jpeg", "tiff", "heif", "png"):
        write_fixture(kind, 6, upload)
    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()
    mgr.shutdown()

    db_path = Path(app_config.cache_dir) / "image_manager.json"
    db = json.loads(db_path.read_text())
    db["version"] = 4
    before = {name: dict(row) for name, row in db["images"].items()}
    swapped = db["images"]["jpeg_o6.jpg"]
    swapped["image_width"], swapped["image_height"] = UPRIGHT_SIZE[1], UPRIGHT_SIZE[0]
    # A mismatch that is NOT a swap is not the #40 bug; the migration must not
    # take it as licence to rewrite the record.
    other = db["images"]["png_o6.png"]
    other["image_width"], other["image_height"] = 50, 50
    db_path.write_text(json.dumps(db))

    mgr2 = image_manager_factory(app_config)
    untouched = mgr2.status("png_o6.png")
    assert untouched is not None
    assert (untouched.image_width, untouched.image_height) == (50, 50)
    for name in ("jpeg_o6.jpg", "tiff_o6.tiff", "heif_o6.heic"):
        rec = mgr2.status(name)
        assert rec is not None
        assert (rec.image_width, rec.image_height) == UPRIGHT_SIZE, name
        assert rec.native_orientation == Orientation.LANDSCAPE
        assert rec.convert_status == "ok"
        assert rec.slugs == before[name]["slugs"], f"{name} would re-render"
