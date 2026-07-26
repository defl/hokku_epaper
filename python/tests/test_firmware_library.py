"""FirmwareStore: the bundled + downloaded + pin selection layer.

The store is a thin *override* over the registry's bundled resolution. These
tests pin down the two invariants that matter:

  * with nothing downloaded and nothing pinned it is a pass-through (the offline
    default — highest bundled, always stable), and
  * a beta is only ever served through an explicit pin.

Bundled state is stubbed via ``firmware_registry`` so the tests don't need a real
build on disk. Downloaded artifacts are written straight into a tmp dir (the
store discovers them by filename); the Bigme F7 ``.img`` is used for the
download-serving cases because it is served verbatim (its "app image" is just its
bytes), which keeps the assertions simple.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hokku.screens import firmware_registry
from hokku.webserver.firmware_library import CHANNEL_BETA, CHANNEL_STABLE, FirmwareStore

MODEL = "bigme_f7"


def _put(
    download_dir: Path,
    version: str,
    *,
    channel: str = CHANNEL_STABLE,
    tag: str | None = None,
    data: bytes = b"AWIH" + b"\x00" * 64,
) -> Path:
    """Write a downloaded artifact + sidecar for MODEL into *download_dir*."""
    download_dir.mkdir(parents=True, exist_ok=True)
    p = download_dir / f"hokku-{MODEL}-{version}.img"
    p.write_bytes(data)
    (download_dir / (p.name + ".meta.json")).write_text(
        json.dumps({"channel": channel, "tag": tag, "version": version})
    )
    return p


@pytest.fixture
def bundled(monkeypatch):
    """Stub the registry's bundled resolution to a single version, no dir files."""

    def _set(version: str | None, *, image: bytes | None = None, listing=None):
        # No bundled version -> no bundled image, matching reality.
        img = image if image is not None else (b"BUNDLED" if version else None)
        monkeypatch.setattr(
            firmware_registry,
            "firmware_version_for",
            lambda m: version if m == MODEL else None,
        )
        monkeypatch.setattr(
            firmware_registry,
            "release_app_image_for",
            lambda m: img if m == MODEL else None,
        )
        monkeypatch.setattr(
            firmware_registry,
            "list_bundled_firmware",
            lambda m: (listing or []) if m == MODEL else [],
        )

    return _set


def test_passthrough_when_nothing_downloaded_or_pinned(tmp_path, bundled):
    bundled("1.2.2", image=b"BUNDLED_IMG")
    store = FirmwareStore(tmp_path / "fw")
    assert store.effective_version(MODEL) == "1.2.2"
    assert store.effective_app_image(MODEL) == b"BUNDLED_IMG"
    eff = store.effective(MODEL)
    assert eff is not None and eff.source == "bundled" and eff.channel == CHANNEL_STABLE


def test_none_when_no_firmware_at_all(tmp_path, bundled):
    bundled(None)
    store = FirmwareStore(tmp_path / "fw")
    assert store.effective_version(MODEL) is None
    assert store.effective(MODEL) is None
    assert store.effective_app_image(MODEL) is None


def test_newer_downloaded_stable_overrides_bundled(tmp_path, bundled):
    bundled("1.2.2")
    dl = tmp_path / "fw"
    _put(dl, "1.3.0", data=b"AWIH" + b"NEW")
    store = FirmwareStore(dl)
    assert store.effective_version(MODEL) == "1.3.0"
    assert store.effective_app_image(MODEL) == b"AWIH" + b"NEW"
    eff = store.effective(MODEL)
    assert eff is not None and eff.source == "downloaded"


def test_older_downloaded_stable_does_not_override(tmp_path, bundled):
    bundled("1.2.2")
    dl = tmp_path / "fw"
    _put(dl, "1.1.0")
    store = FirmwareStore(dl)
    assert store.effective_version(MODEL) == "1.2.2"
    assert store.effective_app_image(MODEL) == b"BUNDLED"


def test_numeric_ordering_not_lexicographic(tmp_path, bundled):
    bundled("1.2.9")
    dl = tmp_path / "fw"
    _put(dl, "1.2.10", data=b"AWIH-10")
    store = FirmwareStore(dl)
    # 1.2.10 > 1.2.9 numerically (a string sort would pick 1.2.9).
    assert store.effective_version(MODEL) == "1.2.10"


def test_beta_never_auto_selected(tmp_path, bundled):
    bundled("1.2.2")
    dl = tmp_path / "fw"
    _put(dl, "1.9.0", channel=CHANNEL_BETA, tag="v1.9.0-beta1")
    store = FirmwareStore(dl)
    # Beta is higher-versioned but must not win without a pin.
    assert store.effective_version(MODEL) == "1.2.2"


