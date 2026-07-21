"""Hokku installer — captive portal setup wizard for first-boot configuration."""

from __future__ import annotations

import argparse
import logging
import signal
import socket
import subprocess
import sys
from importlib.metadata import version as _pkg_version

from hokku.installer import ap_manager, setup_state
from hokku.installer.flask_app import create_app

logger = logging.getLogger(__name__)

_DNSMASQ_CONF = "/etc/hokku-installer/dnsmasq-ap.conf"
_DEFAULT_PORT = 80


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(description="Hokku setup wizard")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    args = parser.parse_args()

    if setup_state.is_setup_complete():
        logger.info("Setup already complete (sentinel exists) — exiting")
        sys.exit(0)

    # Fail fast if port is already in use.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("0.0.0.0", args.port))  # noqa: S104
    except OSError as exc:
        logger.critical("Port %s is already in use: %s", args.port, exc)
        sys.exit(1)
    finally:
        probe.close()

    dnsmasq_proc: subprocess.Popen | None = None

    def shutdown(sig=None, frame=None):
        logger.info("Shutting down installer")
        if dnsmasq_proc and dnsmasq_proc.poll() is None:
            dnsmasq_proc.terminate()
        ap_manager.stop_ap()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    logger.info("Starting setup AP")
    try:
        ap_manager.start_ap()
    except RuntimeError as exc:
        logger.critical("Failed to start AP: %s", exc)
        sys.exit(1)

    logger.info("Starting dnsmasq")
    dnsmasq_proc = _start_dnsmasq()

    version = _read_version()
    logger.info("Hokku installer %s — serving setup wizard on port %s", version, args.port)
    logger.info("AP: 'Hokku Setup' at 192.168.11.1 — connect and visit http://192.168.11.1")

    app = create_app()
    app.run(host="0.0.0.0", port=args.port, use_reloader=False)  # noqa: S104

    shutdown()


def _start_dnsmasq() -> subprocess.Popen | None:
    try:
        proc = subprocess.Popen(
            ["dnsmasq", "--no-daemon", f"--conf-file={_DNSMASQ_CONF}"],  # noqa: S607
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("dnsmasq started (pid %s)", proc.pid)
        return proc
    except FileNotFoundError:
        logger.warning("dnsmasq not found — DHCP and captive DNS will not work")
        return None
    except Exception as exc:
        logger.warning("Failed to start dnsmasq: %s", exc)
        return None


def _read_version() -> str:
    try:
        return _pkg_version("hokku-installer")
    except Exception:
        return "unknown"


if __name__ == "__main__":
    main()
