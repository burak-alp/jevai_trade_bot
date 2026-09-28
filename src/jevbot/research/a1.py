"""Arm A1: ML meta-labeler on the deterministic proposals (spec §9 arm A1), pre-registered
reports/remote/20260929T-regime-ceiling-a1-prereg.md.

L2 logistic regression (numpy, standardised features, lambda fixed at 1) predicts ``net_r > 0`` from
features known at the decision. Monthly walk-forward: a test month is scored by a model trained on every
proposal whose exit is at least ``embargo_days`` before the month starts (purged: no training label
overlaps the test period). Selection: p >= the median p of the training set (top half). Reported against
all proposals (A0) and a random half (R), with 7-day block bootstrap CIs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from jevbot.research.a0 import block_bootstrap_ci
from jevbot.research.data import DAY
from jevbot.judgment.shadow import auc

FAMILIES_SIDES = [("TSM", 1), ("TSM", -1), ("XSM", 1), ("XSM", -1), ("FUND", 1), ("FUND", -1),
                  ("CROWD", 1), ("CROWD", -1), ("FLUSH", 1), ("FLUSH", -1)]
ALIGNED = ("ret_24h_atr", "ret_7d_atr", "rel_ret_7d", "ema_trend", "run_24h_atr", "funding_bps_8h")
NEUTRAL = ("atr_pct", "log_qv_24h", "vol_rank", "log_listing_age_d", "cost_r", "stop_dist_bps")


@dataclass(frozen=True)
class A1Config:
    lam: float = 1.0
    embargo_days: int = 2
    min_train_months: int = 6
    first_test: str = "2024-07-01"
    split: str = "2025-05-01"                # halves of the walk-forward test span
    bootstrap: int = 2000
    seed: int = 7


def load_proposals(runs: list[Path]) -> list[dict[str, Any]]:
    rows = []
    for r in runs:
        for p in pq.read_table(Path(r) / "proposals.parquet").to_pylist():
            if p.get("exit_type") in ("TP", "SL", "TIME"):
                rows.append(p)
    rows.sort(key=lambda p: (p["t_decision"], p["symbol"], p["family"], p["side"]))
    return rows


def _f(p: dict[str, Any], k: str) -> float:
    v = p.get(k)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return v if math.isfinite(v) else float("nan")


def features(rows: list[dict[str, Any]]) -> tuple[np.ndarray, list[str]]:
    """Decision-time features; direction-dependent ones multiplied by the side. NaN -> 0 after scaling."""
    names = [f"side*{k}" for k in ALIGNED] + list(NEUTRAL) + [f"{f}_{'long' if s > 0 else 'short'}"
                                                              for f, s in FAMILIES_SIDES]
    X = np.zeros((len(rows), len(names)))
    for i, p in enumerate(rows):
        s, atr = p["side"], _f(p, "atr_pct")
        raw = {"ret_24h_atr": _f(p, "ret_24h") / atr, "ret_7d_atr": _f(p, "ret_7d") / atr,
               "rel_ret_7d": _f(p, "rel_ret_7d"), "ema_trend": _f(p, "ema_trend"),
               "run_24h_atr": _f(p, "run_24h_atr"), "funding_bps_8h": _f(p, "funding_bps_8h")}
        row = [s * raw[k] for k in ALIGNED]
        row += [atr, math.log(max(_f(p, "qv_24h"), 1.0)) if math.isfinite(_f(p, "qv_24h")) else float("nan"),
                _f(p, "vol_rank"),
                math.log1p(max(_f(p, "listing_age_d"), 0.0)) if math.isfinite(_f(p, "listing_age_d")) else float("nan"),
                _f(p, "cost_r"), _f(p, "stop_dist_bps")]
        row += [1.0 if (p["family"], p["side"]) == fs else 0.0 for fs in FAMILIES_SIDES]
        X[i] = row
    return X, names


def fit_logistic(X: np.ndarray, y: np.ndarray, lam: float, iters: int = 50) -> tuple[np.ndarray, np.ndarray,
                                                                                         np.ndarray]:
    """Newton-IRLS L2 logistic regression on standardised X (intercept unpenalised).
    Returns (weights incl. intercept, feature means, feature scales) — NaN features become the mean."""
    mu = np.nanmean(X, axis=0)
    mu = np.where(np.isfinite(mu), mu, 0.0)
    sd = np.nanstd(X, axis=0)
    sd = np.where(np.isfinite(sd) & (sd > 1e-12), sd, 1.0)
    Z = np.nan_to_num((X - mu) / sd)
    A = np.hstack([np.ones((len(Z), 1)), Z])
    w = np.zeros(A.shape[1])
    pen = np.full(A.shape[1], lam)
    pen[0] = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(A @ w, -30, 30)))
        g = A.T @ (p - y) + pen * w
        H = (A * (p * (1 - p))[:, None]).T @ A + np.diag(pen) + 1e-9 * np.eye(A.shape[1])
        step = np.linalg.solve(H, g)
        w -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w, mu, sd


def predict(w: np.ndarray, mu: np.ndarray, sd: np.ndarray, X: np.ndarray) -> np.ndarray:
    Z = np.nan_to_num((X - mu) / sd)
    return 1.0 / (1.0 + np.exp(-np.clip(w[0] + Z @ w[1:], -30, 30)))


def _month_starts(t0: int, t1: int) -> list[int]:
    d = datetime.fromtimestamp(t0 / 1000, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    out = []
    while True:
        ms = int(d.timestamp() * 1000)
        if ms > t1:
            return out
        out.append(ms)
        d = d.replace(year=d.year + (d.month == 12), month=d.month % 12 + 1)


def walk_forward(rows: list[dict[str, Any]], cfg: A1Config) -> list[dict[str, Any]]:
    """Scores every proposal decided on/after ``first_test`` with a purged, earlier-only model."""
    X, _ = features(rows)
    y = np.array([1.0 if p["net_r"] > 0 else 0.0 for p in rows])
    td = np.array([p["t_decision"] for p in rows], dtype=np.int64)
    te = np.array([p["t_exit"] for p in rows], dtype=np.int64)
    first = int(datetime.fromisoformat(cfg.first_test).replace(tzinfo=timezone.utc).timestamp() * 1000)
    months = _month_starts(first, int(td.max()))
    out: list[dict[str, Any]] = []
    for i, m0 in enumerate(months):
        m1 = months[i + 1] if i + 1 < len(months) else int(td.max()) + 1
        test = np.flatnonzero((td >= m0) & (td < m1))
        train = np.flatnonzero(te < m0 - cfg.embargo_days * DAY)
        if not len(test) or len(train) < 50 or len(np.unique(y[train])) < 2:
            continue
        w, mu, sd = fit_logistic(X[train], y[train], cfg.lam)
        thr = float(np.median(predict(w, mu, sd, X[train])))
        p = predict(w, mu, sd, X[test])
        for j, pj in zip(test, p):
            out.append({**rows[j], "p_a1": float(pj), "a1_thr": thr, "a1_select": bool(pj >= thr),
                        "fold": m0, "train_n": int(len(train)), "train_max_exit": int(te[train].max())})
    return out


def _stats(rows: list[dict[str, Any]], cfg: A1Config) -> dict[str, Any]:
    if len(rows) < 2:
        return {"n": len(rows)}
    net = np.array([r["net_r"] for r in rows])
    blocks = np.array([r["t_decision"] // DAY // 7 for r in rows])
    lo, hi = block_bootstrap_ci(net, blocks, cfg.bootstrap, cfg.seed)
    lo99, _ = block_bootstrap_ci(net, blocks, cfg.bootstrap, cfg.seed, level=0.99)
    return {"n": len(rows), "mean_net_r": round(float(net.mean()), 4), "net_ci95": [round(lo, 4), round(hi, 4)],
            "net_ci99_lo": round(lo99, 4), "sum_net_r": round(float(net.sum()), 1),
            "win": round(float((net > 0).mean()), 4)}


def a1_report(scored: list[dict[str, Any]], cfg: A1Config) -> dict[str, Any]:
    split = int(datetime.fromisoformat(cfg.split).replace(tzinfo=timezone.utc).timestamp() * 1000)
    sel = [r for r in scored if r["a1_select"]]
    rng = np.random.default_rng(cfg.seed)
    rnd = [r for r in scored if rng.random() < 0.5]
    p = np.array([r["p_a1"] for r in scored])
    win = np.array([1 if r["net_r"] > 0 else 0 for r in scored])
    tp = np.array([r["y_success"] for r in scored])
    rep: dict[str, Any] = {
        "folds": len({r["fold"] for r in scored}), "scored": len(scored),
        "auc_net_win": round(auc(p, win), 4), "auc_y_success": round(auc(p, tp), 4),
        "A0_all": _stats(scored, cfg), "A1_selected": _stats(sel, cfg), "R_random_half": _stats(rnd, cfg),
        "A1_selected_h1": _stats([r for r in sel if r["t_decision"] < split], cfg),
        "A1_selected_h2": _stats([r for r in sel if r["t_decision"] >= split], cfg),
        "A1_by_family": {}, "purge_ok": all(r["train_max_exit"] < r["fold"] for r in scored),
    }
    for f in sorted({r["family"] for r in scored}):
        for s, nm in ((1, "long"), (-1, "short")):
            g = [r for r in sel if r["family"] == f and r["side"] == s]
            if g:
                rep["A1_by_family"][f"{f}_{nm}"] = _stats(g, cfg)
    s, h1, h2 = rep["A1_selected"], rep["A1_selected_h1"], rep["A1_selected_h2"]
    rep["verdict"] = {"pass": bool(s.get("net_ci99_lo", -1) > 0 and h1.get("mean_net_r", -1) > 0
                                   and h2.get("mean_net_r", -1) > 0),
                      "rule": "A1-selected net 99 % CI lower > 0 (7-day blocks) and both halves > 0"}
    return rep


def to_markdown(rep: dict[str, Any]) -> str:
    L = [f"# Arm A1 walk-forward ({rep['folds']} monthly folds, {rep['scored']} scored proposals)", "",
         f"**Verdict:** {'PASS' if rep['verdict']['pass'] else 'NO'} — {rep['verdict']['rule']} · "
         f"AUC(net>0) {rep['auc_net_win']} · AUC(TP) {rep['auc_y_success']} · purge ok {rep['purge_ok']}", "",
         "| group | n | mean net R [95 % CI] | net 99 % lo | total R | win |", "|---|---|---|---|---|---|"]
    groups = [(k, rep[k]) for k in ("A0_all", "R_random_half", "A1_selected", "A1_selected_h1", "A1_selected_h2")]
    groups += [(f"A1 {k}", v) for k, v in rep["A1_by_family"].items()]
    for k, g in groups:
        if g.get("n", 0) < 2:
            L.append(f"| {k} | {g.get('n', 0)} | | | | |")
            continue
        L.append(f"| {k} | {g['n']} | {g['mean_net_r']} {g['net_ci95']} | {g['net_ci99_lo']} | {g['sum_net_r']} | "
                 f"{g['win']} |")
    return "\n".join(L) + "\n"
