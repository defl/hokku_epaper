"""Drive a fresh/stock Bigme F7 into Hokku firmware from the web "Flash a screen" UI.

Wraps the proven pure-Python mask-BROM catch + safe slot-0 write (the flashers in
:mod:`hokku.common.xr872`) in a callback-streamed, cancellable routine the web flash
job can run. The catch waits for the operator to power-cycle the unit with a **USB
replug + power press** (the replug brings the CH340 port up first, so we hammer
``0x55`` straight through the BROM sync window — a plain long-press drops the port
and misses it).

Safety is inherited unchanged from ``flash_slot``: only slot 0 + its A/B cfg sector
are written, the bootloader and OEM slot 1 are never touched, the cfg flip is the
last write, and any header/verify failure aborts via ``die()`` (which we surface as
an error) leaving slot 1 bootable. Nothing here relaxes those checks.

The flash primitives live in the packaged :mod:`hokku.common.xr872`, so this feature
ships with the server (including the appliance .deb); :func:`tooling_available` now
only guards against a missing ``pyserial``.
"""

from __future__ import annotations

import contextlib
import io
import sys
import time
from pathlib import Path
from typing import Callable

from hokku.screens.bigme_f7.config import write_config_via_brom

CATCH_TIMEOUT_S = 300.0


def tooling_available() -> bool:
    """True if the XR872 flash primitives (and pyserial) can be imported.

    They now live in the packaged :mod:`hokku.common.xr872`, so this is true on
    any normal install (including the appliance .deb) — the only way it comes
    back false is a missing ``pyserial``.
    """
    try:
        import serial  # noqa: F401, PLC0415

        from hokku.common.xr872 import catch, flasher, slots  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


class _LineWriter(io.TextIOBase):
    """A text sink that forwards complete lines to ``emit`` (for redirect_stdout).

    Reentrancy-guarded: this sink is installed as ``sys.stdout``, so an ``emit``
    callback that itself prints (the obvious thing for a CLI caller to pass) would
    re-enter :meth:`write` and recurse until ``RecursionError`` — which in practice
    aborted a flash immediately after the BROM was caught.

    Nested writes therefore bypass the line buffer and go straight to the real
    interpreter stdout: they stay visible, but they are never fed back through
    ``emit`` (appending them to the buffer instead just turns the recursion into
    an equally fatal infinite loop, since draining re-emits what the callback
    printed).
    """

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._buf = ""
        self._emitting = False

    def _write_passthrough(self, s: str) -> None:
        """Send text emitted from *inside* the callback to the real stdout."""
        with contextlib.suppress(Exception):
            if sys.__stdout__ is not None:
                sys.__stdout__.write(s)

    def _drain(self, final: bool = False) -> None:
        """Emit buffered lines. Callers must hold the reentrancy guard."""
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line.rstrip("\r"))
        if final and self._buf:
            self._emit(self._buf.rstrip("\r"))
            self._buf = ""

    def write(self, s: str) -> int:  # type: ignore[override]
        if self._emitting:  # the callback itself printed — don't re-enter emit
            self._write_passthrough(s)
            return len(s)
        self._buf += s
        self._emitting = True
        try:
            self._drain()
        finally:
            self._emitting = False
        return len(s)

    def flush(self) -> None:
        if self._emitting or not self._buf:
            return
        self._emitting = True
        try:
            self._drain(final=True)
        finally:
            self._emitting = False


def _import_tools():
    # Imported lazily (inside the flash routine) so pyserial and the flasher
    # aren't pulled in just by importing this module.
    import serial  # noqa: PLC0415

    from hokku.common.xr872.catch import hammer_sync, open_stable  # noqa: PLC0415
    from hokku.common.xr872.flasher import XR872Flasher, send_upgrade_command  # noqa: PLC0415
    from hokku.common.xr872.slots import flash_slot  # noqa: PLC0415

    return flash_slot, hammer_sync, open_stable, XR872Flasher, send_upgrade_command, serial


