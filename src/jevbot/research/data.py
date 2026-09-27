"""Load downloaded Binance Vision data (``data/hist/um``) onto aligned numpy minute grids.

All arrays of one symbol share the same minute grid ``[start, end)``: index ``m`` is the
1m bar opening at ``start + m * 60 000``. Missing minutes are NaN (never forward-filled
here; consumers decide). Files flagged ``suspect`` by the downloader are excluded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from jevbot.hist.catalog import list_files

MIN = 60_000
DAY = 86_400_000

KLINE_COLS = ("open", "high", "low", "close", "volume", "quote_volume", "taker_buy_quote_volume")


@dataclass
class SymbolBars:
    symbol: str
    start: int                      # epoch ms of minute index 0
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    quote_volume: np.ndarray
    taker_buy_quote: np.ndarray
    mark_high: np.ndarray           # mark-price klines (stop checks); NaN where unavailable
    mark_low: np.ndarray
    first_minute: int               # first minute with data (listing proxy); -1 if none
    listing_time: int | None = None  # PIT listing (epoch ms) when known; overrides the first-minute proxy

    @property
    def n(self) -> int:
        return len(self.close)


def _grid(start: int, end: int) -> int:
    if start % MIN or end % MIN or end <= start:
        raise ValueError("start/end must be minute aligned and end > start")
    return (end - start) // MIN


def _fill(files: list[Path], cols: tuple[str, ...], start: int, m: int) -> dict[str, np.ndarray]:
    out = {c: np.full(m, np.nan) for c in cols}
    for f in files:
        t = pq.read_table(f, columns=["open_time", *cols])
        ot = t.column("open_time").to_numpy()
        idx = (ot - start) // MIN
        ok = (idx >= 0) & (idx < m) & ((ot - start) % MIN == 0)
        if not ok.any():
            continue
        for c in cols:
            out[c][idx[ok]] = t.column(c).to_numpy(zero_copy_only=False).astype(np.float64)[ok]
    return out


def load_symbol(hist_root: Path, symbol: str, start: int, end: int) -> SymbolBars:
    m = _grid(start, end)
    root = Path(hist_root)
    k = _fill(list_files(root / "klines" / "1m", symbols={symbol}), KLINE_COLS, start, m)
    mk = _fill(list_files(root / "markPriceKlines" / "1m", symbols={symbol}), ("high", "low"), start, m)
    valid = np.flatnonzero(~np.isnan(k["close"]))
    return SymbolBars(symbol=symbol, start=start, open=k["open"], high=k["high"], low=k["low"], close=k["close"],
                      volume=k["volume"], quote_volume=k["quote_volume"], taker_buy_quote=k["taker_buy_quote_volume"],
                      mark_high=mk["high"], mark_low=mk["low"], first_minute=int(valid[0]) if len(valid) else -1)


def load_funding(hist_root: Path, symbol: str) -> tuple[np.ndarray, np.ndarray]:
    """Settled funding (calc_time ms, rate) sorted by time; empty arrays if not downloaded."""
    files = list_files(Path(hist_root) / "fundingRate", symbols={symbol})
    ts, rs = [], []
    for f in files:
        t = pq.read_table(f, columns=["calc_time", "last_funding_rate"])
        ts.append(t.column("calc_time").to_numpy())
        rs.append(t.column("last_funding_rate").to_numpy(zero_copy_only=False).astype(np.float64))
    if not ts:
        return np.array([], dtype=np.int64), np.array([], dtype=np.float64)
    t_all, r_all = np.concatenate(ts), np.concatenate(rs)
    order = np.argsort(t_all, kind="stable")
    t_all, r_all = t_all[order], r_all[order]
    keep = np.concatenate([[True], np.diff(t_all) > 0])
    return t_all[keep], r_all[keep]