def test_pin_beta_serves_it(tmp_path, bundled):
    bundled("1.2.2")
    dl = tmp_path / "fw"
    _put(dl, "1.9.0", channel=CHANNEL_BETA, tag="v1.9.0-beta1", data=b"AWIH-BETA")
    store = FirmwareStore(dl)
    store.set_pin(MODEL, "1.9.0")
    assert store.pinned(MODEL) == "1.9.0"
    assert store.effective_version(MODEL) == "1.9.0"
    assert store.effective_app_image(MODEL) == b"AWIH-BETA"
    eff = store.effective(MODEL)
    assert eff is not None and eff.channel == CHANNEL_BETA


def test_pin_older_bundled_version(tmp_path, bundled):
    old = tmp_path / "hokku-bigme_f7-1.2.0.img"
    old.write_bytes(b"AWIH-OLD")
    new = tmp_path / "hokku-bigme_f7-1.2.2.img"
    new.write_bytes(b"AWIH-NEW")
    bundled("1.2.2", listing=[("1.2.0", old), ("1.2.2", new)])
    store = FirmwareStore(tmp_path / "fw")
    store.set_pin(MODEL, "1.2.0")
    assert store.effective_version(MODEL) == "1.2.0"
    # Served from the exact pinned file, not release_app_image_for (highest).
    assert store.effective_app_image(MODEL) == b"AWIH-OLD"


def test_clear_pin_returns_to_auto(tmp_path, bundled):
    bundled("1.2.2")
    dl = tmp_path / "fw"
    _put(dl, "1.9.0", channel=CHANNEL_BETA)
    store = FirmwareStore(dl)
    store.set_pin(MODEL, "1.9.0")
    assert store.effective_version(MODEL) == "1.9.0"
    store.set_pin(MODEL, None)
    assert store.pinned(MODEL) is None
    assert store.effective_version(MODEL) == "1.2.2"


def test_stale_pin_falls_back_to_bundled(tmp_path, bundled):
    bundled("1.2.2")
    store = FirmwareStore(tmp_path / "fw")
    store.set_pin(MODEL, "9.9.9")  # nothing on disk for this version
    assert store.effective_version(MODEL) == "1.2.2"


def test_variants_dedup_and_channels(tmp_path, bundled):
    same = tmp_path / "hokku-bigme_f7-1.2.2.img"
    same.write_bytes(b"AWIH")
    bundled("1.2.2", listing=[("1.2.2", same)])
    dl = tmp_path / "fw"
    _put(dl, "1.2.2")  # same version, downloaded — should win the dedup
    _put(dl, "1.9.0", channel=CHANNEL_BETA)
    store = FirmwareStore(dl)
    variants = store.variants(MODEL)
    versions = [v.version for v in variants]
    assert versions == ["1.9.0", "1.2.2"]  # highest first, deduped
    by_ver = {v.version: v for v in variants}
    assert by_ver["1.2.2"].source == "downloaded"  # download won over bundled
    assert by_ver["1.9.0"].channel == CHANNEL_BETA


def test_add_download_validates_and_rejects_garbage(tmp_path, bundled):
    bundled("1.2.2")
    store = FirmwareStore(tmp_path / "fw")
    # A valid F7 image starts with the AWIH bootloader magic.
    store.add_download(MODEL, "1.5.0", b"AWIH" + b"\x00" * 32, channel=CHANNEL_STABLE, tag="v1.5.0")
    assert store.effective_version(MODEL) == "1.5.0"
    # Garbage (no AWIH magic) must be rejected and leave nothing behind.
    with pytest.raises(ValueError):
        store.add_download(MODEL, "1.6.0", b"NOTIMG", channel=CHANNEL_STABLE, tag="v1.6.0")
    assert "1.6.0" not in [v.version for v in store.variants(MODEL)]


def test_missing_sidecar_treated_as_stable(tmp_path, bundled):
    bundled("1.2.2")
    dl = tmp_path / "fw"
    dl.mkdir(parents=True)
    # File with no .meta.json sidecar.
    (dl / "hokku-bigme_f7-1.4.0.img").write_bytes(b"AWIH")
    store = FirmwareStore(dl)
    # Conservatively stable, so it wins as the highest stable.
    assert store.effective_version(MODEL) == "1.4.0"


def test_effective_versions_covers_all_models(tmp_path, bundled):
    bundled("1.2.2")
    store = FirmwareStore(tmp_path / "fw")
    versions = store.effective_versions()
    assert set(versions) == set(firmware_registry.known_models())
    assert versions[MODEL] == "1.2.2"
