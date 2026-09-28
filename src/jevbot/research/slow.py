"""Slow setup families ``slow.v1`` (hourly decisions, 24-48 h holds), pre-registered 2026-09-28.

Why: A0 on the 5 m BRK/PB families showed no gross edge and a cost of ~0.15 R, dominated by
taker fees against ~1-1.5 % stops. Hourly signals with 3 x ATR(1h) stops put the round trip at
~0.03-0.07 R. The families and every parameter below were fixed *before* looking at the
development window (2025-03-01 .. 2026-03-30); they are not tuned on it. Evaluation rule:
docs/results + reports/remote/20260928T-slow-families.md.

* TSM  - time-series momentum: fresh close beyond the 7 d (168 x 1h) Donchian channel in the
         direction of the 1h EMA50/EMA200 trend; TP 3 R, 48 h.
* XSM  - cross-sectional momentum: once a day (00:00 UTC tick) long the 3 strongest and short the
         3 weakest tradable symbols by 7 d return relative to BTC; time exit 24 h (TP 10 R).
* FUND - crowding reversal: last settled funding >= +5 bps / 8 h with a 24 h run-up >= 6 ATR(1h)
         -> short; mirror -> long; TP 2 R, 24 h.

Decision tick j = close of 1h bar j (wall time start + (j + 1) h); only completed bars are used.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from jevbot.research.data import DAY, MIN, SymbolBars
from jevbot.research.features import SymbolFeatures, ema, resample, rolling, wilder_atr
from jevbot.research.scanner import CostModel, _xs_rank
from jevbot.research.universe import daily_universe_mask

SLOW_SCHEMA = "f.slow.v1"
FAMILY_VERSION = {"TSM": "tsm.v1", "XSM": "xsm.v1", "FUND": "fund.v1"}
H = 60                                                    # minutes per tick
SNAPSHOT = ("atr_pct", "ret_24h", "ret_7d", "rel_ret_7d", "ema_trend", "funding_bps_8h", "run_24h_atr",
            "qv_24h", "listing_age_d")


@dataclass(frozen=True)
class SlowConfig:
    min_qv_24h: float = 20e6
    min_listing_age_d: float = 30.0
    atr_pct_min: float = 0.002
    atr_pct_max: float = 0.05
    stop_atr: float = 3.0
    max_cost_r: float = 0.10
    tsm_lookback_h: int = 168
    tsm_r_tp: float = 3.0
    tsm_horizon_min: int = 2880
    tsm_cooldown_h: int = 24
    xsm_lookback_h: int = 168
    xsm_k: int = 3
    xsm_r_tp: float = 10.0
    xsm_horizon_min: int = 1440
    fund_bps_8h: float = 5.0
    fund_run_atr: float = 6.0
    fund_r_tp: float = 2.0
    fund_horizon_min: int = 1440
    fund_cooldown_h: int = 24
    families: tuple[str, ...] = ("TSM", "XSM", "FUND")
    cost: CostModel = field(default_factory=CostModel)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_slow_features(b: SymbolBars, funding: tuple[np.ndarray, np.ndarray] | None = None) -> SymbolFeatures:
    first_h = b.first_minute / H if b.first_minute >= 0 else None
    return slow_features_from_hourly(b.symbol, b.start, resample(b, H), funding, b.listing_time, first_h)


def slow_features_from_hourly(symbol: str, start: int, h1: dict[str, np.ndarray],
                              funding: tuple[np.ndarray, np.ndarray] | None = None,
                              listing_time: int | None = None, first_hour: float | None = None) -> SymbolFeatures:
    """Hourly bars (open/high/low/close/qv, NaN = missing) on the grid ``start + k h`` -> slow.v1 features.
    Shared by the historical replay (resampled 1m) and the live paper engine (REST 1h klines)."""
    J = len(h1["close"])
    j = np.arange(J)
    c = h1["close"]
    lc = np.log(c)

    def lag(x: np.ndarray, k: int) -> np.ndarray:
        return np.concatenate([np.full(k, np.nan), x[:-k]]) if k < len(x) else np.full(len(x), np.nan)

    atr = wilder_atr(h1["high"], h1["low"], c, 14)
    with np.errstate(invalid="ignore", divide="ignore"):
        atr_pct = atr / c
        e50, e200 = ema(c, 50), ema(c, 200)
        f = {
            "close": c, "atr_1h": atr, "atr_pct": atr_pct,
            "don_hi": lag(rolling(h1["high"], 168, "max"), 1), "don_lo": lag(rolling(h1["low"], 168, "min"), 1),
            "prev_close": lag(c, 1),
            "ema_trend": np.sign(e50 - e200),
            "ret_24h": lc - lag(lc, 24), "ret_7d": lc - lag(lc, 168),
            "run_24h_atr": (lc - lag(lc, 24)) / atr_pct,
            "qv_24h": rolling(h1["qv"], 24, "sum"),
        }
    t = start + (j + 1) * H * MIN
    f["listing_age_d"] = ((t - listing_time) / DAY if listing_time is not None
                          else np.full(J, np.nan) if first_hour is None else (j + 1 - first_hour) / 24.0)
    f["funding_bps_8h"] = np.full(J, np.nan)
    if funding is not None and len(funding[0]):
        ft, fr = funding
        idx = np.searchsorted(ft, t, side="left") - 1                  # settled strictly before the tick
        ok = idx >= 0
        rate = np.where(ok, fr[np.maximum(idx, 0)] * 1e4, np.nan)
        # interval from the previous settlement (1/2/4/8 h symbols exist); normalised to 8 h
        prev = np.where(idx >= 1, ft[np.maximum(idx - 1, 0)], ft[np.maximum(idx, 0)] - 8 * 3_600_000)
        hrs = np.clip((ft[np.maximum(idx, 0)] - prev) / 3_600_000, 1.0, 8.0)
        f["funding_bps_8h"] = rate * 8.0 / hrs
    return SymbolFeatures(symbol, start, J, f, tick_min=H)


def add_btc_relative(feats: dict[str, SymbolFeatures], btc: str = "BTCUSDT") -> None:
    ref = feats[btc].f["ret_7d"] if btc in feats else None
    for sf in feats.values():
        sf.f["rel_ret_7d"] = sf.f["ret_7d"] - ref[:sf.n_ticks] if ref is not None else np.full(sf.n_ticks, np.nan)


def scan_slow(feats: dict[str, SymbolFeatures], cfg: SlowConfig, tradable_top: int) -> list[dict[str, Any]]:
    symbols = list(feats)
    J = min(sf.n_ticks for sf in feats.values())
    F = {k: np.stack([feats[s].f[k][:J] for s in symbols], axis=1) for k in next(iter(feats.values())).f}
    sf0 = next(iter(feats.values()))
    tt = np.asarray(sf0.tick_time(np.arange(J)))
    vol_rank = _xs_rank(F["qv_24h"])
    close, atr, atr_pct = F["close"], F["atr_1h"], F["atr_pct"]
    with np.errstate(invalid="ignore"):
        elig = (daily_universe_mask(vol_rank, tt, tradable_top) & (F["qv_24h"] >= cfg.min_qv_24h)
                & (F["listing_age_d"] >= cfg.min_listing_age_d) & (atr_pct >= cfg.atr_pct_min)
                & (atr_pct <= cfg.atr_pct_max) & np.isfinite(close) & np.isfinite(atr))
        cands: list[tuple[int, int, str, int, float, int, int]] = []    # j, s, fam, side, r_tp, horizon, cooldown
        if "TSM" in cfg.families:
            for side in (1, -1):
                if side > 0:
                    trig = (close > F["don_hi"]) & (F["prev_close"] <= F["don_hi"]) & (F["ema_trend"] > 0)
                else:
                    trig = (close < F["don_lo"]) & (F["prev_close"] >= F["don_lo"]) & (F["ema_trend"] < 0)
                for j, s in zip(*np.nonzero(elig & trig)):
                    cands.append((int(j), int(s), "TSM", side, cfg.tsm_r_tp, cfg.tsm_horizon_min, cfg.tsm_cooldown_h))
        if "FUND" in cfg.families:
            for side in (-1, 1):                                          # short crowded longs, long crowded shorts
                fb, run = F["funding_bps_8h"], F["run_24h_atr"]
                trig = ((fb >= cfg.fund_bps_8h) & (run >= cfg.fund_run_atr) if side < 0
                        else (fb <= -cfg.fund_bps_8h) & (run <= -cfg.fund_run_atr))
                for j, s in zip(*np.nonzero(elig & trig)):
                    cands.append((int(j), int(s), "FUND", side, cfg.fund_r_tp, cfg.fund_horizon_min,
                                  cfg.fund_cooldown_h))
        if "XSM" in cfg.families:
            daily = np.nonzero((tt % DAY) == 0)[0]                        # the 00:00 UTC tick
            for j in daily:
                ok = elig[j] & np.isfinite(F["rel_ret_7d"][j])
                idx = np.nonzero(ok)[0]
                if len(idx) < 2 * cfg.xsm_k:
                    continue
                order = idx[np.argsort(F["rel_ret_7d"][j, idx], kind="stable")]
                for s in order[-cfg.xsm_k:]:
                    cands.append((int(j), int(s), "XSM", 1, cfg.xsm_r_tp, cfg.xsm_horizon_min, 0))
                for s in order[:cfg.xsm_k]:
                    cands.append((int(j), int(s), "XSM", -1, cfg.xsm_r_tp, cfg.xsm_horizon_min, 0))
    cands.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    spread, slip = cfg.cost.spread_bps(vol_rank), cfg.cost.slip_bps(vol_rank)
    last: dict[tuple[int, str], int] = {}
    out: list[dict[str, Any]] = []
    for j, s, fam, side, r_tp, horizon, cooldown in cands:
        key = (s, fam)
        if cooldown and key in last and j - last[key] < cooldown:
            continue
        entry, a = float(close[j, s]), float(atr[j, s])
        dist = cfg.stop_atr * a
        stop_bps = dist / entry * 1e4
        sp, sl = float(spread[j, s]), float(slip[j, s])
        fund = float(F["funding_bps_8h"][j, s]) if np.isfinite(F["funding_bps_8h"][j, s]) else 0.0
        cost_bps = 2 * cfg.cost.fee_taker_bps + sp + 2 * sl + max(0.0, side * fund) * horizon / 480.0
        cost_r = cost_bps / stop_bps
        if cost_r > cfg.max_cost_r:
            continue
        last[key] = j
        p = {"tick": j, "t_decision": int(tt[j]), "symbol": symbols[s], "family": fam,
             "family_version": FAMILY_VERSION[fam], "side": side, "entry_ref": entry,
             "stop_price": entry - side * dist, "tp_price": entry + side * r_tp * dist, "stop_dist": dist,
             "stop_dist_bps": stop_bps, "r_tp": r_tp, "horizon_min": horizon, "cost_rt_bps": cost_bps,
             "cost_r": cost_r, "spread_bps": sp, "slip_bps": sl, "vol_rank": int(vol_rank[j, s]),
             "selected": True, "rank": 1, "score": float("nan"),
             "btc_trend": float("nan")}
        for k in SNAPSHOT:
            p[k] = float(F[k][j, s]) if k in F else float("nan")
        out.append(p)
    return out
