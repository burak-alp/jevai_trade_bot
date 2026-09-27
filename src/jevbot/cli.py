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


def cmd_recording_report(args: argparse.Namespace) -> int:
    from pathlib import Path

    from jevbot.core.time import now_ms
    from jevbot.recorder.report import build_report, minutes_ago, to_markdown

    cfg = _load(args)
    since = minutes_ago(args.last_min, now_ms()) if args.last_min else None
    rep = build_report(Path(cfg.data_dir), Path(cfg.run_dir), since=since)
    run = Path(cfg.run_dir)
    run.mkdir(parents=True, exist_ok=True)
    (run / "recording_report.json").write_bytes(orjson.dumps(rep, option=orjson.OPT_INDENT_2, default=str))
    md = to_markdown(rep)
    (run / "recording_report.md").write_text(md, encoding="utf-8")
    sys.stdout.write(md)
    return 0 if "error" not in rep else 7


def cmd_research_a0(args: argparse.Namespace) -> int:
    from datetime import date
    from pathlib import Path

    from jevbot.core.time import now_ms
    from jevbot.recorder.integrity import read_manifest
    from jevbot.research.a0 import A0Config, run_a0, to_markdown

    cfg = _load(args)
    hist = Path(args.hist) if args.hist else Path(cfg.data_dir) / "hist" / "um"
    if args.symbols == "ALL":
        symbols = sorted({e.get("symbol") for e in read_manifest(hist / "klines" / "1m").values()
                          if e.get("symbol") and e.get("quality") != "suspect"})
    else:
        symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    if "BTCUSDT" not in symbols:
        symbols.insert(0, "BTCUSDT")                 # needed for beta / regime
    out = Path(args.out) if args.out else Path(cfg.data_dir) / "research" / f"a0-{now_ms()}"
    summary = run_a0(A0Config(hist_root=hist, symbols=symbols, start=date.fromisoformat(args.start),
                              end=date.fromisoformat(args.end), tradable_top=args.tradable_top,
                              max_positions=args.max_positions), out)
    sys.stdout.write(to_markdown(summary))
    sys.stdout.write(f"\noutput: {out}\n")
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    from pathlib import Path

    from jevbot.core.time import DAY_MS, HOUR_MS
    from jevbot.recorder.compact import compact_all

    cfg = _load(args)
    root = Path(args.root) if args.root else Path(cfg.data_dir) / "raw"
    datasets = args.dataset.split(",") if args.dataset else None
    res = compact_all(root, datasets, group_ms=DAY_MS if args.group == "day" else HOUR_MS,
                      grace_ms=int(args.grace_min * 60_000), dry_run=args.dry_run)
    out = {k: v.__dict__ for k, v in res.items()}
    sys.stdout.write(orjson.dumps(out, option=orjson.OPT_INDENT_2).decode() + "\n")
    return 0 if not any(v.skipped_unverified for v in res.values()) else 5


def cmd_download(args: argparse.Namespace) -> int:
    from datetime import date
    from pathlib import Path

    from jevbot.hist.binance_vision import DATASETS, BinanceVision

    cfg = _load(args)
    out_root = Path(args.out) if args.out else Path(cfg.data_dir) / "hist" / "um"

    async def main() -> int:
        bv = BinanceVision(cfg.hist, out_root)
        try:
            if args.dataset == "pit-listing":
                syms = args.symbols.split(",") if args.symbols and args.symbols != "ALL" else None
                table = await bv.pit_listing(args.interval or "1m", syms)
                sys.stdout.write(orjson.dumps({"symbols": table.num_rows}).decode() + "\n")
                return 0
            if args.dataset not in DATASETS:
                sys.stderr.write(f"unknown dataset {args.dataset}; choose from {sorted(DATASETS)} or pit-listing\n")
                return 2
            if not (args.start and args.end and args.symbols):
                sys.stderr.write("--symbols, --start and --end are required\n")
                return 2
            if args.symbols == "ALL":
                symbols = await bv.list_symbols(args.dataset, args.granularity)
            else:
                symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            stats = await bv.download(args.dataset, symbols, date.fromisoformat(args.start),
                                      date.fromisoformat(args.end), interval=args.interval,
                                      granularity=args.granularity, force=args.force)
            sys.stdout.write(orjson.dumps(stats.__dict__, option=orjson.OPT_INDENT_2).decode() + "\n")
            return 0 if stats.failed == 0 else 6
        finally:
            await bv.aclose()

    return _run_async(main)


