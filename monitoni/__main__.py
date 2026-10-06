"""Entry point: python -m monitoni [--mock] [--mock-purchase] [--fake-artnet HOST:PORT]
[--config-dir DIR] [--log-level L]."""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from monitoni.audio import Audio, MockAudio, PygameAudio
from monitoni.config import Config, ConfigError, load_config
from monitoni.daemon import Daemon
from monitoni.hardware.base import Hardware
from monitoni.hardware.mock import MockHardware
from monitoni.hardware.real import RealHardware
from monitoni.leds import ArtnetLeds, Leds, MockLeds
from monitoni.purchase import HttpPurchaseServer, MockPurchaseServer, PurchaseServer
from monitoni.runtime import Runtime

log = logging.getLogger("monitoni")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="monitoni", description="MoniToni vending daemon")
    parser.add_argument("--mock", action="store_true",
                        help="use mock hardware regardless of hardware.mode in config")
    parser.add_argument("--mock-purchase", action="store_true",
                        help="simulate payments instead of talking to purchase_server.base_url")
    parser.add_argument("--fake-artnet", metavar="HOST:PORT",
                        help="real feedback with mock hardware: ArtNet to this fake receiver "
                             "(python -m tests.fake_artnet) and sounds on this computer")
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
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per poll otherwise

    default_path = args.config_dir / "default.yaml"
    local_path = args.config_dir / "local.yaml"
    try:
        config = load_config(default_path, local_path)
    except ConfigError as exc:
        log.error("invalid configuration:\n%s", exc)
        return 1

    # relative data paths are taken from the repo root, not the working directory
    config.database.path = REPO_ROOT / config.database.path
    config.hardware.audio.dir = REPO_ROOT / config.hardware.audio.dir
    # the three runtime switches live next to the database: data/runtime.json by default
    runtime = Runtime.load(config.database.path.parent / "runtime.json", config)
    runtime.apply(config)  # brightness and volume, before the LEDs and audio read the config

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
        if not config.purchase_server.token:
            log.error("purchase_server.token: must be set in %s for the Monitoni server "
                      "(or run with --mock-purchase to simulate payments)", local_path)
            return 1
        purchase = HttpPurchaseServer(config.purchase_server)

    leds: Leds
    audio: Audio
    if args.fake_artnet:
        host, _, port = args.fake_artnet.rpartition(":")
        if not host or not port.isdigit():
            log.error("--fake-artnet needs HOST:PORT, got %r", args.fake_artnet)
            return 1
        config.hardware.wled.ip_address, config.hardware.wled.port = host, int(port)
        leds = ArtnetLeds(config, health_url=f"http://{host}:{port}/json/info")
        audio = PygameAudio(config.hardware.audio)
    else:
        if args.mock or not config.hardware.wled.enabled:
            log.info("LEDs: %s", "mock" if args.mock else "disabled in config")
            leds = MockLeds(config)
        else:
            leds = ArtnetLeds(config)
        if args.mock or not config.hardware.audio.enabled:
            log.info("audio: %s", "mock" if args.mock else "disabled in config")
            audio = MockAudio(config.hardware.audio.volume, config.hardware.audio.enabled)
        else:
            audio = PygameAudio(config.hardware.audio)
    return asyncio.run(run(config, hardware, purchase, leds, audio, runtime))


async def run(config: Config, hardware: Hardware, purchase: PurchaseServer,
              leds: Leds | None = None, audio: Audio | None = None,
              runtime: Runtime | None = None) -> int:
    """Start the daemon, wait for SIGINT/SIGTERM, stop it cleanly."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    daemon = Daemon(config, hardware, purchase, leds, audio, runtime)
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
