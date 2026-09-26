"""Binance USDⓈ-M payload -> normalized event parsers with schema validation.

Every parser raises ``SchemaError`` on a missing/mistyped field; callers count these
and never let a malformed message reach the sinks.
"""

from __future__ import annotations

from typing import Any, Callable

import orjson

from jevbot.core.events import (
    BookTicker,
    DepthSnapshot,
    ForceOrder,
    Kline,
    MarkPrice,
    OpenInterest,
    SchemaError,
)
from jevbot.marketdata.endpoints import family_of


def _f(d: dict[str, Any], k: str) -> float:
    try:
        return float(d[k])
    except (KeyError, TypeError, ValueError) as e:
        raise SchemaError(f"field {k!r}: {e}") from None


def _i(d: dict[str, Any], k: str) -> int:
    v = d.get(k)
    if type(v) is not int:
        raise SchemaError(f"field {k!r}: expected int, got {v!r}")
    return v


def _s(d: dict[str, Any], k: str) -> str:
    v = d.get(k)
    if not isinstance(v, str) or not v:
        raise SchemaError(f"field {k!r}: expected non-empty str, got {v!r}")
    return v


def _obj(v: Any, what: str) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise SchemaError(f"{what}: expected object, got {type(v).__name__}")
    return v


# ---------------------------------------------------------------------------
# stream payloads


def parse_kline(data: dict[str, Any], t_recv: int) -> Kline:
    data = _obj(data, "kline")
    if data.get("e") != "kline":
        raise SchemaError(f"kline: unexpected e={data.get('e')!r}")
    k = _obj(data.get("k"), "kline.k")
    x = k.get("x")
    if type(x) is not bool:
        raise SchemaError("kline: field 'x' must be bool")
    return Kline(
        symbol=_s(data, "s"), interval=_s(k, "i"),
        open_time=_i(k, "t"), close_time=_i(k, "T"),
        open=_f(k, "o"), high=_f(k, "h"), low=_f(k, "l"), close=_f(k, "c"),
        volume=_f(k, "v"), quote_volume=_f(k, "q"), n_trades=_i(k, "n"),
        taker_buy_base=_f(k, "V"), taker_buy_quote=_f(k, "Q"),
        closed=x, t_event=_i(data, "E"), t_recv=t_recv, source="ws",
    )


def parse_mark_price_item(d: dict[str, Any], t_recv: int) -> MarkPrice:
    d = _obj(d, "markPrice")
    if d.get("e") != "markPriceUpdate":
        raise SchemaError(f"markPrice: unexpected e={d.get('e')!r}")
    return MarkPrice(
        symbol=_s(d, "s"), t_event=_i(d, "E"), mark=_f(d, "p"), index=_f(d, "i"),
        est_settle=_f(d, "P"), funding_rate=_f(d, "r"), next_funding_time=_i(d, "T"), t_recv=t_recv,
    )


def parse_mark_price_arr(data: Any, t_recv: int) -> list[MarkPrice]:
    if isinstance(data, dict):          # single-symbol stream variant
        return [parse_mark_price_item(data, t_recv)]
    if not isinstance(data, list):
        raise SchemaError("markPrice@arr: expected list")
    return [parse_mark_price_item(d, t_recv) for d in data]


def parse_book_ticker(data: dict[str, Any], t_recv: int) -> BookTicker:
    data = _obj(data, "bookTicker")
    if data.get("e", "bookTicker") != "bookTicker":
        raise SchemaError(f"bookTicker: unexpected e={data.get('e')!r}")
    bid, ask = _f(data, "b"), _f(data, "a")
    return BookTicker(
        symbol=_s(data, "s"), update_id=_i(data, "u"), t_event=_i(data, "E"), t_trans=_i(data, "T"),
        bid=bid, bid_qty=_f(data, "B"), ask=ask, ask_qty=_f(data, "A"), t_recv=t_recv,
    )


def _levels(v: Any, what: str) -> tuple[tuple[float, ...], tuple[float, ...]]:
    if not isinstance(v, list):
        raise SchemaError(f"{what}: expected list")
    try:
        px = tuple(float(lv[0]) for lv in v)
        qty = tuple(float(lv[1]) for lv in v)
    except (TypeError, ValueError, IndexError) as e:
        raise SchemaError(f"{what}: {e}") from None
    return px, qty


