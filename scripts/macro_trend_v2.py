"""macro.trend.v2 (pre-registered in reports/remote/20261006T-macro-trend-v2-prereg.md).

  python scripts/macro_trend_v2.py   -> run/macro_trend/result_v2.json + printed tables

Same signal as v1 (252 d return vs cash, weekly, inverse-vol shares) on a Binance-tradeable universe, optional
shorts, portfolio volatility targeting with a leverage cap. Perp-like returns: cash + sum w (r - cash) - financing
spread |w| - turnover cost.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import macro_trend as v1  # noqa: E402

BASE = ["SPY", "QQQ", "GLD", "SLV", "USO", "TLT", "BTC-USD", "ETH-USD"]
BROAD = BASE + ["IWM", "EWJ", "EWT", "EWY", "EWZ", "XLE", "SMH", "GDX", "BNO", "CPER", "PPLT", "PALL", "UNG"]
CRYPTO = {"BTC-USD", "ETH-USD"}
LB, VOL_WIN, COST = 252, 60, 0.0010
SPREAD = {"tradfi": 0.01, "crypto": 0.05}


def load_all():
    f = v1.OUT / "prices_v2.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    base = v1.load()
    data = dict(base)
    for t in BROAD:
        if t not in data:
            data[t] = v1.fetch(t)
    f.write_text(json.dumps(data), encoding="utf-8")
    return data


def run(P, cash, days, assets, long_short: bool, target: float, cap: float):
    T, N = P.shape
    with np.errstate(invalid="ignore", divide="ignore"):
        R = np.vstack([np.full(N, np.nan), P[1:] / P[:-1] - 1])
    X = np.nan_to_num(R) - cash[:, None]                   # excess returns
    spread = np.array([SPREAD["crypto" if a in CRYPTO else "tradfi"] / 252 for a in assets])
    ccum = np.cumsum(cash)
    week_end = np.array([i == T - 1 or days[i + 1].isocalendar()[1] != days[i].isocalendar()[1] for i in range(T)])
    w = np.zeros(N)
    raw = np.zeros(T)                                       # unscaled excess return of the signal book
    W = np.zeros((T, N))
    k = 0.0
    out = np.zeros(T)
    for t in range(T):
        W[t] = w * k
        raw[t] = (w * X[t]).sum() - (np.abs(w) * spread).sum()
        out[t] = cash[t] + (W[t] * X[t]).sum() - (np.abs(W[t]) * spread).sum()
        if not week_end[t] or t < max(VOL_WIN, LB):
            continue
        vol = np.nanstd(R[t - VOL_WIN + 1:t + 1], axis=0)
        avail = np.isfinite(vol) & (vol > 0) & np.isfinite(P[t]) & np.isfinite(P[t - LB])
        inv = np.where(avail, 1 / np.where(avail, vol, 1), 0)
        share = inv / inv.sum() if inv.sum() > 0 else inv
        with np.errstate(invalid="ignore", divide="ignore"):
            score = np.nan_to_num(P[t] / P[t - LB] - 1) - (ccum[t] - ccum[t - LB])
        sign = np.where(score > 0, 1.0, -1.0 if long_short else 0.0)
        new_w = np.where(avail, share * sign, 0.0)
        hist = raw[max(t - VOL_WIN + 1, 0):t + 1]
        pv = hist.std() * np.sqrt(252) if hist.std() > 0 else np.nan
        new_k = min(cap, target / pv) if np.isfinite(pv) and pv > 0 else 1.0
        if t + 1 < T:
            out[t + 1] -= COST * np.abs(new_w * new_k - w * k).sum()
        w, k = new_w, new_k
    return out, W


def worst_month(x, days):
    months: dict[tuple[int, int], float] = {}
    for r, d in zip(x, days):
        months[(d.year, d.month)] = (1 + months.get((d.year, d.month), 0.0)) * (1 + r) - 1
    return float(min(months.values()))


def main() -> int:
    data = load_all()
    days, P_all, cash = v1.align(data, BROAD)
    d = np.array(days)
    D, T_ = d < v1.SPLIT, d >= v1.SPLIT
    cols = {a: i for i, a in enumerate(BROAD)}
    cands = {}
    for uni_name, uni in (("TEMEL", BASE), ("GENİŞ", BROAD)):
        P = P_all[:, [cols[a] for a in uni]]
        for ls in (False, True):
            cands[(uni_name, ls)] = (uni, P)
    res = {"dev": {}, "test": {}}
    best, best_sh = None, -9
    for key, (uni, P) in cands.items():
        x, _ = run(P, cash, days, uni, key[1], 0.10, 3.0)
        name = f"{key[0]} {'long/short' if key[1] else 'yalnız long'}"
        sd, st = v1.stats(x[D], cash[D]), v1.stats(x[T_], cash[T_])
        res["dev"][name], res["test"][name] = sd, st
        if sd["sharpe"] > best_sh:
            best, best_sh = key, sd["sharpe"]
    uni, P = cands[best]
    name = f"{best[0]} {'long/short' if best[1] else 'yalnız long'}"
    res["dev_pick"] = name
    lev = {}
    for tgt in (0.10, 0.15, 0.20, 0.30):
        x, W = run(P, cash, days, uni, best[1], tgt, 3.0)
        s = v1.stats(x[T_], cash[T_])
        s["worst_month"] = worst_month(x[T_], d[T_])
        s["avg_gross"] = float(np.abs(W[T_]).sum(1).mean())
        s["max_gross"] = float(np.abs(W[T_]).sum(1).max())
        if tgt == 0.10:
            s["ann_ci95"] = v1.block_ci(x[T_])
            pick_x, pick_W = x, W
        lev[f"{int(tgt * 100)}%"] = s
    res["leverage_test"] = lev
    p = lev["10%"]
    spy = v1.stats(v1.static(P_all, cash, days, {cols["SPY"]: 1.0})[T_], cash[T_])
    res["spy_test"] = spy
    res["go"] = {"ci_low_pos": p["ann_ci95"][0] > 0, "sharpe_beats_v1": p["sharpe"] > 1.09, "dd_smaller": p["max_dd"] < spy["max_dd"]}
    res["go"]["pass"] = all(res["go"].values())
    res["years"] = {}
    for y in range(days[0].year, days[-1].year + 1):
        m = d == d
        m = np.array([dd.year == y for dd in days])
        res["years"][y] = float(np.prod(1 + pick_x[m]) - 1)
    res["now"] = {"date": str(days[-1]), "weights": {a: round(float(pick_W[-1][i]), 3) for i, a in enumerate(uni)
                                                     if abs(pick_W[-1][i]) > 0.001}}
    (v1.OUT / "result_v2.json").write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    for nm in ("dev", "test"):
        print(f"\n{nm}: {'aday':26s} {'yıllık':>8s} {'oynak':>7s} {'sharpe':>7s} {'maksDD':>7s}")
        for k, s in res[nm].items():
            print(f"     {k:26s} {s['ann'] * 100:7.1f}% {s['vol'] * 100:6.1f}% {s['sharpe']:7.2f} {s['max_dd'] * 100:6.1f}%")
    print("\nD seçimi:", name)
    print(f"\nkaldıraç (T): {'hedef':>6s} {'yıllık':>8s} {'sharpe':>7s} {'maksDD':>7s} {'en kötü ay':>11s} {'ort brüt':>9s} {'maks brüt':>9s}")
    for k, s in lev.items():
        print(f"              {k:>6s} {s['ann'] * 100:7.1f}% {s['sharpe']:7.2f} {s['max_dd'] * 100:6.1f}% {s['worst_month'] * 100:10.1f}% "
              f"{s['avg_gross']:8.2f}x {s['max_gross']:8.2f}x")
    print("\nCI (10%):", [round(v * 100, 1) for v in p["ann_ci95"]], "GO:", res["go"])
    print("SPY T:", {k: round(v, 3) for k, v in spy.items()})
    print("yıllar (10% hedef):", {y: round(v * 100, 1) for y, v in res["years"].items()})
    print("şu an:", res["now"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
