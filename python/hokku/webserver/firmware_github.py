"""Fetch firmware artifacts from GitHub Releases.

This is the **only** part of the server that reaches the internet for firmware,
and it runs solely on an explicit user action (the "Check GitHub" / "Download"
buttons), gated by ``config.firmware_online_fetch``. The offline-first appliance
never calls in here on its own.

It reads the public Releases API for ``config.firmware_github_repo`` and matches
release assets against the ``hokku-<model>-<version>.<ext>`` convention that
ci-build.sh produces. Uses the stdlib ``urllib`` (no extra dependency) with short
timeouts; unauthenticated (the 60 req/hr anonymous limit is plenty for a manual
check on a home appliance).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from hokku.screens import firmware_registry
from hokku.webserver.firmware_library import CHANNEL_BETA, CHANNEL_STABLE

logger = logging.getLogger(__name__)

_API = "https://api.github.com/repos/{repo}/releases?per_page=50"
_USER_AGENT = "hokku-server"
_LIST_TIMEOUT = 12
_DOWNLOAD_TIMEOUT = 120
#: Refuse absurd downloads (a merged S3 bin is a few MB; the F7 img ~1 MB).
_MAX_ASSET_BYTES = 64 * 1024 * 1024

_ASSET_RE = re.compile(r"^hokku-(?P<model>[a-z0-9_]+)-(?P<version>.+)\.(?P<ext>bin|img)$")


class FirmwareFetchError(Exception):
    """A GitHub request failed or returned something unusable."""


@dataclass(frozen=True)
class RemoteFirmware:
    """One downloadable firmware asset discovered on a GitHub Release."""

    model_id: str
    version: str
    tag: str
    prerelease: bool
    asset_name: str
    download_url: str
    size: int

    @property
    def channel(self) -> str:
        return CHANNEL_BETA if self.prerelease else CHANNEL_STABLE


def _get(url: str, timeout: int, accept: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(_MAX_ASSET_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise FirmwareFetchError(f"GitHub returned HTTP {e.code} for {url}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FirmwareFetchError(f"could not reach GitHub: {e}") from e


def list_releases(repo: str) -> list[dict]:
    """Raw releases JSON for *repo* (newest first, as GitHub returns them)."""
    raw = _get(_API.format(repo=repo), _LIST_TIMEOUT, "application/vnd.github+json")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise FirmwareFetchError("GitHub returned invalid JSON") from e
    if not isinstance(data, list):
        # GitHub error bodies are objects, e.g. {"message": "Not Found"}.
        msg = data.get("message") if isinstance(data, dict) else "unexpected response"
        raise FirmwareFetchError(f"GitHub releases unavailable: {msg}")
    return data


def available_firmware(repo: str, *, include_prereleases: bool) -> list[RemoteFirmware]:
    """Every downloadable firmware asset across *repo*'s releases.

    Prereleases (betas) are excluded unless *include_prereleases* is True. Assets
    whose filename does not match a known model are ignored (a release also
    carries the ``.deb``, the appliance image, checksums, etc.)."""
    known = set(firmware_registry.known_models())
    out: list[RemoteFirmware] = []
    for rel in list_releases(repo):
        if not isinstance(rel, dict):
            continue
        prerelease = bool(rel.get("prerelease")) or bool(rel.get("draft"))
        if prerelease and not include_prereleases:
            continue
        tag = str(rel.get("tag_name") or rel.get("name") or "")
        for asset in rel.get("assets") or []:
            name = str(asset.get("name") or "")
            m = _ASSET_RE.match(name)
            if not m or m.group("model") not in known:
                continue
            # The extension must match the model's artifact type (a stray
            # hokku-bigme_f7-x.bin, say, is not a real F7 image).
            if firmware_registry.firmware_ext(m.group("model")) != m.group("ext"):
                continue
            url = asset.get("browser_download_url")
            if not url:
                continue
            out.append(
                RemoteFirmware(
                    model_id=m.group("model"),
                    version=m.group("version"),
                    tag=tag,
                    prerelease=prerelease,
                    asset_name=name,
                    download_url=str(url),
                    size=int(asset.get("size") or 0),
                )
            )
    return out


def download(remote: RemoteFirmware) -> bytes:
    """Download the asset bytes for *remote*, or raise FirmwareFetchError.

    Rejects a response larger than the sane cap so a misconfigured URL can't fill
    the disk."""
    data = _get(remote.download_url, _DOWNLOAD_TIMEOUT, "application/octet-stream")
    if len(data) > _MAX_ASSET_BYTES:
        raise FirmwareFetchError(f"{remote.asset_name} exceeds the {_MAX_ASSET_BYTES}-byte cap")
    if not data:
        raise FirmwareFetchError(f"{remote.asset_name} downloaded empty")
    return data
