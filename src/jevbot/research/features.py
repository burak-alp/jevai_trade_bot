"""Bar-based feature engine ``f.v1-hist`` for historical replay (numpy, no lookahead).

Time grid: minute m. Decision tick j is the close of 5m bar j, i.e. wall time
``start + (5j + 5) * 1 min``; everything used at tick j comes from minutes < 5j + 5.
15m features come from *completed* 15m bars only: the last one at tick j is
``q_last(j) = (5j + 5) // 15 - 1``.

Deviations from spec §4 (documented, historical data limits):
* ``er_1h`` uses 12 x 5m closes (spec: 60 x 1m).
* ``rvol_z_15m`` uses a trailing mean/std z-score of log quote volume (spec: median/MAD).
* no microstructure features (no historical order book); ``funding_bps`` = last *settled*
  rate (predicted funding is not in Binance Vision).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from jevbot.research.data import MIN, SymbolBars

FEATURE_SCHEMA = "f.v1-hist"


def resample(b: SymbolBars, k: int) -> dict[str, np.ndarray]:
    """k-minute bars from the 1m grid; a bar with any missing minute is NaN."""
    n = b.n // k
    sl = slice(0, n * k)

    def rs(a: np.ndarray) -> np.ndarray:
        return a[sl].reshape(n, k)
    c = rs(b.close)
    complete = ~np.isnan(c).any(axis=1)
    out = {
        "open": rs(b.open)[:, 0].copy(), "high": np.nanmax(np.where(np.isnan(rs(b.high)), -np.inf, rs(b.high)), axis=1),
        "low": np.nanmin(np.where(np.isnan(rs(b.low)), np.inf, rs(b.low)), axis=1), "close": c[:, -1].copy(),
        "qv": np.nansum(rs(b.quote_volume), axis=1), "tbq": np.nansum(rs(b.taker_buy_quote), axis=1),
    }
    for key in out:
        out[key] = np.where(complete, out[key], np.nan)
    return out


def wilder_atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    prev_c = np.concatenate([[np.nan], c[:-1]])
    tr = np.nanmax(np.stack([h - l, np.abs(h - prev_c), np.abs(l - prev_c)]), axis=0)
    tr = np.where(np.isnan(h) | np.isnan(l), np.nan, tr)
    out = np.full_like(c, np.nan)
    atr, cnt, acc = np.nan, 0, 0.0
    for i, x in enumerate(tr):
        if np.isnan(x):
            atr, cnt, acc = np.nan, 0, 0.0          # restart after a gap
            continue
        if cnt < n:
            acc += x
            cnt += 1
            if cnt == n:
                atr = acc / n
        else:
            atr = (atr * (n - 1) + x) / n
        out[i] = atr
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    a = 2.0 / (n + 1)
    out = np.full_like(x, np.nan)
    v, cnt = np.nan, 0
    for i, xi in enumerate(x):
        if np.isnan(xi):
            v, cnt = np.nan, 0
            continue
        v = xi if cnt == 0 else a * xi + (1 - a) * v
        cnt += 1
        if cnt >= n:                                  # warmup
            out[i] = v
    return out


def rolling(x: np.ndarray, w: int, fn: str) -> np.ndarray:
    """Trailing window statistic including the current element; NaN until w values (any NaN -> NaN)."""
    out = np.full_like(x, np.nan, dtype=np.float64)
    if len(x) < w:
        return out
    v = sliding_window_view(x, w)
    out[w - 1:] = {"max": np.max, "min": np.min, "sum": np.sum}[fn](v, axis=1)
    return out


def rolling_mean_std(x: np.ndarray, w: int) -> tuple[np.ndarray, np.ndarray]:
    ok = ~np.isnan(x)
    xv = np.where(ok, x, 0.0)
    c1 = np.concatenate([[0.0], np.cumsum(xv)])
    c2 = np.concatenate([[0.0], np.cumsum(xv * xv)])
    cn = np.concatenate([[0], np.cumsum(ok)])
    mean = np.full_like(x, np.nan)
    std = np.full_like(x, np.nan)
    i = np.arange(w, len(x) + 1)
    n = cn[i] - cn[i - w]
    s1, s2 = c1[i] - c1[i - w], c2[i] - c2[i - w]
    good = n >= max(2, w // 2)
    m = np.where(good, s1 / np.maximum(n, 1), np.nan)
    var = np.where(good, s2 / np.maximum(n, 1) - m * m, np.nan)
    mean[w - 1:] = m
    std[w - 1:] = np.sqrt(np.maximum(var, 0))
    return mean, std


def efficiency_ratio(c: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(c, np.nan)
    d = np.abs(np.diff(c))
    path = rolling(np.concatenate([[np.nan], d]), n, "sum")
    net = np.abs(c - np.concatenate([np.full(n, np.nan), c[:-n]]))
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(path > 0, net / path, np.nan)
    return out


@dataclass
class SymbolFeatures:
    symbol: str
    start: int
    n_ticks: int
    f: dict[str, np.ndarray] = field(default_factory=dict)

    def tick_time(self, j: np.ndarray | int) -> np.ndarray | int:
        return self.start + (5 * np.asarray(j) + 5) * MIN


def compute_symbol_features(b: SymbolBars, funding: tuple[np.ndarray, np.ndarray] | None = None) -> SymbolFeatures:
    b5, b15 = resample(b, 5), resample(b, 15)
    J = len(b5["close"])
    j = np.arange(J)
    q = (5 * j + 5) // 15 - 1                          # last completed 15m bar at tick j
    qv_ok = q >= 0
    qi = np.where(qv_ok, q, 0)

    def at_q(x15: np.ndarray) -> np.ndarray:
        return np.where(qv_ok & (qi < len(x15)), x15[np.minimum(qi, len(x15) - 1)], np.nan)

    atr15 = wilder_atr(b15["high"], b15["low"], b15["close"], 14)
    e20, e50 = ema(b15["close"], 20), ema(b15["close"], 50)
    c5 = b5["close"]
    atr = at_q(atr15)
    atr_pct = atr / c5
    ema20, ema50 = at_q(e20), at_q(e50)
    h32, l32 = at_q(rolling(b15["high"], 32, "max")), at_q(rolling(b15["low"], 32, "min"))
    lqv15 = np.log(np.where(b15["qv"] > 0, b15["qv"], np.nan))
    mu, sd = rolling_mean_std(lqv15, 672)
    # z of the last completed 15m bar against the 672 bars *before* it
    mu_prev = np.concatenate([[np.nan], mu[:-1]])
    sd_prev = np.concatenate([[np.nan], sd[:-1]])
    with np.errstate(invalid="ignore", divide="ignore"):
        rvol_z15 = np.clip((lqv15 - mu_prev) / sd_prev, -5, 5)
        taker15 = (2 * b15["tbq"] - b15["qv"]) / b15["qv"]
        dist15 = (b15["close"] - e20) / atr15
    lc5 = np.log(c5)
    lag = lambda x, n: np.concatenate([np.full(n, np.nan), x[:-n]])  # noqa: E731
    with np.errstate(invalid="ignore", divide="ignore"):
        f = {
            "close": c5, "high5": b5["high"], "low5": b5["low"],
            "prev_high5": lag(b5["high"], 1), "prev_low5": lag(b5["low"], 1),
            "atr_15m": atr, "atr_pct": atr_pct,
            "ret_15m_atr": (lc5 - lag(lc5, 3)) / atr_pct,
            "ret_1h_atr": (lc5 - lag(lc5, 12)) / atr_pct,
            "ret_4h_atr": (lc5 - lag(lc5, 48)) / atr_pct,
            "ret_1h": lc5 - lag(lc5, 12),
            "ema_spread_atr": (ema20 - ema50) / atr,
            "dist_ema20_atr": (c5 - ema20) / atr,
            "er_1h": efficiency_ratio(c5, 12),
            "er_4h": at_q(efficiency_ratio(b15["close"], 16)),
            "h32": h32, "l32": l32,
            "range32_atr": (h32 - l32) / atr,
            "rvol_z_15m": at_q(rvol_z15),
            "taker_imb_15m": at_q(taker15),
            "max_dist_ema20_12x15": at_q(rolling(dist15, 12, "max")),
            "min_dist_ema20_12x15": at_q(rolling(dist15, 12, "min")),
            "swing_low_2h": rolling(b5["low"], 24, "min"),
            "swing_high_2h": rolling(b5["high"], 24, "max"),
            "qv_24h": rolling(b5["qv"], 288, "sum"),
            "listing_age_d": np.where(b.first_minute >= 0, (5 * j + 5 - b.first_minute) / 1440.0, np.nan),
        }
    if funding is not None and len(funding[0]):
        tt = b.start + (5 * j + 5) * MIN
        idx = np.searchsorted(funding[0], tt, side="left") - 1      # settled strictly before the tick
        f["funding_bps"] = np.where(idx >= 0, funding[1][np.maximum(idx, 0)] * 1e4, np.nan)
    else:
        f["funding_bps"] = np.full(J, np.nan)
    return SymbolFeatures(b.symbol, b.start, J, f)


def add_market_context(feats: dict[str, SymbolFeatures], btc: str = "BTCUSDT", beta_window: int = 2016) -> None:
    """BTC-relative beta / residual return (rolling 7 d of 5m returns) and BTC regime, in place."""
    if btc not in feats:
        for sf in feats.values():
            sf.f["beta_btc"] = np.full(sf.n_ticks, np.nan)
            sf.f["resid_ret_1h_atr"] = np.full(sf.n_ticks, np.nan)
        return
    bf = feats[btc].f
    rb = np.diff(np.log(bf["close"]), prepend=np.nan)
    for sf in feats.values():
        ra = np.diff(np.log(sf.f["close"]), prepend=np.nan)
        ok = ~(np.isnan(ra) | np.isnan(rb))
        x, y = np.where(ok, rb, 0.0), np.where(ok, ra, 0.0)
        cs = lambda v: np.concatenate([[0.0], np.cumsum(v)])  # noqa: E731
        cx, cy, cxy, cxx, cn = cs(x), cs(y), cs(x * y), cs(x * x), cs(ok.astype(float))
        n = len(ra)
        beta = np.full(n, np.nan)
        i = np.arange(beta_window, n + 1)
        N = cn[i] - cn[i - beta_window]
        sx, sy = cx[i] - cx[i - beta_window], cy[i] - cy[i - beta_window]
        sxy, sxx = cxy[i] - cxy[i - beta_window], cxx[i] - cxx[i - beta_window]
        with np.errstate(invalid="ignore", divide="ignore"):
            cov = sxy / N - (sx / N) * (sy / N)
            var = sxx / N - (sx / N) ** 2
            beta[beta_window - 1:] = np.where((N > beta_window // 2) & (var > 0), cov / var, np.nan)
        sf.f["beta_btc"] = beta
        with np.errstate(invalid="ignore"):
            sf.f["resid_ret_1h_atr"] = (sf.f["ret_1h"] - beta * bf["ret_1h"]) / sf.f["atr_pct"]
    # BTC regime (deterministic; spec §4), vol state from a trailing 30 d z-score of BTC atr_pct
    mu, sd = rolling_mean_std(bf["atr_pct"], 8640)
    with np.errstate(invalid="ignore", divide="ignore"):
        vz = (bf["atr_pct"] - mu) / sd
    trend = np.where((bf["ema_spread_atr"] > 0.5) & (bf["er_4h"] > 0.3), 1,
                     np.where((bf["ema_spread_atr"] < -0.5) & (bf["er_4h"] > 0.3), -1, 0))
    vol = np.where(vz < -0.5, 0, np.where(vz > 1.0, 2, 1))
    for sf in feats.values():
        sf.f["btc_trend"] = trend.astype(float)
        sf.f["btc_vol_state"] = np.where(np.isnan(vz), np.nan, vol.astype(float))
        sf.f["btc_ret_1h_atr"] = bf["ret_1h_atr"]
