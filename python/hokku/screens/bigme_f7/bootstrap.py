"""Drive a fresh/stock Bigme F7 into Hokku firmware from the web "Flash a screen" UI.

Wraps the proven pure-Python mask-BROM catch + safe slot-0 write (the flashers in
``tools/``) in a callback-streamed, cancellable routine the web flash job can run.
The catch waits for the operator to power-cycle the unit with a **USB replug + power
press** (the replug brings the CH340 port up first, so we hammer ``0x55`` straight
through the BROM sync window — a plain long-press drops the port and misses it).

Safety is inherited unchanged from ``flash_slot0``: only slot 0 + its A/B cfg sector
are written, the bootloader and OEM slot 1 are never touched, the cfg flip is the
last write, and any header/verify failure aborts via ``die()`` (which we surface as
an error) leaving slot 1 bootable. Nothing here relaxes those checks.

The flash primitives live in the dev-tree ``tools/`` directory (not packaged), so
this feature is only available where that directory is present; callers must gate on
:func:`tooling_available`.
"""

from __future__ import annotations

import contextlib
import io
import sys
import time
from pathlib import Path
from typing import Callable

# python/hokku/screens/bigme_f7/bootstrap.py -> repo root is parents[4]
_REPO_ROOT = Path(__file__).resolve().parents[4]
_TOOLS = _REPO_ROOT / "tools"

CATCH_TIMEOUT_S = 300.0


def tooling_available() -> bool:
    """True if the dev-tree flash primitives this module needs are present."""
    return (_TOOLS / "flash_candidate_slot0.py").exists() and (_TOOLS / "xr872_flasher.py").exists()


class _LineWriter(io.TextIOBase):
    """A text sink that forwards complete lines to ``emit`` (for redirect_stdout)."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._buf = ""

    def write(self, s: str) -> int:  # type: ignore[override]
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line.rstrip("\r"))
        return len(s)

    def flush(self) -> None:
        if self._buf:
            self._emit(self._buf.rstrip("\r"))
            self._buf = ""


def _import_tools():
    if str(_TOOLS) not in sys.path:
        sys.path.insert(0, str(_TOOLS))
    import serial  # noqa: PLC0415

    # These live in the dev-tree tools/ dir (added to sys.path just above), so the
    # static checkers can't resolve them — that's expected, gate on tooling_available.
    from flash_candidate_slot0 import (  # noqa: PLC0415  # pyright: ignore[reportMissingImports]
        flash_slot0,
    )
    from flash_candidate_slot0_catch import (  # noqa: PLC0415  # pyright: ignore[reportMissingImports]
        hammer_sync,
        open_stable,
    )
    from xr872_flasher import XR872Flasher  # noqa: PLC0415  # pyright: ignore[reportMissingImports]

    return flash_slot0, hammer_sync, open_stable, XR872Flasher, serial


def bootstrap_device(
    port: str,
    image_path: str | Path,
    on_line: Callable[[str], None],
    should_cancel: Callable[[], bool],
    timeout_s: float = CATCH_TIMEOUT_S,
) -> dict:
    """Catch the BROM (operator does the replug+press) and write slot 0.

    Streams progress line-by-line via ``on_line``; polls ``should_cancel`` between
    attempts. Returns ``{"ok": True}`` on success. Raises ``RuntimeError`` on
    timeout, cancel, or a safety abort — never leaves the unit unbootable (slot 1
    stays intact throughout)."""
    if not tooling_available():
        raise RuntimeError("Bigme F7 flash tooling (tools/) is not present on this install")
    flash_slot0, hammer_sync, open_stable, XR872Flasher, serial = _import_tools()

    img = Path(image_path).read_bytes()
    if img[:4] != b"AWIH":
        raise RuntimeError("bundled image is not an AWIH xr_system.img")

    on_line(f"Bootstrapping Bigme F7 on {port} ({len(img):,} B image).")
    on_line("Enter the BROM: UNPLUG the USB cable -> REPLUG it -> PRESS the power button.")
    on_line(f"Repeat the replug+press until it catches (waiting up to {int(timeout_s)} s)...")
    writer = _LineWriter(on_line)

    deadline = time.monotonic() + timeout_s
    last_note = 0.0
    while time.monotonic() < deadline:
        if should_cancel():
            raise RuntimeError("cancelled by the operator")
        try:
            ser = open_stable(port)
        except (serial.SerialException, OSError):
            if time.monotonic() - last_note > 4:
                on_line("  waiting for the USB port... replug the cable, then press power")
                last_note = time.monotonic()
            time.sleep(0.15)
            continue
        try:
            try:
                caught = hammer_sync(ser, duration=1.5)
            except (serial.SerialException, OSError):
                caught = False  # port yanked mid-hammer (the replug) — retry
            if caught:
                on_line("*** BROM caught — writing firmware (do NOT unplug now) ***")
                f = XR872Flasher(ser=ser, verbose=False)
                if not f.sync(attempts=30, timeout_per=0.1):
                    on_line("  command channel didn't come up — keep replug+pressing")
                    continue
                # flash_slot0 prints its own progress and raises SystemExit via die()
                # on ANY safety-check failure, which leaves slot 1 (OEM) bootable.
                # reboot=False: sys_reboot only re-enters BROM on this chip, so the
                # operator power-cycles to boot the app.
                try:
                    with contextlib.redirect_stdout(writer):
                        flash_slot0(f, img, reboot=False)
                    writer.flush()
                except SystemExit as e:
                    writer.flush()
                    raise RuntimeError(f"safety check aborted the write: {e}") from e
                on_line("")
                on_line("DONE — Hokku firmware in slot 0 (bootloader + OEM slot untouched).")
                on_line("Next: POWER-CYCLE the unit (unplug/replug, or long-press) to boot it.")
                on_line("Then set Wi-Fi over the console (115200): `wifi <ssid> <pw>`, `cfg save`.")
                on_line("Details: docs/screens/bigme_f7/bootstrap.md")
                return {"ok": True}
        finally:
            with contextlib.suppress(Exception):
                ser.close()
        if time.monotonic() - last_note > 4:
            on_line("  hammering 0x55 — replug the USB, then press the power button")
            last_note = time.monotonic()

    raise RuntimeError("no BROM catch within the time limit — retry the replug+press")
