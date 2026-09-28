"""Funding carry ``carry.v1`` (pre-registered reports/remote/20260929T-carry-prereg.md).

Long spot + short perp, equal notional: price-neutral; income = funding received by the short perp.
Spot is not downloaded: the perp premium index (``premiumIndexKlines`` 1h, (perp - spot) / spot) gives
the basis, and long spot + short perp returns ~ -d(premium). Daily decisions at 00:00 UTC from data
known then. Costs per entry and per exit (pair): spot taker 10 + perp taker 5 + 3 bps slippage per leg.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from jevbot.hist.catalog import list_files
from jevbot.research.a0 import block_bootstrap_ci
from jevbot.research.data import DAY, load_funding

HOUR = 3_600_000


@dataclass(frozen=True)
class CarryConfig:
    enter_apr: float = 0.15
    exit_apr: float = 0.05
    lookback_days: int = 7
    max_positions: int = 10
    perp_leverage: float = 3.0
    cost_bps: float = 21.0                 # per entry or exit of a pair: (10 + 3) spot + (5 + 3) perp
    rebalance_up: float = 0.20             # perp +20 % since the reference -> margin top-up
    rebalance_bps: float = 26.0            # 2 x (10 + 3): sell spot / move collateral
    min_listing_age_d: float = 30.0
    split: str = "2025-05-15"
    bootstrap: int = 2000
    seed: int = 7


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def spot_map(perps: list[str], spot_usdt: set[str]) -> dict[str, str]:
    """Perp -> spot pair: same name, or without a 1000 / 10000 / 1000000 contract multiplier prefix."""
    out = {}
    for p in perps:
        if p in spot_usdt:
            out[p] = p
            continue
        for k in ("1000000", "10000", "1000"):
            if p.startswith(k) and p[len(k):] in spot_usdt:
                out[p] = p[len(k):]
                break
    return out


def _series(files: list[Path], tcol: str, vcol: str, shift: int = 0) -> tuple[np.ndarray, np.ndarray]:
    ts, vs = [], []
    for f in files:
        t = pq.read_table(f, columns=[tcol, vcol])
        ts.append(t.column(tcol).to_numpy().astype(np.int64) + shift)
        vs.append(t.column(vcol).to_numpy(zero_copy_only=False).astype(np.float64))
    if not ts:
        return np.array([], dtype=np.int64), np.array([])
    t, v = np.concatenate(ts), np.concatenate(vs)
    o = np.argsort(t, kind="stable")
    t, v = t[o], v[o]
    keep = np.concatenate([[True], np.diff(t) > 0])
    return t[keep], v[keep]


def load_premium(hist: Path, symbol: str) -> tuple[np.ndarray, np.ndarray]:
    """(hour close time, premium index close)."""
    return _series(list_files(Path(hist) / "premiumIndexKlines" / "1h", symbols={symbol}), "open_time", "close", HOUR)


def load_daily_close(hist: Path, symbol: str) -> tuple[np.ndarray, np.ndarray]:
    """(day close time, perp close)."""
    return _series(list_files(Path(hist) / "klines" / "1d", symbols={symbol}), "open_time", "close", DAY)


def _at(ts: np.ndarray, vs: np.ndarray, t: int) -> float:
    i = int(np.searchsorted(ts, t, side="right")) - 1
    return float(vs[i]) if i >= 0 and t - ts[i] <= 6 * HOUR else float("nan")


@dataclass
class _Pos:
    symbol: str
    notional: float                        # fraction of equity at entry
    t_prev: int
    prem_prev: float
    price_ref: float
    funding: float = 0.0
    basis: float = 0.0
    costs: float = 0.0
    days: int = 0


@dataclass
class _Data:
    funding: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    premium: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    close: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)


def run_carry(hist: Path, per_day: dict[str, list[str]], spot_usdt: set[str], listing: dict[str, int],
              start: date, end: date, cfg: CarryConfig, fixed: list[str] | None = None) -> dict[str, Any]:
    """Daily simulation. ``fixed``: hold these symbols the whole time (carry.base), no signal."""
    syms = sorted({s for v in per_day.values() for s in v} | set(fixed or []))
    smap = spot_map(syms, spot_usdt)
    data = _Data()

    def get(s: str) -> None:
        if s not in data.funding:
            data.funding[s] = load_funding(hist, s)
            data.premium[s] = load_premium(hist, s)
            data.close[s] = load_daily_close(hist, s)

    slot_capital = 1.0 / (len(fixed) if fixed else cfg.max_positions)
    notional = slot_capital / (1.0 + 1.0 / cfg.perp_leverage)
    held: dict[str, _Pos] = {}
    equity, daily, trades, rebal, missing_prem = 1.0, [], [], 0, 0
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    for di, d in enumerate(days):
        t0 = _ms(d)
        pnl = 0.0
        # (1) accrue held positions over (t_prev, t0]
        for p in held.values():
            ft, fr = data.funding[p.symbol]
            sel = (ft > p.t_prev) & (ft <= t0)
            f = float(fr[sel].sum()) * p.notional if len(ft) else 0.0
            pr = _at(*data.premium[p.symbol], t0)
            b = 0.0
            if math.isfinite(pr) and math.isfinite(p.prem_prev):
                b = -(pr - p.prem_prev) * p.notional
            else:
                missing_prem += 1
            c = 0.0
            px = _at(*data.close[p.symbol], t0)
            if math.isfinite(px) and math.isfinite(p.price_ref) and px / p.price_ref - 1 > cfg.rebalance_up:
                c = cfg.rebalance_bps / 1e4 * p.notional
                p.price_ref = px
                rebal += 1
            p.funding += f
            p.basis += b
            p.costs += c
            p.days += 1
            pnl += f + b - c
            p.t_prev = t0
            p.prem_prev = pr if math.isfinite(pr) else p.prem_prev
        # (2) signal
        key = d.isoformat()
        last = di == len(days) - 1
        if fixed:
            cand = {} if last else {s: float("inf") for s in fixed}
        else:
            cand = {}
            for s in per_day.get(key, []):
                if s not in smap or (listing.get(s) is not None and (t0 - listing[s]) / DAY < cfg.min_listing_age_d):
                    continue
                get(s)
                ft, fr = data.funding[s]
                sel = (ft > t0 - cfg.lookback_days * DAY) & (ft <= t0)
                if not sel.any() or ft[sel].max() < t0 - DAY:
                    continue
                cand[s] = float(fr[sel].sum()) * 365.0 / cfg.lookback_days
        # (3) exits
        for s in list(held):
            apr = cand.get(s)
            if last or apr is None or apr < cfg.exit_apr:
                p = held.pop(s)
                c = cfg.cost_bps / 1e4 * p.notional
                p.costs += c
                pnl -= c
                trades.append({"symbol": s, "days": p.days, "funding": p.funding, "basis": p.basis, "costs": p.costs,
                               "net": p.funding + p.basis - p.costs})
        # (4) entries, highest funding first
        if not last:
            for s, apr in sorted(cand.items(), key=lambda kv: -kv[1]):
                if len(held) >= (len(fixed) if fixed else cfg.max_positions):
                    break
                if s in held or apr < cfg.enter_apr:
                    continue
                get(s)
                pr, px = _at(*data.premium[s], t0), _at(*data.close[s], t0)
                c = cfg.cost_bps / 1e4 * notional
                held[s] = _Pos(s, notional, t0, pr, px, costs=c)
                pnl -= c
        r = pnl                             # notionals are fractions of equity at entry (~ daily rebalanced)
        equity *= 1.0 + r
        daily.append({"day": key, "t": t0, "ret": r, "equity": equity, "positions": len(held)})
    return _summary(daily, trades, rebal, missing_prem, cfg, smap)


def _summary(daily: list[dict[str, Any]], trades: list[dict[str, Any]], rebal: int, missing_prem: int,
             cfg: CarryConfig, smap: dict[str, str]) -> dict[str, Any]:
    r = np.array([x["ret"] for x in daily])
    t = np.array([x["t"] for x in daily], dtype=np.int64)
    eq = np.array([x["equity"] for x in daily])
    split = _ms(date.fromisoformat(cfg.split))

    def stats(mask: np.ndarray) -> dict[str, Any]:
        v, tt = r[mask], t[mask]
        if len(v) < 14:
            return {"days": int(len(v))}
        blocks = tt // DAY // 7
        lo, hi = block_bootstrap_ci(v, blocks, cfg.bootstrap, cfg.seed)
        lo99, _ = block_bootstrap_ci(v, blocks, cfg.bootstrap, cfg.seed, level=0.99)
        e = np.cumprod(1 + v)
        dd = float(np.max(1 - e / np.maximum.accumulate(e)))
        return {"days": int(len(v)), "apr": round(float(v.mean() * 365), 4),
                "apr_ci95": [round(lo * 365, 4), round(hi * 365, 4)], "apr_ci99_lo": round(lo99 * 365, 4),
                "total_return": round(float(e[-1] - 1), 4), "max_dd": round(dd, 4)}

    full, h1, h2 = stats(np.ones(len(r), bool)), stats(t < split), stats(t >= split)
    tr = trades
    return {
        "full": full, "h1": h1, "h2": h2,
        "trades": len(tr), "avg_hold_days": round(float(np.mean([x["days"] for x in tr])), 1) if tr else None,
        "avg_positions": round(float(np.mean([x["positions"] for x in daily])), 2),
        "sum_funding": round(float(sum(x["funding"] for x in tr)), 4),
        "sum_basis": round(float(sum(x["basis"] for x in tr)), 4),
        "sum_costs": round(float(sum(x["costs"] for x in tr)), 4),
        "winning_trades": round(float(np.mean([x["net"] > 0 for x in tr])), 3) if tr else None,
        "rebalances": rebal, "missing_premium_days": missing_prem, "spot_mapped": len(smap),
        "pass": bool(full.get("apr_ci99_lo", -1) > 0 and h1.get("apr", -1) > 0 and h2.get("apr", -1) > 0
                     and full.get("max_dd", 1) <= 0.10),
        "daily": daily,
    }
