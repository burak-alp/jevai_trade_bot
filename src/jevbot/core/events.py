"""Normalized market-data events produced by the Binance adapter.

Field conventions: ``t_event`` = exchange event time (``E``), ``t_trans`` = exchange
transaction/match time (``T``) when provided, ``t_recv`` = local wall-clock receive time.
Prices and quantities are floats here (market data only; order prices use Decimal later).
"""

from __future__ import annotations

from dataclasses import dataclass


class SchemaError(ValueError):
    """A message did not match the expected exchange schema."""


@dataclass(frozen=True, slots=True)
class Kline:
    symbol: str
    interval: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    n_trades: int
    taker_buy_base: float
    taker_buy_quote: float
    closed: bool
    t_event: int
    t_recv: int
    source: str = "ws"        # "ws" | "rest"


@dataclass(frozen=True, slots=True)
class MarkPrice:
    symbol: str
    t_event: int
    mark: float
    index: float
    est_settle: float
    funding_rate: float
    next_funding_time: int
    t_recv: int


@dataclass(frozen=True, slots=True)
class BookTicker:
    symbol: str
    update_id: int
    t_event: int
    t_trans: int
    bid: float
    bid_qty: float
    ask: float
    ask_qty: float
    t_recv: int


@dataclass(frozen=True, slots=True)
class DepthSnapshot:
    symbol: str
    t_event: int
    t_trans: int
    last_update_id: int
    bid_px: tuple[float, ...]
    bid_qty: tuple[float, ...]
    ask_px: tuple[float, ...]
    ask_qty: tuple[float, ...]
    t_recv: int


@dataclass(frozen=True, slots=True)
class ForceOrder:
    symbol: str
    t_event: int
    t_trade: int
    side: str
    order_type: str
    time_in_force: str
    qty: float
    price: float
    avg_price: float
    status: str
    last_filled_qty: float
    filled_acc_qty: float
    t_recv: int


@dataclass(frozen=True, slots=True)
class OpenInterest:
    symbol: str
    open_interest: float
    t_server: int
    t_req: int
    t_recv: int


MarketEvent = Kline | MarkPrice | BookTicker | DepthSnapshot | ForceOrder | OpenInterest
