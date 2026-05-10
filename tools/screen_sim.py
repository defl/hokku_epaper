#!/usr/bin/env python3
"""Hokku screen simulator — render the 960K panel binary as a viewable image.

Fetches the live binary from a running hokku-server, or loads a saved .bin
file from disk, and converts it back into a PNG you can actually look at.

Usage:
    # Fetch from the running server, identify this sim as "dev-screen"
    python tools/screen_sim.py dev-screen

    # Auto-refresh, honouring the server's X-Sleep-Seconds header
    python tools/screen_sim.py dev-screen --watch

    # Override the refresh interval to 30 s regardless of X-Sleep-Seconds
    python tools/screen_sim.py dev-screen --watch 30

    # Point at a different server
    python tools/screen_sim.py dev-screen --server http://192.168.1.100:8080

    # Load a local .bin file (name is still required for the window title)
    python tools/screen_sim.py dev-screen --file path/to/hokku.bin

    # Save the rendered PNG instead of displaying it
    python tools/screen_sim.py dev-screen --output preview.png

    # Save the raw binary to disk (for offline inspection or test fixtures)
    python tools/screen_sim.py dev-screen --download hokku.bin

    # Combine: fetch, save binary AND save PNG, then exit
    python tools/screen_sim.py dev-screen --download hokku.bin --output preview.png
"""

import argparse
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import numpy as np
from PIL import Image


# ── Display parameters (mirrors display.py) ────────────────────────────────
PANEL_W = 600
PANEL_H = 1600
PANEL_BYTES = PANEL_W * PANEL_H // 2   # 480 000 bytes
TOTAL_BYTES = PANEL_BYTES * 2           # 960 000 bytes

PALETTE = np.array([
    [0,   0,   0  ],   # 0 Black
    [255, 255, 255],   # 1 White
    [255, 230, 50 ],   # 2 Yellow
    [200, 20,  20 ],   # 3 Red
    [30,  80,  200],   # 4 Blue
    [20,  120, 40 ],   # 5 Green
], dtype=np.uint8)

# Nibble → palette index for the six known inks.
_KNOWN = {0x0: 0, 0x1: 1, 0x2: 2, 0x3: 3, 0x5: 4, 0x6: 5}
# Best-effort mapping for controller-reserved nibbles (hardware_facts.md).
_UNKNOWN = {0x7: 0, 0x8: 0, 0x9: 4, 0xA: 2, 0xB: 3, 0xC: 0, 0xD: 4, 0xE: 5, 0xF: 1}
_LUT = np.zeros(16, dtype=np.uint8)
for _nib, _idx in {**_KNOWN, **_UNKNOWN}.items():
    _LUT[_nib] = _idx


# ── Core rendering ─────────────────────────────────────────────────────────

