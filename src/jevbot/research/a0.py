"""Arm A0 historical replay: every scanner proposal, no filter (spec §14, roadmap Faz 1 go/no-go).

Question answered: do the deterministic setups have an edge before and after all costs?
Primary evaluation is decision-level (every proposal labelled); a simple portfolio simulation
(max N positions, 1 R risk each) is secondary.
"""

from __future__ import annotations

import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import orjson
import pyarrow as pa
import pyarrow.parquet as pq

from jevbot.core.logging import get_logger
from jevbot.research.data import DAY, load_funding, load_open_interest, load_symbol
from jevbot.research.features import FEATURE_SCHEMA, add_market_context, compute_symbol_features
from jevbot.research.labels import LABEL_VERSION, label_all
from jevbot.research.scanner import ScannerConfig, scan
from jevbot.research.slow import (POS_SCHEMA, SLOW_SCHEMA, SlowConfig, add_btc_relative, compute_slow_features,
                                  scan_slow)
from jevbot.research.universe import pit_first_listing

log = get_logger(__name__)


@dataclass
class A0Config:
    hist_root: Path
    symbols: list[str]
    start: date
    end: date                               # exclusive
    tradable_top: int = 50                  # PIT universe: daily top-N by trailing 24 h quote volume
    max_positions: int = 3
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    bootstrap: int = 2000
    seed: int = 7
    per_day: dict[str, list[str]] | None = None   # universe-pool: day -> that day's top-N (loads only these)
    arm_kind: str = "fast"                  # "fast" = 5 m BRK/PB (scanner.py), "slow" = hourly slow.v1 families,
                                            # "pos" = hourly pos.v1 (slow machinery + 5 m open interest)
    slow: SlowConfig = field(default_factory=SlowConfig)
    ci_block_days: int | None = None        # bootstrap block; None -> 1 (fast) / 7 (slow: 24-48 h holds overlap days)
    chunk_days: int = 30                    # bounded memory: features/scan per time chunk
    warmup_days: int = 30                   # feature warm-up before each chunk (BTC vol state uses 30 d)


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def block_bootstrap_ci(values: np.ndarray, days: np.ndarray, n: int, seed: int,
                       level: float = 0.95) -> tuple[float, float]:
    """``level`` CI of the mean resampling whole days (labels overlap within a day)."""
    if len(values) < 2:
        return float("nan"), float("nan")
    uniq, inv = np.unique(days, return_inverse=True)
    sums = np.bincount(inv, weights=values)
    cnts = np.bincount(inv).astype(float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(uniq), size=(n, len(uniq)))
    means = sums[idx].sum(axis=1) / np.maximum(cnts[idx].sum(axis=1), 1)
    a = (1 - level) / 2 * 100
    return float(np.percentile(means, a)), float(np.percentile(means, 100 - a))


def _block_days(cfg: A0Config) -> int:
    return cfg.ci_block_days or (1 if cfg.arm_kind == "fast" else 7)


