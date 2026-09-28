"""Signal-decay diagnostic for scanner proposals: forward return in R at fixed horizons.

Independent of TP/SL/horizon choices: for every proposal, entry at the open of the first
minute after the decision (as the labeler), gross forward R at horizon h =
side * (close of minute h - entry) / stop_dist. If no horizon shows gross drift above the
round-trip cost, the setup carries no directional information worth trading; if some
horizon does, geometry (horizon / barriers) is the thing to redesign, not the signal.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import orjson
import pyarrow.parquet as pq

from jevbot.research.a0 import block_bootstrap_ci
from jevbot.research.data import DAY, MIN, load_symbol

HORIZONS_MIN = (15, 30, 60, 120, 240, 480, 1440, 2880)


def forward_r(props: list[dict[str, Any]], hist_root: Path, horizons: tuple[int, ...] = HORIZONS_MIN,
              chunk_days: int = 30) -> np.ndarray:
    """[n_props x n_horizons] gross forward R (NaN where bars are missing)."""
    out = np.full((len(props), len(horizons)), np.nan)
    hmax = max(horizons)
    by_sym: dict[str, list[int]] = defaultdict(list)
    for i, p in enumerate(props):
        by_sym[p["symbol"]].append(i)
    for sym, idx in by_sym.items():
        idx.sort(key=lambda i: props[i]["t_decision"])
        k = 0
        while k < len(idx):                                  # bounded memory: one time chunk per load
            t0 = props[idx[k]]["t_decision"] // MIN * MIN
            t1 = t0 + chunk_days * DAY
            grp = []
            while k < len(idx) and props[idx[k]]["t_decision"] < t1:
                grp.append(idx[k])
                k += 1
            b = load_symbol(hist_root, sym, t0, t1 + (hmax + 1) * MIN)
            for i in grp:
                p = props[i]
                m0 = (p["t_decision"] - b.start) // MIN
                if not 0 <= m0 < b.n or not np.isfinite(b.open[m0]) or not p["stop_dist"] > 0:
                    continue
                entry = b.open[m0]
                for j, h in enumerate(horizons):
                    m = m0 + h - 1
                    if m < b.n:
                        out[i, j] = p["side"] * (b.close[m] - entry) / p["stop_dist"]
    return out


def drift_table(props: list[dict[str, Any]], fr: np.ndarray, horizons: tuple[int, ...] = HORIZONS_MIN,
                n_boot: int = 1000, seed: int = 7) -> dict[str, Any]:
    groups: dict[str, list[int]] = {"all": list(range(len(props)))}
    for i, p in enumerate(props):
        groups.setdefault(f"{p['family']}_{'long' if p['side'] > 0 else 'short'}", []).append(i)
    cost = np.array([p.get("cost_r", np.nan) for p in props], float)
    days = np.array([p["t_decision"] // DAY for p in props])
    res: dict[str, Any] = {}
    for g, idx in groups.items():
        rows = []
        ix = np.array(idx)
        for j, h in enumerate(horizons):
            v = fr[ix, j]
            ok = np.isfinite(v)
            if ok.sum() < 2:
                rows.append({"h_min": h, "n": int(ok.sum())})
                continue
            lo, hi = block_bootstrap_ci(v[ok], days[ix][ok], n_boot, seed)
            rows.append({"h_min": h, "n": int(ok.sum()), "mean_gross_r": round(float(v[ok].mean()), 4),
                         "ci95": [round(lo, 4), round(hi, 4)],
                         "median_cost_r": round(float(np.nanmedian(cost[ix][ok])), 4),
                         "hit_gt_cost": round(float((v[ok] > cost[ix][ok]).mean()), 4)})
        res[g] = rows
    return res


def run_drift(proposals: Path, hist_root: Path, out_dir: Path, selected_only: bool = True) -> dict[str, Any]:
    t = pq.read_table(proposals, columns=None).to_pylist()
    props = [p for p in t if p.get("exit_type") in ("TP", "SL", "TIME") and (p.get("selected") or not selected_only)]
    fr = forward_r(props, hist_root)
    summary = {"proposals": str(proposals), "n": len(props), "horizons_min": list(HORIZONS_MIN),
               "note": "gross forward R from next-minute open; edge needs CI lower bound > median cost R",
               "groups": drift_table(props, fr)}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "drift.json").write_bytes(orjson.dumps(summary, option=orjson.OPT_INDENT_2))
    (out_dir / "drift.md").write_text(to_markdown(summary), encoding="utf-8")
    return summary


def to_markdown(s: dict[str, Any]) -> str:
    hs = s["horizons_min"]
    L = [f"# Signal decay (gross forward R) · n {s['n']}", "", s["note"], "",
         "| group | " + " | ".join(f"{h}m" for h in hs) + " | cost R |", "|" + "---|" * (len(hs) + 2)]
    for g, rows in s["groups"].items():
        cells = [f"{r['mean_gross_r']:+.3f} [{r['ci95'][0]:+.2f},{r['ci95'][1]:+.2f}]" if "mean_gross_r" in r else "-"
                 for r in rows]
        cost = next((r["median_cost_r"] for r in rows if "median_cost_r" in r), float("nan"))
        L.append(f"| {g} (n={rows[0]['n']}) | " + " | ".join(cells) + f" | {cost:.3f} |")
    return "\n".join(L) + "\n"
