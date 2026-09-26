"""1m kline continuity tracking and gap detection.

A symbol's closed 1m klines must arrive for consecutive ``open_time`` minutes. Gaps are
found two ways:

* continuity: a WS kline arrives with ``open_time > last + 1m``;
* watchdog: wall clock passed ``close_time + grace`` of a minute we have not received.

Both produce a missing range that the recorder backfills from REST.
"""

from __future__ import annotations

from dataclasses import dataclass

from jevbot.core.time import MINUTE_MS, floor_ms


@dataclass(slots=True)
class _SymState:
    since_open: int                  # first minute we are responsible for
    last_open: int | None = None     # latest closed open_time recorded (ws or rest)
    next_retry_ms: int = 0


class KlineTracker:
    def __init__(self) -> None:
        self._s: dict[str, _SymState] = {}
        self.duplicates = 0

    def symbols(self) -> list[str]:
        return list(self._s)

    def add(self, symbol: str, now: int) -> None:
        if symbol not in self._s:
            # the bar in progress at `now` is the first one we can fully observe
            self._s[symbol] = _SymState(since_open=floor_ms(now, MINUTE_MS))

    def remove(self, symbol: str) -> None:
        self._s.pop(symbol, None)

    def last_open(self, symbol: str) -> int | None:
        st = self._s.get(symbol)
        return st.last_open if st else None

    def on_kline(self, symbol: str, open_time: int) -> tuple[bool, tuple[int, int] | None]:
        """Returns (accept, gap_range). gap_range = (first_missing_open, last_missing_open)."""
        st = self._s.get(symbol)
        if st is None:
            return False, None                       # not a tracked symbol (left the universe)
        if st.last_open is not None and open_time <= st.last_open:
            self.duplicates += 1
            return False, None
        gap = None
        prev = st.last_open if st.last_open is not None else st.since_open - MINUTE_MS
        if open_time > prev + MINUTE_MS and open_time - MINUTE_MS >= st.since_open:
            gap = (max(prev + MINUTE_MS, st.since_open), open_time - MINUTE_MS)
        st.last_open = open_time
        return True, gap

    def mark_filled(self, symbol: str, up_to_open: int) -> None:
        st = self._s.get(symbol)
        if st is not None and (st.last_open is None or up_to_open > st.last_open):
            st.last_open = up_to_open

    @staticmethod
    def expected_last_open(now: int, grace_ms: int) -> int:
        """open_time of the latest minute that must be closed and delivered by ``now``."""
        return floor_ms(now - grace_ms, MINUTE_MS) - MINUTE_MS

    def overdue(self, now: int, grace_ms: int, backfill_after_ms: int) -> list[tuple[str, int, int]]:
        """Symbols whose missing minutes are old enough to backfill: (symbol, first, last)."""
        exp = self.expected_last_open(now, grace_ms)
        out = []
        for sym, st in self._s.items():
            if exp < st.since_open or now < st.next_retry_ms:
                continue
            first = st.since_open if st.last_open is None else st.last_open + MINUTE_MS
            if first > exp:
                continue
            if now - (first + MINUTE_MS) < backfill_after_ms:   # first missing bar closed recently
                continue
            out.append((sym, first, exp))
        return out

    def defer(self, symbol: str, until_ms: int) -> None:
        st = self._s.get(symbol)
        if st is not None:
            st.next_retry_ms = until_ms

    def fresh_count(self, now: int, grace_ms: int) -> int:
        exp = self.expected_last_open(now, grace_ms)
        return sum(1 for st in self._s.values()
                   if exp < st.since_open or (st.last_open is not None and st.last_open >= exp))
