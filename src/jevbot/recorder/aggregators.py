"""Stateful reducers between parsed events and sink rows.

All reducers are idempotent w.r.t. duplicated exchange messages (reconnect/rotation
overlap): they drop events whose exchange sequence/time does not advance.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from jevbot.core.events import BookTicker, DepthSnapshot, MarkPrice

Row = tuple


@dataclass(slots=True)
class _Bar:
    sec: int
    o: float
    h: float
    l: float
    c: float
    sp_sum: float
    sp_min: float
    sp_max: float
    n: int
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float
    last_u: int
    t_event: int
    t_recv: int


class BookTicker1s:
    """bookTicker -> 1-second bars of mid and spread keyed by exchange event second."""

    def __init__(self) -> None:
        self._bars: dict[str, _Bar] = {}
        self._last_u: dict[str, int] = {}
        self.duplicates = 0
        self.invalid = 0

    def update(self, e: BookTicker) -> Row | None:
        last = self._last_u.get(e.symbol)
        if last is not None and e.update_id <= last:
            self.duplicates += 1
            return None
        self._last_u[e.symbol] = e.update_id
        if e.bid <= 0 or e.ask <= 0 or e.ask < e.bid:
            self.invalid += 1
            return None
        mid = (e.bid + e.ask) * 0.5
        sp = (e.ask - e.bid) / mid * 1e4
        sec = e.t_event - e.t_event % 1000
        bar = self._bars.get(e.symbol)
        out = None
        if bar is not None and sec > bar.sec:
            out = self._row(e.symbol, bar)
            bar = None
        elif bar is not None and sec < bar.sec:        # late event from an older second: ignore
            return None
        if bar is None:
            self._bars[e.symbol] = _Bar(sec, mid, mid, mid, mid, sp, sp, sp, 1, e.bid, e.ask, e.bid_qty,
                                        e.ask_qty, e.update_id, e.t_event, e.t_recv)
            return out
        bar.h = max(bar.h, mid)
        bar.l = min(bar.l, mid)
        bar.c = mid
        bar.sp_sum += sp
        bar.sp_min = min(bar.sp_min, sp)
        bar.sp_max = max(bar.sp_max, sp)
        bar.n += 1
        bar.bid, bar.ask, bar.bid_qty, bar.ask_qty = e.bid, e.ask, e.bid_qty, e.ask_qty
        bar.last_u, bar.t_event, bar.t_recv = e.update_id, e.t_event, e.t_recv
        return out

    def flush_older_than(self, t_sec_exclusive: int) -> list[Row]:
        """Emit bars whose second started before ``t_sec_exclusive`` (idle symbols)."""
        rows = []
        for sym in [s for s, b in self._bars.items() if b.sec < t_sec_exclusive]:
            rows.append(self._row(sym, self._bars.pop(sym)))
        return rows

    def flush_all(self) -> list[Row]:
        rows = [self._row(s, b) for s, b in self._bars.items()]
        self._bars.clear()
        return rows

    @staticmethod
    def _row(sym: str, b: _Bar) -> Row:
        return (sym, b.sec, b.o, b.h, b.l, b.c, b.sp_sum / b.n, b.sp_min, b.sp_max, b.bid, b.ask,
                b.bid_qty, b.ask_qty, b.n, b.last_u, b.t_event, b.t_recv)


class DepthSampler:
    """Keeps the first depth snapshot of every ``interval_ms`` window per symbol."""

    def __init__(self, interval_ms: int) -> None:
        self.interval_ms = interval_ms
        self._last_window: dict[str, int] = {}
        self._last_u: dict[str, int] = {}
        self.duplicates = 0

    def update(self, e: DepthSnapshot) -> Row | None:
        last = self._last_u.get(e.symbol)
        if last is not None and e.last_update_id <= last:
            self.duplicates += 1
            return None
        self._last_u[e.symbol] = e.last_update_id
        w = e.t_event - e.t_event % self.interval_ms
        if self._last_window.get(e.symbol) == w:
            return None
        self._last_window[e.symbol] = w
        return (e.symbol, e.t_event, e.t_trans, e.t_recv, e.last_update_id,
                list(e.bid_px), list(e.bid_qty), list(e.ask_px), list(e.ask_qty))


class MarkDedup:
    def __init__(self) -> None:
        self._last: dict[str, int] = {}
        self.duplicates = 0

    def accept(self, e: MarkPrice) -> bool:
        last = self._last.get(e.symbol)
        if last is not None and e.t_event <= last:
            self.duplicates += 1
            return False
        self._last[e.symbol] = e.t_event
        return True


class LatencyAggregator:
    """Per-minute distribution of ``t_recv - t_event`` per (family, route).

    Uses reservoir sampling (``max_samples`` per key and minute) to bound memory.
    """

    def __init__(self, max_samples: int = 4000, seed: int = 7) -> None:
        self.max_samples = max_samples
        self._rng = random.Random(seed)
        self._minute: int | None = None
        self._data: dict[tuple[str, str], tuple[list[float], list[int]]] = {}

    def add(self, family: str, route: str, t_event: int, t_recv: int) -> list[Row]:
        m = t_recv - t_recv % 60_000
        out: list[Row] = []
        if self._minute is not None and m != self._minute:
            out = self.flush()
        self._minute = m
        key = (family, route)
        entry = self._data.get(key)
        if entry is None:
            entry = ([], [0])
            self._data[key] = entry
        samples, count = entry
        count[0] += 1
        lag = float(t_recv - t_event)
        if len(samples) < self.max_samples:
            samples.append(lag)
        else:
            j = self._rng.randrange(count[0])
            if j < self.max_samples:
                samples[j] = lag
        return out

    def flush(self) -> list[Row]:
        rows: list[Row] = []
        if self._minute is None:
            return rows
        for (fam, route), (samples, count) in sorted(self._data.items()):
            if not samples:
                continue
            s = sorted(samples)
            n = len(s)

            def q(p: float) -> float:
                return s[min(n - 1, int(round(p * (n - 1))))]
            rows.append((self._minute, fam, route, count[0], n, s[0], q(0.5), q(0.9), q(0.95), q(0.99), s[-1]))
        self._data.clear()
        return rows

    def snapshot_p99(self) -> dict[str, float]:
        out = {}
        for (fam, route), (samples, _) in self._data.items():
            if samples:
                s = sorted(samples)
                out[f"{fam}/{route}"] = s[min(len(s) - 1, int(0.99 * (len(s) - 1)))]
        return out


class IntervalQuantiles:
    """Reservoir-sampled distribution over one reporting interval; ``take()`` resets it."""

    def __init__(self, max_samples: int = 20_000, seed: int = 11) -> None:
        self.max_samples = max_samples
        self._rng = random.Random(seed)
        self._s: list[float] = []
        self._n = 0
        self._max = float("-inf")

    def add(self, v: float) -> None:
        self._n += 1
        if v > self._max:
            self._max = v
        if len(self._s) < self.max_samples:
            self._s.append(v)
        else:
            j = self._rng.randrange(self._n)
            if j < self.max_samples:
                self._s[j] = v

    def take(self) -> dict[str, float]:
        s = sorted(self._s)
        n = len(s)

        def q(p: float) -> float:
            return s[min(n - 1, int(round(p * (n - 1))))] if n else float("nan")
        out = {"n": self._n, "p50": q(0.5), "p95": q(0.95), "p99": q(0.99),
               "max": self._max if n else float("nan")}
        self._s, self._n, self._max = [], 0, float("-inf")
        return out
