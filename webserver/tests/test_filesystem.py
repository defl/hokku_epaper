"""Tests for hokku_server.filesystem.atomic_write_json."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from hokku_server.filesystem import atomic_write_json

_REPLACE = "hokku_server.filesystem._replace"
_SLEEP = "hokku_server.filesystem._sleep"


def test_creates_file(tmp_path: Path):
    path = tmp_path / "db.json"
    atomic_write_json(path, {"key": "value"})
    assert path.exists()
    assert json.loads(path.read_text()) == {"key": "value"}


def test_overwrites_existing(tmp_path: Path):
    path = tmp_path / "db.json"
    atomic_write_json(path, {"v": 1})
    atomic_write_json(path, {"v": 2})
    assert json.loads(path.read_text()) == {"v": 2}


def test_no_tmp_file_left_on_success(tmp_path: Path):
    path = tmp_path / "db.json"
    atomic_write_json(path, {})
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_retries_on_permission_error(tmp_path: Path):
    path = tmp_path / "db.json"
    real_replace = os.replace
    test_thread = threading.current_thread()
    call_count = 0

    def flaky(src, dst):
        nonlocal call_count
        if threading.current_thread() is not test_thread:
            real_replace(src, dst)
            return
        call_count += 1
        if call_count < 3:
            raise PermissionError("locked")
        real_replace(src, dst)

    with patch(_REPLACE, side_effect=flaky), patch(_SLEEP):
        atomic_write_json(path, {"x": 1})

    assert call_count == 3
    assert json.loads(path.read_text()) == {"x": 1}


def test_raises_after_max_retries(tmp_path: Path):
    test_thread = threading.current_thread()
    real_replace = os.replace

    def flaky(src, dst):
        if threading.current_thread() is not test_thread:
            real_replace(src, dst)
            return
        raise PermissionError("locked")

    with patch(_REPLACE, side_effect=flaky), patch(_SLEEP):
        with pytest.raises(PermissionError):
            atomic_write_json(tmp_path / "db.json", {})


def test_sleep_durations_between_retries(tmp_path: Path):
    path = tmp_path / "db.json"
    real_replace = os.replace
    test_thread = threading.current_thread()
    call_count = 0
    sleep_calls: list[float] = []

    def flaky(src, dst):
        nonlocal call_count
        if threading.current_thread() is not test_thread:
            real_replace(src, dst)
            return
        call_count += 1
        if call_count < 5:
            raise PermissionError("locked")
        real_replace(src, dst)

    def track_sleep(s):
        if threading.current_thread() is test_thread:
            sleep_calls.append(s)

    with patch(_REPLACE, side_effect=flaky), \
         patch(_SLEEP, side_effect=track_sleep):
        atomic_write_json(path, {})

    assert sleep_calls == pytest.approx([0.05, 0.10, 0.20, 0.40])
