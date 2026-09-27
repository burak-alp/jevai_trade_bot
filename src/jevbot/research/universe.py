"""Point-in-time tradable universe for replay (no look-ahead, bounded survivorship).

* ``daily_universe_mask``: at every decision tick the tradable set is the top-N by trailing
  24 h quote volume measured at the first tick of that UTC day (a daily refresh, as live).
* ``candidate_pool``: which symbols must be downloaded at 1m so that the daily top-N is
  complete. Uses cheap 1d klines of every symbol in the PIT listing (delisted included):
  the pool is the union over days d of the top-N by quote volume of day d-1.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from jevbot.hist.catalog import list_files
from jevbot.research.data import DAY


def daily_universe_mask(vol_rank: np.ndarray, tick_times: np.ndarray, top: int) -> np.ndarray:
    """vol_rank [J x S] (1 = largest, causal per tick); tick_times [J] ms -> bool mask [J x S]."""
    day = tick_times // DAY
    first = np.searchsorted(day, day, side="left")        # index of the first tick of each tick's day
    return vol_rank[first] <= top


def daily_quote_volume(hist_root: Path, start: date, end: date,
                       symbols: set[str] | None = None) -> tuple[list[str], np.ndarray]:
    """(symbols, qv [days x S]) from klines/1d for days [start - 1 d, end); NaN where missing."""
    d0 = _ms(start) - DAY
    n = (_ms(end) - d0) // DAY
    root = Path(hist_root) / "klines" / "1d"
    cols: dict[str, np.ndarray] = {}
    for f in list_files(root, symbols=symbols):
        t = pq.read_table(f, columns=["open_time", "quote_volume"])
        sym = f.parent.name.split("=", 1)[-1]
        ot = t.column("open_time").to_numpy()
        idx = (ot - d0) // DAY
        ok = (idx >= 0) & (idx < n)
        arr = cols.setdefault(sym, np.full(n, np.nan))
        arr[idx[ok]] = t.column("quote_volume").to_numpy(zero_copy_only=False).astype(float)[ok]
    syms = sorted(cols)
    return syms, (np.stack([cols[s] for s in syms], axis=1) if syms else np.zeros((n, 0)))


def candidate_pool(hist_root: Path, start: date, end: date, top: int) -> dict[str, object]:
    syms, qv = daily_quote_volume(hist_root, start, end)
    pool: set[str] = set()
    per_day: dict[str, list[str]] = {}
    for i in range(qv.shape[0] - 1):                       # row i = day d-1 for trading day d = start + i
        v = np.where(np.isnan(qv[i]), -np.inf, qv[i])
        order = [k for k in np.argsort(-v, kind="stable")[:top] if np.isfinite(v[k])]
        names = [syms[k] for k in order]
        per_day[(start + timedelta(days=i)).isoformat()] = names
        pool.update(names)
    missing_days = [d for d, names in per_day.items() if len(names) < top]
    return {"top": top, "start": str(start), "end": str(end), "symbols_seen": len(syms),
            "pool": sorted(pool), "per_day": per_day, "days_short": missing_days}


def pit_first_listing(hist_root: Path) -> dict[str, int]:
    """symbol -> first daily kline date (epoch ms) from the downloader's PIT listing; {} if absent."""
    out: dict[str, int] = {}
    for f in sorted((Path(hist_root) / "_pit").glob("symbol_listing-klines-*.parquet")):
        for r in pq.read_table(f, columns=["symbol", "first_date"]).to_pylist():
            t = _ms(date.fromisoformat(r["first_date"]))
            out[r["symbol"]] = min(out.get(r["symbol"], t), t)
    return out


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
