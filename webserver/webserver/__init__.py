"""Hokku Spectra 6 e-ink image server."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from webserver.app_state import AppState
from webserver.app_config import AppConfig
from webserver.flask_app import create_app
from webserver.image_classifier import ImageClassifier
from webserver.image_manager import ImageManager
from webserver.serve_scheduler import ServeScheduler
from webserver.watcher import Watcher


def main() -> None:
    parser = argparse.ArgumentParser(description="Hokku Spectra 6 image server")
    parser.add_argument("config", help="Path to config.json")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = AppConfig.load(config_path)

    if not config.upload_dir:
        print("Error: upload_dir is not set in config — edit your config.json and set upload_dir", file=sys.stderr)
        sys.exit(1)
    if not config.cache_dir:
        print("Error: cache_dir is not set in config — edit your config.json and set cache_dir", file=sys.stderr)
        sys.exit(1)

    upload_dir = Path(config.upload_dir)
    cache_dir = Path(config.cache_dir)
    if not upload_dir.is_dir():
        print(f"Error: upload_dir does not exist: {upload_dir}", file=sys.stderr)
        sys.exit(1)
    if not cache_dir.is_dir():
        print(f"Error: cache_dir does not exist: {cache_dir}", file=sys.stderr)
        sys.exit(1)
    if not os.access(cache_dir, os.W_OK):
        print(f"Error: cache_dir is not writable: {cache_dir}", file=sys.stderr)
        sys.exit(1)

    classifier = ImageClassifier(config)
    manager = ImageManager(config, classifier)
    scheduler = ServeScheduler(manager)
    state = AppState(config, classifier, manager, scheduler)
    app = create_app(state, config_path=config_path)

    print(f"Hokku image server")
    print(f"  Upload dir: {upload_dir}")
    print(f"  Cache dir:  {cache_dir}")
    print(f"  Refresh at: {list(config.refresh_image_at_time)}")
    print(f"  Poll interval: {config.poll_interval_seconds}s")
    print(f"  Orientation: {config.orientation}")
    print(f"  Pipeline slug: {config.cache_slug()}")
    print(f"  Endpoints:")
    print(f"    GET /hokku/screen/  — panel binary + X-Sleep-Seconds")
    print(f"    GET /hokku/ui       — web GUI")

    watcher = Watcher(state)

    # Suppress Werkzeug access-log noise for high-frequency polling endpoints.
    _SILENT_PATHS = {"/hokku/api/status"}

    class _SilentFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            return not any(p in msg for p in _SILENT_PATHS)

    logging.getLogger("werkzeug").addFilter(_SilentFilter())

    print(f"  Starting server on port {config.port}...")
    watcher.kick()  # everything is built — let the first sync begin
    app.run(host="0.0.0.0", port=config.port)


if __name__ == "__main__":
    main()
