"""Tests for hokku_config CLI tool (NVS partition flashing approach)."""

import json
import os
import struct
import sys
import tempfile
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import hokku_config
from hokku.screens import seeedstudio_e1004


class TestFindPort:
    @patch("serial.tools.list_ports.comports")
    def test_finds_esp32(self, mock_comports):
        port = MagicMock()
        port.vid = 0x303A
        port.pid = 0x1001
        port.device = "/dev/ttyACM0"
        mock_comports.return_value = [port]
        assert hokku_config.find_esp32_port() == "/dev/ttyACM0"

    @patch("serial.tools.list_ports.comports")
    def test_finds_e1004_behind_ch340k(self, mock_comports):
        # The E1004's USB-C is a CH340K bridge (1A86:7522), never 303A:1001 (#45).
        port = MagicMock()
        port.vid = 0x1A86
        port.pid = 0x7522
        port.device = "COM12"
        mock_comports.return_value = [port]
        assert hokku_config.find_esp32_port() == "COM12"

    @patch("serial.tools.list_ports.comports")
    def test_ignores_bigme_f7_ch340(self, mock_comports):
        # The F7's CH340 (1A86:7523) is not an ESP32 screen.
        port = MagicMock()
        port.vid = 0x1A86
        port.pid = 0x7523
        port.device = "COM9"
        mock_comports.return_value = [port]
        assert hokku_config.find_esp32_port() is None

    @patch("serial.tools.list_ports.comports")
    def test_no_esp32(self, mock_comports):
        mock_comports.return_value = []
        assert hokku_config.find_esp32_port() is None

    @patch("serial.tools.list_ports.comports")
    def test_wrong_device(self, mock_comports):
        port = MagicMock()
        port.vid = 0x1234
        port.pid = 0x5678
        port.device = "/dev/ttyUSB0"
        mock_comports.return_value = [port]
        assert hokku_config.find_esp32_port() is None


class TestNvsBinaryGeneration:
    def test_build_produces_correct_size(self):
        """Generated binary is exactly NVS_SIZE bytes."""
        binary = hokku_config._build_nvs_binary({"wifi_ssid1": "test"})
        assert len(binary) == hokku_config.NVS_SIZE

    def test_roundtrip_single_key(self):
        """Write and read back a single key."""
        config = {"wifi_ssid1": "MyNetwork"}
        binary = hokku_config._build_nvs_binary(config)
        result = hokku_config._read_nvs(binary)
        assert result.get("wifi_ssid1") == "MyNetwork"

    def test_roundtrip_with_screen_name(self):
        """screen_name survives roundtrip."""
        config = {"wifi_ssid1": "Test", "screen_name": "Living Room"}
        binary = hokku_config._build_nvs_binary(config)
        result = hokku_config._read_nvs(binary)
        assert result["screen_name"] == "Living Room"

    def test_roundtrip_primary_network(self):
        """Primary wifi credentials survive roundtrip."""
        config = {
            "wifi_ssid1": "PrimaryNet",
            "wifi_pass1": "secret123",
            "image_url": "http://192.168.1.100:8080/hokku/screen/",
        }
        binary = hokku_config._build_nvs_binary(config)
        result = hokku_config._read_nvs(binary)
        assert result["wifi_ssid1"] == "PrimaryNet"
        assert result["wifi_pass1"] == "secret123"
        assert result["image_url"] == "http://192.168.1.100:8080/hokku/screen/"

    def test_roundtrip_both_networks(self):
        """Primary and secondary wifi credentials both survive roundtrip."""
        config = {
            "wifi_ssid1": "PrimaryNet",
            "wifi_pass1": "primary_pw",
            "wifi_ssid2": "BackupNet",
            "wifi_pass2": "backup_pw",
            "image_url": "http://192.168.1.100:8080/hokku/screen/",
        }
        binary = hokku_config._build_nvs_binary(config)
        result = hokku_config._read_nvs(binary)
        assert result["wifi_ssid1"] == "PrimaryNet"
        assert result["wifi_pass1"] == "primary_pw"
        assert result["wifi_ssid2"] == "BackupNet"
        assert result["wifi_pass2"] == "backup_pw"

    def test_roundtrip_secondary_absent(self):
        """Config with only primary network has no secondary keys."""
        config = {"wifi_ssid1": "PrimaryNet", "image_url": "http://h:8080/hokku/screen/"}
        binary = hokku_config._build_nvs_binary(config)
        result = hokku_config._read_nvs(binary)
        assert "wifi_ssid2" not in result
        assert "wifi_pass2" not in result

    def test_empty_config(self):
        """Empty config still has cfg_ver and wifi_order."""
        binary = hokku_config._build_nvs_binary({})
        result = hokku_config._read_nvs(binary)
        assert result == {"cfg_ver": hokku_config.CONFIG_VERSION, "wifi_order": 0}

    def test_config_version_written(self):
        """cfg_ver is always written as uint8."""
        binary = hokku_config._build_nvs_binary({"wifi_ssid": "test"})
        result = hokku_config._read_nvs(binary)
        assert result["cfg_ver"] == hokku_config.CONFIG_VERSION
        assert isinstance(result["cfg_ver"], int)

    def test_long_url(self):
        """Long URL values survive roundtrip."""
        long_url = "http://very-long-hostname.example.com:8080/hokku/with/extra/path"
        config = {"image_url": long_url}
        binary = hokku_config._build_nvs_binary(config)
        result = hokku_config._read_nvs(binary)
        assert result["image_url"] == long_url

    def test_page_header_valid(self):
        """Page header has correct state and version."""
        binary = hokku_config._build_nvs_binary({"wifi_ssid1": "x"})
        state = struct.unpack_from("<I", binary, 0)[0]
        assert state == hokku_config.PAGE_ACTIVE
        assert binary[8] == 0xFE  # NVS version 2

    def test_read_empty_partition(self):
        """Reading all-0xFF partition returns empty dict."""
        empty = b"\xff" * hokku_config.NVS_SIZE
        result = hokku_config._read_nvs(empty)
        assert result == {}

    def test_read_short_data(self):
        """Reading too-short data returns empty dict."""
        result = hokku_config._read_nvs(b"\xff" * 100)
        assert result == {}


