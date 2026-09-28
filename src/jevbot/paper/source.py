"""Market data for the paper engine: live Binance REST, or historical files (tests / dry runs).

Both return exactly what the decision and settlement steps need, on the same grids the
historical replay uses, so paper decisions and labels are computed by the replay's code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import numpy as np

from jevbot.research.data import MIN, SymbolBars, load_funding, load_symbol
from jevbot.research.features import resample

HOUR = 60 * MIN
Funding = tuple[np.ndarray, np.ndarray]


class MarketSource(Protocol):
    async def candidates(self, t: int, n: int) -> list[str]: ...
    async def hourly(self, symbol: str, start: int, end: int) -> dict[str, np.ndarray]: ...
    async def funding(self, symbol: str, start: int, end: int) -> Funding: ...
    async def listing_time(self, symbol: str) -> int | None: ...
    async def minute_bars(self, symbol: str, start: int, end: int) -> SymbolBars: ...
    async def spread_bps(self, symbol: str) -> float | None: ...


def _grid(rows: list[tuple[int, float, float, float, float, float]], start: int, end: int,
          step: int) -> dict[str, np.ndarray]:
    n = (end - start) // step
    out = {k: np.full(n, np.nan) for k in ("open", "high", "low", "close", "qv")}
    for ot, o, h, lo, c, qv in rows:
        i = (ot - start) // step
        if 0 <= i < n and (ot - start) % step == 0:
            for k, v in zip(("open", "high", "low", "close", "qv"), (o, h, lo, c, qv)):
                out[k][i] = v
    return out


class HistSource:
    """Historical files (``data/hist/um``) viewed as if live at time ``t``."""

    def __init__(self, hist_root: Path, symbols: list[str], listing: dict[str, int] | None = None) -> None:
        self.root, self.symbols, self.listing = Path(hist_root), list(symbols), listing or {}

    async def candidates(self, t: int, n: int) -> list[str]:
        return self.symbols[:n]

    async def hourly(self, symbol: str, start: int, end: int) -> dict[str, np.ndarray]:
        return resample(load_symbol(self.root, symbol, start, end), 60)

    async def funding(self, symbol: str, start: int, end: int) -> Funding:
        ft, fr = load_funding(self.root, symbol)
        m = (ft < end)
        return ft[m], fr[m]

    async def listing_time(self, symbol: str) -> int | None:
        return self.listing.get(symbol)

    async def minute_bars(self, symbol: str, start: int, end: int) -> SymbolBars:
        return load_symbol(self.root, symbol, start, end)

    async def spread_bps(self, symbol: str) -> float | None:
        return None


class RestSource:
    """Live Binance USD-M public REST (no key). Weight per hourly decision ~ 60 x (5 + 1) + 40."""

    STABLE = {"USDCUSDT", "FDUSDUSDT", "BUSDUSDT", "TUSDUSDT", "USDPUSDT"}

    def __init__(self, rest: Any) -> None:
        self.rest = rest
        self._onboard: dict[str, int] = {}

    async def candidates(self, t: int, n: int) -> list[str]:
        info = await self.rest.exchange_info()
        ok = {s["symbol"] for s in info.get("symbols", [])
              if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
              and s.get("status") == "TRADING" and s["symbol"] not in self.STABLE}
        self._onboard = {s["symbol"]: int(s.get("onboardDate") or 0) for s in info.get("symbols", [])}
        tick = [x for x in await self.rest.ticker_24h() if x.get("symbol") in ok]
        tick.sort(key=lambda x: -float(x.get("quoteVolume") or 0))
        return [x["symbol"] for x in tick[:n]]

    async def hourly(self, symbol: str, start: int, end: int) -> dict[str, np.ndarray]:
        ks = await self.rest.klines(symbol, start, end - 1, interval="1h", limit=1000)
        return _grid([(k.open_time, k.open, k.high, k.low, k.close, k.quote_volume) for k in ks], start, end, HOUR)

    async def funding(self, symbol: str, start: int, end: int) -> Funding:
        rows = await self.rest.get("/fapi/v1/fundingRate", {"symbol": symbol, "startTime": start,
                                                            "endTime": end - 1, "limit": 1000}, weight=1)
        rows = sorted((int(r["fundingTime"]), float(r["fundingRate"])) for r in rows)
        return (np.array([r[0] for r in rows], dtype=np.int64), np.array([r[1] for r in rows], dtype=float))

    async def listing_time(self, symbol: str) -> int | None:
        return self._onboard.get(symbol) or None

    async def minute_bars(self, symbol: str, start: int, end: int) -> SymbolBars:
        ks = await self.rest.klines(symbol, start, end - 1, interval="1m", limit=1500)
        g = _grid([(k.open_time, k.open, k.high, k.low, k.close, k.quote_volume) for k in ks], start, end, MIN)
        mk = await self._mark(symbol, start, end)
        z = np.zeros_like(g["close"])
        valid = np.flatnonzero(~np.isnan(g["close"]))
        return SymbolBars(symbol, start, g["open"], g["high"], g["low"], g["close"], z, g["qv"], z,
                          mk["high"], mk["low"], int(valid[0]) if len(valid) else -1)

    async def _mark(self, symbol: str, start: int, end: int) -> dict[str, np.ndarray]:
        rows: list[tuple[int, float, float, float, float, float]] = []
        cursor = start
        while cursor < end:
            batch = await self.rest.get("/fapi/v1/markPriceKlines", {"symbol": symbol, "interval": "1m",
                                                                     "startTime": cursor, "endTime": end - 1,
                                                                     "limit": 1500}, weight=10)
            if not batch:
                break
            rows += [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), 0.0) for r in batch]
            cursor = int(batch[-1][0]) + MIN
            if len(batch) < 1500:
                break
        return _grid(rows, start, end, MIN)

    async def spread_bps(self, symbol: str) -> float | None:
        d = await self.rest.get("/fapi/v1/ticker/bookTicker", {"symbol": symbol}, weight=2)
        bid, ask = float(d["bidPrice"]), float(d["askPrice"])
        return (ask - bid) / ((ask + bid) / 2) * 1e4 if bid > 0 and ask > 0 else None
