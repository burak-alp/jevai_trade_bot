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

``pos.v1`` (pre-registered 2026-09-28, reports/remote/20260928T-slow-verdict-response.md) adds two
positioning families on Binance Vision 5 m open interest (``metrics.sum_open_interest``, contracts), same
stops / costs / universe. An OI row counts at tick t only if ``create_time + 5 min <= t`` and it is at
most 30 min older than that (else NaN -> no signal).

* CROWD - late crowd: 24 h move >= +4 ATR(1h) with 24 h OI change >= +15 % -> short; mirror
          (<= -4 ATR, OI >= +15 %) -> long; TP 2 R, 24 h, 24 h cooldown.
* FLUSH - after deleveraging: 4 h OI change <= -8 % with 4 h move <= -3 ATR(1h) -> long; mirror
          (move >= +3 ATR) -> short; TP 2 R, 24 h, 24 h cooldown.

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
POS_SCHEMA = "f.pos.v1"
FAMILY_VERSION = {"TSM": "tsm.v1", "XSM": "xsm.v1", "FUND": "fund.v1", "CROWD": "crowd.v1", "FLUSH": "flush.v1"}
POS_FAMILIES = ("CROWD", "FLUSH")
H = 60                                                    # minutes per tick
OI_LAG_MIN = 5                                            # metrics row usable once create_time + 5 min <= tick
OI_MAX_AGE_MIN = 30                                       # ... and not older than this beyond the lag
SNAPSHOT = ("atr_pct", "ret_24h", "ret_7d", "rel_ret_7d", "ema_trend", "funding_bps_8h", "run_24h_atr",
            "qv_24h", "listing_age_d", "oi_chg_24h", "oi_chg_4h", "run_4h_atr")


@dataclass(frozen=True)
class PosConfig:
    crowd_run_atr: float = 4.0
    crowd_oi_chg: float = 0.15
    crowd_r_tp: float = 2.0
    crowd_horizon_min: int = 1440
    crowd_cooldown_h: int = 24
    flush_oi_chg: float = -0.08
    flush_run_atr: float = 3.0
    flush_r_tp: float = 2.0
    flush_horizon_min: int = 1440
    flush_cooldown_h: int = 24


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
    pos: PosConfig | None = None                         # pos.v1 parameters (CROWD / FLUSH families)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d["pos"] is None:                             # keeps the slow.v1 paper config hash unchanged
            d.pop("pos")
        return d


def pos_config() -> SlowConfig:
    """pos.v1 as pre-registered: CROWD + FLUSH with slow.v1 stops, costs and universe."""
    return SlowConfig(families=POS_FAMILIES, pos=PosConfig())


def compute_slow_features(b: SymbolBars, funding: tuple[np.ndarray, np.ndarray] | None = None,
                          oi: tuple[np.ndarray, np.ndarray] | None = None) -> SymbolFeatures:
    first_h = b.first_minute / H if b.first_minute >= 0 else None
    return slow_features_from_hourly(b.symbol, b.start, resample(b, H), funding, b.listing_time, first_h, oi)


def oi_at(oi: tuple[np.ndarray, np.ndarray], tq: np.ndarray) -> np.ndarray:
    """Open interest known at times ``tq``: the last row with create_time + OI_LAG_MIN <= tq, NaN if none
    or if that row is older than OI_MAX_AGE_MIN beyond the lag."""
    ot, ov = oi
    if not len(ot):
        return np.full(len(tq), np.nan)
    lag = OI_LAG_MIN * MIN
    idx = np.searchsorted(ot, tq - lag, side="right") - 1
    i = np.maximum(idx, 0)
    ok = (idx >= 0) & (tq - lag - ot[i] <= OI_MAX_AGE_MIN * MIN)
    return np.where(ok, ov[i], np.nan)


def slow_features_from_hourly(symbol: str, start: int, h1: dict[str, np.ndarray],
                              funding: tuple[np.ndarray, np.ndarray] | None = None,
                              listing_time: int | None = None, first_hour: float | None = None,
                              oi: tuple[np.ndarray, np.ndarray] | None = None) -> SymbolFeatures:
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
            "run_4h_atr": (lc - lag(lc, 4)) / atr_pct,
            "qv_24h": rolling(h1["qv"], 24, "sum"),
        }
    t = start + (j + 1) * H * MIN
    f["oi_chg_24h"] = f["oi_chg_4h"] = np.full(J, np.nan)
    if oi is not None and len(oi[0]):
        now = oi_at(oi, t)
        with np.errstate(invalid="ignore", divide="ignore"):
            f["oi_chg_24h"] = now / oi_at(oi, t - 24 * H * MIN) - 1.0
            f["oi_chg_4h"] = now / oi_at(oi, t - 4 * H * MIN) - 1.0
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
    """7 d return relative to BTC, and ``btc_trend`` = sign of BTC's 1h EMA50 - EMA200 (descriptive regime
    tag for reports; no slow/pos family conditions on it)."""
    ref = feats[btc].f["ret_7d"] if btc in feats else None
    trend = feats[btc].f["ema_trend"] if btc in feats else None
    for sf in feats.values():
        sf.f["rel_ret_7d"] = sf.f["ret_7d"] - ref[:sf.n_ticks] if ref is not None else np.full(sf.n_ticks, np.nan)
        sf.f["btc_trend"] = trend[:sf.n_ticks].copy() if trend is not None else np.full(sf.n_ticks, np.nan)


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
        pc = cfg.pos or PosConfig()
        if "CROWD" in cfg.families:
            run, oc = F["run_24h_atr"], F["oi_chg_24h"]
            for side in (-1, 1):                                          # fade the crowd that piled in late
                trig = (oc >= pc.crowd_oi_chg) & (run >= pc.crowd_run_atr if side < 0 else run <= -pc.crowd_run_atr)
                for j, s in zip(*np.nonzero(elig & trig)):
                    cands.append((int(j), int(s), "CROWD", side, pc.crowd_r_tp, pc.crowd_horizon_min,
                                  pc.crowd_cooldown_h))
        if "FLUSH" in cfg.families:
            run4, oc4 = F["run_4h_atr"], F["oi_chg_4h"]
            for side in (1, -1):                                          # reversal after forced deleveraging
                trig = (oc4 <= pc.flush_oi_chg) & (run4 <= -pc.flush_run_atr if side > 0 else run4 >= pc.flush_run_atr)
                for j, s in zip(*np.nonzero(elig & trig)):
                    cands.append((int(j), int(s), "FLUSH", side, pc.flush_r_tp, pc.flush_horizon_min,
                                  pc.flush_cooldown_h))
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
             "btc_trend": float(F["btc_trend"][j, s]) if "btc_trend" in F else float("nan")}
        for k in SNAPSHOT:
            p[k] = float(F[k][j, s]) if k in F else float("nan")
        out.append(p)
    return out
