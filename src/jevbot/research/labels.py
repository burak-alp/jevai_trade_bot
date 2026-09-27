"""Triple-barrier labels for historical proposals (spec §6), pessimistic by construction.

* entry: open of the first minute after the decision tick (+ half spread + slippage);
* stop: checked on **mark price** (live stops use workingType=MARK_PRICE), TP on last price;
* both barriers inside the same minute -> stop first;
* a minute opening beyond the stop fills at that open (gap), plus ``stop_slip_mult`` x slippage;
* time exit at the close of the last minute of the horizon;
* costs: taker fee both sides, half spread + slippage on both fills, funding settled in (fill, exit].
Proposals whose horizon contains missing minutes are labelled ``exit_type = "data_gap"``.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from jevbot.research.data import MIN, SymbolBars
from jevbot.research.scanner import CostModel

LABEL_VERSION = "lbl.v1-hist"


def label_one(p: dict[str, Any], b: SymbolBars, funding: tuple[np.ndarray, np.ndarray], cost: CostModel) -> dict[str, Any]:
    s = p["side"]
    m0 = (p["t_decision"] - b.start) // MIN
    H = int(p["horizon_min"])
    out: dict[str, Any] = {"label_version": LABEL_VERSION}
    if m0 < 0 or m0 + H > b.n:
        out["exit_type"] = "out_of_range"
        return out
    o, h, l, c = (a[m0:m0 + H] for a in (b.open, b.high, b.low, b.close))
    mh, ml = b.mark_high[m0:m0 + H], b.mark_low[m0:m0 + H]
    stop, tp, dist = p["stop_price"], p["tp_price"], p["stop_dist"]
    half = p["spread_bps"] / 2 / 1e4
    slip = p["slip_bps"] / 1e4
    fill_raw = o[0]
    if not np.isfinite(fill_raw):
        out["exit_type"] = "data_gap"
        return out
    # stop on mark price where available, otherwise last price
    sl_lo = np.where(np.isnan(ml), l, ml)
    sl_hi = np.where(np.isnan(mh), h, mh)
    sl_hit = (sl_lo <= stop) if s > 0 else (sl_hi >= stop)
    tp_hit = (h >= tp) if s > 0 else (l <= tp)
    any_hit = sl_hit | tp_hit
    k = int(np.argmax(any_hit)) if any_hit.any() else H - 1
    if np.isnan(c[:k + 1]).any() or np.isnan(h[:k + 1]).any():
        out["exit_type"] = "data_gap"
        return out
    if any_hit.any() and sl_hit[k]:                  # stop first (also when both in the same minute)
        exit_type = "SL"
        exit_raw = o[k] if s * (o[k] - stop) < 0 else stop
        exit_cost = exit_raw * (1 - s * (half + slip * cost.stop_slip_mult))
    elif any_hit.any():
        exit_type = "TP"
        exit_raw = tp
        exit_cost = exit_raw * (1 - s * (half + slip))
    else:
        exit_type = "TIME"
        exit_raw = c[k]
        exit_cost = exit_raw * (1 - s * (half + slip))
    fill_cost = fill_raw * (1 + s * (half + slip))
    t_fill = b.start + m0 * MIN
    t_exit = b.start + (m0 + k + 1) * MIN
    ft, fr = funding
    sel = (ft > t_fill) & (ft <= t_exit) if len(ft) else np.zeros(0, bool)
    funding_r = float(s * fr[sel].sum() * fill_raw / dist) if len(ft) else 0.0
    fee_r = 2 * cost.fee_taker_bps / 1e4 * fill_raw / dist
    path_lo, path_hi = np.nanmin(l[:k + 1]), np.nanmax(h[:k + 1])
    out.update(
        exit_type=exit_type, y_success=int(exit_type == "TP"), t_fill=t_fill, t_exit=t_exit,
        hold_min=k + 1, fill_price=float(fill_cost), exit_price=float(exit_cost),
        gross_r=float(s * (exit_raw - fill_raw) / dist),
        net_r=float(s * (exit_cost - fill_cost) / dist - fee_r - funding_r),
        fee_r=float(fee_r), funding_r=funding_r,
        mae_r=float(((fill_raw - path_lo) if s > 0 else (path_hi - fill_raw)) / dist),
        mfe_r=float(((path_hi - fill_raw) if s > 0 else (fill_raw - path_lo)) / dist),
    )
    return out


def label_all(props: list[dict[str, Any]], bars: dict[str, SymbolBars],
              funding: dict[str, tuple[np.ndarray, np.ndarray]], cost: CostModel) -> None:
    """Adds label fields to every proposal in place."""
    empty = (np.array([], dtype=np.int64), np.array([], dtype=float))
    for p in props:
        p.update(label_one(p, bars[p["symbol"]], funding.get(p["symbol"], empty), cost))
