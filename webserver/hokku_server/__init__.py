"""Hokku Spectra 6 e-ink image server."""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
from pathlib import Path

import psutil as _psutil

from hokku_server.app_config import AppConfig
from hokku_server.app_state import AppState, build_manager
from hokku_server.flask_app import create_app
from hokku_server.image_classifier import ImageClassifier
from hokku_server.mdns import start_mdns
from hokku_server.serve_scheduler import ServeScheduler
from hokku_server.watcher import Watcher

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # Keep third-party loggers quiet at INFO level.
    logging.getLogger("PIL").setLevel(logging.WARNING)
    logging.getLogger("numba").setLevel(logging.WARNING)
    logging.getLogger("zeroconf").setLevel(logging.WARNING)

    # Suppress Werkzeug access-log noise for high-frequency polling endpoints.
    _SILENT_PATHS = {"/hokku/api/status"}

    class _SilentFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            return not any(p in msg for p in _SILENT_PATHS)

    logging.getLogger("werkzeug").addFilter(_SilentFilter())

    parser = argparse.ArgumentParser(description="Hokku Spectra 6 image server")
    parser.add_argument("config", help="Path to config.json")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = AppConfig.load(config_path)

    if not config.upload_dir:
        logger.critical("upload_dir is not set in config — edit your config.json and set upload_dir")
        sys.exit(1)
    if not config.cache_dir:
        logger.critical("cache_dir is not set in config — edit your config.json and set cache_dir")
        sys.exit(1)

    upload_dir = Path(config.upload_dir)
    cache_dir = Path(config.cache_dir)
    if not upload_dir.is_dir():
        logger.critical("upload_dir does not exist: %s", upload_dir)
        sys.exit(1)
    if not cache_dir.is_dir():
        logger.critical("cache_dir does not exist: %s", cache_dir)
        sys.exit(1)
    if not os.access(cache_dir, os.W_OK):
        logger.critical("cache_dir is not writable: %s", cache_dir)
        sys.exit(1)

    classifier = ImageClassifier(config)
    manager = build_manager(config, classifier)
    logger.info(
        "Image workers: configured=%s -> resolved=%s (%s, cores=%s, free RAM=%.1f GB)",
        config.image_worker_thread_count,
        manager.resolved_worker_count,
        type(manager).__name__,
        os.cpu_count(),
        _psutil.virtual_memory().available / 1e9,
    )
    logger.info("BW detection: %s", config.classifier_bw_detect_enabled)

    scheduler = ServeScheduler(manager)
    state = AppState(config, classifier, manager, scheduler)
    watcher = Watcher(state)
    state.watcher = watcher
    app = create_app(state, config_path=config_path)

    logger.info("Hokku image server starting")
    logger.info("Upload dir: %s", upload_dir)
    logger.info("Cache dir: %s", cache_dir)
    logger.info("Refresh at: %s", list(config.refresh_image_at_time))
    logger.info("Poll interval: %ss", config.poll_interval_seconds)
    logger.info("Orientation: %s", config.orientation)
    logger.info("Pipeline slug: %s", config.cache_slug())
    logger.info("Endpoints: GET /hokku/screen/ (panel binary), GET /hokku/ui (web GUI)")

    # Fail fast if port is taken — Werkzeug's own error can be missed and
    # leaves you debugging a stale process serving stale content.
    _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _probe.bind(("0.0.0.0", config.port))
    except OSError as exc:
        logger.critical(
            "Port %s is already in use (%s). Find the owner with: ss -tlnp | grep :%s",
            config.port, exc, config.port,
        )
        sys.exit(1)
    finally:
        _probe.close()

    logger.info("Starting server on port %s", config.port)
    _zc = start_mdns(config.port, config.mdns_hostname) if config.mdns_hostname else None
    state._zc = _zc  # hand ownership to AppState so config reloads can restart mDNS
    app.run(host="0.0.0.0", port=config.port)


if __name__ == "__main__":
    main()
