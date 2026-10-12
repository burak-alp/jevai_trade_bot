"""macro.brake.v1 (pre-registered in reports/remote/20261013T-macro-brake-prereg.md): event-reaction variants of v1.

  python scripts/macro_brake.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jevbot.research.macro import COST, OUT, SPLIT, VOL_WIN, align, brake_hits, load, stats, strategy  # noqa: E402

LB = 252


def run(P, cash, days, asset_brake=False, port_brake=False, daily=False, k_sigma=3.0):
    T, N = P.shape
    with np.errstate(invalid="ignore", divide="ignore"):
        R = np.vstack([np.full(N, np.nan), P[1:] / P[:-1] - 1])
    Rz = np.nan_to_num(R)
    ccum = np.cumsum(cash)
    week_end = np.array([i == T - 1 or days[i + 1].isocalendar()[1] != days[i].isocalendar()[1] for i in range(T)])
    base, eff = np.zeros(N), np.zeros(N)
    frozen = np.zeros(N, bool)
    halved = False
    eq, anchor = 1.0, 1.0
    port = np.zeros(T)
    for t in range(T):
        r = (eff * Rz[t]).sum() + (1 - eff.sum()) * cash[t]
        port[t] += r
        eq *= 1 + port[t]
        if t < VOL_WIN:
            continue
        new_base = None
        if week_end[t] or daily:
            vol = np.nanstd(R[t - VOL_WIN + 1:t + 1], axis=0)
            avail = np.isfinite(vol) & (vol > 0) & np.isfinite(P[t]) & (t - LB >= 0) & np.isfinite(P[max(t - LB, 0)])
            inv = np.where(avail, 1 / np.where(avail, vol, 1), 0)
            share = inv / inv.sum() if inv.sum() > 0 else inv
            with np.errstate(invalid="ignore", divide="ignore"):
                score = np.nan_to_num(P[t] / P[max(t - LB, 0)] - 1) - (ccum[t] - ccum[t - LB])
            new_base = np.where(avail & (score > 0), share, 0.0)
        if new_base is not None and (week_end[t] or daily):
            base = new_base
            if week_end[t]:                                    # brakes reset at the weekly rebalance
                frozen[:] = False
                halved = False
                anchor = eq
        if asset_brake:
            frozen |= brake_hits(R, t, k_sigma)                # shared with the live paper (jevbot.research.macro)
        if port_brake and eq / anchor - 1 <= -0.05:
            halved = True
        new_eff = np.where(frozen, 0.0, base) * (0.5 if halved else 1.0)
        if t + 1 < T:
            port[t + 1] -= COST * np.abs(new_eff - eff).sum()
        eff = new_eff
    return port


def main() -> int:
    days, P, cash = align(load())
    d = np.array(days)
    D, T_ = d < SPLIT, d >= SPLIT
    ref, _ = strategy(P, cash, days, (LB,))
    variants = {"v1 (haftalık)": {}, "A varlık freni": {"asset_brake": True}, "B portföy freni": {"port_brake": True},
                "C günlük dengeleme": {"daily": True}, "A+B": {"asset_brake": True, "port_brake": True}}
    res = {}
    for k, kw in variants.items():
        x = run(P, cash, days, **kw)
        res[k] = {"dev": stats(x[D], cash[D]), "test": stats(x[T_], cash[T_])}
    check = stats(ref[T_], cash[T_])
    print("sanity v1 engine vs research.macro.strategy (test):", round(res["v1 (haftalık)"]["test"]["ann"], 5),
          round(check["ann"], 5))
    cands = [k for k in variants if k != "v1 (haftalık)"]
    pick = max(cands, key=lambda k: res[k]["dev"]["sharpe"])
    t = res[pick]["test"]
    res["dev_pick"] = pick
    res["pass"] = bool(t["sharpe"] > 1.09 and t["max_dd"] <= 0.161)
    for nm in ("dev", "test"):
        print(f"\n{nm}: {'varyant':22s} {'yıllık':>8s} {'sharpe':>7s} {'maksDD':>7s}")
        for k in variants:
            s = res[k][nm]
            print(f"     {k:22s} {s['ann'] * 100:7.1f}% {s['sharpe']:7.2f} {s['max_dd'] * 100:6.1f}%")
    print("\nD seçimi:", pick, "| GEÇER:", res["pass"])
    (OUT / "result_brake.json").write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
