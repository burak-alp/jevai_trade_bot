"""Stream names and routed WebSocket URLs for Binance USDⓈ-M Futures.

Nothing here hardcodes a host or route: base URL, route paths and the
stream-family -> route mapping all come from ``WsConfig`` (2026 /public, /market,
/private split). Stream *names* follow the exchange naming scheme.
"""

from __future__ import annotations

from urllib.parse import quote

from jevbot.core.config import WsConfig

MARK_PRICE_ALL = "!markPrice@arr@1s"
FORCE_ORDER_ALL = "!forceOrder@arr"

FAMILIES = ("kline", "bookTicker", "depth", "markPrice", "forceOrder", "aggTrade")


def kline_stream(symbol: str, interval: str = "1m") -> str:
    return f"{symbol.lower()}@kline_{interval}"


def book_ticker_stream(symbol: str) -> str:
    return f"{symbol.lower()}@bookTicker"


def depth_stream(symbol: str, levels: int = 20, speed: str = "500ms") -> str:
    return f"{symbol.lower()}@depth{levels}@{speed}"


_family_cache: dict[str, str] = {}


def family_of(stream: str) -> str:
    """'btcusdt@kline_1m' -> 'kline', '!markPrice@arr@1s' -> 'markPrice', ..."""
    fam = _family_cache.get(stream)
    if fam is not None:
        return fam
    head, _, rest = stream.partition("@")
    token = head[1:] if head.startswith("!") else rest.split("@", 1)[0]
    for f in FAMILIES:
        if token.startswith(f):
            _family_cache[stream] = f
            return f
    raise ValueError(f"unknown stream family: {stream!r}")


def symbol_of(stream: str) -> str | None:
    head = stream.split("@", 1)[0]
    return None if head.startswith("!") else head.upper()


def route_of(stream: str, cfg: WsConfig) -> str:
    fam = family_of(stream)
    try:
        return cfg.stream_routes[fam]
    except KeyError:
        raise ValueError(f"no route configured for stream family {fam!r} (binance.ws.stream_routes)") from None


def route_base_url(route: str, cfg: WsConfig) -> str:
    try:
        path = cfg.routes[route]
    except KeyError:
        raise ValueError(f"unknown ws route {route!r} (binance.ws.routes)") from None
    return cfg.base.rstrip("/") + "/" + path.strip("/")


def combined_stream_url(route: str, streams: list[str], cfg: WsConfig) -> str:
    """Combined-stream URL: ``<base>/<route>/stream?streams=a/b/c``.

    Payloads then arrive wrapped as ``{"stream": name, "data": {...}}``.
    """
    base = route_base_url(route, cfg) + "/stream"
    if not streams:
        return base
    return base + "?streams=" + "/".join(quote(s, safe="@!_") for s in streams)
