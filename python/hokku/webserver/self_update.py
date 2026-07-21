"""Check GitHub for a newer ``hokku-server`` release and install it.

Only meaningful on a package install (the ``.deb``, not a dev checkout) —
see :func:`is_package_install`. The unprivileged ``hokku`` service user can
download and stage the new ``.deb`` (into ``/var/lib/hokku/update/``, which
it already owns), but the actual ``apt-get install`` must run as root. That
handoff is a single fixed, argument-less command
(``sudo systemctl start --no-block hokku-server-self-update.service``,
see :meth:`SelfUpdateManager.start_install`) authorised by a narrow
``/etc/sudoers.d`` rule — see ``python/debian/hokku-server.sudoers`` and
``python/debian/self-update.sh`` for the root side of this.

``hokku-server-self-update.service`` is deliberately its own systemd unit,
not ordered after/bound to ``hokku-server.service``: the package's own
``postinst`` stops ``hokku-server`` as its first step, and systemd's default
``KillMode=control-group`` would kill the installer too if it ran inside
that unit's cgroup. Because of that, this module can only track a job up to
the point where the install is triggered — the actual outcome is read back
from ``/var/lib/hokku/update/status.json``, written by the root-side script,
since the Flask process itself may be killed and replaced mid-install.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

logger = logging.getLogger(__name__)

GITHUB_RELEASES_ALL = "https://api.github.com/repos/defl/hokku_epaper/releases"
_REQUEST_TIMEOUT = 15

_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_ASSET_RE = re.compile(r"^hokku-server_.*_all\.deb$")

_SELF_UPDATE_SCRIPT = Path("/usr/lib/hokku-server/self-update.sh")
_UPDATE_DIR = Path("/var/lib/hokku/update")
_STAGED_DEB = _UPDATE_DIR / "staged.deb"
_STAGED_META = _UPDATE_DIR / "staged.json"
_STATUS_FILE = _UPDATE_DIR / "status.json"

_job_ids = itertools.count(1)

# Terminal states a job can be in — once here, a new check/install may start.
# Everything else ("checking", "downloading", "verifying", "triggering") is
# in-flight and holds the single job slot.
_FINISHED_STATES = {"up_to_date", "checked", "triggered", "error"}


def is_package_install() -> bool:
    """Whether this server is running from the ``.deb`` (not a dev checkout).

    Mirrors the existing ``installer_available`` heuristic (a fixed path that
    only exists on the relevant install type) used for the "Reset to Setup
    Wizard" button in ``flask_app.py``.
    """
    return _SELF_UPDATE_SCRIPT.exists()


def _parse_release_tag(tag: str) -> tuple[int, int, int] | None:
    """Parse a clean ``vMAJOR.MINOR.PATCH`` release tag.

    Returns ``None`` for anything else — pre-release/dev tags such as
    ``v3.0.0-alpha2`` or ``v3.0.1-dev.47`` are never offered as an update
    target. Combined with the odd/even PATCH convention (root ``AGENTS.md``),
    callers additionally filter to an even PATCH to land only on the
    human-tagged release track.
    """
    m = _TAG_RE.match(tag)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _current_version() -> tuple[int, int, int] | None:
    """The installed package's version, as a comparable ``(major, minor, patch)``.

    Read from package metadata (not the dev-tree-first ``git_describe`` used
    elsewhere) since this feature only runs on a package install anyway. A
    dev-track version (``4.0.1.dev70``) parses via its release segment
    (``4.0.1``) — comparisons against a clean release tag still work: a dev
    build one behind the next even patch correctly reports that release as
    newer.
    """
    try:
        raw = _pkg_version("hokku-server")
    except PackageNotFoundError:
        return None
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", raw)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


@dataclass
class ReleaseInfo:
    tag: str
    version: tuple[int, int, int]
    asset_name: str
    asset_url: str
    asset_size: int
    body: str

    @property
    def version_str(self) -> str:
        return ".".join(str(p) for p in self.version)

    @property
    def deb_version(self) -> str:
        """The Debian version string a clean release tag maps to.

        The release track's revision suffix is always ``-1`` (never a
        ``~dev.N`` suffix — that's the dev track only) per
        ``python/AGENTS.md``'s versioning rules, and a release-track tag is
        always a clean ``MAJOR.MINOR.PATCH`` with no pre-release suffix (see
        :func:`_parse_release_tag`). This lets ``self-update.sh`` verify the
        staged ``.deb``'s actual ``Version`` field matches what we meant to
        download, independent of the asset filename.
        """
        return f"{self.version_str}-1"


def _fetch_all_releases() -> list[dict]:
    """Fetch all releases from GitHub, newest first. Raises on failure.

    Split out as its own function (rather than inlined into
    :func:`find_newer_release`) so tests can monkeypatch this single seam
    instead of stubbing ``urllib`` directly.
    """
    req = urllib.request.Request(
        GITHUB_RELEASES_ALL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "hokku-server"},
    )
    with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _release_asset(release: dict) -> dict | None:
    for asset in release.get("assets") or []:
        if _ASSET_RE.match(asset.get("name", "")):
            return asset
    return None


def find_newer_release(current: tuple[int, int, int]) -> ReleaseInfo | None:
    """Return the newest clean, even-patch release strictly newer than *current*
    that ships a ``hokku-server_*_all.deb`` asset, or ``None`` if there isn't one.

    Deliberately does not use GitHub's ``/releases/latest`` endpoint — that
    depends on every dev/pre-release having been correctly flagged
    ``--prerelease`` at ``gh release create`` time, an assumption this
    shouldn't rely on. Instead this walks all releases (already newest-first)
    and applies our own even-patch/asset-name filter.
    """
    releases = _fetch_all_releases()
    for release in releases:
        version = _parse_release_tag(release.get("tag_name", ""))
        if version is None or version[2] % 2 != 0:
            continue
        if version <= current:
            continue
        asset = _release_asset(release)
        if asset is None:
            continue
        return ReleaseInfo(
            tag=release["tag_name"],
            version=version,
            asset_name=asset["name"],
            asset_url=asset["browser_download_url"],
            asset_size=int(asset.get("size") or 0),
            body=release.get("body") or "",
        )
    return None


def download_asset(info: ReleaseInfo, dest: Path, on_progress=None) -> None:
    """Stream *info*'s asset to *dest*, atomically. Raises on failure.

    Same shape as ``tools/release_cache.py``'s ``_download_with_progress``,
    adapted to report through a callback (fed into the job log) instead of
    printing to stdout.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(info.asset_url, headers={"User-Agent": "hokku-server"})
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length", 0)) or info.asset_size
        written = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                if on_progress:
                    on_progress(written, total)
    tmp.replace(dest)  # atomic on POSIX


