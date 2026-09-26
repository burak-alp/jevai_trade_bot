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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jevbot", description=__doc__)
    parser.add_argument("--version", action="version", version=f"jevbot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("config", help="print the resolved configuration")
    _add_common(p)
    p.set_defaults(func=cmd_config)
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
