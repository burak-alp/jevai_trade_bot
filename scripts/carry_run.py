"""Run carry.v1 (pre-registered) and the BTC+ETH baseline on the PIT pools; print a markdown summary.
Usage: python scripts/carry_run.py [--end 2026-09-26]
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import orjson

from jevbot.research.carry import CarryConfig, run_carry
from jevbot.research.universe import pit_first_listing

HIST = Path("data/hist/um")
POOLS = ("data/research/pool-2024.json", "data/research/pool-dev.json", "data/research/pool-180d.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-09-26")
    a = ap.parse_args()
    per_day: dict[str, list[str]] = {}
    for p in POOLS:                                        # later pools win on overlapping days
        per_day.update(orjson.loads(Path(p).read_bytes())["per_day"])
    spot = set(orjson.loads(Path("data/research/spot-symbols.json").read_bytes())["spot_usdt"])
    listing = pit_first_listing(HIST)
    cfg = CarryConfig()
    s0, s1 = date.fromisoformat(a.start), date.fromisoformat(a.end)
    res = {"carry.v1": run_carry(HIST, per_day, spot, listing, s0, s1, cfg),
           "carry.base BTC+ETH": run_carry(HIST, per_day, spot, listing, s0, s1, cfg, fixed=["BTCUSDT", "ETHUSDT"])}
    L = [f"# carry.v1 result {a.start} → {a.end} (pre-registered 20260929T-carry-prereg.md)", "",
         "| arm | APR [95 % CI] | 99 % lo | total | max DD | h1 APR | h2 APR | trades | avg hold d | avg pos | pass |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, s in res.items():
        f, h1, h2 = s["full"], s["h1"], s["h2"]
        L.append(f"| {k} | {f['apr']:+.2%} [{f['apr_ci95'][0]:+.2%}, {f['apr_ci95'][1]:+.2%}] | {f['apr_ci99_lo']:+.2%} | "
                 f"{f['total_return']:+.2%} | {f['max_dd']:.2%} | {h1['apr']:+.2%} | {h2['apr']:+.2%} | {s['trades']} | "
                 f"{s['avg_hold_days']} | {s['avg_positions']} | {s['pass']} |")
    L += ["", "| arm | funding | basis | costs | winning trades | rebalances | missing premium days | spot-mapped |",
          "|---|---|---|---|---|---|---|---|"]
    for k, s in res.items():
        L.append(f"| {k} | {s['sum_funding']:+.4f} | {s['sum_basis']:+.4f} | {s['sum_costs']:.4f} | "
                 f"{s['winning_trades']} | {s['rebalances']} | {s['missing_premium_days']} | {s['spot_mapped']} |")
    L.append("\n(funding/basis/costs: sums of per-trade fractions of equity)")
    out = "\n".join(L) + "\n"
    Path("data/research/carry").mkdir(parents=True, exist_ok=True)
    Path("data/research/carry/summary.md").write_text(out, encoding="utf-8")
    Path("data/research/carry/daily.json").write_bytes(orjson.dumps({k: s["daily"] for k, s in res.items()}))
    print(out)


if __name__ == "__main__":
    main()
