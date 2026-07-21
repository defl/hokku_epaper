"""Resolve paths to gitignored `.private/` resources without leaking any of their
internal structure (vendor tool names/versions, dump filenames, device serials)
into tracked code. See AGENTS.md -> "Privacy & .private".

The real, IP-sensitive paths live in `<private>/resources.json` (also gitignored),
which maps generic keys -> real relative paths. Tracked code references only the
generic keys. The private root defaults to `.private/` at the repo root and can be
overridden with the `HOKKU_PRIVATE_DIR` environment variable.

Example `<private>/resources.json` (NOT committed):
    {
      "bigme_flash_tool_dir": "screens/bigme_f7/tools/<vendor-tool>",
      "bigme_flash_tool_exe": "screens/bigme_f7/tools/<vendor-tool>/<exe>",
      "bigme_units_dir":      "screens/bigme_f7/units"
    }

Usage:
    from _private import res, private_root
    exe = res("bigme_flash_tool_exe")
"""

import json
import os
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def private_root() -> pathlib.Path:
    """Return the private data root (env `HOKKU_PRIVATE_DIR`, else `<repo>/.private`)."""
    env = os.environ.get("HOKKU_PRIVATE_DIR")
    return pathlib.Path(env) if env else _REPO_ROOT / ".private"


def _resource_map() -> dict:
    cfg = private_root() / "resources.json"
    if not cfg.exists():
        raise SystemExit(
            f"Missing private resource map: {cfg}\n"
            "Create it (gitignored) mapping generic keys -> real relative paths. "
            "See tools/_private.py docstring and AGENTS.md."
        )
    return json.loads(cfg.read_text(encoding="utf-8"))


def res(key: str) -> pathlib.Path:
    """Return the absolute path for a generic resource key, via `<private>/resources.json`."""
    rel = _resource_map().get(key)
    if rel is None:
        raise SystemExit(f"Resource key {key!r} not found in {private_root() / 'resources.json'}.")
    return private_root() / rel
