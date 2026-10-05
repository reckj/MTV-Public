"""Entry point: python -m monitoni [--mock] [--config-dir DIR] [--log-level LEVEL]."""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from monitoni.config import Config, ConfigError, load_config
from monitoni.daemon import Daemon
from monitoni.hardware.base import Hardware
from monitoni.hardware.mock import MockHardware

log = logging.getLogger("monitoni")

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="monitoni", description="MoniToni vending daemon")
    parser.add_argument("--mock", action="store_true",
                        help="use mock hardware regardless of hardware.mode in config")
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR,
                        help="directory holding default.yaml and local.yaml")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    default_path = args.config_dir / "default.yaml"
    local_path = args.config_dir / "local.yaml"
    try:
        config = load_config(default_path, local_path)
    except ConfigError as exc:
        log.error("invalid configuration:\n%s", exc)
        return 1

    if args.mock:
        config.hardware.mode = "mock"

    if config.hardware.mode == "real":
        if not local_path.exists():
            log.error("refusing to start with real hardware: %s is missing "
                      "(copy local.yaml.example and fill in your machine's values, "
                      "or run with --mock)", local_path)
            return 1
        log.error("real hardware mode is not implemented yet; run with --mock")
        return 1

    return asyncio.run(run(config, MockHardware()))


async def run(config: Config, hardware: Hardware) -> int:
    """Start the daemon, wait for SIGINT/SIGTERM, stop it cleanly."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    daemon = Daemon(config, hardware)
    await daemon.start()
    try:
        await stop.wait()
        log.info("shutdown requested")
    finally:
        await daemon.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