def parse_depth(data: dict[str, Any], t_recv: int) -> DepthSnapshot:
    data = _obj(data, "depth")
    if data.get("e") != "depthUpdate":
        raise SchemaError(f"depth: unexpected e={data.get('e')!r}")
    bpx, bq = _levels(data.get("b"), "depth.b")
    apx, aq = _levels(data.get("a"), "depth.a")
    return DepthSnapshot(
        symbol=_s(data, "s"), t_event=_i(data, "E"), t_trans=_i(data, "T"), last_update_id=_i(data, "u"),
        bid_px=bpx, bid_qty=bq, ask_px=apx, ask_qty=aq, t_recv=t_recv,
    )


def parse_force_order(data: dict[str, Any], t_recv: int) -> ForceOrder:
    data = _obj(data, "forceOrder")
    if data.get("e") != "forceOrder":
        raise SchemaError(f"forceOrder: unexpected e={data.get('e')!r}")
    o = _obj(data.get("o"), "forceOrder.o")
    return ForceOrder(
        symbol=_s(o, "s"), t_event=_i(data, "E"), t_trade=_i(o, "T"), side=_s(o, "S"),
        order_type=_s(o, "o"), time_in_force=_s(o, "f"), qty=_f(o, "q"), price=_f(o, "p"),
        avg_price=_f(o, "ap"), status=_s(o, "X"), last_filled_qty=_f(o, "l"),
        filled_acc_qty=_f(o, "z"), t_recv=t_recv,
    )


# ---------------------------------------------------------------------------
# REST payloads


def parse_open_interest(d: Any, t_req: int, t_recv: int) -> OpenInterest:
    d = _obj(d, "openInterest")
    return OpenInterest(symbol=_s(d, "symbol"), open_interest=_f(d, "openInterest"),
                        t_server=_i(d, "time"), t_req=t_req, t_recv=t_recv)


def parse_rest_kline(symbol: str, row: Any, t_recv: int, interval: str = "1m") -> Kline:
    """REST /fapi/v1/klines row: [openTime, o, h, l, c, v, closeTime, q, n, V, Q, ignore]."""
    if not isinstance(row, list) or len(row) < 11:
        raise SchemaError("rest kline: expected list with >= 11 items")
    try:
        return Kline(
            symbol=symbol, interval=interval, open_time=int(row[0]), close_time=int(row[6]),
            open=float(row[1]), high=float(row[2]), low=float(row[3]), close=float(row[4]),
            volume=float(row[5]), quote_volume=float(row[7]), n_trades=int(row[8]),
            taker_buy_base=float(row[9]), taker_buy_quote=float(row[10]),
            closed=True, t_event=int(row[6]), t_recv=t_recv, source="rest",
        )
    except (TypeError, ValueError) as e:
        raise SchemaError(f"rest kline: {e}") from None


# ---------------------------------------------------------------------------
# combined-stream envelope


PARSERS: dict[str, Callable[[Any, int], Any]] = {
    "kline": parse_kline,
    "markPrice": parse_mark_price_arr,
    "bookTicker": parse_book_ticker,
    "depth": parse_depth,
    "forceOrder": parse_force_order,
}


class Envelope:
    """Result of decoding one WebSocket frame."""

    __slots__ = ("kind", "stream", "family", "data", "control_id", "error")

    def __init__(self, kind: str, stream: str | None = None, family: str | None = None, data: Any = None,
                 control_id: int | None = None, error: Any = None) -> None:
        self.kind = kind            # "data" | "control"
        self.stream = stream
        self.family = family
        self.data = data
        self.control_id = control_id
        self.error = error


def decode_frame(raw: bytes | str) -> Envelope:
    try:
        msg = orjson.loads(raw)
    except orjson.JSONDecodeError as e:
        raise SchemaError(f"invalid json: {e}") from None
    if not isinstance(msg, dict):
        raise SchemaError("frame: expected object")
    stream = msg.get("stream")
    if stream is not None:
        if not isinstance(stream, str):
            raise SchemaError("frame.stream: expected str")
        try:
            fam = family_of(stream)
        except ValueError as e:
            raise SchemaError(str(e)) from None
        return Envelope("data", stream=stream, family=fam, data=msg.get("data"))
    if "id" in msg and ("result" in msg or "error" in msg):
        return Envelope("control", control_id=msg.get("id"), data=msg.get("result"), error=msg.get("error"))
    raise SchemaError("frame: neither combined-stream data nor control reply")


def parse_payload(env: Envelope, t_recv: int) -> Any:
    parser = PARSERS.get(env.family or "")
    if parser is None:
        raise SchemaError(f"no parser for family {env.family!r}")
    return parser(env.data, t_recv)
