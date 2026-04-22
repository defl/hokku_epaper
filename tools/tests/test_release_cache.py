"""Tests for tools/release_cache.py and esp32_setup.resolve_firmware_dir."""
import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import release_cache
import esp32_setup


@pytest.fixture(autouse=True)
def reset_release_cache():
    release_cache._reset_cache_for_tests()
    yield
    release_cache._reset_cache_for_tests()


# ---------- find_asset ----------

class TestFindAsset:
    def test_picks_by_predicate(self):
        release = {"assets": [
            {"name": "a.bin", "browser_download_url": "u1"},
            {"name": "b.bin", "browser_download_url": "u2"},
        ]}
        a = release_cache.find_asset(release, lambda n: n == "b.bin")
        assert a["browser_download_url"] == "u2"

    def test_returns_none_when_no_match(self):
        assert release_cache.find_asset({"assets": []}, lambda n: True) is None

    def test_handles_null_assets(self):
        assert release_cache.find_asset({"assets": None}, lambda n: True) is None

    def test_handles_missing_assets_key(self):
        assert release_cache.find_asset({}, lambda n: True) is None


# ---------- get_latest_release memoisation ----------

class TestGetLatestReleaseMemoised:
    def test_single_call_when_memoised(self):
        release = {"tag_name": "v1.0", "assets": []}
        # Fake urlopen: return json bytes
        class FakeResp:
            def __init__(self, body): self._body = body
            def read(self): return self._body
            def __enter__(self): return self
            def __exit__(self, *a): pass

        call_count = {"n": 0}
        def fake_urlopen(req, timeout=None):
            call_count["n"] += 1
            return FakeResp(json.dumps(release).encode("utf-8"))

        with patch.object(release_cache.urllib.request, "urlopen", side_effect=fake_urlopen):
            r1 = release_cache.get_latest_release()
            r2 = release_cache.get_latest_release()
            r3 = release_cache.get_latest_release()
        assert r1 is r2 is r3
        assert call_count["n"] == 1, "expected memoisation, got multiple API calls"


# ---------- ensure_cached_asset ----------

class TestEnsureCachedAsset:
    def test_skip_download_when_cached(self, tmp_path):
        asset = {"name": "foo.bin", "browser_download_url": "http://example", "size": 5}
        target = tmp_path / "foo.bin"
        target.write_bytes(b"hello")  # 5 bytes, matches size
        # Should NOT attempt to download — any urlopen would raise
        with patch.object(release_cache.urllib.request, "urlopen",
                          side_effect=AssertionError("should not download")):
            result = release_cache.ensure_cached_asset(asset, tmp_path)
        assert result == target

    def test_redownload_when_size_mismatch(self, tmp_path):
        asset = {"name": "foo.bin", "browser_download_url": "http://example", "size": 10}
        target = tmp_path / "foo.bin"
        target.write_bytes(b"hello")  # 5 bytes, != size
        class FakeResp:
            def __init__(self, body):
                self._body = body
                self.headers = {"Content-Length": str(len(body))}
                self._read = False
            def read(self, n=-1):
                if self._read: return b""
                self._read = True
                return self._body
            def __enter__(self): return self
            def __exit__(self, *a): pass
        with patch.object(release_cache.urllib.request, "urlopen",
                          return_value=FakeResp(b"1234567890")):
            result = release_cache.ensure_cached_asset(asset, tmp_path)
        assert result == target
        assert target.read_bytes() == b"1234567890"

    def test_missing_url(self, tmp_path):
        asset = {"name": "foo.bin"}
        assert release_cache.ensure_cached_asset(asset, tmp_path) is None


# ---------- resolve_firmware_dir ----------

class TestResolveFirmwareDir:
    def test_uses_local_when_all_files_present(self, tmp_path, monkeypatch):
        local = tmp_path / "release"
        local.mkdir()
        for f in esp32_setup.FIRMWARE_FILES:
            (local / f).write_bytes(b"x")
        monkeypatch.setattr(esp32_setup, "LOCAL_FIRMWARE_DIR", local)
        monkeypatch.setattr(esp32_setup, "FIRMWARE_DIR", None)
        result = esp32_setup.resolve_firmware_dir()
        assert result == local

    def test_downloads_when_local_incomplete(self, tmp_path, monkeypatch):
        """Simulate the three firmware assets being absent locally; the resolver
        should call into release_cache.ensure_cached_asset for each file."""
        local = tmp_path / "release"
        local.mkdir()  # empty — all three files missing

        cache = tmp_path / "firmware-cache"
        monkeypatch.setattr(esp32_setup, "LOCAL_FIRMWARE_DIR", local)
        monkeypatch.setattr(esp32_setup, "FIRMWARE_CACHE_DIR", cache)
        monkeypatch.setattr(esp32_setup, "FIRMWARE_DIR", None)

        fake_release = {
            "tag_name": "v9.9.9",
            "assets": [
                {"name": f, "browser_download_url": f"http://x/{f}", "size": 3}
                for f in esp32_setup.FIRMWARE_FILES
            ],
        }
        monkeypatch.setattr(release_cache, "get_latest_release", lambda: fake_release)

        downloaded = []
        def fake_ensure(asset, target_dir, label=""):
            path = target_dir / asset["name"]
            target_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"xxx")
            downloaded.append(asset["name"])
            return path
        monkeypatch.setattr(release_cache, "ensure_cached_asset", fake_ensure)

        result = esp32_setup.resolve_firmware_dir()
        assert result == cache / "v9.9.9"
        assert set(downloaded) == set(esp32_setup.FIRMWARE_FILES)

    def test_returns_none_when_asset_missing_from_release(self, tmp_path, monkeypatch):
        """If the latest release is missing any required firmware file, fail
        fast — we refuse to flash a partial firmware image."""
        local = tmp_path / "release"
        local.mkdir()
        monkeypatch.setattr(esp32_setup, "LOCAL_FIRMWARE_DIR", local)
        monkeypatch.setattr(esp32_setup, "FIRMWARE_CACHE_DIR", tmp_path / "cache")
        monkeypatch.setattr(esp32_setup, "FIRMWARE_DIR", None)

        # Release has only 2 of 3 assets
        fake_release = {
            "tag_name": "v1",
            "assets": [
                {"name": "bootloader.bin", "browser_download_url": "u1", "size": 1},
                {"name": "hokku_epaper.bin", "browser_download_url": "u2", "size": 1},
                # partition-table.bin intentionally missing
            ],
        }
        monkeypatch.setattr(release_cache, "get_latest_release", lambda: fake_release)
        assert esp32_setup.resolve_firmware_dir() is None

    def test_returns_none_when_github_unreachable(self, tmp_path, monkeypatch):
        local = tmp_path / "release"
        local.mkdir()
        monkeypatch.setattr(esp32_setup, "LOCAL_FIRMWARE_DIR", local)
        monkeypatch.setattr(esp32_setup, "FIRMWARE_DIR", None)

        def boom():
            raise OSError("network down")
        monkeypatch.setattr(release_cache, "get_latest_release", boom)
        assert esp32_setup.resolve_firmware_dir() is None