def unpack_panel(data: bytes) -> np.ndarray:
    """Unpack 480 K of 4-bpp panel data → (1600, 600) uint8 palette-index array."""
    arr = np.frombuffer(data, dtype=np.uint8).reshape(PANEL_H, PANEL_W // 2)
    panel = np.empty((PANEL_H, PANEL_W), dtype=np.uint8)
    panel[:, 0::2] = (arr >> 4) & 0x0F
    panel[:, 1::2] = arr & 0x0F
    return _LUT[panel]


def render_to_image(binary: bytes) -> Image.Image:
    """Convert a 960 K panel binary → landscape PIL Image (1600×1200)."""
    if len(binary) != TOTAL_BYTES:
        raise ValueError(f"Expected {TOTAL_BYTES} bytes, got {len(binary)}")
    p1 = unpack_panel(binary[:PANEL_BYTES])   # (1600, 600)
    p2 = unpack_panel(binary[PANEL_BYTES:])   # (1600, 600)
    rgb = PALETTE[np.concatenate([p1, p2], axis=1)]  # (1600, 1200, 3)
    img = Image.fromarray(rgb, "RGB")
    return img.rotate(90, expand=True)          # → (1600, 1200) landscape


# ── Network fetch ──────────────────────────────────────────────────────────

def fetch_screen(
    server_url: str,
    screen_name: str,
    *,
    battery_mv: int | None = None,
    timeout: int = 30,
) -> tuple[bytes, dict[str, str]]:
    """GET /hokku/screen/ and return (binary, response_headers).

    Sends the same headers the real firmware does so the server records the
    request in its screen telemetry.

    Args:
        server_url:  Base URL, e.g. ``http://localhost:8080``.
        screen_name: Sent as ``X-Screen-Name``; shows up in the server UI.
        battery_mv:  Optional battery voltage in mV (``X-Battery-mV``).
        timeout:     Socket timeout in seconds.

    Returns:
        A (binary_data, headers_dict) tuple. headers_dict keys are lowercase.

    Raises:
        URLError:   On network errors.
        ValueError: If the server returns an unexpected payload size.
    """
    url = server_url.rstrip("/") + "/hokku/screen/"
    hdrs: dict[str, str] = {
        "User-Agent": f"hokku-screen-sim/2.0 ({screen_name})",
        "X-Screen-Name": screen_name,
    }
    if battery_mv is not None:
        hdrs["X-Battery-mV"] = str(battery_mv)

    req = Request(url, headers=hdrs)
    with urlopen(req, timeout=timeout) as resp:
        status = resp.status
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        data = resp.read()

    return data, resp_headers


def load_from_file(path: Path) -> tuple[bytes, dict[str, str]]:
    """Read a .bin file from disk, return (data, empty_headers)."""
    data = path.read_bytes()
    return data, {}


# ── Display helpers ────────────────────────────────────────────────────────

def _show_once(img: Image.Image) -> None:
    """Open the image in the OS default viewer (new window each time)."""
    try:
        img.show()
    except Exception as e:
        tmp = Path("hokku_screen_preview.png")
        img.save(tmp)
        print(f"  Saved to {tmp} (system viewer unavailable: {e})")


def _watch_tkinter(
    fetch_fn,
    screen_name: str,
    interval_override: int | None,
    log,
) -> None:
    """Run a persistent tkinter window that refreshes in-place.

    fetch_fn() → (binary, headers) is called on every tick.
    Sleeps for X-Sleep-Seconds from the server, or interval_override if set.
    Falls back to showing new windows if tkinter / ImageTk is unavailable.
    """
    try:
        import tkinter as tk
        from PIL import ImageTk
    except ImportError as e:
        log(f"  tkinter/ImageTk not available ({e}); opening a new window each refresh")
        _watch_fallback(fetch_fn, screen_name, interval_override, log)
        return

    root = tk.Tk()
    root.title(f"Hokku — {screen_name}")
    root.configure(bg="black")

    # Scale down to 800×600 so it fits on most screens.
    DISPLAY_W, DISPLAY_H = 800, 600
    label = tk.Label(root, bg="black", bd=0)
    label.pack(fill=tk.BOTH, expand=True)
    _photo_ref: list = [None]  # keep reference to prevent GC

    def _do_fetch():
        try:
            binary, headers = fetch_fn()
            img = render_to_image(binary)
            img_small = img.resize((DISPLAY_W, DISPLAY_H), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img_small)
            label.config(image=photo)
            _photo_ref[0] = photo
            root.title(f"Hokku — {screen_name} — {img.size[0]}×{img.size[1]}")
        except ValueError as e:
            log(f"  Render error: {e}")
            headers = {}
        except URLError as e:
            log(f"  Fetch error: {e}")
            headers = {}
        except Exception as e:
            log(f"  Error: {e}")
            headers = {}

        sleep_s = interval_override
        if sleep_s is None:
            try:
                sleep_s = int(headers.get("x-sleep-seconds", 60))
            except (ValueError, TypeError):
                sleep_s = 60
        log(f"  Next refresh in {sleep_s}s")
        root.after(sleep_s * 1000, _do_fetch)

    root.after(0, _do_fetch)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        log("\n  Stopped.")


def _watch_fallback(fetch_fn, screen_name, interval_override, log):
    """Non-tkinter watch loop — opens a new window on each refresh."""
    while True:
        try:
            binary, headers = fetch_fn()
            img = render_to_image(binary)
            _show_once(img)
        except (URLError, ValueError) as e:
            log(f"  Error: {e}")
            headers = {}

        sleep_s = interval_override
        if sleep_s is None:
            try:
                sleep_s = int(headers.get("x-sleep-seconds", 60))
            except (ValueError, TypeError):
                sleep_s = 60
        log(f"  Waiting {sleep_s}s...")
        try:
            time.sleep(sleep_s)
        except KeyboardInterrupt:
            log("\n  Stopped.")
            break


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Hokku screen simulator — render 960 K panel binary as an image",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Mandatory: the screen name sent to the server.
    parser.add_argument(
        "name",
        help="Screen name (sent as X-Screen-Name header; shown in server UI)",
    )

    src = parser.add_mutually_exclusive_group()
    src.add_argument(
        "--server", "-s",
        default="http://localhost:8080",
        metavar="URL",
        help="Hokku server base URL (default: http://localhost:8080)",
    )
    src.add_argument(
        "--file", "-f",
        type=Path,
        metavar="FILE",
        help="Load from a local .bin file instead of fetching from a server",
    )

    parser.add_argument(
        "--output", "-o",
        type=Path,
        metavar="FILE",
        help="Save the rendered PNG to this file instead of (or in addition to) displaying",
    )
    parser.add_argument(
        "--download", "-d",
        type=Path,
        metavar="FILE",
        help="Save the raw panel binary (.bin) to this file",
    )
    parser.add_argument(
        "--watch", "-w",
        nargs="?",
        const=None,      # --watch with no value → use server's X-Sleep-Seconds
        type=int,
        metavar="SECONDS",
        help="Auto-refresh mode. Without a value: honour the server's X-Sleep-Seconds. "
             "With a value: refresh every N seconds regardless.",
    )
    parser.add_argument(
        "--battery", "-b",
        type=int,
        default=None,
        metavar="MV",
        help="Simulated battery voltage in mV sent as X-Battery-mV (e.g. 3800)",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress status messages",
    )

    args = parser.parse_args()

    def log(msg: str):
        if not args.quiet:
            print(msg, file=sys.stderr)

    # Build the fetch function (closure over args).
    if args.file:
        def _fetch():
            log(f"  Loading: {args.file}")
            return load_from_file(args.file)
    else:
        def _fetch():
            url = args.server.rstrip("/") + "/hokku/screen/"
            log(f"  → GET {url}  (X-Screen-Name: {args.name!r})")
            data, headers = fetch_screen(
                args.server, args.name, battery_mv=args.battery,
            )
            sleep_s = headers.get("x-sleep-seconds", "?")
            epoch  = headers.get("x-server-time-epoch", "?")
            ctype  = headers.get("content-type", "?")
            log(f"  ← {len(data)} bytes  X-Sleep-Seconds={sleep_s}"
                f"  X-Server-Time-Epoch={epoch}  Content-Type={ctype}")
            return data, headers

    if args.watch is not None or (args.watch is None and "--watch" in sys.argv or "-w" in sys.argv):
        # Watch mode — determine if --watch was actually passed.
        watch_mode = any(a in sys.argv for a in ("--watch", "-w"))
    else:
        watch_mode = False

    # Re-check: argparse sets args.watch to None both for "not provided" and
    # for "--watch" with no value. Distinguish by checking sys.argv directly.
    watch_mode = any(a.startswith("--watch") or a == "-w" for a in sys.argv)

    if watch_mode and not args.file:
        log(f"  Screen: {args.name!r}  server: {args.server}")
        if args.watch:
            log(f"  Refresh interval: {args.watch}s (fixed)")
        else:
            log("  Refresh interval: from server X-Sleep-Seconds")
        _watch_tkinter(_fetch, args.name, args.watch, log)
        return

    # Single-shot fetch.
    log(f"  Screen: {args.name!r}")
    try:
        binary, headers = _fetch()
    except URLError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if len(binary) != TOTAL_BYTES:
        print(
            f"Warning: got {len(binary)} bytes, expected {TOTAL_BYTES}",
            file=sys.stderr,
        )

    if args.download:
        args.download.parent.mkdir(parents=True, exist_ok=True)
        args.download.write_bytes(binary)
        log(f"  Saved binary: {args.download}  ({len(binary)} bytes)")

    img = render_to_image(binary)
    log(f"  Rendered: {img.size[0]}×{img.size[1]}")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        img.save(args.output)
        log(f"  Saved PNG: {args.output}")
    else:
        _show_once(img)


if __name__ == "__main__":
    main()
