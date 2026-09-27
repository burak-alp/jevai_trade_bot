"""Deterministic scanner for historical replay (spec §5): eligibility -> BRK/PB detectors ->
geometry + cost gate -> cooldown -> cross-sectional ranking -> top-K per tick.

All thresholds are starting values ([CAL] in the spec); they live in ``ScannerConfig`` and are
recorded with every run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from jevbot.research.features import SymbolFeatures

FAMILY_VERSION = {"BRK": "brk.v1", "PB": "pb.v1"}
SIDE_ALIGNED = {"taker_imb_15m", "resid_ret_1h_atr", "ema_spread_atr"}   # ranking uses side * value

SNAPSHOT_FEATURES = ("atr_pct", "ret_15m_atr", "ret_1h_atr", "ret_4h_atr", "resid_ret_1h_atr", "ema_spread_atr",
                     "dist_ema20_atr", "er_1h", "er_4h", "rvol_z_15m", "taker_imb_15m", "range32_atr",
                     "funding_bps", "beta_btc", "btc_ret_1h_atr", "btc_trend", "btc_vol_state", "listing_age_d")


@dataclass(frozen=True)
class CostModel:
    fee_taker_bps: float = 5.0
    # (max volume rank, value): spread / slippage per side by 24h quote-volume rank at the tick
    spread_bps_by_rank: tuple[tuple[int, float], ...] = ((10, 1.5), (30, 3.0), (10_000, 5.0))
    slip_bps_by_rank: tuple[tuple[int, float], ...] = ((10, 1.0), (30, 2.0), (10_000, 4.0))
    stop_slip_mult: float = 2.0            # stop fills: extra adverse slippage multiple

    def spread_bps(self, rank: np.ndarray) -> np.ndarray:
        return self._tier(rank, self.spread_bps_by_rank)

    def slip_bps(self, rank: np.ndarray) -> np.ndarray:
        return self._tier(rank, self.slip_bps_by_rank)

    @staticmethod
    def _tier(rank: np.ndarray, table: tuple[tuple[int, float], ...]) -> np.ndarray:
        out = np.full(rank.shape, table[-1][1], dtype=float)
        for max_rank, v in reversed(table):
            out = np.where(rank <= max_rank, v, out)
        return out


@dataclass(frozen=True)
class ScannerConfig:
    min_qv_24h: float = 20e6
    atr_pct_min: float = 0.0015
    atr_pct_max: float = 0.03
    min_listing_age_d: float = 14.0
    # BRK
    brk_rvol_z_min: float = 1.0
    brk_taker_min: float = 0.05
    brk_ret15_max: float = 2.5
    brk_er1h_min: float = 0.30
    brk_range_atr_min: float = 2.0
    brk_stop_back_atr: float = 0.5
    brk_horizon_min: int = 120
    # PB
    pb_trend_min: float = 0.5
    pb_er4h_min: float = 0.30
    pb_extension_min: float = 1.0
    pb_zone_lo: float = -0.5
    pb_zone_hi: float = 0.3
    pb_stop_back_atr: float = 0.25
    pb_horizon_min: int = 180
    # geometry / cost
    k_min_atr: float = 1.0
    k_max_atr: float = 2.5
    r_tp: float = 1.5
    max_cost_r: float = 0.25
    min_stop_cost_mult: float = 4.0
    # selection
    cooldown_ticks: int = 6                 # 30 min
    top_k: int = 12
    family_cap: int = 8
    cost: CostModel = field(default_factory=CostModel)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _stack(feats: dict[str, SymbolFeatures], name: str, J: int) -> np.ndarray:
    return np.stack([sf.f[name][:J] for sf in feats.values()], axis=1)


def _xs_rank(x: np.ndarray) -> np.ndarray:
    """Cross-sectional rank per row (1 = largest); NaN -> +inf rank."""
    v = np.where(np.isnan(x), -np.inf, x)
    order = np.argsort(-v, axis=1, kind="stable")
    rank = np.empty_like(order)
    rows = np.arange(x.shape[0])[:, None]
    rank[rows, order] = np.arange(1, x.shape[1] + 1)
    return np.where(np.isnan(x), 10**9, rank)


def scan(feats: dict[str, SymbolFeatures], cfg: ScannerConfig, tradable: set[str] | None = None) -> list[dict[str, Any]]:
    symbols = list(feats)
    J = min(sf.n_ticks for sf in feats.values())
    F = {k: _stack(feats, k, J) for k in next(iter(feats.values())).f}
    close, atr, atr_pct = F["close"], F["atr_15m"], F["atr_pct"]
    vol_rank = _xs_rank(F["qv_24h"])
    trad = np.array([tradable is None or s in tradable for s in symbols])[None, :]
    with np.errstate(invalid="ignore"):
        eligible = (trad & (F["qv_24h"] >= cfg.min_qv_24h) & (atr_pct >= cfg.atr_pct_min)
                    & (atr_pct <= cfg.atr_pct_max) & (F["listing_age_d"] >= cfg.min_listing_age_d)
                    & ~np.isnan(close) & ~np.isnan(atr))
        prev_close = np.vstack([np.full((1, close.shape[1]), np.nan), close[:-1]])
        prev_h32 = np.vstack([np.full((1, close.shape[1]), np.nan), F["h32"][:-1]])
        prev_l32 = np.vstack([np.full((1, close.shape[1]), np.nan), F["l32"][:-1]])
        cands: list[tuple[int, int, str, int, float, float]] = []   # j, s, family, side, struct_stop, horizon
        for side in (1, -1):
            # BRK: fresh close beyond the 8 h (32 x 15m) range of completed bars
            brk_trig = (close > F["h32"]) & (prev_close <= prev_h32) if side > 0 else \
                       (close < F["l32"]) & (prev_close >= prev_l32)
            brk = (eligible & brk_trig & (F["rvol_z_15m"] >= cfg.brk_rvol_z_min)
                   & (side * F["taker_imb_15m"] >= cfg.brk_taker_min)
                   & (side * F["ret_15m_atr"] <= cfg.brk_ret15_max)
                   & (F["er_1h"] >= cfg.brk_er1h_min) & (F["range32_atr"] >= cfg.brk_range_atr_min))
            level = F["h32"] if side > 0 else F["l32"]
            struct = level - side * cfg.brk_stop_back_atr * atr
            for j, s in zip(*np.nonzero(brk)):
                cands.append((int(j), int(s), "BRK", side, float(struct[j, s]), cfg.brk_horizon_min))
            # PB: trend, recent extension, pullback into the EMA zone, resumption trigger
            ext = F["max_dist_ema20_12x15"] if side > 0 else -F["min_dist_ema20_12x15"]
            trig = (close > F["prev_high5"]) if side > 0 else (close < F["prev_low5"])
            pb = (eligible & (side * F["ema_spread_atr"] >= cfg.pb_trend_min) & (F["er_4h"] >= cfg.pb_er4h_min)
                  & (ext >= cfg.pb_extension_min)
                  & (side * F["dist_ema20_atr"] >= cfg.pb_zone_lo) & (side * F["dist_ema20_atr"] <= cfg.pb_zone_hi)
                  & trig & (side * F["taker_imb_15m"] > 0))
            swing = F["swing_low_2h"] if side > 0 else F["swing_high_2h"]
            struct_pb = swing - side * cfg.pb_stop_back_atr * atr
            for j, s in zip(*np.nonzero(pb)):
                cands.append((int(j), int(s), "PB", side, float(struct_pb[j, s]), cfg.pb_horizon_min))
    cands.sort(key=lambda c: (c[0], c[1], c[2], c[3]))
    last_fire: dict[tuple[int, str, int], int] = {}
    per_tick: dict[int, list[dict[str, Any]]] = {}
    spread = cfg.cost.spread_bps(vol_rank)
    slip = cfg.cost.slip_bps(vol_rank)
    for j, s, fam, side, struct, horizon in cands:
        key = (s, fam, side)
        if key in last_fire and j - last_fire[key] < cfg.cooldown_ticks:
            continue
        entry = float(close[j, s])
        a = float(atr[j, s])
        dist = side * (entry - struct)
        if not np.isfinite(dist) or dist <= 0:
            dist = cfg.k_min_atr * a                          # structure on the wrong side -> ATR floor
        dist = max(dist, cfg.k_min_atr * a)
        if dist > cfg.k_max_atr * a:
            continue
        stop_bps = dist / entry * 1e4
        sp, sl = float(spread[j, s]), float(slip[j, s])
        fund = float(F["funding_bps"][j, s]) if np.isfinite(F["funding_bps"][j, s]) else 0.0
        cost_bps = 2 * cfg.cost.fee_taker_bps + sp + 2 * sl + max(0.0, side * fund) * horizon / 480.0
        if stop_bps < cfg.min_stop_cost_mult * (sp + sl):
            continue
        cost_r = cost_bps / stop_bps
        if cost_r > cfg.max_cost_r:
            continue
        last_fire[key] = j
        p = {"tick": j, "t_decision": int(feats[symbols[s]].tick_time(j)), "symbol": symbols[s], "family": fam,
             "family_version": FAMILY_VERSION[fam], "side": side, "entry_ref": entry,
             "stop_price": entry - side * dist, "tp_price": entry + side * cfg.r_tp * dist, "stop_dist": dist,
             "stop_dist_bps": stop_bps, "r_tp": cfg.r_tp, "horizon_min": horizon, "cost_rt_bps": cost_bps,
             "cost_r": cost_r, "spread_bps": sp, "slip_bps": sl, "vol_rank": int(vol_rank[j, s]),
             "structural_stop_used": bool(side * (entry - struct) >= cfg.k_min_atr * a)}
        for k in SNAPSHOT_FEATURES:
            p[k] = float(F[k][j, s]) if k in F else float("nan")
        per_tick.setdefault(j, []).append(p)
    out: list[dict[str, Any]] = []
    for j in sorted(per_tick):
        props = per_tick[j]
        for fam in ("BRK", "PB"):
            grp = [p for p in props if p["family"] == fam]
            if not grp:
                continue
            comps = (["rvol_z_15m", "taker_imb_15m", "resid_ret_1h_atr"] if fam == "BRK"
                     else ["ema_spread_atr", "er_4h", "taker_imb_15m"])
            mat = []
            for c in comps:
                v = np.array([p["side"] * p[c] if c in SIDE_ALIGNED else p[c] for p in grp])
                mat.append(_pct(v))
            mat.append(_pct(-np.array([p["cost_r"] for p in grp])))
            score = np.nanmean(np.vstack(mat), axis=0)
            for p, sc in zip(grp, score):
                p["score"] = float(sc)
        props.sort(key=lambda p: -p["score"])
        taken: dict[str, int] = {}
        rank = 0
        for p in props:
            if taken.get(p["family"], 0) >= cfg.family_cap or rank >= cfg.top_k:
                p["selected"] = False
                continue
            taken[p["family"]] = taken.get(p["family"], 0) + 1
            rank += 1
            p["selected"] = True
            p["rank"] = rank
        out.extend(props)
    return out


def _pct(v: np.ndarray) -> np.ndarray:
    """Percentile rank in [0, 1] within the vector (ties averaged); NaN stays NaN."""
    ok = ~np.isnan(v)
    out = np.full(v.shape, np.nan)
    n = ok.sum()
    if n == 0:
        return out
    if n == 1:
        out[ok] = 0.5
        return out
    vals = v[ok]
    order = vals.argsort(kind="stable")
    ranks = np.empty(n)
    ranks[order] = np.arange(n)
    out[ok] = ranks / (n - 1)
    return out