class SelfUpdateManager:
    """Owns at most one in-flight check/install job.

    Structurally mirrors ``flashing.FlashJobManager``: a lock-guarded single
    slot, one daemon thread, a polled ``status()`` snapshot. Unlike a flash
    job this never reaches an in-process "done" — once the install is
    triggered the Flask process may itself be killed and replaced by the
    package upgrade, so success is only ever observed by reading
    :meth:`read_disk_status` or by the frontend noticing the server came
    back on a new version.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: dict | None = None

    def _new_job(self, kind: str) -> dict | None:
        with self._lock:
            if self._job is not None and self._job["state"] not in _FINISHED_STATES:
                return None
            job = {
                "id": next(_job_ids),
                "kind": kind,
                "state": "checking" if kind == "check" else "downloading",
                "log": [],
                "error": None,
                "result": None,
                "started_at": time.time(),
                "finished_at": None,
            }
            self._job = job
            return job

    def _append(self, job: dict, line: str) -> None:
        with self._lock:
            job["log"].append(line)

    def _finish(self, job: dict, *, state: str, result=None, error: str | None = None) -> None:
        with self._lock:
            job["state"] = state
            job["result"] = result
            job["error"] = error
            job["finished_at"] = time.time()

    def check(self) -> int | None:
        """Run a synchronous version check. Returns the job id, or ``None`` if
        a job is already running."""
        job = self._new_job("check")
        if job is None:
            return None
        try:
            current = _current_version()
            if current is None:
                raise RuntimeError("could not determine the installed package version")
            release = find_newer_release(current)
            result = {
                "current_version": ".".join(str(p) for p in current),
                "update_available": release is not None,
                "latest_version": release.version_str if release else None,
                "tag": release.tag if release else None,
                "asset_name": release.asset_name if release else None,
                "release_notes": release.body if release else None,
            }
            self._finish(job, state="up_to_date" if release is None else "checked", result=result)
        except (OSError, urllib.error.URLError, ValueError, RuntimeError) as exc:
            logger.error("Update check failed: %s", exc)
            self._finish(job, state="error", error=str(exc))
        return job["id"]

    def start_install(self, tag: str) -> int | None:
        """Re-check GitHub for *tag* (never trust a stale client-provided
        target), download its asset, and trigger the privileged install.
        Returns the job id, or ``None`` if a job is already running."""
        job = self._new_job("install")
        if job is None:
            return None
        thread = threading.Thread(
            target=self._run_install, args=(job, tag), name=f"self-update-{job['id']}", daemon=True
        )
        thread.start()
        return job["id"]

    def _run_install(self, job: dict, tag: str) -> None:
        try:
            current = _current_version()
            if current is None:
                raise RuntimeError("could not determine the installed package version")
            release = find_newer_release(current)
            if release is None or release.tag != tag:
                raise RuntimeError(
                    f"requested update {tag!r} is no longer the latest available release"
                )

            self._append(job, f"Downloading {release.asset_name}...")

            def on_progress(written: int, total: int) -> None:
                with self._lock:
                    job["state"] = "downloading"

            download_asset(release, _STAGED_DEB, on_progress)
            self._append(job, "Download complete, verifying...")
            with self._lock:
                job["state"] = "verifying"
            _STAGED_META.write_text(
                json.dumps(
                    {
                        "expected_package": "hokku-server",
                        "expected_version_deb": release.deb_version,
                        "tag": release.tag,
                    }
                ),
                encoding="utf-8",
            )

            self._append(job, "Triggering privileged install...")
            with self._lock:
                job["state"] = "triggering"
            # "sudo" is a partial path (relies on PATH) — intentional: this
            # exact command line must match python/debian/hokku-server.sudoers
            # byte-for-byte, which itself spells the command as `sudo ...`.
            cmd = [
                "sudo",
                "/usr/bin/systemctl",
                "start",
                "--no-block",
                "hokku-server-self-update.service",
            ]
            subprocess.run(cmd, check=True, timeout=15)
            self._append(job, "Install triggered — service will restart shortly.")
            self._finish(job, state="triggered", result={"tag": release.tag})
        except (
            OSError,
            urllib.error.URLError,
            ValueError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as exc:
            logger.error("Self-update install failed: %s", exc)
            self._append(job, f"ERROR: {exc}")
            self._finish(job, state="error", error=str(exc))

    def status(self) -> dict | None:
        with self._lock:
            if self._job is None:
                return None
            job = self._job
            return {
                "id": job["id"],
                "kind": job["kind"],
                "state": job["state"],
                "log": list(job["log"]),
                "error": job["error"],
                "result": job["result"],
                "started_at": job["started_at"],
                "finished_at": job["finished_at"],
            }

    @staticmethod
    def read_disk_status() -> dict | None:
        """Read the root-written status file left by ``self-update.sh``.

        This is the only source of truth once the install has actually been
        triggered — the Flask process that triggered it may be killed and
        replaced by the package upgrade before it can observe the outcome
        itself."""
        try:
            return json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
