"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__
from .config import Config, ConfigError
from .server import run_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dotwave-vapi-transcriber",
        description="Vapi custom transcriber server backed by .wave. Configured by environment "
        "variables; see the README.",
    )
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on")
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING or ERROR")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"dotwave-vapi-transcriber: {exc}", file=sys.stderr)
        return 2
    try:
        asyncio.run(run_server(config, args.host))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
