"""Shared helper for fetching assets from the project's latest GitHub release.

Used by both the Pi installer (hokku-server .deb) and the ESP32 setup
(firmware binaries). One release query is memoised per process so we don't
hammer the GitHub API during a single run.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

GITHUB_RELEASES_LATEST = "https://api.github.com/repos/defl/hokku_epaper/releases/latest"

REPO_ROOT = Path(__file__).parent.parent
CACHE_DIR = REPO_ROOT / ".cache"

_cached_release = None


def get_latest_release():
    """Fetch the latest-release JSON from GitHub. Memoised per process so callers
    that need multiple assets don't re-hit the API. Raises on failure."""
    global _cached_release
    if _cached_release is not None:
        return _cached_release
    req = urllib.request.Request(
        GITHUB_RELEASES_LATEST,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "hokku-setup"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        _cached_release = json.loads(r.read().decode("utf-8"))
    return _cached_release


def find_asset(release, name_predicate):
    """Return the first asset whose name matches `name_predicate(name) -> bool`."""
    for a in release.get("assets") or []:
        if name_predicate(a.get("name", "")):
            return a
    return None


def _download_with_progress(url, dest):
    """Stream `url` to `dest`, showing a simple % indicator. Returns True on success."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            total = int(r.headers.get("Content-Length", 0))
            written = 0
            last = time.time()
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    now = time.time()
                    if now - last > 0.5:
                        if total:
                            pct = written * 100 // total
                            sys.stdout.write(
                                f"\r    {pct:3d}%  {written / 1024**2:.1f} / {total / 1024**2:.1f} MB"
                            )
                        else:
                            sys.stdout.write(f"\r    {written / 1024**2:.1f} MB")
                        sys.stdout.flush()
                        last = now
        print()
        tmp.replace(dest)  # replace() overwrites on Windows; rename() would fail
        return True
    except Exception as e:
        print(f"\n  ERROR: download failed: {e}")
        try:
            tmp.unlink()
        except Exception:
            pass
        return False


def ensure_cached_asset(asset, target_dir, label=""):
    """If `asset` is already cached at the matching size, return its Path.
    Otherwise download it. Returns Path or None on failure."""
    name = asset["name"]
    size = int(asset.get("size") or 0)
    url = asset.get("browser_download_url")
    if not url:
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    if target.exists() and size and target.stat().st_size == size:
        return target
    prefix = f"{label} " if label else ""
    print(f"  Downloading {prefix}{name} ({size // 1024} KB)...")
    if not _download_with_progress(url, target):
        return None
    return target


def _reset_cache_for_tests():
    """Test helper — wipe the memoised release between tests."""
    global _cached_release
    _cached_release = None
