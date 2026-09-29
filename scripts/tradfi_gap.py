"""gap.v1 (pre-registered reports/remote/20260929T-tradfi-gap-prereg.md): do Binance stock perps revert at the US open?
Usage: python scripts/tradfi_gap.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import orjson

from jevbot.research.a0 import block_bootstrap_ci
from jevbot.research.data import DAY, MIN, load_symbol
from jevbot.research.universe import pit_first_listing

HIST = Path("data/hist/um")
NY = ZoneInfo("America/New_York")
EXCLUDE = {"XAUUSDT", "XAGUSDT", "XPDUSDT", "XPTUSDT", "PAXGUSDT", "COPPERUSDT", "NATGASUSDT"}
HOLIDAYS = {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
            date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7)}
START, END, SPLIT = date(2026, 1, 28), date(2026, 9, 25), date(2026, 6, 1)
THR, COST = 0.005, 0.0020


def ms(d: date, t: time) -> int:
    return int(datetime.combine(d, t, NY).timestamp() * 1000)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    syms = [s for s in orjson.loads(Path("data/research/pool-tradfi.json").read_bytes())["pool"] if s not in EXCLUDE]
    listing = pit_first_listing(HIST)
    days = [START + timedelta(days=i) for i in range((END - START).days + 1)]
    tdays = [d for d in days if d.weekday() < 5 and d not in HOLIDAYS]
    g0 = int(datetime(2026, 1, 20).timestamp() // 86400 * DAY)
    g1 = g0 + 260 * DAY
    rows = []
    for s in syms:
        b = load_symbol(HIST, s, g0, g1)

        def px(t: int) -> float:
            i = (t - g0) // MIN - 1                       # bar that closes at t
            return float(b.close[i]) if 0 <= i < b.n else float("nan")
        lt = listing.get(s)
        for prev, d in zip(tdays, tdays[1:]):
            tc, to = ms(prev, time(16, 0)), ms(d, time(9, 30))
            if lt is None or tc - lt < 7 * DAY:
                continue
            pc, pp, p30 = px(tc), px(to - 5 * MIN), px(to + 30 * MIN)
            if not (pc > 0 and pp > 0 and p30 > 0):
                continue
            rows.append({"s": s, "day": d, "t": to, "d": np.log(pp / pc), "r": np.log(p30 / pp),
                         "weekend": (d - prev).days > 1})
    dd = np.array([x["d"] for x in rows])
    rr = np.array([x["r"] for x in rows])
    t = np.array([x["t"] for x in rows], dtype=np.int64)
    wk = np.array([x["weekend"] for x in rows])
    blocks = t // DAY // 7

    def slope(m: np.ndarray) -> str:
        if m.sum() < 10:
            return "n<10"
        x, y = dd[m], rr[m]
        b1 = np.cov(x, y)[0, 1] / np.var(x, ddof=1)
        return f"n={m.sum()} slope={b1:+.3f} corr={np.corrcoef(x, y)[0, 1]:+.3f} mean|d|={np.abs(x).mean():.4f}"
    L = ["# gap.v1 result", "", f"symbols {len(syms)}, closed periods {len(rows)}", "",
         "## descriptive: r (open-5m -> open+30m) on d (close -> open-5m)",
         f"- all: {slope(np.ones(len(rows), bool))}", f"- weekend: {slope(wk)}", f"- weeknight: {slope(~wk)}", ""]
    trade = np.abs(dd) >= THR
    for name, sgn in (("reversion (primary)", -1.0), ("continuation (info)", 1.0)):
        net = sgn * np.sign(dd[trade]) * rr[trade] - COST
        tt, bb = t[trade], blocks[trade]
        lo, hi = block_bootstrap_ci(net, bb, 4000, 7)
        lo99, _ = block_bootstrap_ci(net, bb, 4000, 7, level=0.99)
        h1 = net[tt < ms(SPLIT, time(0))]
        h2 = net[tt >= ms(SPLIT, time(0))]
        L.append(f"## {name}: n={len(net)} mean net {net.mean()*1e4:+.1f} bps [95% {lo*1e4:+.1f}, {hi*1e4:+.1f}] "
                 f"99% lo {lo99*1e4:+.1f} | h1 {h1.mean()*1e4:+.1f} (n={len(h1)}) h2 {h2.mean()*1e4:+.1f} (n={len(h2)}) "
                 f"| win {np.mean(net > 0):.3f} | gross {(net + COST).mean()*1e4:+.1f} bps")
        if sgn < 0:
            passed = len(net) >= 200 and lo99 > 0 and h1.mean() > 0 and h2.mean() > 0
            L.append(f"PASS (pre-registered): {passed}")
    out = "\n".join(L) + "\n"
    Path("data/research/tradfi-gap.md").write_text(out, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
