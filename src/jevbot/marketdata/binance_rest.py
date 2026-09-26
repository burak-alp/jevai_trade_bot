"""Minimal async REST client for *public* Binance USDⓈ-M market-data endpoints.

No API key, no signed endpoints. Tracks request weight from the
``X-MBX-USED-WEIGHT-1M`` response header and backs off on 429/418.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

import httpx
import orjson

from jevbot.core.config import RestConfig
from jevbot.core.events import Kline, OpenInterest
from jevbot.core.logging import get_logger
from jevbot.core.time import MINUTE_MS, mono_ms, now_ms
from jevbot.marketdata.parsers import parse_open_interest, parse_rest_kline

log = get_logger(__name__)

# Estimated weights (actual usage is read back from the response header).
WEIGHTS = {
    "/fapi/v1/time": 1,
    "/fapi/v1/exchangeInfo": 1,
    "/fapi/v1/ticker/24hr": 40,       # all symbols
    "/fapi/v1/openInterest": 1,
    "/fapi/v1/fundingInfo": 1,
}


def kline_weight(limit: int) -> int:
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


class RestError(Exception):
    def __init__(self, status: int, path: str, body: str) -> None:
        super().__init__(f"HTTP {status} {path}: {body[:200]}")
        self.status = status
        self.path = path


class RateLimited(RestError):
    """429: request rate exceeded; ``retry_after_s`` from header when present."""

    def __init__(self, status: int, path: str, body: str, retry_after_s: float) -> None:
        super().__init__(status, path, body)
        self.retry_after_s = retry_after_s


class IpBanned(RateLimited):
    """418: IP auto-banned after ignoring 429s. Stop polling until retry_after."""


@dataclass
class WeightTracker:
    limit_per_min: int
    budget_frac: float
    used_1m: int = 0
    updated_ms: int = 0
    header_seen: bool = False
    hard_block_until_mono: int = 0
    requests: int = 0
    errors: int = 0
    rate_limit_hits: int = 0
    limits_from_exchange: list[dict[str, Any]] = field(default_factory=list)

    def observe(self, used: int | None) -> None:
        if used is not None:
            self.used_1m = used
            self.header_seen = True
        self.updated_ms = now_ms()

    def current_used(self) -> int:
        # header counts reset on minute boundaries
        if self.updated_ms // MINUTE_MS != now_ms() // MINUTE_MS:
            return 0
        return self.used_1m

    def budget_ok(self, weight: int = 1) -> bool:
        if mono_ms() < self.hard_block_until_mono:
            return False
        return self.current_used() + weight <= self.limit_per_min * self.budget_frac

    def seconds_to_next_minute(self) -> float:
        return (MINUTE_MS - now_ms() % MINUTE_MS) / 1000 + 0.05


class BinanceRest:
    def __init__(self, base_url: str, cfg: RestConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.cfg = cfg
        self.weights = WeightTracker(cfg.weight_limit_per_min, cfg.weight_budget_frac)
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=cfg.timeout_s, transport=transport,
            headers={"User-Agent": "jevbot-recorder/0.1"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def wait_budget(self, weight: int) -> None:
        while not self.weights.budget_ok(weight):
            blocked = self.weights.hard_block_until_mono - mono_ms()
            await asyncio.sleep(max(blocked / 1000, self.weights.seconds_to_next_minute()) if blocked > 0
                                else self.weights.seconds_to_next_minute())

    async def get(self, path: str, params: dict[str, Any] | None = None, weight: int | None = None) -> Any:
        w = weight if weight is not None else WEIGHTS.get(path, 1)
        attempt = 0
        while True:
            await self.wait_budget(w)
            attempt += 1
            self.weights.requests += 1
            try:
                r = await self._client.get(path, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                self.weights.errors += 1
                if attempt > self.cfg.max_retries:
                    raise
                delay = min(30.0, 0.5 * 2 ** (attempt - 1)) * (1 + random.random() * 0.3)
                log.warning("rest_transport_error", path=path, attempt=attempt, err=repr(e), retry_in_s=round(delay, 2))
                await asyncio.sleep(delay)
                continue
            used = r.headers.get("x-mbx-used-weight-1m")
            self.weights.observe(int(used) if used and used.isdigit() else None)
            if r.status_code == 200:
                return orjson.loads(r.content)
            body = r.text
            if r.status_code in (418, 429):
                self.weights.rate_limit_hits += 1
                retry_after = float(r.headers.get("retry-after", "60") or 60)
                self.weights.hard_block_until_mono = mono_ms() + int(retry_after * 1000)
                cls = IpBanned if r.status_code == 418 else RateLimited
                log.error("rest_rate_limited", path=path, status=r.status_code, retry_after_s=retry_after)
                raise cls(r.status_code, path, body, retry_after)
            if r.status_code >= 500 and attempt <= self.cfg.max_retries:
                self.weights.errors += 1
                delay = min(30.0, 0.5 * 2 ** (attempt - 1))
                log.warning("rest_server_error", path=path, status=r.status_code, attempt=attempt)
                await asyncio.sleep(delay)
                continue
            self.weights.errors += 1
            raise RestError(r.status_code, path, body)

    # -- endpoints ----------------------------------------------------------

    async def server_time(self) -> tuple[int, int, int]:
        """Returns (t_send, t_server, t_recv) for clock-offset estimation."""
        t_send = now_ms()
        d = await self.get("/fapi/v1/time")
        t_recv = now_ms()
        return t_send, int(d["serverTime"]), t_recv

    async def exchange_info(self) -> dict[str, Any]:
        info = await self.get("/fapi/v1/exchangeInfo")
        limits = info.get("rateLimits") or []
        self.weights.limits_from_exchange = limits
        for lim in limits:
            if lim.get("rateLimitType") == "REQUEST_WEIGHT" and lim.get("interval") == "MINUTE" \
                    and lim.get("intervalNum") == 1 and isinstance(lim.get("limit"), int):
                if lim["limit"] != self.weights.limit_per_min:
                    log.info("rest_weight_limit_updated", old=self.weights.limit_per_min, new=lim["limit"])
                self.weights.limit_per_min = lim["limit"]
        return info

    async def ticker_24h(self) -> list[dict[str, Any]]:
        return await self.get("/fapi/v1/ticker/24hr")

    async def funding_info(self) -> list[dict[str, Any]]:
        return await self.get("/fapi/v1/fundingInfo")

    async def open_interest(self, symbol: str) -> OpenInterest:
        t_req = now_ms()
        d = await self.get("/fapi/v1/openInterest", {"symbol": symbol})
        return parse_open_interest(d, t_req, now_ms())

    async def klines(self, symbol: str, start_ms: int, end_ms: int, interval: str = "1m",
                     limit: int = 1000) -> list[Kline]:
        """Closed klines with open_time in [start_ms, end_ms]; pages forward as needed."""
        out: list[Kline] = []
        cursor = start_ms
        while cursor <= end_ms:
            rows = await self.get("/fapi/v1/klines", {"symbol": symbol, "interval": interval,
                                                      "startTime": cursor, "endTime": end_ms, "limit": limit},
                                  weight=kline_weight(limit))
            t_recv = now_ms()
            if not rows:
                break
            batch = [parse_rest_kline(symbol, r, t_recv, interval) for r in rows]
            out.extend(k for k in batch if k.close_time < t_recv)     # drop the still-forming bar
            cursor = batch[-1].open_time + 1
            if len(rows) < limit:
                break
        return out
