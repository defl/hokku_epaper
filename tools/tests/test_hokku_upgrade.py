"""Tests for tools/hokku_upgrade.py's pure helpers."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import hokku_upgrade as hu


def _release(tag, assets, prerelease=True, draft=False, body=""):
    return {
        "tag_name": tag,
        "name": tag,
        "prerelease": prerelease,
        "draft": draft,
        "published_at": "2026-10-07T12:00:00Z",
        "body": body,
        "assets": assets,
    }


def _asset(name, digest=None):
    return {"name": name, "browser_download_url": f"https://example/{name}", "digest": digest}


class TestVersions:
    def test_tag_to_upstream_version(self):
        assert hu.tag_to_upstream_version("v4.0.0-beta4") == "4.0.0~beta4"
        assert hu.tag_to_upstream_version("v3.0.0") == "3.0.0"
        assert hu.tag_to_upstream_version("4.0.0-rc1") == "4.0.0~rc1"

    def test_upstream_of(self):
        assert hu.upstream_of("4.0.0~beta4-1") == "4.0.0~beta4"
        assert hu.upstream_of("4.0.0") == "4.0.0"


class TestParseReleases:
    def test_picks_server_deb_not_other_assets(self):
        rel = _release(
            "v4.0.0-beta4",
            [
                _asset("hokku-installer_4.0.0.beta3-1_all.deb"),
                _asset("hokku_server-4.0.0b4-py3-none-any.whl"),
                _asset("hokku-huessen_epf1301-1.2.27.bin"),
                _asset("hokku-server_4.0.0.beta4-1_all.deb", "sha256:abc"),
            ],
        )
        (r,) = hu.parse_releases([rel])
        assert r.deb_name == "hokku-server_4.0.0.beta4-1_all.deb"
        assert r.deb_sha256 == "abc"
        assert r.upstream_version == "4.0.0~beta4"

    def test_skips_drafts_and_keeps_releases_without_deb(self):
        out = hu.parse_releases(
            [_release("v9", [], draft=True), _release("v1.0.0", [_asset("fw.bin")])]
        )
        assert [r.tag for r in out] == ["v1.0.0"]
        assert out[0].deb_url is None

    def test_missing_digest(self):
        (r,) = hu.parse_releases([_release("v1", [_asset("hokku-server_1-1_all.deb")])])
        assert r.deb_sha256 is None


class TestUpgradingNotes:
    def test_extracts_section_until_same_level_heading(self):
        body = "## What's new\nstuff\n## Upgrading from beta 3\nstep 1\n### Detail\nmore\n## Firmware\nfw"
        assert hu.upgrading_notes(body) == "## Upgrading from beta 3\nstep 1\n### Detail\nmore"

    def test_none(self):
        assert hu.upgrading_notes("## What's new\nstuff") == ""


class TestSettings:
    def test_new_settings(self):
        before = {"version": 9, "port": 8080, "image_config_default": {"gamma": 1.0}}
        after = {
            "version": 11,
            "port": 8080,
            "image_config_default": {"gamma": 1.0, "prepare_autocontrast": "off"},
        }
        assert hu.new_settings(before, after) == ["image_config_default.prepare_autocontrast"]

    def test_preset_differences_and_payload(self):
        presets = {
            "default_general": {
                "label": "General",
                "description": "d",
                "gamma": 1.1,
                "dither": {"algorithm": "atkinson"},
            },
            "default_bw": {"gamma": 1.0},
        }
        config = {
            "image_config_default": {"gamma": 1.0, "dither": {"algorithm": "fs"}},
            "image_config_bw": {"gamma": 1.0},
        }
        assert hu.preset_differences(config, presets) == [
            ("image_config_default.gamma", 1.0, 1.1),
            ("image_config_default.dither.algorithm", "fs", "atkinson"),
        ]
        assert hu.preset_payload(presets) == {
            "image_config_default": {"gamma": 1.1, "dither": {"algorithm": "atkinson"}},
            "image_config_bw": {"gamma": 1.0},
        }

    def test_backup_paths_adds_outside_dirs_only(self, tmp_path):
        outside = tmp_path / "pics"
        outside.mkdir()
        cfg = {"upload_dir": str(outside), "cache_dir": "/var/lib/hokku/cache"}
        assert hu.backup_paths(cfg) == [hu.STATE_DIR, outside]
