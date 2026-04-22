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

class TestMergedFirmwareDetection:
    def test_picks_merged_file(self, tmp_path):
        (tmp_path / "hokku-firmware_v1.0.0.bin").write_bytes(b"x")
        assert esp32_setup._merged_firmware_file(tmp_path).name == "hokku-firmware_v1.0.0.bin"

    def test_returns_none_when_no_merged(self, tmp_path):
        assert esp32_setup._merged_firmware_file(tmp_path) is None

    def test_ignores_non_matching(self, tmp_path):
        (tmp_path / "bootloader.bin").write_bytes(b"x")
        assert esp32_setup._merged_firmware_file(tmp_path) is None

    def test_picks_latest_sorted(self, tmp_path):
        (tmp_path / "hokku-firmware_v1.bin").write_bytes(b"x")
        (tmp_path / "hokku-firmware_v2.bin").write_bytes(b"x")
        assert esp32_setup._merged_firmware_file(tmp_path).name == "hokku-firmware_v2.bin"

    def test_nonexistent_dir(self, tmp_path):
        assert esp32_setup._merged_firmware_file(tmp_path / "nope") is None


class TestIsMergedAsset:
    def test_matches_merged(self):
        assert esp32_setup._is_merged_firmware_asset("hokku-firmware_v2.1.20.bin")

    def test_rejects_parts(self):
        for n in ("bootloader.bin", "partition-table.bin", "hokku_epaper.bin"):
            assert not esp32_setup._is_merged_firmware_asset(n)

    def test_rejects_deb(self):
        assert not esp32_setup._is_merged_firmware_asset("hokku-server_2.1.20-1_all.deb")


class TestResolveFirmwareDir:
    def test_uses_local_when_merged_file_present(self, tmp_path, monkeypatch):
        local = tmp_path / "release"
        local.mkdir()
        (local / "hokku-firmware_v1.0.0.bin").write_bytes(b"x")
        monkeypatch.setattr(esp32_setup, "LOCAL_FIRMWARE_DIR", local)
        monkeypatch.setattr(esp32_setup, "FIRMWARE_DIR", None)
        assert esp32_setup.resolve_firmware_dir() == local

    def test_downloads_merged_when_local_empty(self, tmp_path, monkeypatch):
        local = tmp_path / "release"
        local.mkdir()  # empty

        cache = tmp_path / "firmware-cache"
        monkeypatch.setattr(esp32_setup, "LOCAL_FIRMWARE_DIR", local)
        monkeypatch.setattr(esp32_setup, "FIRMWARE_CACHE_DIR", cache)
        monkeypatch.setattr(esp32_setup, "FIRMWARE_DIR", None)

        fake_release = {
            "tag_name": "v9.9.9",
            "assets": [
                {"name": "hokku-firmware_v9.9.9.bin", "browser_download_url": "http://x/fw.bin", "size": 42},
            ],
        }
        monkeypatch.setattr(release_cache, "get_latest_release", lambda: fake_release)

        downloaded = []
        def fake_ensure(asset, target_dir, label=""):
            path = target_dir / asset["name"]
            target_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * asset["size"])
            downloaded.append(asset["name"])
            return path
        monkeypatch.setattr(release_cache, "ensure_cached_asset", fake_ensure)

        result = esp32_setup.resolve_firmware_dir()
        assert result == cache / "v9.9.9"
        assert downloaded == ["hokku-firmware_v9.9.9.bin"]

    def test_returns_none_when_release_has_no_merged_asset(self, tmp_path, monkeypatch):
        local = tmp_path / "release"
        local.mkdir()
        monkeypatch.setattr(esp32_setup, "LOCAL_FIRMWARE_DIR", local)
        monkeypatch.setattr(esp32_setup, "FIRMWARE_CACHE_DIR", tmp_path / "cache")
        monkeypatch.setattr(esp32_setup, "FIRMWARE_DIR", None)

        fake_release = {
            "tag_name": "v1",
            "assets": [  # the three legacy parts — no merged file
                {"name": "bootloader.bin", "browser_download_url": "u1", "size": 1},
                {"name": "partition-table.bin", "browser_download_url": "u2", "size": 1},
                {"name": "hokku_epaper.bin", "browser_download_url": "u3", "size": 1},
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


class TestReleaseAppHeader:
    def test_reads_from_merged_at_app_offset(self, tmp_path):
        """Merged file must be read at offset 0x10000 (app region)."""
        merged = tmp_path / "hokku-firmware_v1.bin"
        # Fill with zeros then write a recognizable pattern at APP_OFFSET
        blob = bytearray(esp32_setup.APP_OFFSET + 256)
        blob[esp32_setup.APP_OFFSET:esp32_setup.APP_OFFSET + 16] = b"APPHEADER_START\x00"
        merged.write_bytes(bytes(blob))
        header = esp32_setup._release_app_header(directory=tmp_path)
        assert header is not None
        assert header.startswith(b"APPHEADER_START\x00")

    def test_ignores_three_file_layout(self, tmp_path):
        """Three-file layout is no longer supported — only merged files count."""
        (tmp_path / "hokku_epaper.bin").write_bytes(b"DIRECT_APP_HEADER" + b"\x00" * 300)
        assert esp32_setup._release_app_header(directory=tmp_path) is None

    def test_returns_none_when_no_merged(self, tmp_path):
        assert esp32_setup._release_app_header(directory=tmp_path) is None
