"""Command line entry point: ``jevbot <command> [options]``."""

from __future__ import annotations

import argparse
import sys

import orjson

from jevbot import __version__
from jevbot.core.config import AppConfig, ConfigError, load_config
from jevbot.core.logging import setup_logging

DEFAULT_CONFIG = "config/base.yaml"


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", action="append", default=None,
                   help=f"YAML config file; repeat to overlay (default: {DEFAULT_CONFIG})")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="override a single config value, e.g. --set recorder.depth.top_n_by_volume=10")


def _load(args: argparse.Namespace) -> AppConfig:
    cfg = load_config(args.config or [DEFAULT_CONFIG], args.set)
    setup_logging(cfg.logging.level, cfg.logging.json, cfg.logging.file)
    return cfg


def cmd_config(args: argparse.Namespace) -> int:
    cfg = _load(args)
    sys.stdout.write(orjson.dumps({"config_hash": cfg.config_hash(), "config": cfg.to_dict()},
                                  option=orjson.OPT_INDENT_2).decode() + "\n")
    return 0


def _run_async(coro_factory):
    import asyncio

    try:
        import uvloop  # type: ignore[import-not-found]
        uvloop.install()
    except ImportError:
        pass
    return asyncio.run(coro_factory())


def cmd_record(args: argparse.Namespace) -> int:
    import asyncio
    import signal

    from jevbot.recorder.recorder import Recorder

    cfg = _load(args)

    async def main() -> int:
        rec = Recorder(cfg)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, rec.request_stop)
            except NotImplementedError:
                pass
        return await rec.run(duration_s=args.duration)

    return _run_async(main)


def cmd_smoke(args: argparse.Namespace) -> int:
    from jevbot.recorder.smoke import run_smoke

    cfg = _load(args)
    report = _run_async(lambda: run_smoke(cfg))
    sys.stdout.write(orjson.dumps(report.to_dict(), option=orjson.OPT_INDENT_2, default=str).decode() + "\n")
    return 0 if report.ok else 3


def cmd_verify(args: argparse.Namespace) -> int:
    from pathlib import Path

    from jevbot.recorder.integrity import verify_tree

    cfg = _load(args)
    root = Path(args.root) if args.root else Path(cfg.data_dir)
    rep = verify_tree(root)
    sys.stdout.write(orjson.dumps(rep.to_dict(), option=orjson.OPT_INDENT_2).decode() + "\n")
    return 0 if rep.ok else 5


def cmd_fake_exchange(args: argparse.Namespace) -> int:
    from jevbot.core.logging import setup_logging
    from jevbot.testing.fake_binance import FakeConfig, serve_forever

    setup_logging("WARNING", json=True)
    cfg = FakeConfig(n_symbols=args.symbols, book_rate_total=args.book_rate, ws_port=args.ws_port,
                     http_port=args.http_port)
    try:
        _run_async(lambda: serve_forever(cfg))
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jevbot", description=__doc__)
    parser.add_argument("--version", action="version", version=f"jevbot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("config", help="print the resolved configuration")
    _add_common(p)
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("record", help="run the public market-data recorder (no API key, no orders)")
    _add_common(p)
    p.add_argument("--duration", type=float, default=None, help="stop after N seconds (tests)")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("smoke", help="WS/REST integration smoke test against the configured endpoints")
    _add_common(p)
    p.set_defaults(func=cmd_smoke)

    p = sub.add_parser("verify", help="verify Parquet checksums/manifests below a data directory")
    _add_common(p)
    p.add_argument("--root", default=None, help="directory to verify (default: data_dir)")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("fake-exchange", help="local fake Binance WS/REST for tests and load runs (dev only)")
    p.add_argument("--symbols", type=int, default=200)
    p.add_argument("--book-rate", type=float, default=1500.0, help="bookTicker msgs/s across all symbols")
    p.add_argument("--ws-port", type=int, default=18766)
    p.add_argument("--http-port", type=int, default=18765)
    p.set_defaults(func=cmd_fake_exchange)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as e:
        sys.stderr.write(f"config error: {e}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
