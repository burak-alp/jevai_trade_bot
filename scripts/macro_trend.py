"""macro.trend.v1 (pre-registered in reports/remote/20261006T-macro-trend-prereg.md): cross-asset trend.

  python scripts/macro_trend.py          -> run/macro_trend/{prices.json, result.json} + printed tables

Weekly (last trading day of the week): per asset, trailing return vs cash over the lookback; positive -> hold its
inverse-volatility (60 d) share of the book, else that share sits in cash. No leverage, no shorts, 10 bps per side.
Weights are held constant between rebalances (cost charged only on rebalance turnover).
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ASSETS = ["SPY", "QQQ", "GLD", "SLV", "USO", "TLT", "BTC-USD", "ETH-USD"]
CASH = "^IRX"
START, SPLIT, END = date(2006, 5, 1), date(2016, 1, 1), date(2026, 10, 1)
COST, VOL_WIN = 0.0010, 60
GRID = {"63": (63,), "126": (126,), "252": (252,), "blend": (21, 63, 252)}
OUT = Path("run/macro_trend")


def fetch(ticker: str) -> dict[str, float]:
    import httpx
    from jevbot.core.tls import ensure_system_trust
    ensure_system_trust()
    r = httpx.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                  params={"period1": 1104537600, "period2": int(datetime.now(timezone.utc).timestamp()), "interval": "1d"},
                  headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    j = r.json()["chart"]["result"][0]
    off = j["meta"].get("gmtoffset", 0)
    ind = j["indicators"]
    vals = ind["adjclose"][0]["adjclose"] if "adjclose" in ind else ind["quote"][0]["close"]
    out = {}
    for t, v in zip(j["timestamp"], vals):
        if v is not None:
            out[datetime.fromtimestamp(t + off, timezone.utc).date().isoformat()] = float(v)
    return out


def load() -> dict[str, dict[str, float]]:
    OUT.mkdir(parents=True, exist_ok=True)
    f = OUT / "prices.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    data = {t: fetch(t) for t in ASSETS + [CASH]}
    f.write_text(json.dumps(data), encoding="utf-8")
    return data


def align(data):
    """NYSE calendar = SPY dates; other series forward-filled to the last value on or before each date."""
    days = sorted(d for d in data["SPY"] if START.isoformat() <= d < END.isoformat())
    P = np.full((len(days), len(ASSETS)), np.nan)
    for k, a in enumerate(ASSETS):
        src = sorted(data[a].items())
        j, last = 0, np.nan
        for i, d in enumerate(days):
            while j < len(src) and src[j][0] <= d:
                last = src[j][1]
                j += 1
            P[i, k] = last if j > 0 and (date.fromisoformat(d) - date.fromisoformat(src[j - 1][0])).days <= 5 else np.nan
    irx = sorted(data[CASH].items())
    cash = np.zeros(len(days))
    j, last = 0, 0.0
    for i, d in enumerate(days):
        while j < len(irx) and irx[j][0] <= d:
            last = irx[j][1]
            j += 1
        cash[i] = last / 100 / 252
    return [date.fromisoformat(d) for d in days], P, cash


def strategy(P, cash, days, lbs: tuple[int, ...]):
    T, N = P.shape
    with np.errstate(invalid="ignore", divide="ignore"):
        R = np.vstack([np.full(N, np.nan), P[1:] / P[:-1] - 1])
    ccum = np.cumsum(cash)
    week_end = np.array([i == T - 1 or days[i + 1].isocalendar()[1] != days[i].isocalendar()[1] for i in range(T)])
    w = np.zeros(N)
    W = np.zeros((T, N))
    turn = np.zeros(T)
    for t in range(T):
        W[t] = w                                            # weights in force for day t's return (set at t-1 or earlier)
        if not week_end[t] or t < VOL_WIN:
            continue
        vol = np.nanstd(R[t - VOL_WIN + 1:t + 1], axis=0)
        avail = np.isfinite(vol) & (vol > 0) & np.isfinite(P[t])
        for L in lbs:
            avail &= (t - L >= 0) & np.isfinite(P[max(t - L, 0)])
        inv = np.where(avail, 1 / np.where(avail, vol, 1), 0)
        share = inv / inv.sum() if inv.sum() > 0 else inv
        score = np.zeros(N)
        for L in lbs:
            with np.errstate(invalid="ignore", divide="ignore"):
                score += np.nan_to_num(P[t] / P[max(t - L, 0)] - 1) - (ccum[t] - ccum[t - L])
        new = np.where(avail & (score > 0), share, 0.0)
        turn[t] = np.abs(new - w).sum()
        w = new
    port = (W * np.nan_to_num(R)).sum(1) + (1 - W.sum(1)) * cash
    port[1:] -= COST * turn[:-1]                            # trades at day t's close are paid in day t+1's return
    return port, W


def static(P, cash, days, weights: dict[int, float] | None):
    """Monthly-rebalanced fixed weights (None = equal weight over available assets)."""
    T, N = P.shape
    with np.errstate(invalid="ignore", divide="ignore"):
        R = np.vstack([np.full(N, np.nan), P[1:] / P[:-1] - 1])
    w, port = np.zeros(N), np.zeros(T)
    for t in range(T):
        port[t] = (w * np.nan_to_num(R[t])).sum() + (1 - w.sum()) * cash[t]
        if t == 0 or days[t].month != days[t - 1].month:
            avail = np.isfinite(P[t])
            if weights is None:
                new = np.where(avail, 1 / avail.sum(), 0.0)
            else:
                new = np.zeros(N)
                for k, v in weights.items():
                    new[k] = v if avail[k] else 0
            if t + 1 < T:
                port[t + 1] -= COST * np.abs(new - w).sum()
            w = new
    return port


def stats(x, c):
    ex = x - c
    eq = np.cumprod(1 + x)
    return {"ann": float(np.prod(1 + x) ** (252 / len(x)) - 1), "vol": float(x.std() * np.sqrt(252)),
            "sharpe": float(ex.mean() / ex.std() * np.sqrt(252)) if ex.std() > 0 else float("nan"),
            "max_dd": float((1 - eq / np.maximum.accumulate(eq)).max())}


def block_ci(x, n=2000, block=20, seed=0):
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(len(x) / block))
    st = np.arange(len(x) - block + 1)
    vals = [np.prod(1 + x[(rng.choice(st, nb)[:, None] + np.arange(block)).ravel()[:len(x)]]) ** (252 / len(x)) - 1
            for _ in range(n)]
    return [float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))]


def main() -> int:
    days, P, cash = align(load())
    days_a = np.array(days)
    D, T_ = days_a < SPLIT, days_a >= SPLIT
    i_spy, i_tlt = ASSETS.index("SPY"), ASSETS.index("TLT")
    refs = {"SPY al-tut": static(P, cash, days, {i_spy: 1.0}),
            "60/40": static(P, cash, days, {i_spy: 0.6, i_tlt: 0.4}),
            "eşit ağırlık al-tut": static(P, cash, days, None)}
    runs = {k: strategy(P, cash, days, v) for k, v in GRID.items()}
    best = max(runs, key=lambda k: stats(runs[k][0][D], cash[D])["sharpe"])
    res = {"dev_pick": best, "dev": {}, "test": {}, "years": {}}
    for nm, m in (("dev", D), ("test", T_)):
        for k, (x, _) in runs.items():
            res[nm][f"trend_{k}"] = stats(x[m], cash[m])
        for k, x in refs.items():
            res[nm][k] = stats(x[m], cash[m])
    x = runs[best][0]
    res["test"][f"trend_{best}"]["ann_ci95"] = block_ci(x[T_])
    t = res["test"]
    pick = t[f"trend_{best}"]
    res["go"] = {"ci_low_pos": pick["ann_ci95"][0] > 0,
                 "sharpe_beats": pick["sharpe"] > t["60/40"]["sharpe"] and pick["sharpe"] > t["SPY al-tut"]["sharpe"],
                 "dd_smaller": pick["max_dd"] < t["SPY al-tut"]["max_dd"]}
    res["go"]["pass"] = all(res["go"].values())
    for y in range(days[0].year, days[-1].year + 1):
        m = np.array([d.year == y for d in days])
        res["years"][y] = {"trend": float(np.prod(1 + x[m]) - 1),
                           **{k: float(np.prod(1 + v[m]) - 1) for k, v in refs.items()}}
    W = runs[best][1]
    res["now"] = {"date": str(days[-1]), "weights": {a: round(float(W[-1][k]), 3) for k, a in enumerate(ASSETS)},
                  "cash": round(float(1 - W[-1].sum()), 3)}
    res["avg_weight_test"] = {a: round(float(W[T_, k].mean()), 3) for k, a in enumerate(ASSETS)}
    (OUT / "result.json").write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"data {days[0]} -> {days[-1]}; dev pick: {best}")
    for nm in ("dev", "test"):
        print(f"\n{nm}: {'strateji':22s} {'yıllık':>8s} {'oynaklık':>9s} {'sharpe':>7s} {'maks düşüş':>11s}")
        for k, s in res[nm].items():
            print(f"     {k:22s} {s['ann'] * 100:7.1f}% {s['vol'] * 100:8.1f}% {s['sharpe']:7.2f} {s['max_dd'] * 100:10.1f}%")
    print("\ntest CI:", [round(v * 100, 1) for v in pick["ann_ci95"]], "GO:", res["go"])
    print("\nyıl   trend    SPY   60/40   eşit")
    for y, v in res["years"].items():
        print(f"{y} {v['trend'] * 100:6.1f}% {v['SPY al-tut'] * 100:6.1f}% {v['60/40'] * 100:6.1f}% {v['eşit ağırlık al-tut'] * 100:6.1f}%")
    print("\nşu an:", res["now"])
    print("test ort. ağırlık:", res["avg_weight_test"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
