"""Synthetic Binance-Vision-shaped datasets for research tests (no network)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from jevbot.recorder.integrity import append_manifest, sha256_file, write_sidecar

MIN = 60_000


def _write(root: Path, rel: str, table: pa.Table, symbol: str, period: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, p)
    write_sidecar(p, sha256_file(p))
    ds_root = root / rel.split("/symbol=")[0]
    append_manifest(ds_root, {"file": p.relative_to(ds_root).as_posix(), "rows": table.num_rows,
                              "sha256": sha256_file(p), "symbol": symbol, "period": period, "quality": "ok"})


def make_symbol(root: Path, symbol: str, start: int, minutes: int, seed: int, base: float = 100.0,
                vol: float = 0.0008, drift: float = 0.0, events: list[tuple[int, float, int]] | None = None,
                btc: np.ndarray | None = None, beta: float = 0.0) -> np.ndarray:
    """Random-walk 1m klines (+ mark klines + 8h funding) for one symbol; returns log-return path.

    ``events``: (minute, total_log_move, duration_minutes) injected trends (breakouts).
    """
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, minutes)
    if btc is not None:
        r += beta * btc
    for m0, move, dur in events or []:
        r[m0:m0 + dur] += move / dur
    logp = np.log(base) + np.cumsum(r)
    close = np.exp(logp)
    open_ = np.concatenate([[base], close[:-1]])
    wig = np.abs(rng.normal(0, vol / 2, minutes)) * close
    high = np.maximum(open_, close) + wig
    low = np.minimum(open_, close) - wig
    qv = rng.lognormal(12, 0.5, minutes) * (1 + 3 * (np.abs(r) > 3 * vol))
    tb = qv * np.clip(0.5 + 40 * r, 0.05, 0.95)
    ot = start + MIN * np.arange(minutes, dtype=np.int64)
    kl = pa.table({"open_time": ot, "open": open_, "high": high, "low": low, "close": close, "volume": qv / close,
                   "close_time": ot + MIN - 1, "quote_volume": qv, "count": np.full(minutes, 100),
                   "taker_buy_volume": tb / close, "taker_buy_quote_volume": tb})
    _write(root, f"klines/1m/symbol={symbol}/{symbol}-klines-1m-all.parquet", kl, symbol, "all")
    mk = pa.table({"open_time": ot, "open": open_, "high": high * 0.9999 + low * 0.0001,
                   "low": low * 0.9999 + high * 0.0001, "close": close, "close_time": ot + MIN - 1})
    _write(root, f"markPriceKlines/1m/symbol={symbol}/{symbol}-markPriceKlines-1m-all.parquet", mk, symbol, "all")
    ft = np.arange(start - start % (8 * 3_600_000) + 8 * 3_600_000, start + minutes * MIN, 8 * 3_600_000, dtype=np.int64)
    fr = pa.table({"calc_time": ft, "funding_interval_hours": np.full(len(ft), 8),
                   "last_funding_rate": np.full(len(ft), 0.0001)})
    _write(root, f"fundingRate/symbol={symbol}/{symbol}-fundingRate-all.parquet", fr, symbol, "all")
    return r


def make_metrics(root: Path, symbol: str, create_time: np.ndarray, open_interest: np.ndarray) -> None:
    """``metrics`` rows (5 m open interest); the ratio columns are constant placeholders."""
    n = len(create_time)
    one = np.ones(n)
    t = pa.table({"create_time": np.asarray(create_time, dtype=np.int64), "symbol": [symbol] * n,
                  "sum_open_interest": np.asarray(open_interest, dtype=np.float64),
                  "sum_open_interest_value": np.asarray(open_interest, dtype=np.float64) * 100.0,
                  "count_toptrader_long_short_ratio": one, "sum_toptrader_long_short_ratio": one,
                  "count_long_short_ratio": one, "sum_taker_long_short_vol_ratio": one})
    _write(root, f"metrics/symbol={symbol}/{symbol}-metrics-all.parquet", t, symbol, "all")
