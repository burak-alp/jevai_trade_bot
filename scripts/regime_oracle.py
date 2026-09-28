"""Regime-knowledge ceiling for the user's thesis (descriptive; no gate, no parameter search).

Thesis: "if we know we are in a bull (bear / range) market and run the strategy that suits it, we profit".
The same labelled slow.v1 proposals (TSM / XSM / FUND, 2024-01 .. 2026-03) are filtered by a BTC regime:

* oracle  - BTC log return over the NEXT ``H`` days from the decision (known only in hindsight);
* lagged  - BTC log return over the PREVIOUS ``H`` days (what is actually known at the decision);
* noisy   - the oracle label kept with probability q, otherwise replaced by one of the two wrong labels:
            how accurate must a regime forecast be before regime switching pays?

bull: return > +thr, bear: < -thr, range otherwise. Policy "suited": bull -> long proposals, bear -> short
proposals, range -> no trade. Mean net R per trade and total R; 7-day block bootstrap 95 % CI.

Usage: python scripts/regime_oracle.py [--days 30] [--thr 0.05]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from jevbot.research.data import DAY, MIN, load_symbol

RUNS = ("data/research/slow-holdout", "data/research/slow-dev")


def btc_daily(hist: Path, start: int, end: int) -> tuple[np.ndarray, np.ndarray]:
    b = load_symbol(hist, "BTCUSDT", start, end)
    per = DAY // MIN
    n = b.n // per
    c = b.close[:n * per].reshape(n, per)
    last = np.array([row[np.flatnonzero(~np.isnan(row))[-1]] if (~np.isnan(row)).any() else np.nan for row in c])
    t_close = start + (np.arange(n) + 1) * DAY                 # close of day k known at this time
    return t_close, last


def price_at(t_close: np.ndarray, px: np.ndarray, t: np.ndarray) -> np.ndarray:
    i = np.searchsorted(t_close, t, side="right") - 1            # last daily close at or before t
    ok = (i >= 0) & (i < len(px))
    return np.where(ok, px[np.clip(i, 0, len(px) - 1)], np.nan)


def label(r: np.ndarray, thr: float) -> np.ndarray:
    return np.where(r > thr, 1, np.where(r < -thr, -1, 0))


def block_ci(v: np.ndarray, t: np.ndarray, n: int = 2000, seed: int = 7) -> tuple[float, float]:
    if len(v) < 2:
        return float("nan"), float("nan")
    b = (t // DAY) // 7
    u, inv = np.unique(b, return_inverse=True)
    s, c = np.bincount(inv, weights=v), np.bincount(inv).astype(float)
    idx = np.random.default_rng(seed).integers(0, len(u), (n, len(u)))
    m = s[idx].sum(1) / np.maximum(c[idx].sum(1), 1)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def row(name: str, keep: np.ndarray, net: np.ndarray, t: np.ndarray) -> str:
    v, tt = net[keep], t[keep]
    if not len(v):
        return f"| {name} | 0 | | | |"
    lo, hi = block_ci(v, tt)
    return f"| {name} | {len(v)} | {v.mean():+.4f} [{lo:+.4f}, {hi:+.4f}] | {v.sum():+.1f} | {(v > 0).mean():.3f} |"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--thr", type=float, default=0.05)
    ap.add_argument("--hist", default="data/hist/um")
    a = ap.parse_args()
    cols = ["t_decision", "family", "side", "net_r", "exit_type"]
    tabs = [pq.read_table(Path(r) / "proposals.parquet", columns=cols).to_pylist() for r in RUNS]
    rows = [r for t in tabs for r in t if r["exit_type"] in ("TP", "SL", "TIME")]
    t = np.array([r["t_decision"] for r in rows], dtype=np.int64)
    side = np.array([r["side"] for r in rows])
    fam = np.array([r["family"] for r in rows])
    net = np.array([r["net_r"] for r in rows], dtype=float)
    H = a.days * DAY
    t0 = int(t.min() - H - 2 * DAY) // DAY * DAY
    t1 = int(t.max() + H + 2 * DAY) // DAY * DAY
    tc, px = btc_daily(Path(a.hist), t0, t1)
    p_now = price_at(tc, px, t)
    fut = np.log(price_at(tc, px, t + H) / p_now)
    past = np.log(p_now / price_at(tc, px, t - H))
    ok = np.isfinite(fut) & np.isfinite(past)
    t, side, fam, net, fut, past = t[ok], side[ok], fam[ok], net[ok], fut[ok], past[ok]
    lo_, lp_ = label(fut, a.thr), label(past, a.thr)
    suited = lambda lab: (lab != 0) & (side == lab)                           # noqa: E731
    print(f"# Regime ceiling: slow.v1 proposals {len(net)} (2024-01..2026-03), BTC {a.days} d, thr ±{a.thr:.0%}\n")
    print(f"oracle regime shares: bull {np.mean(lo_ == 1):.2f} · range {np.mean(lo_ == 0):.2f} · bear {np.mean(lo_ == -1):.2f}"
          f" | lagged == oracle on {np.mean(lp_ == lo_):.2f} of proposals\n")
    print("| policy | n | mean net R [95 % CI, 7-day blocks] | total R | win |\n|---|---|---|---|---|")
    print(row("all proposals (no regime)", np.ones(len(net), bool), net, t))
    print(row("ORACLE suited (future BTC)", suited(lo_), net, t))
    print(row("ORACLE opposite (sanity)", (lo_ != 0) & (side == -lo_), net, t))
    print(row("LAGGED suited (past BTC, knowable)", suited(lp_), net, t))
    print("\n## by family x side under the oracle regime (mean net R, n)\n")
    print("| family x side | bull | range | bear |\n|---|---|---|---|")
    for f in ("TSM", "XSM", "FUND"):
        for s_, nm in ((1, "long"), (-1, "short")):
            cells = []
            for lab in (1, 0, -1):
                k = (fam == f) & (side == s_) & (lo_ == lab)
                cells.append(f"{net[k].mean():+.3f} ({k.sum()})" if k.any() else "—")
            print(f"| {f} {nm} | " + " | ".join(cells) + " |")
    print("\n## how accurate must the regime call be? (oracle label kept with prob. q, else a wrong label)\n")
    print("| accuracy q | trades | mean net R | total R |\n|---|---|---|---|")
    rng = np.random.default_rng(11)
    for q in (0.33, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        means, tots, ns = [], [], []
        for _ in range(200):
            keep_true = rng.random(len(lo_)) < q
            wrong = np.where(rng.random(len(lo_)) < 0.5, (lo_ + 2) % 3 - 1, (lo_ + 3) % 3 - 1)   # the two others
            lab = np.where(keep_true, lo_, wrong)
            k = suited(lab)
            means.append(net[k].mean())
            tots.append(net[k].sum())
            ns.append(k.sum())
        print(f"| {q:.2f} | {np.mean(ns):.0f} | {np.mean(means):+.4f} | {np.mean(tots):+.1f} |")


if __name__ == "__main__":
    main()
