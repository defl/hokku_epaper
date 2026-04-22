"""Tests for pi_installer.py — parsers, validators, shell rendering."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pi_installer as pi


# ---------- PowerShell drive listing parser ----------

class TestParsePowerShellDrives:
    def test_multi_row_array(self):
        raw = json.dumps([
            {"Index": 0, "Model": "WDS100T1X0E-00AFY0", "Size": 1000202273280,
             "InterfaceType": "SCSI", "MediaType": "Fixed hard disk media", "Letters": ["C:"]},
            {"Index": 2, "Model": "USB Mass Storage Device", "Size": 63861073920,
             "InterfaceType": "USB", "MediaType": "Removable Media", "Letters": ["E:"]},
        ])
        drives = pi.parse_powershell_drives(raw)
        assert len(drives) == 2
        assert drives[0]["index"] == 0
        assert drives[0]["removable"] is False
        assert drives[0]["letters"] == ["C:"]
        assert drives[1]["index"] == 2
        assert drives[1]["removable"] is True
        assert drives[1]["size_bytes"] == 63861073920
        assert drives[1]["letters"] == ["E:"]

    def test_single_row_not_wrapped(self):
        """PowerShell emits a JSON object (not array) when there's only one row."""
        raw = json.dumps({
            "Index": 0, "Model": "X", "Size": 500,
            "InterfaceType": "USB", "MediaType": "Removable Media", "Letters": ["E:"],
        })
        drives = pi.parse_powershell_drives(raw)
        assert len(drives) == 1
        assert drives[0]["index"] == 0

    def test_letters_as_string_becomes_list(self):
        """Single-letter rows sometimes emit 'Letters': 'E:' instead of ['E:']."""
        raw = json.dumps([{
            "Index": 2, "Model": "X", "Size": 500,
            "InterfaceType": "USB", "MediaType": "Removable Media", "Letters": "E:",
        }])
        drives = pi.parse_powershell_drives(raw)
        assert drives[0]["letters"] == ["E:"]

    def test_letters_null_or_missing(self):
        raw = json.dumps([{
            "Index": 3, "Model": "X", "Size": 0,
            "InterfaceType": "", "MediaType": "", "Letters": None,
        }])
        drives = pi.parse_powershell_drives(raw)
        assert drives[0]["letters"] == []

    def test_empty_string_is_empty_list(self):
        assert pi.parse_powershell_drives("") == []
        assert pi.parse_powershell_drives("   ") == []

    def test_invalid_json(self):
        assert pi.parse_powershell_drives("not-json") == []

    def test_external_media_is_removable(self):
        raw = json.dumps([{
            "Index": 4, "Model": "X", "Size": 500,
            "InterfaceType": "USB", "MediaType": "External hard disk media", "Letters": [],
        }])
        assert pi.parse_powershell_drives(raw)[0]["removable"] is True


# ---------- wmic parser ----------

class TestParseWmicTable:
    def test_basic(self):
        out = (
            "Index  InterfaceType  MediaType              Model             Size         \n"
            "0      SCSI           Fixed hard disk media  Samsung SSD 860   1000204886016\n"
            "2      USB            Removable Media        USB Mass Storage  63861073920  \n"
        )
        rows = pi.parse_wmic_table(out)
        assert len(rows) == 2
        assert rows[0]["Index"] == "0"
        assert rows[0]["Model"] == "Samsung SSD 860"
        assert rows[1]["Index"] == "2"
        assert rows[1]["MediaType"] == "Removable Media"
        assert rows[1]["Size"] == "63861073920"

    def test_empty(self):
        assert pi.parse_wmic_table("") == []

    def test_header_only(self):
        assert pi.parse_wmic_table("Index  Model\n") == []


# ---------- Input validators ----------

