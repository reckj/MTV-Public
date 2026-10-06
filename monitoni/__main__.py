"""Entry point: python -m monitoni [--mock] [--mock-purchase] [--config-dir DIR] [--log-level L]."""

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
from monitoni.hardware.real import RealHardware
from monitoni.purchase import HttpPurchaseServer, MockPurchaseServer, PurchaseServer

log = logging.getLogger("monitoni")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="monitoni", description="MoniToni vending daemon")
    parser.add_argument("--mock", action="store_true",
                        help="use mock hardware regardless of hardware.mode in config")
    parser.add_argument("--mock-purchase", action="store_true",
                        help="simulate payments instead of talking to purchase_server.base_url")
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

    # relative data paths are taken from the repo root, not the working directory
    config.database.path = REPO_ROOT / config.database.path
    config.qr.dir = REPO_ROOT / config.qr.dir

    if args.mock:
        config.hardware.mode = "mock"

    hardware: Hardware
    if config.hardware.mode == "real":
        if not local_path.exists():
            log.error("refusing to start with real hardware: %s is missing "
                      "(copy local.yaml.example and fill in your machine's values, "
                      "or run with --mock)", local_path)
            return 1
        hardware = RealHardware(config)
    else:
        hardware = MockHardware(config.vending.levels)
    purchase: PurchaseServer
    if args.mock_purchase:
        purchase = MockPurchaseServer()
    else:
        purchase = HttpPurchaseServer(config.purchase_server, config.system.machine_id)
    return asyncio.run(run(config, hardware, purchase))


async def run(config: Config, hardware: Hardware, purchase: PurchaseServer) -> int:
    """Start the daemon, wait for SIGINT/SIGTERM, stop it cleanly."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    daemon = Daemon(config, hardware, purchase)
    try:
        await daemon.start()
        await stop.wait()
        log.info("shutdown requested")
    except OSError as exc:
        log.error("cannot bind http://%s:%s: %s",
                  config.web.host, config.web.port, exc.strerror or exc)
        return 1
    finally:
        await daemon.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
