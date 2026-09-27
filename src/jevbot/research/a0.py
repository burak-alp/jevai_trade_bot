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
from jevbot.research.data import DAY, load_funding, load_symbol
from jevbot.research.features import FEATURE_SCHEMA, add_market_context, compute_symbol_features
from jevbot.research.labels import LABEL_VERSION, label_all
from jevbot.research.scanner import ScannerConfig, scan

log = get_logger(__name__)


@dataclass
class A0Config:
    hist_root: Path
    symbols: list[str]
    start: date
    end: date                               # exclusive
    tradable_top: int = 50                  # tradable universe = top-N by median 24 h quote volume
    max_positions: int = 3
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    bootstrap: int = 2000
    seed: int = 7


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def block_bootstrap_ci(values: np.ndarray, days: np.ndarray, n: int, seed: int) -> tuple[float, float]:
    """95 % CI of the mean resampling whole days (labels overlap within a day)."""
    if len(values) < 2:
        return float("nan"), float("nan")
    uniq, inv = np.unique(days, return_inverse=True)
    sums = np.bincount(inv, weights=values)
    cnts = np.bincount(inv).astype(float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(uniq), size=(n, len(uniq)))
    means = sums[idx].sum(axis=1) / np.maximum(cnts[idx].sum(axis=1), 1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def group_stats(rows: list[dict[str, Any]], cfg: A0Config) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    net = np.array([r["net_r"] for r in rows])
    gross = np.array([r["gross_r"] for r in rows])
    days = np.array([r["t_decision"] // DAY for r in rows])
    lo, hi = block_bootstrap_ci(net, days, cfg.bootstrap, cfg.seed)
    glo, ghi = block_bootstrap_ci(gross, days, cfg.bootstrap, cfg.seed)
    exits = defaultdict(int)
    for r in rows:
        exits[r["exit_type"]] += 1
    return {
        "n": len(rows), "days": int(len(np.unique(days))),
        "tp_rate": round(float(np.mean([r["y_success"] for r in rows])), 4),
        "mean_gross_r": round(float(gross.mean()), 4), "gross_ci95": [round(glo, 4), round(ghi, 4)],
        "mean_net_r": round(float(net.mean()), 4), "net_ci95": [round(lo, 4), round(hi, 4)],
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


def run_a0(cfg: A0Config, out_dir: Path) -> dict[str, Any]:
    t0 = time.time()
    start, end = _ms(cfg.start), _ms(cfg.end)
    feats, fund, med_qv = {}, {}, {}
    for s in cfg.symbols:                                   # pass 1: features (1m bars discarded)
        b = load_symbol(cfg.hist_root, s, start, end)
        if b.first_minute < 0:
            log.warning("a0_symbol_no_data", symbol=s)
            continue
        fund[s] = load_funding(cfg.hist_root, s)
        feats[s] = compute_symbol_features(b, fund[s])
        med_qv[s] = float(np.nanmedian(feats[s].f["qv_24h"]))
    add_market_context(feats)
    ranked = sorted(med_qv, key=lambda s: -med_qv[s] if np.isfinite(med_qv[s]) else 0)
    tradable = set(ranked[:cfg.tradable_top])
    props = scan(feats, cfg.scanner, tradable=tradable)
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in props:
        by_sym[p["symbol"]].append(p)
    for s, ps in by_sym.items():                            # pass 2: labels (reload 1m bars per symbol)
        b = load_symbol(cfg.hist_root, s, start, end)
        label_all(ps, {s: b}, {s: fund.get(s)}, cfg.scanner.cost)
    labelled = [p for p in props if p.get("exit_type") in ("TP", "SL", "TIME")]
    sel = [p for p in labelled if p.get("selected")]
    groups: dict[str, Any] = {"all": group_stats(labelled, cfg), "selected": group_stats(sel, cfg)}
    for fam in ("BRK", "PB"):
        groups[fam] = group_stats([p for p in labelled if p["family"] == fam], cfg)
        for side, nm in ((1, "long"), (-1, "short")):
            groups[f"{fam}_{nm}"] = group_stats([p for p in labelled if p["family"] == fam and p["side"] == side], cfg)
    for tr, tn in ((1, "up"), (0, "flat"), (-1, "down")):
        groups[f"btc_trend_{tn}"] = group_stats([p for p in labelled if p.get("btc_trend") == tr], cfg)
    port = portfolio_sim(sel, cfg.max_positions)
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:
        sha = ""
    summary = {
        "arm": "A0", "git_sha": sha, "feature_schema": FEATURE_SCHEMA, "label_version": LABEL_VERSION,
        "period": [str(cfg.start), str(cfg.end)], "symbols_loaded": len(feats), "tradable": sorted(tradable),
        "proposals": len(props), "labelled": len(labelled),
        "data_gaps": sum(1 for p in props if p.get("exit_type") == "data_gap"),
        "groups": groups, "portfolio": port, "scanner_config": cfg.scanner.to_dict(),
        "runtime_s": round(time.time() - t0, 1),
    }
    g = groups["all"]
    summary["verdict"] = {
        "gross_edge": g.get("n", 0) > 0 and g["gross_ci95"][0] > 0,
        "net_edge": g.get("n", 0) > 0 and g["net_ci95"][0] > 0,
        "note": "decision-level, day-block bootstrap 95 % CI of the mean; 'edge' = CI lower bound > 0",
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
         f"symbols {s['symbols_loaded']} · tradable {len(s['tradable'])} · proposals {s['proposals']} · "
         f"labelled {s['labelled']} · data gaps {s['data_gaps']} · runtime {s['runtime_s']} s", "",
         f"**Verdict:** gross edge {'YES' if v['gross_edge'] else 'NO'} · net edge {'YES' if v['net_edge'] else 'NO'} "
         f"({v['note']})", "",
         "| group | n | days | TP rate | mean gross R [95% CI] | mean net R [95% CI] | median cost R | win (net) | "
         "MAE / MFE R | exits |", "|" + "---|" * 10]
    for k, g in s["groups"].items():
        if not g.get("n"):
            L.append(f"| {k} | 0 | | | | | | | | |")
            continue
        L.append(f"| {k} | {g['n']} | {g['days']} | {g['tp_rate']} | {g['mean_gross_r']} {g['gross_ci95']} | "
                 f"{g['mean_net_r']} {g['net_ci95']} | {g['median_cost_r']} | {g['win_rate_net']} | "
                 f"{g['mean_mae_r']} / {g['mean_mfe_r']} | {g['exit_types']} |")
    p = s["portfolio"]
    L += ["", "## Portfolio (selected, max positions, 1 R per trade)", "",
          " · ".join(f"{k} {v}" for k, v in p.items())]
    return "\n".join(L) + "\n"
