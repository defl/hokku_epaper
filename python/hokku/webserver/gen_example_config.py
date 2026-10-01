"""Generate ``config/config.json.example`` from the AppConfig defaults.

The example file is shipped in the wheel, the ``.deb`` and the appliance image,
and a fresh install is seeded from it — so if it drifts from the code, every
new install starts from a stale config. It did drift: it was hand-maintained,
fell behind a field that ImageConfig gained, and every pipeline in it was being
silently discarded on load.

Generating it removes the class of bug entirely: there is one source of truth
(the dataclass defaults) and ``test_example_config.py`` fails if the checked-in
file does not match what this produces.

Usage::

    python -m hokku.webserver.gen_example_config          # rewrite in place
    python -m hokku.webserver.gen_example_config --check   # exit 1 if stale
    python -m hokku.webserver.gen_example_config -o FILE   # write elsewhere
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hokku.webserver.app_config import AppConfig

#: The checked-in example, relative to this file.
EXAMPLE_PATH = Path(__file__).resolve().parent / "config" / "config.json.example"


def render() -> str:
    """Return the example file's exact intended contents."""
    # asdict() of a freshly constructed AppConfig: every field, including the
    # schema version and the three image pipelines, at its documented default.
    payload = AppConfig().to_dict()
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate config/config.json.example from the AppConfig defaults."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the checked-in file is out of date",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=EXAMPLE_PATH,
        help=f"where to write (default: {EXAMPLE_PATH})",
    )
    args = parser.parse_args(argv)

    want = render()

    if args.check:
        have = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if have == want:
            print(f"{args.output} is up to date")
            return 0
        print(
            f"{args.output} is OUT OF DATE.\n"
            "Regenerate it with:  python -m hokku.webserver.gen_example_config",
            file=sys.stderr,
        )
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(want, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
