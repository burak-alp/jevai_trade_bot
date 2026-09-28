"""hlp.v1 analysis (pre-registered 20260929T-hl-persistence-prereg.md): does Hyperliquid trader ROI persist?
Usage: python scripts/hl_persistence.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import orjson

OUT = Path("data/hl")
DAY = 86_400_000
P = [("2026-03-01", "2026-06-01"), ("2026-06-01", "2026-09-01")]


def ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def at(series: list, t: int) -> float:
    if not series:
        return float("nan")
    ts = np.array([x[0] for x in series], dtype=np.int64)
    i = int(np.argmin(np.abs(ts - t)))
    return float(series[i][1]) if abs(int(ts[i]) - t) <= 5 * DAY else float("nan")


def roi(pf: dict, a: str, b: str) -> float:
    ph, av = pf.get("pnlHistory", []), pf.get("accountValueHistory", [])
    t0, t1 = ms(a), ms(b)
    pnl = at(ph, t1) - at(ph, t0)
    vals = [float(v) for t, v in av if t0 <= t <= t1]
    base = float(np.mean(vals)) if vals else float("nan")
    return pnl / base if base >= 1000 else float("nan")


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    rx, ry = x.argsort().argsort(), y.argsort().argsort()
    return float(np.corrcoef(rx, ry)[0, 1])


def boot(fn, n: int, B: int = 4000, seed: int = 7) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    v = [fn(rng.integers(0, n, n)) for _ in range(B)]
    return float(np.percentile(v, 0.5)), float(np.percentile(v, 99.5))


def analyse(users: list[str], port: dict, turnover: dict, label: str) -> list[str]:
    r1, r2, tv = [], [], []
    for u in users:
        pf = port.get(u, {}).get("perpAllTime")
        if not pf:
            continue
        a, b = roi(pf, *P[0]), roi(pf, *P[1])
        if np.isfinite(a) and np.isfinite(b):
            r1.append(a); r2.append(b); tv.append(turnover.get(u, np.nan))
    r1, r2, tv = np.array(r1), np.array(r2), np.array(tv)
    n = len(r1)
    L = [f"## {label}: {n} hesap (iki dönemde de ROI hesaplanabilen)"]
    if n < 50:
        return L + ["yetersiz örnek"]
    r2w = np.clip(r2, -1, 5)                                    # one exploded account must not drive the mean
    top = r1 >= np.quantile(r1, 0.9)
    lo, hi = boot(lambda i: r2w[i][top[i]].mean() if top[i].any() else np.nan, n)
    s = spearman(r1, r2)
    slo, shi = boot(lambda i: spearman(r1[i], r2[i]), n, B=1000)
    L += [f"- P1 üst ondalık ({top.sum()} hesap) P2 ROI ort. {r2w[top].mean():+.3f} (99 % CI [{lo:+.3f}, {hi:+.3f}]); "
          f"medyan {np.median(r2[top]):+.3f}; P2'de kârlı payı {np.mean(r2[top] > 0):.2f}",
          f"- tüm hesaplar P2 ROI medyan {np.median(r2):+.3f}, kârlı payı {np.mean(r2 > 0):.2f}",
          f"- Spearman(P1, P2) {s:+.3f} (99 % CI [{slo:+.3f}, {shi:+.3f}])"]
    ok = np.isfinite(tv)
    if ok.sum() > 150:
        q = np.quantile(tv[ok], [1 / 3, 2 / 3])
        for nm, m in (("düşük ciro", tv <= q[0]), ("orta ciro", (tv > q[0]) & (tv <= q[1])), ("yüksek ciro", tv > q[1])):
            m = m & ok
            if m.sum() > 30:
                tt = m & (r1 >= np.quantile(r1[m], 0.9))
                L.append(f"- {nm} ({m.sum()}): Spearman {spearman(r1[m], r2[m]):+.3f}; üst ondalık P2 ROI ort. "
                         f"{r2w[tt].mean():+.3f}, kârlı payı {np.mean(r2[tt] > 0):.2f}")
    return L


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    s = orjson.loads((OUT / "sample.json").read_bytes())
    port = {}
    for line in (OUT / "portfolio.jsonl").read_bytes().splitlines():
        if line.strip():
            d = orjson.loads(line)
            port[d["user"]] = d["portfolio"]
    rows = orjson.loads((OUT / "leaderboard.json").read_bytes())["leaderboardRows"]
    turnover = {}
    for r in rows:
        w = dict(r["windowPerformances"]).get("allTime", {})
        av = float(r.get("accountValue") or 0)
        if av > 0:
            turnover[r["ethAddress"]] = float(w.get("vlm") or 0) / av
    L = [f"# hlp.v1 — Hyperliquid kalıcılık (P1 {P[0][0]}→{P[0][1]}, P2 {P[1][0]}→{P[1][1]})", "",
         f"çerçeve: {s['frame']}; çekilen portföy: {len(port)}", ""]
    L += analyse(s["random"], port, turnover, "rastgele örneklem (birincil)") + [""]
    L += analyse(s["top_pnl"], port, turnover, "görünür kazananlar (allTime PnL ilk 500)")
    out = "\n".join(L) + "\n"
    (OUT / "summary.md").write_text(out, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