class TestBackupRestore:
    def test_backup_file_format(self):
        """Backup creates valid JSON."""
        config = {
            "wifi_ssid1": "TestNet",
            "wifi_pass1": "secret",
            "image_url": "http://test:8080/hokku/",
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config, f, indent=2)
            temp_path = f.name
        try:
            with open(temp_path) as f:
                loaded = json.load(f)
            assert loaded == config
        finally:
            os.unlink(temp_path)

    def test_restore_reads_json(self):
        """Restore parses JSON correctly."""
        config = {
            "wifi_ssid1": "RestoreNet",
            "wifi_pass1": "secret123",
            "image_url": "http://restore:8080/hokku/",
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config, f, indent=2)
            temp_path = f.name
        try:
            with open(temp_path) as f:
                loaded = json.load(f)
            assert loaded["wifi_ssid1"] == "RestoreNet"
            assert loaded["wifi_pass1"] == "secret123"
        finally:
            os.unlink(temp_path)

    def test_backup_dir_creation(self):
        """Backup directory is created if missing."""
        d = hokku_config.backup_dir()
        assert d.exists()


class TestBaud:
    @patch("serial.tools.list_ports.comports")
    def test_uses_the_screens_spec_baud(self, mock_comports):
        port = MagicMock()
        port.vid, port.pid, port.device = 0x1A86, 0x7522, "COM12"
        mock_comports.return_value = [port]
        assert hokku_config.port_baud("COM12") == seeedstudio_e1004.SPEC.baud

    @patch("serial.tools.list_ports.comports")
    def test_override_wins(self, mock_comports, monkeypatch):
        mock_comports.return_value = []
        monkeypatch.setattr(hokku_config, "BAUD_OVERRIDE", "115200")
        assert hokku_config._baud("COM12") == "115200"


class TestSetReadFailure:
    def test_set_aborts_without_writing_when_the_read_fails(self, monkeypatch):
        # A failed read must not become "empty config" and overwrite the device
        # with only the flags given (#45 follow-up).
        def failing_read(port):
            raise RuntimeError("A fatal error occurred: Failed to connect")

        writes = []
        monkeypatch.setattr(hokku_config, "_read_nvs_from_device", failing_read)
        monkeypatch.setattr(hokku_config, "_flash_nvs", lambda *a: writes.append(a))
        args = MagicMock(port="COM12", ssid="net", url="http://x/hokku/screen/")
        try:
            hokku_config.cmd_set(args)
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("cmd_set should exit on a failed read")
        assert writes == []