def _software_entry(port, writer, should_cancel, XR872Flasher, send_upgrade_command, attempts):
    """No-touch BROM entry for a unit already running Hokku firmware.

    ``upgrade`` on our console sets the boot flag and watchdog-resets into the BROM,
    so no replug/press is needed. Returns a synced ``XR872Flasher`` (caller closes
    it), or ``None`` if the unit never answered — i.e. it's stock, and the caller
    should fall back to the manual catch. Non-destructive: stock firmware just
    ignores the console line."""
    for _ in range(attempts):
        if should_cancel():
            raise RuntimeError("cancelled by the operator")
        with contextlib.suppress(Exception), contextlib.redirect_stdout(writer):
            send_upgrade_command(port)  # sends `upgrade\n`; harmless on a stock unit
        writer.flush()
        time.sleep(0.4)
        try:
            ft = XR872Flasher(port, verbose=False)
            if ft.sync(attempts=4, timeout_per=0.25):
                return ft
            ft.close()
        except Exception:
            # port can bounce between close/reopen on Windows — just retry
            time.sleep(0.2)
    return None


def _catch_entry(
    port, on_line, should_cancel, open_stable, hammer_sync, XR872Flasher, serial, timeout_s
):
    """Manual mask-BROM catch for a stock unit: hammer 0x55 while the operator does
    a USB replug + power press. Returns a synced ``XR872Flasher`` (caller closes it);
    raises on timeout/cancel."""
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
        caught = False
        try:
            caught = hammer_sync(ser, duration=1.5)
        except (serial.SerialException, OSError):
            caught = False  # port yanked mid-hammer (the replug) — retry
        if caught:
            on_line("*** BROM caught — writing firmware (do NOT unplug now) ***")
            f = XR872Flasher(ser=ser, verbose=False)  # f now owns ser
            if f.sync(attempts=30, timeout_per=0.1):
                return f
            on_line("  command channel didn't come up — keep replug+pressing")
        with contextlib.suppress(Exception):
            ser.close()
        if time.monotonic() - last_note > 4:
            on_line("  hammering 0x55 — replug the USB, then press the power button")
            last_note = time.monotonic()
    raise RuntimeError("no BROM catch within the time limit — retry the replug+press")


# The F7 console tokenizer (cmd_parse_argv) splits on whitespace with no quoting, so
# an SSID / password / screen name written over the console must be a single token.
def console_safe_token(s: str) -> bool:
    """True if ``s`` is a single console token (no whitespace or control chars)."""
    return bool(s) and not any(c.isspace() or ord(c) < 0x20 for c in s)


PROVISION_BOOT_TIMEOUT_S = 150.0


def _open_console(port, serial):
    """Open the F7 console; return the serial handle if the firmware answers, else None."""
    # DTR/RTS set on an unopened Serial object, not after Serial(...) has already
    # opened the port. Confirmed on the huessen board that the latter is too
    # late — Windows' serial driver asserts the control lines as part of its own
    # port-open sequence, before any Python attribute assignment runs, so a
    # chip wired to reset on RTS (as esptool's own "hard reset" relies on) has
    # already been reset by the time `s.dtr = False` executes. Not confirmed to
    # cause a problem on THIS board specifically — applied here for correctness
    # and consistency with tools/send_frame.py's open_device(), not because a
    # fault was reproduced here.
    s = serial.Serial()
    s.port = port
    s.baudrate = 115200
    s.timeout = 0.3
    s.dtr = False
    s.rts = False
    try:
        s.open()
    except (serial.SerialException, OSError):
        return None
    try:
        s.reset_input_buffer()
        s.write(b"cfg show\r\n")
        s.flush()
        time.sleep(1.0)
        if b"cfg:" in s.read(600):
            return s
    except (serial.SerialException, OSError):
        pass
    with contextlib.suppress(Exception):
        s.close()
    return None


def _console_send(s, line: str, settle: float = 0.8) -> bytes:
    """Send one console command (CRLF) and return whatever it printed back."""
    s.reset_input_buffer()
    s.write((line + "\r\n").encode())
    s.flush()
    time.sleep(settle)
    return s.read(800)


