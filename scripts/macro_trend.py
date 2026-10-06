"""macro.trend.v1 backtest (pre-registered in reports/remote/20261006T-macro-trend-prereg.md); core in
jevbot.research.macro.

  python scripts/macro_trend.py          -> run/macro_trend/{prices.json, result.json} + printed tables
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jevbot.research.macro import (ASSETS, CASH, COST, END, GRID, OUT, SPLIT, START, VOL_WIN, align, block_ci,  # noqa: E402,F401
                                   fetch, load, static, stats, strategy)


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
