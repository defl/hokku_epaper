#!/usr/bin/env python3
"""Upgrade (or downgrade / roll back) a Debian ``hokku-server`` install.

Run it on the server itself — a Debian / Ubuntu box or the Raspberry Pi
appliance (Pi OS is Debian; the package is ``Architecture: all``)::

    sudo python3 hokku_upgrade.py                 # pick a release from a list
    sudo python3 hokku_upgrade.py --tag v4.0.0-beta4
    python3 hokku_upgrade.py --list               # releases + tags, no changes
    sudo python3 hokku_upgrade.py --tag v4.0.0-beta3 --restore BACKUP.tar   # roll back

What one run does, in order:

  1. Lists the GitHub releases that ship a ``hokku-server_*.deb`` (plus tags
     without one, for reference) and marks the installed version.
  2. Prints the chosen release's "Upgrading" notes and asks for confirmation.
  3. Downloads the ``.deb`` and checks its SHA-256 against the release.
  4. Stops the service and tars ``/var/lib/hokku`` (plus ``upload_dir`` /
     ``cache_dir`` if they live elsewhere) into ``--backup-dir``, after
     checking the disk has room for it — an SD card often does not.
  5. ``apt-get install`` the package. Its postinst pip-installs anything the
     new release needs that isn't on the box yet, so the server needs internet.
  6. Waits for the server, then reports settings that are new in this release
     and where the pipeline settings differ from the release's shipped
     defaults, and optionally adopts those defaults.
  7. Optionally clears the classifier and render caches and waits for every
     picture to be re-rendered (the same as Admin -> "Clear caches and
     reconvert"). On a Pi Zero 2 W that takes a while for a large library.
  8. Prints the rollback command.

With ``--restore`` it installs the chosen release and then puts a backup made
by step 4 back in place, which is a byte-for-byte rollback of settings, photos
and renders.

Stdlib only, and standalone on purpose: it runs on the server with the
``python3`` the package already depends on, where there is no repo checkout to
import ``release_cache`` from.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_REPO = "defl/hokku_epaper"
PACKAGE = "hokku-server"
SERVICE = "hokku-server"
STATE_DIR = Path("/var/lib/hokku")
CONFIG_PATH = STATE_DIR / "config.json"
DEFAULT_BACKUP_DIR = Path("/var/backups/hokku")
DOWNLOAD_DIR = Path("/var/cache/hokku-upgrade")
# Regenerable, so not backed up — but kept across a restore: on a Pi Zero 2 W
# re-JITting the dither kernel takes minutes of the only fast core.
NUMBA_CACHE = STATE_DIR / "numba_cache"
# The appliance's first-boot wizard. While its sentinel is missing the
# hokku-server unit's ConditionPathExists keeps the server from starting.
INSTALLER_DIR = Path("/var/lib/hokku-installer")
SETUP_SENTINEL = INSTALLER_DIR / "setup_complete"
# Headroom on top of the backup's own size, so the backup can't fill the disk.
FREE_SPACE_MARGIN = 200 * 1024**2
PIPELINES = {
    "image_config_default": "default_general",
    "image_config_bw": "default_bw",
    "image_config_face": "default_face",
}
_DEB_RE = re.compile(r"^hokku-server_.+_all\.deb$")


@dataclass
class Release:
    tag: str
    name: str
    prerelease: bool
    published_at: str
    body: str
    deb_name: str | None = None
    deb_url: str | None = None
    deb_sha256: str | None = None

    @property
    def upstream_version(self) -> str:
        return tag_to_upstream_version(self.tag)


# ── pure helpers (unit tested) ──────────────────────────────────────────


def tag_to_upstream_version(tag: str) -> str:
    """``v4.0.0-beta4`` -> ``4.0.0~beta4`` (the Debian upstream-version form)."""
    v = tag[1:] if tag[:1] in ("v", "V") else tag
    return v.replace("-", "~", 1)


def upstream_of(deb_version: str) -> str:
    """Strip the Debian revision: ``4.0.0~beta4-1`` -> ``4.0.0~beta4``."""
    return deb_version.rsplit("-", 1)[0] if "-" in deb_version else deb_version


def parse_releases(payload: list[dict[str, Any]]) -> list[Release]:
    out = []
    for r in payload:
        if r.get("draft"):
            continue
        rel = Release(
            tag=r["tag_name"],
            name=r.get("name") or r["tag_name"],
            prerelease=bool(r.get("prerelease")),
            published_at=(r.get("published_at") or "")[:10],
            body=r.get("body") or "",
        )
        for a in r.get("assets") or []:
            if _DEB_RE.match(a.get("name", "")):
                rel.deb_name = a["name"]
                rel.deb_url = a["browser_download_url"]
                digest = a.get("digest") or ""
                rel.deb_sha256 = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
                break
        out.append(rel)
    return out


def upgrading_notes(body: str) -> str:
    """The release body's "Upgrading ..." section(s), or ``""``."""
    lines = body.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    level = 0
    for line in lines:
        m = re.match(r"^(#+)\s+(.*)$", line)
        if m:
            if level and len(m.group(1)) <= level:
                level = 0
            if not level and m.group(2).lower().startswith("upgrading"):
                level = len(m.group(1))
        if level:
            out.append(line)
    return "\n".join(out).strip()