class TestValidators:
    @pytest.mark.parametrize("s", ["MyNetwork", "x", "abc-123", " space ", "32charsOK_123456789012345678901"])
    def test_valid_ssids(self, s):
        ok, _ = pi.validate_ssid(s)
        assert ok, f"expected {s!r} to be valid"

    @pytest.mark.parametrize("s,why", [
        ("", "empty"),
        ("has\"quote", "quote"),
        ("has\\backslash", "backslash"),
        ("line\nbreak", "newline"),
        ("a" * 33, "too long"),
        ("tab\there", "non-printable"),
    ])
    def test_invalid_ssids(self, s, why):
        ok, reason = pi.validate_ssid(s)
        assert not ok, f"expected {s!r} to be invalid ({why})"
        assert reason

    def test_unicode_ssid_bytes_counted(self):
        # Emoji is 4 bytes in UTF-8; would pass char count but fail byte count if near limit.
        # Also emoji is non-ASCII so bad-char rule catches it.
        ok, _ = pi.validate_ssid("abc\U0001F600")
        assert not ok

    @pytest.mark.parametrize("s", ["", "password", "12345678", "a" * 63, "P@ssw0rd!"])
    def test_valid_wifi_passwords(self, s):
        ok, _ = pi.validate_wifi_password(s)
        assert ok, f"expected {s!r} valid"

    @pytest.mark.parametrize("s,why", [
        ("short", "<8"),
        ("a" * 64, ">63"),
        ("has\"quote", "quote"),
        ("back\\slash", "backslash"),
        ("line\nbreak", "newline"),
    ])
    def test_invalid_wifi_passwords(self, s, why):
        ok, _ = pi.validate_wifi_password(s)
        assert not ok, f"expected {s!r} invalid ({why})"

    @pytest.mark.parametrize("s", ["hokku", "pi", "h", "_user", "user-1", "a" * 32])
    def test_valid_usernames(self, s):
        ok, _ = pi.validate_username(s)
        assert ok, f"expected {s!r} valid"

    @pytest.mark.parametrize("s,why", [
        ("", "empty"),
        ("Admin", "uppercase start"),
        ("1user", "digit start"),
        ("user name", "space"),
        ("user$", "special"),
        ("a" * 33, "too long"),
    ])
    def test_invalid_usernames(self, s, why):
        ok, _ = pi.validate_username(s)
        assert not ok, f"expected {s!r} invalid ({why})"

    def test_valid_linux_passwords(self):
        for s in ["hokku", "P@ssw0rd!", "x"]:
            ok, _ = pi.validate_linux_password(s)
            assert ok, f"expected {s!r} valid"

    def test_invalid_linux_passwords(self):
        for s, _why in [("", "empty"), ("has:colon", "colon"), ("nl\nhere", "newline"),
                        ("has\"quote", "quote"), ("back\\slash", "backslash")]:
            ok, _ = pi.validate_linux_password(s)
            assert not ok, f"expected {s!r} invalid"


# ---------- Shell escaping ----------

class TestShellEscape:
    def test_noop_for_plain(self):
        assert pi._shell_escape("hello") == "hello"

    def test_escapes_dollar(self):
        assert pi._shell_escape("a$b") == "a\\$b"

    def test_escapes_backslash(self):
        assert pi._shell_escape("a\\b") == "a\\\\b"

    def test_escapes_quote(self):
        assert pi._shell_escape('say "hi"') == 'say \\"hi\\"'

    def test_escapes_backtick(self):
        assert pi._shell_escape("`cmd`") == "\\`cmd\\`"


# ---------- Shell script rendering ----------

class TestRenderFirstrun:
    def _cfg(self, **overrides):
        base = dict(
            hostname="hokku-server", wifi_ssid="MyWifi", wifi_pass="mypass1234",
            user="hokku", password="hokku", ssh_enabled=True, samba=False, server_ip="192.168.1.10",
        )
        base.update(overrides)
        return base

    def test_no_unsubstituted_braces(self):
        """No f-string placeholders should remain unexpanded."""
        script = pi._render_firstrun(self._cfg())
        # bash uses ${var} but not {var} on its own line with no $; any bare { that isn't
        # `$(...){...}` or `cmd {...}` should be flagged. Simplest check: no '{user}' etc left.
        for marker in ("{user}", "{password}", "{wifi_ssid}", "{wifi_pass}", "{hostname}"):
            assert marker not in script, f"unexpanded {marker!r}"

    def test_contains_user_and_hostname(self):
        script = pi._render_firstrun(self._cfg(user="alice", hostname="pi-alice"))
        assert "alice" in script
        assert "pi-alice" in script

    def test_ssh_enabled(self):
        assert 'systemctl enable ssh' in pi._render_firstrun(self._cfg(ssh_enabled=True))

    def test_ssh_disabled(self):
        assert 'systemctl disable ssh' in pi._render_firstrun(self._cfg(ssh_enabled=False))

    def test_samba_flag_triggers_marker(self):
        script = pi._render_firstrun(self._cfg(samba=True))
        assert "install-samba" in script

    def test_no_crlf_line_endings(self):
        """Bash scripts must have LF endings; the write path strips CRLF but the source shouldn't
        have any to begin with."""
        script = pi._render_firstrun(self._cfg())
        assert "\r\n" not in script


