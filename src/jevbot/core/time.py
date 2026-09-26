"""Time helpers. All timestamps in the system are ``int`` milliseconds since the UTC epoch."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

MINUTE_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000


def now_ms() -> int:
    """Wall-clock time (used for ``t_recv`` stamps; must be NTP-disciplined in production)."""
    return time.time_ns() // 1_000_000


def mono_ms() -> int:
    """Monotonic time for measuring intervals/timeouts (never compared with exchange time)."""
    return time.monotonic_ns() // 1_000_000


def floor_ms(t: int, step: int) -> int:
    return t - (t % step)


def ms_to_iso(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def ms_to_date(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class ClockOffsetEstimator:
    """Estimates ``server_time - local_time`` from request/response round trips.

    offset sample = t_server - (t_send + t_recv) / 2. Samples with a round trip above
    ``max_rtt_ms`` are discarded (the midpoint assumption is too loose); accepted samples
    are smoothed with an EWMA.
    """

    alpha: float = 0.2
    max_rtt_ms: int = 500
    offset_ms: float | None = None
    last_rtt_ms: int | None = None
    samples: int = 0
    rejected: int = 0

    def add_sample(self, t_send: int, t_server: int, t_recv: int) -> bool:
        rtt = t_recv - t_send
        self.last_rtt_ms = rtt
        if rtt < 0 or rtt > self.max_rtt_ms:
            self.rejected += 1
            return False
        sample = t_server - (t_send + t_recv) / 2
        self.offset_ms = sample if self.offset_ms is None else (1 - self.alpha) * self.offset_ms + self.alpha * sample
        self.samples += 1
        return True
