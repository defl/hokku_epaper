#!/usr/bin/env python3
"""Hokku screen simulator — render the 960K panel binary as a viewable image.

Fetches the live binary from a running hokku-server, or loads a saved .bin
file from disk, and converts it back into a PNG you can actually look at.

This is the inverse of the dither pipeline: the server quantises a photo
into 6-colour Spectra-6 nibbles and packs them into 960K bytes; this tool
unpacks those bytes back into pixels and renders them with the preview
palette so a human can see what the e-paper frame is showing.

Usage:
    # Fetch from a running server (default http://localhost:8080)
    python tools/screen_sim.py

    # Load a local .bin file
    python tools/screen_sim.py --file path/to/hokku.bin

    # Auto-refresh every N seconds (like the real firmware)
    python tools/screen_sim.py --watch 180

    # Save to a specific file instead of showing interactively
    python tools/screen_sim.py --output preview.png

    # Point at a different server
    python tools/screen_sim.py --server http://192.168.1.100:8080
"""

import argparse
import io
import time
import struct
import sys
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

import numpy as np
from PIL import Image


# ── Display parameters (mirrors webserver.py) ──────────────────────
PANEL_W = 600       # columns per physical panel
PANEL_H = 1600      # rows per physical panel
PANEL_BYTES = PANEL_W * PANEL_H // 2   # 480,000 at 4bpp
TOTAL_BYTES = PANEL_BYTES * 2           # 960,000

# Preview palette — matches PALETTE_PREVIEW_RGB in webserver.py
PALETTE = np.array([
    [0, 0, 0],           # 0: Black
    [255, 255, 255],     # 1: White
    [255, 230, 50],      # 2: Yellow
    [200, 20, 20],       # 3: Red
    [30, 80, 200],       # 4: Blue
    [20, 120, 40],       # 5: Green
], dtype=np.uint8)

# Nibble-to-palette-index mapping (nibble values that aren't in this
# table get mapped to the nearest colour or left as-is — the display
# shows them as muted intermediates, but for preview we snap to the
# closest standard colour).
NIBBLE_TO_INDEX = {0x0: 0, 0x1: 1, 0x2: 2, 0x3: 3, 0x5: 4, 0x6: 5}