class TestRenderFirstboot:
    def test_samba_share_path_expanded(self):
        cfg = dict(user="alice", password="pw", samba=True, wifi_ssid="s", wifi_pass="p",
                   ssh_enabled=True, hostname="h", server_ip=None)
        script = pi._render_firstboot(cfg)
        assert "/home/alice" in script
        assert "{user}" not in script
        assert "{password}" not in script

    def test_installs_deb(self):
        cfg = dict(user="u", password="p", samba=False, wifi_ssid="s", wifi_pass="p",
                   ssh_enabled=True, hostname="h", server_ip=None)
        script = pi._render_firstboot(cfg)
        assert "hokku-server.deb" in script
        assert "systemctl enable hokku-server" in script


# ---------- .deb pre-flight ----------

class TestFindDebPackage:
    def test_finds_deb_in_cache(self, tmp_path, monkeypatch):
        cache = tmp_path / ".cache"
        cache.mkdir()
        (cache / "hokku-server_2.1.20_all.deb").write_bytes(b"fake")

        monkeypatch.setattr(pi, "CACHE_DIR", cache)
        monkeypatch.setattr(pi, "REPO_ROOT", tmp_path)
        result = pi.find_deb_package()
        assert result is not None
        assert result.name == "hokku-server_2.1.20_all.deb"

    def test_picks_latest_when_multiple(self, tmp_path, monkeypatch):
        cache = tmp_path / ".cache"
        cache.mkdir()
        (cache / "hokku-server_2.1.19_all.deb").write_bytes(b"v1")
        (cache / "hokku-server_2.1.20_all.deb").write_bytes(b"v2")
        monkeypatch.setattr(pi, "CACHE_DIR", cache)
        monkeypatch.setattr(pi, "REPO_ROOT", tmp_path)
        result = pi.find_deb_package()
        # sorted() is lexicographic but version sort works for these
        assert result.name == "hokku-server_2.1.20_all.deb"

    def test_returns_none_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pi, "CACHE_DIR", tmp_path / ".cache")
        monkeypatch.setattr(pi, "REPO_ROOT", tmp_path)
        assert pi.find_deb_package() is None


# ---------- .deb asset predicate (matching hokku-server_*.deb) ----------

class TestDebAssetPredicate:
    def test_matches_deb(self):
        assert pi._deb_name_matches("hokku-server_2.1.20-1_all.deb")

    def test_rejects_non_deb(self):
        assert not pi._deb_name_matches("bootloader.bin")
        assert not pi._deb_name_matches("hokku-server_2.1.20-1_all.txt")

    def test_rejects_other_packages(self):
        assert not pi._deb_name_matches("something-else_1.0_all.deb")
        assert not pi._deb_name_matches("hokku-other_1_all.deb")

    def test_empty_and_none(self):
        assert not pi._deb_name_matches("")


# ---------- SD drive guess ----------

class TestGuessSdDrive:
    def test_prefers_usb_removable(self):
        drives = [
            {"index": 0, "removable": False, "size_bytes": 1000 * 1024**3, "interface": "SCSI"},
            {"index": 2, "removable": True, "size_bytes": 64 * 1024**3, "interface": "USB"},
        ]
        picked = pi.guess_sd_drive(drives)
        assert picked["index"] == 2

    def test_ignores_fixed_disks(self):
        drives = [{"index": 0, "removable": False, "size_bytes": 64 * 1024**3, "interface": "USB"}]
        assert pi.guess_sd_drive(drives) is None

    def test_ignores_too_large(self):
        drives = [{"index": 0, "removable": True, "size_bytes": 500 * 1024**3, "interface": "USB"}]
        assert pi.guess_sd_drive(drives) is None

    def test_ignores_too_small(self):
        drives = [{"index": 0, "removable": True, "size_bytes": 1 * 1024**3, "interface": "USB"}]
        assert pi.guess_sd_drive(drives) is None


# ---------- fmt_gb ----------

class TestFmtGb:
    def test_zero(self):
        assert pi.fmt_gb(0) == "?"

    def test_64gb(self):
        assert pi.fmt_gb(64 * 1024**3) == "64.0 GB"