def _provision_over_console(port, prov, on_line, should_cancel, serial):
    """After the firmware is written, wait for the operator to power-cycle, then write
    Wi-Fi + config over the booted firmware's console. Never logs the password."""
    on_line("")
    on_line("POWER-CYCLE the unit now (unplug/replug USB, or long-press) to boot it —")
    on_line("Wi-Fi and config are then written over the console automatically. Waiting...")
    deadline = time.monotonic() + PROVISION_BOOT_TIMEOUT_S
    last = 0.0
    s = None
    while time.monotonic() < deadline:
        if should_cancel():
            raise RuntimeError("cancelled by the operator")
        s = _open_console(port, serial)
        if s is not None:
            break
        if time.monotonic() - last > 5:
            on_line("  waiting for the unit to boot... (power-cycle it if you haven't)")
            last = time.monotonic()
        time.sleep(1.0)
    if s is None:
        raise RuntimeError("console never came up — power-cycle the unit and re-run to provision")

    try:
        on_line("Console up — writing configuration...")
        # server + name + save first, so a refresh right after Wi-Fi connects uses them.
        if prov.get("server_url"):
            _console_send(s, f"cfg server {prov['server_url']}")
            on_line(f"  server = {prov['server_url']}")
        if prov.get("name"):
            _console_send(s, f"cfg name {prov['name']}")
            on_line(f"  name   = {prov['name']}")
        _console_send(s, "cfg save")
        if prov.get("ssid"):
            # `wifi` persists to sysinfo AND connects; never echo the password.
            _console_send(s, f"wifi {prov['ssid']} {prov['psk']}", settle=1.0)
            on_line(f"  Wi-Fi  = '{prov['ssid']}' (connecting)")

        verify = _console_send(s, "cfg show").decode("utf-8", "replace")
        name_ok = not prov.get("name") or f"name='{prov['name']}'" in verify
        url_ok = not prov.get("server_url") or f"url='{prov['server_url']}'" in verify
        on_line("Configuration saved." if (name_ok and url_ok) else "Configuration written.")

        # Best-effort: watch briefly for the join + first server POST as confirmation.
        if prov.get("ssid"):
            on_line("Waiting for the unit to associate and check in...")
            end = time.monotonic() + 40
            while time.monotonic() < end:
                if should_cancel():
                    break
                chunk = s.read(400)
                if b"network up" in chunk or b"POST http" in chunk:
                    on_line("  associated + checked in with the server.")
                    break
    finally:
        with contextlib.suppress(Exception):
            s.close()
    on_line("Provisioned — the unit is configured and should appear in the screen list.")


