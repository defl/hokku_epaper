"""GitHub Releases firmware discovery/parsing.

Network is stubbed at ``firmware_github._get`` (the single urllib entry point) so
nothing here touches the internet. The tests cover the asset-name matching, the
stable/prerelease filter, and the error surfaces.
"""

from __future__ import annotations

import json

import pytest

from hokku.webserver import firmware_github
from hokku.webserver.firmware_github import FirmwareFetchError, RemoteFirmware

REPO = "defl/hokku_epaper"

_RELEASES = [
    {
        "tag_name": "v4.0.0",
        "prerelease": False,
        "assets": [
            {
                "name": "hokku-huessen_epf1301-1.2.4.bin",
                "browser_download_url": "https://x/h124",
                "size": 2_000_000,
            },
            {
                "name": "hokku-seeedstudio_e1004-1.2.4.bin",
                "browser_download_url": "https://x/s124",
                "size": 2_100_000,
            },
            {
                "name": "hokku-bigme_f7-1.3.0.img",
                "browser_download_url": "https://x/b130",
                "size": 1_000_000,
            },
            {
                "name": "hokku-server_4.0.0_all.deb",
                "browser_download_url": "https://x/deb",
                "size": 9,
            },
            {"name": "checksums.txt", "browser_download_url": "https://x/sum", "size": 9},
        ],
    },
    {
        "tag_name": "v4.1.0-beta1",
        "prerelease": True,
        "assets": [
            {
                "name": "hokku-bigme_f7-1.4.0.img",
                "browser_download_url": "https://x/b140",
                "size": 1_050_000,
            },
            {
                "name": "hokku-unknownmodel-1.0.0.bin",
                "browser_download_url": "https://x/u",
                "size": 5,
            },
            {
                "name": "hokku-bigme_f7-1.4.0.bin",
                "browser_download_url": "https://x/wrongext",
                "size": 5,
            },
        ],
    },
]


@pytest.fixture
def stub_get(monkeypatch):
    def _fake_get(url, timeout, accept):
        if "api.github.com" in url:
            return json.dumps(_RELEASES).encode()
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(firmware_github, "_get", _fake_get)


def test_available_firmware_stable_only(stub_get):
    got = firmware_github.available_firmware(REPO, include_prereleases=False)
    keys = {(r.model_id, r.version) for r in got}
    assert keys == {
        ("huessen_epf1301", "1.2.4"),
        ("seeedstudio_e1004", "1.2.4"),
        ("bigme_f7", "1.3.0"),
    }
    # The .deb, checksums, unknown model, and wrong-ext F7 asset are all ignored.
    assert all(isinstance(r, RemoteFirmware) for r in got)
    assert all(r.channel == "stable" and not r.prerelease for r in got)


def test_available_firmware_includes_prereleases(stub_get):
    got = firmware_github.available_firmware(REPO, include_prereleases=True)
    beta = [r for r in got if r.version == "1.4.0"]
    assert len(beta) == 1
    assert beta[0].model_id == "bigme_f7"
    assert beta[0].prerelease is True
    assert beta[0].channel == "beta"
    assert beta[0].tag == "v4.1.0-beta1"
    # The wrong-extension F7 asset (.bin) and the unknown model are still skipped.
    assert not any(r.model_id == "unknownmodel" for r in got)
    assert all(not (r.model_id == "bigme_f7" and r.download_url.endswith("wrongext")) for r in got)


def test_list_releases_rejects_error_object(monkeypatch):
    monkeypatch.setattr(
        firmware_github,
        "_get",
        lambda url, timeout, accept: json.dumps({"message": "Not Found"}).encode(),
    )
    with pytest.raises(FirmwareFetchError, match="Not Found"):
        firmware_github.available_firmware(REPO, include_prereleases=False)


def test_list_releases_rejects_bad_json(monkeypatch):
    monkeypatch.setattr(firmware_github, "_get", lambda url, timeout, accept: b"not json")
    with pytest.raises(FirmwareFetchError):
        firmware_github.list_releases(REPO)


def test_download_rejects_empty(monkeypatch):
    monkeypatch.setattr(firmware_github, "_get", lambda url, timeout, accept: b"")
    r = RemoteFirmware(
        "bigme_f7", "1.3.0", "v4.0.0", False, "hokku-bigme_f7-1.3.0.img", "https://x/b", 10
    )
    with pytest.raises(FirmwareFetchError, match="empty"):
        firmware_github.download(r)


def test_download_returns_bytes(monkeypatch):
    monkeypatch.setattr(
        firmware_github, "_get", lambda url, timeout, accept: b"AWIH" + b"\x00" * 100
    )
    r = RemoteFirmware(
        "bigme_f7", "1.3.0", "v4.0.0", False, "hokku-bigme_f7-1.3.0.img", "https://x/b", 104
    )
    assert firmware_github.download(r) == b"AWIH" + b"\x00" * 100
