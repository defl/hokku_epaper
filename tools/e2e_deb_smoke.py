#!/usr/bin/env python3
"""End-to-end smoke driver for a *running* Hokku server.

This talks to a live server over HTTP only — it does not import the ``hokku``
package — so the exact same driver validates:

  * the Debian package installed in a Docker container (``test_server/e2e/``),
  * and, later, a real appliance (Pi + screen) over the network.

What it does, in order:

  1. Wait for ``GET /hokku/api/status`` to answer (server booted + bound).
  2. Upload every image in ``--images-dir`` via ``POST /hokku/api/upload``.
  3. Check the upload response: files the expectations mark ``reject`` must be
     refused (with a plausible reason); everything else must be saved.
  4. Poll ``/hokku/api/status`` until every saved image reaches a terminal
     convert state (``ok`` / ``failed``) or ``--convert-timeout`` elapses.
  5. For each image, compare the outcome against expectations:
       - ``ok``      → must be dithered; fetch ``/hokku/api/dithered/<name>``,
                       assert it is a valid PNG that uses **only** the panel
                       palette (<= --max-palette-colors distinct colours). This
                       is the real "dithering happened" proof — the preview PNG
                       is reconstructed from panel indices, so a photo that was
                       genuinely quantised has <= 6 colours, an un-dithered one
                       has thousands.
       - ``fail``    → must end FAILED (e.g. an over-budget decode the server
                       is expected to reject gracefully rather than OOM on).
       - ``reject``  → handled at step 3 (never reaches the server's pool).

Exit code 0 iff every image matched its expectation; 1 otherwise. Expectations
default to "must dither ok" for any file not listed in the expectations JSON.

Stdlib + Pillow only. Pillow ships with the server (python3-pil), and is in the
repo venv, so it is present wherever this driver runs.
"""

from __future__ import annotations

import argparse
import io
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from PIL import Image

# Extensions the server accepts (mirror of image_renderer.IMAGE_EXTENSIONS).
# Kept as a literal so the driver has no dependency on the hokku package.
_IMAGE_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".tiff",
    ".webp",
    ".gif",
    ".heic",
    ".heif",
    ".avif",
    ".jxl",
    ".svg",
}


class Colours:
    OK = "\033[32m"
    BAD = "\033[31m"
    DIM = "\033[2m"
    END = "\033[0m"


def _c(s: str, colour: str) -> str:
    return f"{colour}{s}{Colours.END}" if sys.stdout.isatty() else s


# ── HTTP helpers (stdlib only) ────────────────────────────────────────────