def unpack_panel(data: bytes) -> np.ndarray:
    """Unpack 480K of 4bpp panel data into a (1600, 600) uint8 index array.

    Each byte holds two pixels: high nibble = left pixel, low nibble = right pixel.
    Unknown nibble values are snapped to the nearest standard colour.
    """
    arr = np.frombuffer(data, dtype=np.uint8).reshape(PANEL_H, PANEL_W // 2)
    high = (arr >> 4) & 0x0F
    low = arr & 0x0F
    # Interleave: high nibble first, then low nibble
    panel = np.empty((PANEL_H, PANEL_W), dtype=np.uint8)
    panel[:, 0::2] = high
    panel[:, 1::2] = low

    # Map nibble values to palette indices. Unknown values get the
    # nearest standard colour via a small LUT.
    lut = np.full(16, 0, dtype=np.uint8)  # default to Black
    for nib, idx in NIBBLE_TO_INDEX.items():
        lut[nib] = idx
    # For unknown nibbles, find the closest standard colour in RGB space.
    # Precompute once.
    _build_fallback_lut(lut)
    return lut[panel]


def _build_fallback_lut(lut: np.ndarray):
    """Fill LUT entries for unknown nibbles (7-F) with the nearest
    standard palette colour by Euclidean distance in RGB."""
    # Standard palette RGB values (measured colours from webserver.py)
    standard_rgb = np.array([
        [2, 2, 2],        # 0: Black
        [190, 200, 200],  # 1: White
        [205, 202, 0],    # 2: Yellow
        [135, 19, 0],     # 3: Red
        [5, 64, 158],     # 4: Blue
        [39, 102, 60],    # 5: Green
    ], dtype=np.float32)

    for nibble in range(16):
        if nibble in NIBBLE_TO_INDEX:
            continue
        # We don't know the actual display colour for this nibble.
        # Use the preview palette RGB and find nearest standard index.
        # This is a best-effort visualisation.
        preview_rgb = np.array([
            [0, 0, 0],
            [255, 255, 255],
            [255, 230, 50],
            [200, 20, 20],
            [30, 80, 200],
            [20, 120, 40],
        ], dtype=np.float32)
        # Just map unknown nibbles to their closest standard by index
        # (since we don't know what they actually look like on the display).
        # Map 7→0 (black), 8→0, 9→4 (blue), A→2 (yellow), B→3 (red),
        # C→0, D→4 (blue-purple), E→5 (olive), F→1 (white-ish)
        # These are rough guesses based on hardware_facts.md notes.
        guess_map = {
            0x7: 0, 0x8: 0, 0x9: 4, 0xA: 2, 0xB: 3,
            0xC: 0, 0xD: 4, 0xE: 5, 0xF: 1,
        }
        lut[nibble] = guess_map.get(nibble, 0)


def render_to_image(binary: bytes) -> Image.Image:
    """Convert a 960K panel binary into a landscape PIL Image (1600×1200)."""
    assert len(binary) == TOTAL_BYTES, (
        f"Expected {TOTAL_BYTES} bytes, got {len(binary)}"
    )

    panel1_data = binary[:PANEL_BYTES]
    panel2_data = binary[PANEL_BYTES:]

    panel1_idx = unpack_panel(panel1_data)   # (1600, 600)
    panel2_idx = unpack_panel(panel2_data)   # (1600, 600)

    # Concatenate horizontally: left panel then right panel → (1600, 1200)
    full_idx = np.concatenate([panel1_idx, panel2_idx], axis=1)

    # Map indices to RGB
    rgb = PALETTE[full_idx]  # (1600, 1200, 3)

    # The display is viewed in landscape. The panel data is stored in
    # portrait orientation (1200 wide × 1600 tall). Rotate 90° CCW to
    # get the landscape view (1600 × 1200).
    img = Image.fromarray(rgb, "RGB")
    img = img.rotate(90, expand=True)
    return img


def fetch_from_server(url: str) -> bytes:
    """GET /hokku/screen/ and return the raw binary."""
    screen_url = url.rstrip("/") + "/hokku/screen/"
    req = Request(screen_url, headers={"User-Agent": "hokku-screen-sim/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            data = resp.read()
    except URLError as e:
        print(f"Error fetching from {screen_url}: {e}", file=sys.stderr)
        sys.exit(1)
    if len(data) != TOTAL_BYTES:
        print(
            f"Warning: server returned {len(data)} bytes, expected {TOTAL_BYTES}",
            file=sys.stderr,
        )
    return data


def load_from_file(path: Path) -> bytes:
    """Read a .bin file from disk."""
    data = path.read_bytes()
    if len(data) != TOTAL_BYTES:
        print(
            f"Warning: {path} is {len(data)} bytes, expected {TOTAL_BYTES}",
            file=sys.stderr,
        )
    return data


def show_image(img: Image.Image):
    """Display the image — tries to open the system viewer, falls back
    to saving a temp file with instructions."""
    try:
        img.show()
    except Exception as e:
        tmp = Path("hokku_screen_preview.png")
        img.save(tmp)
        print(f"Saved preview to {tmp} (system viewer unavailable: {e})")


def main():
    parser = argparse.ArgumentParser(
        description="Hokku screen simulator — render 960K panel binary as an image",
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument(
        "--server", "-s",
        default="http://localhost:8080",
        help="Hokku server URL (default: http://localhost:8080)",
    )
    src.add_argument(
        "--file", "-f",
        type=Path,
        help="Load from a local .bin file instead of a server",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        help="Save the rendered image to this file instead of displaying",
    )
    parser.add_argument(
        "--watch", "-w",
        type=int,
        metavar="SECONDS",
        help="Auto-refresh mode: fetch and display every N seconds",
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

    iteration = 0
    while True:
        iteration += 1
        if args.watch and iteration > 1:
            log(f"\n--- Refresh #{iteration} ---")

        # Fetch or load the binary
        if args.file:
            if not args.file.exists():
                print(f"File not found: {args.file}", file=sys.stderr)
                sys.exit(1)
            log(f"Loading: {args.file}")
            binary = load_from_file(args.file)
        else:
            log(f"Fetching from: {args.server}/hokku/screen/")
            binary = fetch_from_server(args.server)

        # Render to image
        img = render_to_image(binary)
        log(f"Rendered: {img.size[0]}x{img.size[1]}")

        # Save or show
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            img.save(args.output)
            log(f"Saved: {args.output}")
        else:
            show_image(img)

        # Auto-refresh loop
        if not args.watch:
            break

        delay = args.watch
        log(f"Waiting {delay}s...")
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            log("\nStopped.")
            break


if __name__ == "__main__":
    main()