def group_stats(rows: list[dict[str, Any]], cfg: A0Config) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    net = np.array([r["net_r"] for r in rows])
    gross = np.array([r["gross_r"] for r in rows])
    days = np.array([r["t_decision"] // DAY for r in rows])
    blocks = days // _block_days(cfg)                       # calendar blocks of ci_block_days
    lo, hi = block_bootstrap_ci(net, blocks, cfg.bootstrap, cfg.seed)
    lo99, _ = block_bootstrap_ci(net, blocks, cfg.bootstrap, cfg.seed, level=0.99)
    glo, ghi = block_bootstrap_ci(gross, blocks, cfg.bootstrap, cfg.seed)
    exits = defaultdict(int)
    for r in rows:
        exits[r["exit_type"]] += 1
    return {
        "n": len(rows), "days": int(len(np.unique(days))),
        "tp_rate": round(float(np.mean([r["y_success"] for r in rows])), 4),
        "mean_gross_r": round(float(gross.mean()), 4), "gross_ci95": [round(glo, 4), round(ghi, 4)],
        "mean_net_r": round(float(net.mean()), 4), "net_ci95": [round(lo, 4), round(hi, 4)],
        "net_ci99_lo": round(lo99, 4),
        "median_cost_r": round(float(np.median([r["cost_r"] for r in rows])), 4),
        "mean_fee_r": round(float(np.mean([r["fee_r"] for r in rows])), 4),
        "mean_funding_r": round(float(np.mean([r["funding_r"] for r in rows])), 4),
        "mean_mae_r": round(float(np.mean([r["mae_r"] for r in rows])), 3),
        "mean_mfe_r": round(float(np.mean([r["mfe_r"] for r in rows])), 3),
        "exit_types": dict(exits), "win_rate_net": round(float((net > 0).mean()), 4),
    }


def portfolio_sim(rows: list[dict[str, Any]], max_positions: int) -> dict[str, Any]:
    """Chronological, ranked: take a proposal when a slot and its symbol are free; 1 R risk each."""
    rows = sorted(rows, key=lambda r: (r["t_decision"], r.get("rank", 999)))
    open_pos: list[tuple[int, str]] = []                    # (exit_time, symbol)
    trades: list[dict[str, Any]] = []
    for r in rows:
        t = r["t_decision"]
        open_pos = [(e, s) for e, s in open_pos if e > t]
        if len(open_pos) >= max_positions or any(s == r["symbol"] for _, s in open_pos):
            continue
        open_pos.append((r["t_exit"], r["symbol"]))
        trades.append(r)
    if not trades:
        return {"trades": 0}
    trades.sort(key=lambda r: r["t_exit"])
    net = np.array([r["net_r"] for r in trades])
    eq = np.cumsum(net)
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], eq])) - np.concatenate([[0.0], eq])))
    days = np.array([r["t_exit"] // DAY for r in trades])
    span = max(1, int(days.max() - days.min() + 1))
    daily = np.zeros(span)
    np.add.at(daily, days - days.min(), net)
    gains, losses = net[net > 0].sum(), -net[net < 0].sum()
    return {
        "trades": len(trades), "trades_per_day": round(len(trades) / span, 2),
        "sum_net_r": round(float(net.sum()), 2), "mean_net_r": round(float(net.mean()), 4),
        "win_rate": round(float((net > 0).mean()), 4), "max_dd_r": round(dd, 2),
        "profit_factor": round(float(gains / losses), 3) if losses > 0 else None,
        "sharpe_daily_ann": round(float(daily.mean() / daily.std() * np.sqrt(365)), 3) if daily.std() > 0 else None,
        "fee_share_of_gross": round(float(np.sum([r["fee_r"] for r in trades]) /
                                          max(1e-9, abs(np.sum([r["gross_r"] for r in trades])))), 3),
    }


def _universe_stats(feats: dict[str, Any], top: int) -> dict[str, Any]:
    """Loaded symbols that were in the daily top-N at least once in this chunk."""
    from jevbot.research.scanner import _xs_rank
    from jevbot.research.universe import daily_universe_mask
    syms = list(feats)
    J = min(sf.n_ticks for sf in feats.values())
    qv = np.stack([feats[s].f["qv_24h"][:J] for s in syms], axis=1)
    mask = daily_universe_mask(_xs_rank(qv), np.asarray(feats[syms[0]].tick_time(np.arange(J))), top)
    return {"ever": [s for s, m in zip(syms, mask.any(axis=0)) if m]}


def _chunk_features(cfg: A0Config, syms: list[str], c0: int, c1: int,
                    listing: dict[str, int]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Features for [c0, c1) computed on [c0 - warmup, c1) and trimmed to the chunk's ticks."""
    w0 = c0 - cfg.warmup_days * DAY
    slow = cfg.arm_kind in ("slow", "pos")
    k0 = (c0 - w0) // ((60 if slow else 5) * 60_000)
    btc = "BTCUSDT"
    feats, fund = {}, {}
    order = [btc] + [s for s in syms if s != btc] if btc in syms else syms
    btc_sf = None
    for s in order:                                         # one symbol's 1m bars in memory at a time
        b = load_symbol(cfg.hist_root, s, w0, c1)
        if b.first_minute < 0:
            continue
        b.listing_time = listing.get(s)                     # PIT listing date, not first minute in the window
        fund[s] = load_funding(cfg.hist_root, s)
        if slow:
            oi = load_open_interest(cfg.hist_root, s, w0, c1) if cfg.arm_kind == "pos" else None
            feats[s] = compute_slow_features(b, fund[s], oi)
            continue
        sf = compute_symbol_features(b, fund[s])
        if s == btc:
            btc_sf = sf
        pair = {btc: btc_sf, s: sf} if btc_sf is not None else {s: sf}
        add_market_context(pair)
        feats[s] = sf
    if slow:
        add_btc_relative(feats)
    for sf in feats.values():                               # trim warm-up ticks
        sf.f = {k: v[k0:] for k, v in sf.f.items()}
        sf.start, sf.n_ticks = c0, sf.n_ticks - k0
    return feats, fund


def _pass_99(groups: dict[str, Any], labelled: list[dict[str, Any]], suffix: str) -> list[str]:
    fams = {p["family"] for p in labelled}
    out = []
    for k, v in groups.items():
        base, _, tag = k.partition("@")
        if (base.split("_")[0] in fams and base.count("_") == 1 and ("@" + tag if tag else "") == suffix
                and v.get("n", 0) >= 30 and v["net_ci99_lo"] > 0):
            out.append(k)
    return sorted(out)


def run_a0(cfg: A0Config, out_dir: Path) -> dict[str, Any]:
    t0 = time.time()
    start, end = _ms(cfg.start), _ms(cfg.end)
    listing = pit_first_listing(cfg.hist_root)
    props: list[dict[str, Any]] = []
    loaded: set[str] = set()
    ever: set[str] = set()
    chunks = 0
    for c0 in range(start, end, cfg.chunk_days * DAY):
        c1 = min(end, c0 + cfg.chunk_days * DAY)
        if cfg.per_day is not None:
            days = [str(datetime.fromtimestamp(t / 1000, timezone.utc).date()) for t in range(c0, c1, DAY)]
            syms = sorted({"BTCUSDT"} | {s for d in days for s in cfg.per_day.get(d, [])})
        else:
            syms = list(cfg.symbols)
        feats, fund = _chunk_features(cfg, syms, c0, c1, listing)
        if not feats:
            continue
        chunks += 1
        loaded.update(feats)
        ever.update(_universe_stats(feats, cfg.tradable_top)["ever"])
        cp = (scan_slow(feats, cfg.slow, cfg.tradable_top) if cfg.arm_kind != "fast"
              else scan(feats, cfg.scanner, tradable_top=cfg.tradable_top))
        del feats
        by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for p in cp:
            by_sym[p["symbol"]].append(p)
        for s, ps in by_sym.items():                        # labels: chunk bars + 1 day for open horizons
            b = load_symbol(cfg.hist_root, s, c0, min(end, c1 + 3 * DAY))   # covers horizons up to 48 h
            label_all(ps, {s: b}, {s: fund.get(s)}, cfg.scanner.cost if cfg.arm_kind == "fast" else cfg.slow.cost)
        props.extend(cp)
        log.info("a0_chunk_done", start=c0, symbols=len(syms), proposals=len(cp))
    universe = {"mode": "pit_daily", "top": cfg.tradable_top, "pool": len(loaded), "ever_tradable": len(ever),
                "never_tradable": sorted(loaded - ever), "chunks": chunks, "chunk_days": cfg.chunk_days,
                "warmup_days": cfg.warmup_days, "pool_file": cfg.per_day is not None,
                "note": "tradable at tick = top-N by trailing 24 h quote volume at the day's first tick, ranked "
                        "within the loaded pool; the pool must contain every day's top-N (universe-pool)"}
    labelled = [p for p in props if p.get("exit_type") in ("TP", "SL", "TIME")]
    sel = [p for p in labelled if p.get("selected")]
    groups: dict[str, Any] = {"all": group_stats(labelled, cfg), "selected": group_stats(sel, cfg)}
    for fam in sorted({p["family"] for p in labelled}):
        groups[fam] = group_stats([p for p in labelled if p["family"] == fam], cfg)
        for side, nm in ((1, "long"), (-1, "short")):
            groups[f"{fam}_{nm}"] = group_stats([p for p in labelled if p["family"] == fam and p["side"] == side], cfg)
    for tr, tn in ((1, "up"), (0, "flat"), (-1, "down")):
        groups[f"btc_trend_{tn}"] = group_stats([p for p in labelled if p.get("btc_trend") == tr], cfg)
    if cfg.arm_kind != "fast":                              # reg.v1: side must agree with the BTC 1h trend
        for fam in sorted({p["family"] for p in labelled}):
            for side, nm in ((1, "long"), (-1, "short")):
                groups[f"{fam}_{nm}@reg"] = group_stats([p for p in labelled if p["family"] == fam
                                                         and p["side"] == side and p.get("btc_trend") == side], cfg)
    port = portfolio_sim(sel, cfg.max_positions)
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:
        sha = ""
    summary = {
        "arm": {"slow": "A0-slow", "pos": "A0-pos"}.get(cfg.arm_kind, "A0"), "git_sha": sha,
        "feature_schema": {"slow": SLOW_SCHEMA, "pos": POS_SCHEMA}.get(cfg.arm_kind, FEATURE_SCHEMA),
        "label_version": LABEL_VERSION,
        "period": [str(cfg.start), str(cfg.end)], "symbols_loaded": len(loaded), "universe": universe,
        "proposals": len(props), "labelled": len(labelled),
        "data_gaps": sum(1 for p in props if p.get("exit_type") == "data_gap"),
        "groups": groups, "portfolio": port,
        "scanner_config": cfg.scanner.to_dict() if cfg.arm_kind == "fast" else cfg.slow.to_dict(),
        "runtime_s": round(time.time() - t0, 1),
    }
    g = groups["all"]
    summary["verdict"] = {
        "gross_edge": g.get("n", 0) > 0 and g["gross_ci95"][0] > 0,
        "net_edge": g.get("n", 0) > 0 and g["net_ci95"][0] > 0,
        "note": f"decision-level, {_block_days(cfg)}-day block bootstrap 95 % CI of the mean; "
                "'edge' = CI lower bound > 0",
        "ci_block_days": _block_days(cfg),
        # pre-registered per family x side (slow.v1, pos.v1): net 99 % CI lower bound > 0 with n >= 30
        "pass_99": _pass_99(groups, labelled, ""),
        "reg_pass_99": _pass_99(groups, labelled, "@reg"),    # reg.v1 (decided on the 2024 holdout only)
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    if props:
        cols = sorted({k for p in props for k in p})
        pq.write_table(pa.Table.from_pylist([{k: p.get(k) for k in cols} for p in props]),
                       out_dir / "proposals.parquet")
    (out_dir / "summary.json").write_bytes(orjson.dumps(summary, option=orjson.OPT_INDENT_2, default=str))
    (out_dir / "summary.md").write_text(to_markdown(summary), encoding="utf-8")
    return summary


def to_markdown(s: dict[str, Any]) -> str:
    v = s["verdict"]
    L = [f"# Arm A0 replay {s['period'][0]} → {s['period'][1]}", "",
         f"symbols {s['symbols_loaded']} · PIT universe top {s['universe']['top']} daily "
         f"({s['universe']['ever_tradable']} ever tradable) · proposals {s['proposals']} · "
         f"labelled {s['labelled']} · data gaps {s['data_gaps']} · runtime {s['runtime_s']} s", "",
         f"**Verdict:** gross edge {'YES' if v['gross_edge'] else 'NO'} · net edge {'YES' if v['net_edge'] else 'NO'} "
         f"({v['note']}) · family x side passing net 99 % lower > 0: {v.get('pass_99') or 'none'}"
         + (f" · BTC-trend gated (@reg): {v.get('reg_pass_99') or 'none'}" if "reg_pass_99" in v else ""), "",
         "| group | n | days | TP rate | mean gross R [95% CI] | mean net R [95% CI] | net 99% lo | median cost R | "
         "win (net) | MAE / MFE R | exits |", "|" + "---|" * 11]
    for k, g in s["groups"].items():
        if not g.get("n"):
            L.append(f"| {k} | 0 | | | | | | | | | |")
            continue
        L.append(f"| {k} | {g['n']} | {g['days']} | {g['tp_rate']} | {g['mean_gross_r']} {g['gross_ci95']} | "
                 f"{g['mean_net_r']} {g['net_ci95']} | {g['net_ci99_lo']} | {g['median_cost_r']} | "
                 f"{g['win_rate_net']} | "
                 f"{g['mean_mae_r']} / {g['mean_mfe_r']} | {g['exit_types']} |")
    p = s["portfolio"]
    L += ["", "## Portfolio (selected, max positions, 1 R per trade)", "",
          " · ".join(f"{k} {v}" for k, v in p.items())]
    return "\n".join(L) + "\n"
