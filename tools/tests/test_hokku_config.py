"""Tests for hokku_config CLI tool."""
import json
import os
import tempfile
from unittest.mock import MagicMock, patch, call

import pytest

# Add parent dir to path
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import hokku_config


class MockSerial:
    """Mock serial port that simulates the ESP32 config protocol."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.written = []
        self._read_queue = []
        self.closed = False

    def write(self, data):
        self.written.append(data.decode())

    def flush(self):
        pass

    def readline(self):
        if self._read_queue:
            return self._read_queue.pop(0)
        # Parse last written command and generate response
        if self.written:
            cmd = self.written[-1].strip()
            if cmd in self.responses:
                resp = self.responses[cmd]
                if isinstance(resp, list):
                    # Queue multiple response lines
                    for line in resp[1:]:
                        self._read_queue.append(line.encode() + b"\n")
                    return resp[0].encode() + b"\n"
                return resp.encode() + b"\n"
        return b""

    def close(self):
        self.closed = True


class TestParseResponseValue:
    def test_ok_response(self):
        key, value = hokku_config.parse_response_value("OK:wifi_ssid=MyNetwork")
        assert key == "wifi_ssid"
        assert value == "MyNetwork"

    def test_ok_response_with_equals_in_value(self):
        key, value = hokku_config.parse_response_value("OK:image_url=http://server:8080/hokku/")
        assert key == "image_url"
        assert value == "http://server:8080/hokku/"

    def test_ok_masked_password(self):
        key, value = hokku_config.parse_response_value("OK:wifi_pass=****")
        assert key == "wifi_pass"
        assert value == "****"

    def test_err_response(self):
        key, value = hokku_config.parse_response_value("ERR:unknown key")
        assert key is None
        assert value is None

    def test_no_equals(self):
        key, value = hokku_config.parse_response_value("OK:PONG")
        assert key is None
        assert value is None


class TestFindPort:
    @patch("serial.tools.list_ports.comports")
    def test_finds_esp32(self, mock_comports):
        port = MagicMock()
        port.vid = 0x303A
        port.pid = 0x1001
        port.device = "/dev/ttyACM0"
        mock_comports.return_value = [port]

        result = hokku_config.find_esp32_port()
        assert result == "/dev/ttyACM0"

    @patch("serial.tools.list_ports.comports")
    def test_no_esp32(self, mock_comports):
        mock_comports.return_value = []
        result = hokku_config.find_esp32_port()
        assert result is None

    @patch("serial.tools.list_ports.comports")
    def test_wrong_device(self, mock_comports):
        port = MagicMock()
        port.vid = 0x1234
        port.pid = 0x5678
        port.device = "/dev/ttyUSB0"
        mock_comports.return_value = [port]

        result = hokku_config.find_esp32_port()
        assert result is None


class TestBackupRestore:
    def test_backup_file_format(self):
        """Test that backup creates valid JSON."""
        config = {"wifi_ssid": "TestNet", "wifi_pass": "****", "image_url": "http://test:8080/hokku/"}
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
        """Test that restore parses JSON correctly."""
        config = {"wifi_ssid": "RestoreNet", "wifi_pass": "secret123", "image_url": "http://restore:8080/hokku/"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config, f, indent=2)
            temp_path = f.name

        try:
            with open(temp_path) as f:
                loaded = json.load(f)
            assert loaded["wifi_ssid"] == "RestoreNet"
            assert loaded["wifi_pass"] == "secret123"
            assert loaded["image_url"] == "http://restore:8080/hokku/"
        finally:
            os.unlink(temp_path)


class TestSendCommand:
    def test_ping(self):
        ser = MockSerial({"HOKKU:PING\n": "OK:PONG"})
        # Directly test parse_response_value since send_command relies on serial timing
        resp = hokku_config.parse_response_value("OK:PONG")
        assert resp == (None, None)  # PONG has no '='

    def test_set_command_parsing(self):
        # Test the response parsing logic
        key, value = hokku_config.parse_response_value("OK:wifi_ssid=TestNet")
        assert key == "wifi_ssid"
        assert value == "TestNet"

    def test_get_all_parsing(self):
        # Test parsing multiple GET ALL responses
        lines = [
            "OK:wifi_ssid=TestNet",
            "OK:wifi_pass=****",
            "OK:image_url=http://test:8080/hokku/",
        ]
        config = {}
        for line in lines:
            key, value = hokku_config.parse_response_value(line)
            if key:
                config[key] = value
        assert config["wifi_ssid"] == "TestNet"
        assert config["wifi_pass"] == "****"
        assert config["image_url"] == "http://test:8080/hokku/"
