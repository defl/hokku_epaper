"""Filesystem utilities."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Module-level references so tests can patch just these bindings without
# touching the global os/time singletons (which would affect background threads).
_replace = os.replace
_sleep = time.sleep


def atomic_write_json(path: Path, payload: dict) -> None:
    """Write *payload* as JSON to *path* atomically via a tmp file + os.replace.

    On Windows, AV scanners (e.g. Defender) can briefly hold the destination
    file open without FILE_SHARE_DELETE, making os.replace fail with
    PermissionError.  Retries with exponential backoff to ride out the scan
    window; re-raises on the fifth failure.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    for attempt in range(5):
        try:
            _replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            _sleep(0.05 * (2 ** attempt))