def cmd_loadtest(args: argparse.Namespace) -> int:
    from pathlib import Path

    from jevbot.core.logging import setup_logging
    from jevbot.testing.loadtest import DEFAULT_PHASES, QUICK_PHASES, Harness, to_markdown

    setup_logging("INFO", json=False)
    h = Harness(Path(args.workdir), phases=list(QUICK_PHASES if args.scenario == "quick" else DEFAULT_PHASES),
                fake_workers=args.fake_workers, rotate_s=args.rotate_s)
    rep = h.run()
    sys.stdout.write(to_markdown(rep))
    return 0 if rep["ok"] else 8


def cmd_fake_exchange(args: argparse.Namespace) -> int:
    from jevbot.core.logging import setup_logging
    from jevbot.testing.fake_binance import FakeConfig, serve_forever

    setup_logging("WARNING", json=True)
    cfg = FakeConfig(n_symbols=args.symbols, book_rate_total=args.book_rate, ws_port=args.ws_port,
                     http_port=args.http_port, reuse_port=args.reuse_port, control_file=args.control_file,
                     stats_file=args.stats_file)
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

    p = sub.add_parser("recording-report", help="summarize a recorder run (throughput, latency, gaps, disk, integrity)")
    _add_common(p)
    p.add_argument("--last-min", type=float, default=None, help="only the last N minutes (default: everything)")
    p.set_defaults(func=cmd_recording_report)

    p = sub.add_parser("research-a0", help="Arm A0 historical replay: scanner proposals, labels, edge stats")
    _add_common(p)
    p.add_argument("--hist", default=None, help="historical root (default: <data_dir>/hist/um)")
    p.add_argument("--symbols", default="ALL", help="comma separated or ALL (every symbol with 1m klines)")
    p.add_argument("--start", required=True, help="YYYY-MM-DD inclusive")
    p.add_argument("--end", required=True, help="YYYY-MM-DD exclusive")
    p.add_argument("--tradable-top", type=int, default=50, help="tradable = top-N by median 24h quote volume")
    p.add_argument("--max-positions", type=int, default=3)
    p.add_argument("--out", default=None, help="output dir (default: <data_dir>/research/a0-<ts>)")
    p.set_defaults(func=cmd_research_a0)

    p = sub.add_parser("compact", help="merge small recorder parts into hourly/daily sorted files (verified, atomic)")
    _add_common(p)
    p.add_argument("--root", default=None, help="recorder raw root (default: <data_dir>/raw)")
    p.add_argument("--dataset", default=None, help="comma separated datasets (default: all present)")
    p.add_argument("--group", choices=["hour", "day"], default="hour")
    p.add_argument("--grace-min", type=float, default=10.0, help="only groups that ended at least this long ago")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_compact)

    p = sub.add_parser("download", help="download historical data from data.binance.vision (futures/um)")
    _add_common(p)
    p.add_argument("--dataset", required=True,
                   help="klines | markPriceKlines | indexPriceKlines | premiumIndexKlines | aggTrades | metrics | "
                        "fundingRate | bookDepth | pit-listing")
    p.add_argument("--symbols", default=None, help="comma separated, or ALL (from the bucket listing)")
    p.add_argument("--start", default=None, help="YYYY-MM-DD (inclusive)")
    p.add_argument("--end", default=None, help="YYYY-MM-DD (inclusive)")
    p.add_argument("--interval", default=None, help="kline interval, e.g. 1m (kline-type datasets)")
    p.add_argument("--granularity", choices=["daily", "monthly"], default="daily")
    p.add_argument("--out", default=None, help="output root (default: <data_dir>/hist/um)")
    p.add_argument("--force", action="store_true", help="re-download files that already exist")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("loadtest", help="burst load test: multi-process fake exchange -> recorder -> analysis")
    p.add_argument("--workdir", required=True)
    p.add_argument("--scenario", choices=["default", "quick"], default="default")
    p.add_argument("--fake-workers", type=int, default=4)
    p.add_argument("--rotate-s", type=float, default=30.0, help="small rotation so files rotate during bursts")
    p.set_defaults(func=cmd_loadtest)

    p = sub.add_parser("fake-exchange", help="local fake Binance WS/REST for tests and load runs (dev only)")
    p.add_argument("--symbols", type=int, default=200)
    p.add_argument("--book-rate", type=float, default=1500.0, help="bookTicker msgs/s across all symbols")
    p.add_argument("--ws-port", type=int, default=18766)
    p.add_argument("--http-port", type=int, default=18765)
    p.add_argument("--reuse-port", action="store_true", help="allow several fake processes on the same ports")
    p.add_argument("--control-file", default=None, help="JSON control file polled for rate/drop/silence")
    p.add_argument("--stats-file", default=None, help="write per-process counters here every second")
    p.set_defaults(func=cmd_fake_exchange)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):     # Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as e:
        sys.stderr.write(f"config error: {e}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
