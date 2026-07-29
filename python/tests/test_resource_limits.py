"""Unit tests for resource_limits: cgroup-aware memory/CPU detection."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hokku.webserver import resource_limits as rl

_MB = 1024 * 1024


# ── _read_int parsing ─────────────────────────────────────────────────────────


def test_read_int_plain(tmp_path):
    p = tmp_path / "v"
    p.write_text("314572800")
    assert rl._read_int(str(p)) == 314572800


def test_read_int_max_is_none(tmp_path):
    p = tmp_path / "v"
    p.write_text("max")
    assert rl._read_int(str(p)) is None


def test_read_int_takes_first_token(tmp_path):
    # cgroup v2 cpu.max is "<quota> <period>".
    p = tmp_path / "v"
    p.write_text("200000 100000")
    assert rl._read_int(str(p)) == 200000


def test_read_int_missing_file_is_none(tmp_path):
    assert rl._read_int(str(tmp_path / "nope")) is None


# ── memory: min(host, cgroup) ─────────────────────────────────────────────────


def _vm(total):
    m = MagicMock()
    m.total = total
    return m


def test_memory_uses_cgroup_when_lower():
    with (
        patch.object(rl.psutil, "virtual_memory", return_value=_vm(2000 * _MB)),
        patch.object(rl, "_cgroup_memory_limit_bytes", return_value=300 * _MB),
    ):
        assert rl.detect_memory_limit_bytes() == 300 * _MB


def test_memory_falls_back_to_host_when_no_cgroup():
    with (
        patch.object(rl.psutil, "virtual_memory", return_value=_vm(2000 * _MB)),
        patch.object(rl, "_cgroup_memory_limit_bytes", return_value=None),
    ):
        assert rl.detect_memory_limit_bytes() == 2000 * _MB


def test_memory_uses_host_when_cgroup_higher():
    with (
        patch.object(rl.psutil, "virtual_memory", return_value=_vm(500 * _MB)),
        patch.object(rl, "_cgroup_memory_limit_bytes", return_value=4000 * _MB),
    ):
        assert rl.detect_memory_limit_bytes() == 500 * _MB


# ── cpu: min(count, affinity, cgroup quota) ───────────────────────────────────


def test_cpu_uses_cgroup_quota_when_lower():
    with (
        patch("os.cpu_count", return_value=24),
        patch("os.sched_getaffinity", return_value=set(range(24)), create=True),
        patch.object(rl, "_cgroup_cpu_limit", return_value=2.0),
    ):
        assert rl.detect_cpu_limit() == 2


def test_cpu_rounds_fractional_quota_up():
    with (
        patch("os.cpu_count", return_value=24),
        patch("os.sched_getaffinity", return_value=set(range(24)), create=True),
        patch.object(rl, "_cgroup_cpu_limit", return_value=1.5),
    ):
        assert rl.detect_cpu_limit() == 2


def test_cpu_falls_back_to_count_when_no_cgroup():
    with (
        patch("os.cpu_count", return_value=8),
        patch("os.sched_getaffinity", return_value=set(range(8)), create=True),
        patch.object(rl, "_cgroup_cpu_limit", return_value=None),
    ):
        assert rl.detect_cpu_limit() == 8


def test_cpu_always_at_least_1():
    with (
        patch("os.cpu_count", return_value=None),
        patch.object(rl, "_cgroup_cpu_limit", return_value=None),
    ):
        assert rl.detect_cpu_limit() >= 1