def bootstrap_device(
    port: str,
    image_path: str | Path,
    on_line: Callable[[str], None],
    should_cancel: Callable[[], bool],
    timeout_s: float = CATCH_TIMEOUT_S,
    provision: dict | None = None,
) -> dict:
    """Enter the BROM and write slot 0. Tries the no-touch ``upgrade`` entry first
    (works when the unit already runs Hokku firmware), then falls back to the manual
    replug+press catch for a stock unit.

    If ``provision`` is given (``{"ssid","psk","name","server_url"}``, all optional),
    after the write it waits for the operator to power-cycle, then writes those over
    the booted firmware's console.

    Streams progress line-by-line via ``on_line``; polls ``should_cancel`` between
    attempts. Returns ``{"ok": True}`` on success. Raises ``RuntimeError`` on
    timeout, cancel, or a safety abort — never leaves the unit unbootable (slot 1
    stays intact throughout)."""
    if not tooling_available():
        raise RuntimeError("Bigme F7 flash tooling (tools/) is not present on this install")
    (
        flash_slot,
        hammer_sync,
        open_stable,
        XR872Flasher,
        send_upgrade_command,
        serial,
    ) = _import_tools()

    img = Path(image_path).read_bytes()
    if img[:4] != b"AWIH":
        raise RuntimeError("bundled image is not an AWIH xr_system.img")

    on_line(f"Bootstrapping Bigme F7 on {port}.")
    on_line(
        f"Firmware: {Path(image_path).name} ({len(img):,} bytes) -> slot 0 and its A/B cfg "
        "sector [slot 1 (OEM) is left untouched]"
    )
    writer = _LineWriter(on_line)

    # Phase A — no-touch software entry (a unit already on Hokku firmware answers
    # `upgrade`). No replug/press needed; a stock unit ignores it and we fall through.
    on_line("Trying software BROM entry via `upgrade` (no action needed if this unit")
    on_line("already runs Hokku firmware)...")
    f = _software_entry(port, writer, should_cancel, XR872Flasher, send_upgrade_command, 6)
    # Entering via `upgrade` means the unit already runs Hokku firmware, so its
    # Wi-Fi is already in sysinfo — no console Wi-Fi step is needed after the write.
    entered_via_upgrade = f is not None

    # Phase B — manual mask-BROM catch (stock unit; no software way in).
    if f is None:
        on_line("No `upgrade` response — looks like a stock unit. Catch the BROM by hand:")
        on_line("UNPLUG the USB cable -> REPLUG it -> PRESS the power button; repeat until caught.")
        f = _catch_entry(
            port, on_line, should_cancel, open_stable, hammer_sync, XR872Flasher, serial, timeout_s
        )
    else:
        on_line("*** BROM entered via `upgrade` — writing firmware (do NOT unplug now) ***")

    # Write. flash_slot prints its own progress and raises SystemExit via die() on
    # ANY safety-check failure, which leaves slot 1 (OEM) bootable. reboot=False:
    # sys_reboot only re-enters BROM on this chip, so the operator power-cycles.
    had_existing_cfg = False
    try:
        try:
            on_line(f"Writing {Path(image_path).name} -> slot 0 (do NOT unplug now)...")
            with contextlib.redirect_stdout(writer):
                flash_slot(f, img, slot=0, reboot=False, allow_active_slot=True)
            writer.flush()
        except SystemExit as e:
            writer.flush()
            raise RuntimeError(f"safety check aborted the write: {e}") from e
        # Provision the app config (server URL + screen name) over the SAME BROM
        # session — deterministic, so it can't race the booted firmware's console
        # (which is only alive briefly between the device's ~180 s hibernations).
        # Wi-Fi lives in sysinfo, untouched by the reflash, so a re-provisioned
        # unit keeps its Wi-Fi and needs no console step at all.
        if provision and (provision.get("server_url") or provision.get("name")):
            on_line("Provisioning config over BROM (no power-cycle/console needed)...")
            _cfg, had_existing_cfg = write_config_via_brom(
                f,
                on_line,
                server_url=provision.get("server_url"),
                screen_name=provision.get("name"),
            )
    finally:
        with contextlib.suppress(Exception):
            f.close()

    on_line("")
    on_line("DONE — Hokku firmware in slot 0 (bootloader + OEM slot untouched).")

    # Wi-Fi is only needed for a genuinely FRESH unit. A unit we entered via
    # `upgrade` (already running Hokku firmware) or that already had a config blob
    # keeps its sysinfo Wi-Fi — skip the fragile console step for it.
    needs_wifi = bool(
        provision and provision.get("ssid") and not entered_via_upgrade and not had_existing_cfg
    )
    if needs_wifi:
        on_line("Fresh unit — setting Wi-Fi over the console (config already on flash)...")
        try:
            _provision_over_console(port, provision, on_line, should_cancel, serial)
        except RuntimeError as e:
            # Config is already persisted via BROM; a Wi-Fi console miss is non-fatal.
            on_line(f"NOTE: Wi-Fi not set over console ({e}).")
            on_line("Set it after boot over the console (115200): `wifi <ssid> <pw>`.")
    elif provision:
        on_line("Config provisioned to flash. POWER-CYCLE the unit (unplug/replug) to boot it.")
        on_line("It keeps its existing Wi-Fi and comes straight back online under the new name.")
    else:
        on_line("POWER-CYCLE the unit (unplug/replug, or long-press) to boot it.")
        on_line("A fresh unit needs Wi-Fi + server set over the console (115200):")
        on_line("`wifi <ssid> <pw>`, then `cfg save`. Details: docs/screens/bigme_f7/bootstrap.md")
    return {"ok": True}
