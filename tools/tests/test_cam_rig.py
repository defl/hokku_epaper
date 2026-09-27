"""Regression tests for the camera rig's file transfer.

Both behaviours pinned here cost an unattended capture run, and both failed
*silently* — `scp` exits 0 in each case, so the only symptom was a file that
never appeared, surfacing much later as a parse error or a missing frame.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import cam_rig

# Paths on the Pi, not on this host.
REMOTE = "/tmp/x.dng"  # noqa: S108
REMOTE_AWKWARD = "/tmp/sp ace(2).dng"  # noqa: S108


class FakeScp:
    """Stands in for scp + ssh, refusing bracketed local paths as Windows does."""

    def __init__(self, payload=b"x" * 32, break_on_brackets=True):
        self.payload = payload
        self.break_on_brackets = break_on_brackets
        self.local_paths: list[str] = []

    def __call__(self, cmd, **kwargs):
        if cmd[0] == "scp":
            local = cmd[-1]
            self.local_paths.append(local)
            # The real failure: nothing written, and still exit 0.
            if not (self.break_on_brackets and ("(" in local or ")" in local)):
                Path(local).write_bytes(self.payload)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        # Anything else is the `stat -c %s` probe.
        return subprocess.CompletedProcess(cmd, 0, str(len(self.payload)), "")


class TestScpFromPi:
    def test_delivers_to_a_bracketed_destination(self, tmp_path, monkeypatch):
        """The whole point: a name like IMG_4179(2).HEIC must still arrive."""
        fake = FakeScp()
        monkeypatch.setattr(cam_rig.subprocess, "run", fake)
        dest = tmp_path / "IMG_4179(2)__baseline__shot.dng"
        cam_rig._scp_from_pi("h", Path("k"), REMOTE, dest)
        assert dest.exists()
        assert dest.read_bytes() == fake.payload

    def test_copies_via_a_sanitised_temporary_name(self, tmp_path, monkeypatch):
        fake = FakeScp()
        monkeypatch.setattr(cam_rig.subprocess, "run", fake)
        dest = tmp_path / "has (brackets) and spaces.dng"
        cam_rig._scp_from_pi("h", Path("k"), REMOTE, dest)
        assert fake.local_paths, "scp was never invoked"
        for path in fake.local_paths:
            name = Path(path).name
            assert "(" not in name and ")" not in name and " " not in name
        assert not list(tmp_path.glob(".scp_*")), "temporary file left behind"

    def test_remote_path_is_never_shell_quoted(self, tmp_path, monkeypatch):
        """OpenSSH 9+ uses SFTP and passes the path literally; quotes break it."""
        fake = FakeScp()
        monkeypatch.setattr(cam_rig.subprocess, "run", fake)
        captured = {}

        def spy(cmd, **kwargs):
            if cmd[0] == "scp":
                captured["remote"] = cmd[-2]
            return fake(cmd, **kwargs)

        monkeypatch.setattr(cam_rig.subprocess, "run", spy)
        cam_rig._scp_from_pi("host", Path("k"), REMOTE_AWKWARD, tmp_path / "o.dng")
        assert captured["remote"] == "host:/tmp/sp ace(2).dng"
        assert "'" not in captured["remote"]

    def test_raises_when_nothing_is_written(self, tmp_path, monkeypatch):
        """scp exiting 0 with no file must not pass as success."""

        def silent(cmd, **kwargs):
            if cmd[0] == "scp":
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, "1024", "")

        monkeypatch.setattr(cam_rig.subprocess, "run", silent)
        monkeypatch.setattr(cam_rig.time, "sleep", lambda _s: None)
        with pytest.raises(RuntimeError, match="wrote no file"):
            cam_rig._scp_from_pi("h", Path("k"), REMOTE, tmp_path / "o.dng")

    def test_raises_on_a_truncated_transfer(self, tmp_path, monkeypatch):
        """A short read is what a full tmpfs on the Pi produces."""

        def short(cmd, **kwargs):
            if cmd[0] == "scp":
                Path(cmd[-1]).write_bytes(b"partial")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, "99999", "")

        monkeypatch.setattr(cam_rig.subprocess, "run", short)
        monkeypatch.setattr(cam_rig.time, "sleep", lambda _s: None)
        with pytest.raises(RuntimeError, match="truncated"):
            cam_rig._scp_from_pi("h", Path("k"), REMOTE, tmp_path / "o.dng")

    def test_retries_then_succeeds(self, tmp_path, monkeypatch):
        """A race against a file still being written should recover, not fail."""
        state = {"n": 0}

        def flaky(cmd, **kwargs):
            if cmd[0] == "scp":
                state["n"] += 1
                if state["n"] > 1:
                    Path(cmd[-1]).write_bytes(b"y" * 16)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, "16", "")

        monkeypatch.setattr(cam_rig.subprocess, "run", flaky)
        monkeypatch.setattr(cam_rig.time, "sleep", lambda _s: None)
        dest = tmp_path / "out(1).dng"
        cam_rig._scp_from_pi("h", Path("k"), REMOTE, dest)
        assert dest.read_bytes() == b"y" * 16
        assert state["n"] == 2