def _get(url: str, timeout: float = 30.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def _get_json(url: str, timeout: float = 30.0) -> dict:
    return json.loads(_get(url, timeout=timeout))


def _multipart_upload(url: str, files: list[Path], timeout: float = 300.0) -> dict:
    """POST a batch of files as multipart/form-data under the field name ``file``."""
    boundary = f"----hokku-e2e-{uuid.uuid4().hex}"
    body = io.BytesIO()
    for path in files:
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body.write(f"--{boundary}\r\n".encode())
        body.write(
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode()
        )
        body.write(f"Content-Type: {ctype}\r\n\r\n".encode())
        body.write(path.read_bytes())
        body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        url,
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ── validation steps ──────────────────────────────────────────────────────


def _wait_for_server(base: str, timeout: float) -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            status = _get_json(f"{base}/hokku/api/status", timeout=5)
            if "upload_files" in status:
                print(_c(f"server up at {base}", Colours.OK))
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            last_err = e
        time.sleep(1)
    raise SystemExit(
        _c(f"server never came up at {base} within {timeout}s: {last_err}", Colours.BAD)
    )


def _distinct_colours(png_bytes: bytes) -> int:
    with Image.open(io.BytesIO(png_bytes)) as im:
        im = im.convert("RGB")
        colours = im.getcolors(maxcolors=1 << 24)
    # getcolors returns None only if there are more than maxcolors distinct
    # colours — treat that as "way too many" (definitely not dithered).
    return len(colours) if colours is not None else (1 << 24)


def _load_expectations(path: Path | None) -> dict[str, str]:
    """Map filename -> expected outcome ('ok' | 'fail' | 'reject').

    Files absent from the map default to 'ok'. The JSON file may use either a
    flat ``{"name": "reject"}`` shape or ``{"name": {"expect": "reject",
    "reason": "..."}}`` — only the outcome is required here.
    """
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for name, val in raw.get("images", raw).items():
        if isinstance(val, dict):
            out[name] = val["expect"]
        else:
            out[name] = val
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--images-dir", required=True, type=Path)
    ap.add_argument(
        "--expectations",
        type=Path,
        default=None,
        help="JSON mapping filename -> ok|fail|reject (default: everything ok)",
    )
    ap.add_argument("--startup-timeout", type=float, default=120.0)
    ap.add_argument(
        "--convert-timeout",
        type=float,
        default=900.0,
        help="max seconds to wait for every image to reach a terminal state "
        "(cold numba JIT + decoding every format is slow on first run)",
    )
    ap.add_argument("--max-palette-colours", type=int, default=6)
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    expectations = _load_expectations(args.expectations)

    images = sorted(
        p for p in args.images_dir.iterdir() if p.is_file() and p.suffix.lower() in _IMAGE_EXTS
    )
    if not images:
        raise SystemExit(_c(f"no images found in {args.images_dir}", Colours.BAD))
    print(f"found {len(images)} image(s) in {args.images_dir}")

    _wait_for_server(base, args.startup_timeout)

    # ── upload ────────────────────────────────────────────────────────────
    print("uploading all images...")
    resp = _multipart_upload(f"{base}/hokku/api/upload", images)
    saved = set(resp.get("saved", []))
    skipped = {s["name"]: s.get("reason", "") for s in resp.get("skipped", [])}
    print(f"  saved={len(saved)} skipped={len(skipped)}")
    for name, reason in skipped.items():
        print(_c(f"    skipped {name}: {reason}", Colours.DIM))

    failures: list[str] = []

    # Files expected to be rejected at upload must appear in 'skipped'; every
    # other file must have been saved.
    for path in images:
        name = path.name
        expect = expectations.get(name, "ok")
        if expect == "reject":
            if name not in skipped:
                failures.append(f"{name}: expected upload rejection, but it was accepted")
        elif name not in saved:
            reason = skipped.get(name, "not in saved list and no skip reason")
            failures.append(f"{name}: expected upload accept, but rejected: {reason}")

    # ── wait for conversion to settle ──────────────────────────────────────
    expected_terminal = {p.name for p in images if expectations.get(p.name, "ok") != "reject"}
    print(f"waiting for {len(expected_terminal)} image(s) to finish converting...")
    deadline = time.time() + args.convert_timeout
    status_by_name: dict[str, dict] = {}
    while time.time() < deadline:
        status = _get_json(f"{base}/hokku/api/status")
        status_by_name = {e["name"]: e for e in status.get("upload_files", [])}
        pending = [
            n
            for n in expected_terminal
            if n not in status_by_name or status_by_name[n]["status"] == "pending"
        ]
        done = len(expected_terminal) - len(pending)
        print(
            _c(
                f"  {done}/{len(expected_terminal)} terminal "
                f"(converting={status.get('converting_name') or '-'})",
                Colours.DIM,
            )
        )
        if not pending:
            break
        time.sleep(3)
    else:
        failures.append(
            f"timeout: {len(expected_terminal) - len([n for n in expected_terminal if n in status_by_name and status_by_name[n]['status'] != 'pending'])} "
            f"image(s) never reached a terminal state within {args.convert_timeout}s"
        )

    # ── per-image validation ───────────────────────────────────────────────
    print("validating outcomes...")
    for path in images:
        name = path.name
        expect = expectations.get(name, "ok")
        if expect == "reject":
            mark = "reject" if name in skipped else "ACCEPTED(!)"
            colour = Colours.OK if name in skipped else Colours.BAD
            print(_c(f"  {name:<48} expect=reject  got={mark}", colour))
            continue

        rec = status_by_name.get(name)
        if rec is None:
            failures.append(f"{name}: no status record after conversion window")
            print(_c(f"  {name:<48} expect={expect:<6} got=MISSING", Colours.BAD))
            continue

        got = rec["status"]
        if expect == "fail":
            colour = Colours.OK if got == "failed" else Colours.BAD
            print(_c(f"  {name:<48} expect=fail    got={got} ({rec.get('error')})", colour))
            if got != "failed":
                failures.append(f"{name}: expected render FAIL, got status={got}")
            continue

        # expect == "ok": must be dithered AND the dithered PNG must use only
        # the panel palette.
        if got != "ok" or not rec.get("dithered"):
            failures.append(
                f"{name}: expected dither OK, got status={got} error={rec.get('error')}"
            )
            print(_c(f"  {name:<48} expect=ok      got={got} ({rec.get('error')})", Colours.BAD))
            continue
        try:
            png = _get(f"{base}/hokku/api/dithered/{urllib.parse.quote(name)}")
            ncol = _distinct_colours(png)
        except (urllib.error.URLError, OSError) as e:
            failures.append(f"{name}: dithered PNG fetch failed: {e}")
            print(_c(f"  {name:<48} expect=ok      got=ok but PNG fetch failed", Colours.BAD))
            continue
        if ncol > args.max_palette_colours:
            failures.append(
                f"{name}: dithered PNG has {ncol} distinct colours "
                f"(> {args.max_palette_colours}); not quantised to the palette"
            )
            print(_c(f"  {name:<48} expect=ok      got=ok but {ncol} colours(!)", Colours.BAD))
        else:
            print(
                _c(
                    f"  {name:<48} expect=ok      got=ok  dithered={len(png)}B {ncol} colours",
                    Colours.OK,
                )
            )

    # ── verdict ────────────────────────────────────────────────────────────
    print()
    if failures:
        print(_c(f"FAIL — {len(failures)} problem(s):", Colours.BAD))
        for f in failures:
            print(_c(f"  - {f}", Colours.BAD))
        return 1
    print(_c(f"PASS — all {len(images)} image(s) matched expectations", Colours.OK))
    return 0


if __name__ == "__main__":
    sys.exit(main())
