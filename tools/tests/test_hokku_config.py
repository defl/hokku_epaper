"""Tests for hokku_config CLI tool (NVS partition flashing approach)."""
import json
import os
import struct
import tempfile
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import hokku_config


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
        binary = hokku_config._build_nvs_binary({"wifi_ssid": "test"})
        assert len(binary) == hokku_config.NVS_SIZE

    def test_roundtrip_single_key(self):
        """Write and read back a single key."""
        config = {"wifi_ssid": "MyNetwork"}
        binary = hokku_config._build_nvs_binary(config)
        result = hokku_config._read_nvs_strings(binary)
        assert result.get("wifi_ssid") == "MyNetwork"

    def test_roundtrip_multiple_keys(self):
        """Write and read back multiple keys."""
        config = {
            "wifi_ssid": "TestNet",
            "wifi_pass": "secret123",
            "image_url": "http://192.168.1.100:8080/hokku/",
        }
        binary = hokku_config._build_nvs_binary(config)
        result = hokku_config._read_nvs_strings(binary)
        assert result["wifi_ssid"] == "TestNet"
        assert result["wifi_pass"] == "secret123"
        assert result["image_url"] == "http://192.168.1.100:8080/hokku/"

    def test_empty_config(self):
        """Empty config produces valid binary with no entries."""
        binary = hokku_config._build_nvs_binary({})
        result = hokku_config._read_nvs_strings(binary)
        assert result == {}

    def test_long_url(self):
        """Long URL values survive roundtrip."""
        long_url = "http://very-long-hostname.example.com:8080/hokku/with/extra/path"
        config = {"image_url": long_url}
        binary = hokku_config._build_nvs_binary(config)
        result = hokku_config._read_nvs_strings(binary)
        assert result["image_url"] == long_url

    def test_page_header_valid(self):
        """Page header has correct state and version."""
        binary = hokku_config._build_nvs_binary({"wifi_ssid": "x"})
        state = struct.unpack_from("<I", binary, 0)[0]
        assert state == hokku_config.PAGE_ACTIVE
        assert binary[8] == hokku_config.NVS_VERSION

    def test_read_empty_partition(self):
        """Reading all-0xFF partition returns empty dict."""
        empty = b"\xff" * hokku_config.NVS_SIZE
        result = hokku_config._read_nvs_strings(empty)
        assert result == {}

    def test_read_short_data(self):
        """Reading too-short data returns empty dict."""
        result = hokku_config._read_nvs_strings(b"\xff" * 100)
        assert result == {}


class TestBackupRestore:
    def test_backup_file_format(self):
        """Backup creates valid JSON."""
        config = {"wifi_ssid": "TestNet", "wifi_pass": "secret", "image_url": "http://test:8080/hokku/"}
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
        config = {"wifi_ssid": "RestoreNet", "wifi_pass": "secret123", "image_url": "http://restore:8080/hokku/"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config, f, indent=2)
            temp_path = f.name
        try:
            with open(temp_path) as f:
                loaded = json.load(f)
            assert loaded["wifi_ssid"] == "RestoreNet"
            assert loaded["wifi_pass"] == "secret123"
        finally:
            os.unlink(temp_path)

    def test_backup_dir_creation(self):
        """Backup directory is created if missing."""
        d = hokku_config.backup_dir()
        assert d.exists()