def flatten(d: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(d, dict):
        return {prefix: d}
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            out.update(flatten(v, key))
        else:
            out[key] = v
    return out


def new_settings(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Setting paths present after the upgrade that the old config.json lacked."""
    b, a = flatten(before), flatten(after)
    return sorted(k for k in a if k not in b and k != "version")


def preset_differences(
    config: dict[str, Any], presets: dict[str, Any]
) -> list[tuple[str, Any, Any]]:
    """(path, current, release default) for every pipeline value that differs."""
    diffs = []
    for slot, preset_name in PIPELINES.items():
        preset = presets.get(preset_name)
        if not isinstance(preset, dict) or slot not in config:
            continue
        cur = flatten(config[slot], slot)
        ref = flatten({k: v for k, v in preset.items() if k not in ("label", "description")}, slot)
        for k, v in ref.items():
            if cur.get(k) != v:
                diffs.append((k, cur.get(k), v))
    return diffs


def preset_payload(presets: dict[str, Any]) -> dict[str, Any]:
    return {
        slot: {k: v for k, v in presets[name].items() if k not in ("label", "description")}
        for slot, name in PIPELINES.items()
        if isinstance(presets.get(name), dict)
    }


def backup_command(dest: Path, paths: list[Path]) -> list[str]:
    """``tar`` invocation for the backup. Uncompressed: the bulk is JPEGs and
    rendered panels that don't shrink, and gzip on a Pi Zero's A53 would double
    the downtime for nothing."""
    rel = [str(p).lstrip("/") for p in paths]
    exclude = str(NUMBA_CACHE).lstrip("/")
    return ["tar", "-C", "/", "--exclude", exclude, "-cpf", str(dest), *rel]


def appliance_blocker(installer_dir: Path = INSTALLER_DIR) -> str | None:
    """Why the server can't be started right now on an appliance, or None."""
    if installer_dir.is_dir() and not (installer_dir / SETUP_SENTINEL.name).exists():
        return (
            "this appliance is in setup mode (no "
            f"{installer_dir / SETUP_SENTINEL.name}), so hokku-server won't start; "
            "finish the Hokku Setup wizard first"
        )
    return None


def compare_versions(a: str, b: str) -> int:
    """dpkg ordering of two Debian versions: -1, 0 or 1."""

    def holds(op: str) -> bool:
        cmd = ["dpkg", "--compare-versions", a, op, b]
        return subprocess.run(cmd, check=False).returncode == 0

    if holds("eq"):
        return 0
    lt = holds("lt")
    return -1 if lt else 1


# ── GitHub ──────────────────────────────────────────────────────────────


def _gh_get(url: str) -> Any:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "hokku-upgrade"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_releases(repo: str) -> list[Release]:
    return parse_releases(_gh_get(f"https://api.github.com/repos/{repo}/releases?per_page=100"))


def fetch_tags(repo: str) -> list[str]:
    return [t["name"] for t in _gh_get(f"https://api.github.com/repos/{repo}/tags?per_page=100")]


# ── local system ────────────────────────────────────────────────────────


def run(cmd: list[str], dry_run: bool = False, check: bool = True) -> int:
    print(f"  $ {' '.join(cmd)}", flush=True)
    if dry_run:
        return 0
    rc = subprocess.run(cmd, check=False).returncode
    if check and rc != 0:
        sys.exit(f"error: command failed ({rc}): {' '.join(cmd)}")
    return rc


def installed_version() -> str | None:
    r = subprocess.run(
        ["dpkg-query", "-W", "-f=${Status} ${Version}", PACKAGE],  # noqa: S607 — dpkg-query on PATH
        capture_output=True,
        text=True,
        check=False,
    )
    parts = r.stdout.split()
    if r.returncode != 0 or "installed" not in parts or len(parts) < 4:
        return None
    return parts[-1]


def read_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def server_url(config: dict[str, Any]) -> str:
    return f"http://127.0.0.1:{config.get('port', 8080)}"


def api(base: str, path: str, body: dict[str, Any] | None = None, method: str | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method or ("POST" if data is not None else "GET"),
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def wait_for_server(base: str, timeout: float) -> dict[str, Any]:
    deadline = time.time() + timeout
    while True:
        try:
            return api(base, "/hokku/api/config")
        except (urllib.error.URLError, OSError, ValueError):
            if time.time() > deadline:
                sys.exit(
                    f"error: server at {base} did not answer within {timeout:.0f}s; see journalctl -u {SERVICE}"
                )
            time.sleep(3)


def download(rel: Release, dry_run: bool) -> Path:
    assert rel.deb_name and rel.deb_url
    dest = DOWNLOAD_DIR / rel.deb_name
    print(f"\n== Download {rel.deb_name}")
    if dry_run:
        print(f"  (dry run) {rel.deb_url} -> {dest}")
        return dest
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    h = hashlib.sha256()
    with urllib.request.urlopen(rel.deb_url, timeout=60) as r, open(tmp, "wb") as f:
        for chunk in iter(lambda: r.read(1 << 16), b""):
            h.update(chunk)
            f.write(chunk)
    sha = h.hexdigest()
    if rel.deb_sha256 and sha != rel.deb_sha256:
        tmp.unlink()
        sys.exit(
            f"error: SHA-256 mismatch for {rel.deb_name}: got {sha}, release says {rel.deb_sha256}"
        )
    tmp.replace(dest)
    print(
        f"  sha256 {sha} {'(matches release)' if rel.deb_sha256 else '(release publishes no digest)'}"
    )
    return dest


def backup_paths(config: dict[str, Any]) -> list[Path]:
    paths = [STATE_DIR]
    for key in ("upload_dir", "cache_dir"):
        p = Path(config.get(key) or STATE_DIR)
        if p.is_absolute() and p.exists() and STATE_DIR not in (p, *p.parents):
            paths.append(p)
    return paths


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        if Path(root) == NUMBA_CACHE or NUMBA_CACHE in Path(root).parents:
            continue
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def make_backup(backup_dir: Path, version: str, config: dict[str, Any], dry_run: bool) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"hokku-{version}-{stamp}.tar"
    paths = backup_paths(config)
    print(f"\n== Back up {', '.join(map(str, paths))} -> {dest}")
    need = sum(dir_size(p) for p in paths)
    probe = backup_dir if backup_dir.exists() else backup_dir.parent
    free = shutil.disk_usage(probe).free
    print(f"  {need / 1024**2:.0f} MB to copy, {free / 1024**2:.0f} MB free on {probe}")
    if need + FREE_SPACE_MARGIN > free:
        sys.exit(
            "error: not enough free space for the backup; point --backup-dir at a "
            "bigger disk (e.g. a USB stick) or pass --no-backup"
        )
    if dry_run:
        return dest
    backup_dir.mkdir(parents=True, exist_ok=True)
    run(backup_command(dest, paths))
    (backup_dir / (dest.name + ".json")).write_text(
        json.dumps(
            {"package_version": version, "paths": [str(p) for p in paths], "created": stamp},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  {dest.stat().st_size / 1024**2:.1f} MB")
    return dest


def restore_backup(backup: Path, dry_run: bool) -> None:
    print(f"\n== Restore {backup}")
    meta_path = backup.with_name(backup.name + ".json")
    paths = [STATE_DIR]
    if meta_path.exists():
        paths = [
            Path(p) for p in json.loads(meta_path.read_text(encoding="utf-8")).get("paths", [])
        ] or paths
    run(["systemctl", "stop", SERVICE], dry_run)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    numba_aside = None
    for p in paths:
        if p.exists():
            aside = p.with_name(f"{p.name}.pre-restore-{stamp}")
            print(f"  move {p} -> {aside}")
            if not dry_run:
                p.rename(aside)
            if p == STATE_DIR:
                numba_aside = aside / NUMBA_CACHE.name
    # tar auto-detects compression, so older .tgz backups restore the same way.
    run(["tar", "-C", "/", "-xpf", str(backup)], dry_run)
    if numba_aside and not dry_run and numba_aside.is_dir() and not NUMBA_CACHE.exists():
        numba_aside.rename(NUMBA_CACHE)
    run(["systemctl", "start", SERVICE], dry_run)


# ── interactive ─────────────────────────────────────────────────────────


def print_menu(releases: list[Release], tags: list[str], installed: str | None) -> list[Release]:
    installable = [r for r in releases if r.deb_url]
    inst_up = upstream_of(installed) if installed else None
    print(f"\nInstalled: {PACKAGE} {installed or '(not installed)'}\n")
    print("Releases with a server package:")
    for i, r in enumerate(installable, 1):
        mark = ""
        if inst_up:
            c = compare_versions(r.upstream_version, inst_up)
            mark = {0: "<- installed", 1: "upgrade", -1: "downgrade"}[c]
        kind = "pre-release" if r.prerelease else "release"
        print(f"  {i:2d}) {r.tag:<18} {r.published_at}  {kind:<11} {mark}")
    have = {r.tag for r in installable}
    other = [t for t in tags if t not in have]
    if other:
        print("\nTags without a server .deb (not installable with this script):")
        print("  " + ", ".join(other))
    return installable


def choose(installable: list[Release]) -> Release:
    while True:
        ans = input("\nInstall which number (q to quit)? ").strip().lower()
        if ans in ("q", "quit", ""):
            sys.exit("aborted")
        if ans.isdigit() and 1 <= int(ans) <= len(installable):
            return installable[int(ans) - 1]
        print("  not a number from the list")


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"{prompt} [y/N] y (--yes)")
        return True
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


# ── steps after install ─────────────────────────────────────────────────


def report_settings(
    base: str, before: dict[str, Any], mode: str, assume_yes: bool, dry_run: bool
) -> None:
    print("\n== Settings")
    if dry_run:
        print("  (dry run) would compare settings and offer the release's default presets")
        return
    cfg = api(base, "/hokku/api/config")
    config, defaults = cfg.get("config", {}), flatten(cfg.get("config_defaults", {}))
    added = new_settings(before, config) if before else []
    if added:
        print("  New in this release (filled in by the config migration):")
        for k in added:
            print(
                f"    {k} = {json.dumps(flatten(config).get(k))}  (fresh-install default {json.dumps(defaults.get(k))})"
            )
    else:
        print("  No new settings.")
    presets = cfg.get("dither_presets") or {}
    diffs = preset_differences(config, presets)
    if not diffs:
        print("  Pipeline settings already match this release's default presets.")
        return
    print(
        "  Pipeline settings that differ from this release's default presets (current -> default):"
    )
    for k, cur, ref in diffs:
        print(f"    {k}: {json.dumps(cur)} -> {json.dumps(ref)}")
    if mode == "keep":
        print("  Keeping current settings (--settings keep).")
        return
    if mode == "defaults" or (
        mode == "ask" and confirm("  Adopt this release's default presets?", assume_yes)
    ):
        result = api(base, "/hokku/api/config", preset_payload(presets))
        if not result.get("ok"):
            sys.exit(f"error: config save failed: {result}")
        print("  Adopted the default presets for all three pipelines.")
    else:
        print("  Kept current settings.")


def reconvert(base: str, mode: str, timeout: float, assume_yes: bool, dry_run: bool) -> None:
    print("\n== Reconvert pictures")
    if mode == "no":
        print("  Skipped (--reconvert no).")
        return
    if mode == "ask" and not confirm(
        "  Clear caches and re-render every picture with this release's pipeline?", assume_yes
    ):
        print("  Skipped; Admin -> 'Clear caches and reconvert' does it later.")
        return
    if dry_run:
        print("  (dry run) POST /hokku/api/classifier/clear, POST /hokku/api/clear_cache, wait")
        return
    api(base, "/hokku/api/classifier/clear", {})
    api(base, "/hokku/api/clear_cache", {})
    time.sleep(2)
    deadline = time.time() + timeout
    last = ""
    while True:
        st = api(base, "/hokku/api/status")
        line = f"  {st.get('converting_done', 0)}/{st.get('converting_total', 0)}"
        if line != last:
            print(line, st.get("converting_name") or "")
            last = line
        if not st.get("converting"):
            print("  All pictures re-rendered.")
            return
        if time.time() > deadline:
            print(f"  Still converting after {timeout:.0f}s; it continues in the background.")
            return
        time.sleep(5)


def report_firmware(base: str, dry_run: bool) -> None:
    if dry_run:
        return
    st = api(base, "/hokku/api/status")
    bundled = st.get("bundled_firmware_versions") or {}
    print("\n== Frame firmware")
    print(
        "  Served firmware: "
        + (", ".join(f"{m} {v}" for m, v in sorted(bundled.items())) or "unknown")
    )
    for name, s in sorted((st.get("screens") or {}).items()):
        want = bundled.get(s.get("screen_model") or "")
        have = s.get("firmware_version")
        flag = (
            "  (older: tick 'Update firmware on next refresh')"
            if want and have and have != want
            else ""
        )
        print(f"  {name}: {s.get('screen_model')} {have}{flag}")


# ── main ────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repo to install from")
    ap.add_argument("--tag", help="release tag to install, e.g. v4.0.0-beta4 (default: pick one)")
    ap.add_argument("--list", action="store_true", help="list releases and tags, then exit")
    ap.add_argument(
        "--settings",
        choices=("ask", "keep", "defaults"),
        default="ask",
        help="adopt the release's default presets for the three pipelines",
    )
    ap.add_argument(
        "--reconvert",
        choices=("ask", "yes", "no"),
        default="ask",
        help="clear caches and re-render every picture after the install",
    )
    ap.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    ap.add_argument("--no-backup", action="store_true", help="skip the backup (not recommended)")
    ap.add_argument(
        "--restore",
        type=Path,
        metavar="BACKUP",
        help="after installing, put this backup back in place (rollback)",
    )
    ap.add_argument(
        "--startup-timeout",
        type=float,
        default=900.0,
        help="max seconds to wait for the server to answer (a Pi Zero 2 W may "
        "re-JIT the dither kernel on first start)",
    )
    ap.add_argument(
        "--convert-timeout",
        type=float,
        default=3600.0,
        help="max seconds to wait for re-rendering; it carries on in the background",
    )
    ap.add_argument("-y", "--yes", action="store_true", help="answer yes to every question")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    args = ap.parse_args(argv)

    if not shutil.which("dpkg-query"):
        sys.exit("error: this script is for Debian package (.deb) installs")
    if sys.platform != "win32" and not (args.list or args.dry_run) and os.geteuid() != 0:
        sys.exit("error: run with sudo (or use --list / --dry-run)")
    if not args.list and (why := appliance_blocker()):
        sys.exit(f"error: {why}")

    try:
        releases = fetch_releases(args.repo)
    except urllib.error.HTTPError as e:
        hint = (
            " (GitHub rate limit: set GITHUB_TOKEN and use sudo -E)" if e.code in (403, 429) else ""
        )
        sys.exit(f"error: listing releases of {args.repo} failed: {e}{hint}")
    except urllib.error.URLError as e:
        sys.exit(f"error: cannot reach GitHub: {e.reason}")
    installed = installed_version()
    if args.list or not args.tag:
        try:
            tags = fetch_tags(args.repo)
        except urllib.error.URLError:
            tags = []
        installable = print_menu(releases, tags, installed)
        if args.list:
            return 0
        target = choose(installable)
    else:
        target = next((r for r in releases if r.tag == args.tag), None)
        if target is None or not target.deb_url:
            sys.exit(
                f"error: no release {args.tag!r} with a {PACKAGE} .deb in {args.repo} (try --list)"
            )

    if args.restore and not args.restore.is_file():
        sys.exit(f"error: backup {args.restore} not found")
    direction = "install"
    if installed:
        c = compare_versions(target.upstream_version, upstream_of(installed))
        direction = {0: "reinstall", 1: "upgrade", -1: "downgrade"}[c]
    print(f"\n== {direction}: {installed or '(none)'} -> {target.tag} ({target.deb_name})")
    notes = upgrading_notes(target.body)
    if notes and not args.restore:
        print("\n" + notes)
    if direction == "downgrade" and not args.restore:
        print("\nNote: an older server reuses the newer config as-is. For a true rollback pass "
              "--restore with the backup taken before the upgrade.")  # fmt: skip
    if not confirm(f"\nProceed with the {direction}?", args.yes):
        sys.exit("aborted")

    before = read_config()
    base = server_url(before)
    deb = download(target, args.dry_run)

    backup = None
    if not args.no_backup and installed and STATE_DIR.exists():
        run(["systemctl", "stop", SERVICE], args.dry_run, check=False)
        backup = make_backup(args.backup_dir, installed, before, args.dry_run)

    print(f"\n== Install {deb.name}")
    run(["apt-get", "install", "-y", "--allow-downgrades", str(deb)], args.dry_run)
    run(["systemctl", "start", SERVICE], args.dry_run)
    if args.restore:
        restore_backup(args.restore, args.dry_run)
        before = read_config()
        base = server_url(before)

    now = installed_version() if not args.dry_run else target.upstream_version
    print(f"  installed: {PACKAGE} {now}")
    if not args.dry_run:
        if not now or upstream_of(now) != target.upstream_version:
            sys.exit(f"error: expected {target.upstream_version}, dpkg reports {now}")
        cfg = wait_for_server(base, args.startup_timeout)
        print(f"  server answering at {base}, version {cfg.get('git_describe')}")

    if not args.restore:
        report_settings(base, before, args.settings, args.yes, args.dry_run)
        reconvert(base, args.reconvert, args.convert_timeout, args.yes, args.dry_run)
    report_firmware(base, args.dry_run)

    print("\nDone.")
    if backup and installed:
        old = next(
            (r for r in releases if r.deb_url and r.upstream_version == upstream_of(installed)),
            None,
        )
        script = Path(sys.argv[0]).resolve()
        if old:
            print(f"Roll back with:\n  sudo python3 {script} --tag {old.tag} --restore {backup}")
        else:
            print(f"Backup: {backup} ({installed} is not a GitHub release; reinstall that .deb, then "
                  f"--restore it with any --tag)")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
