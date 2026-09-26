"""Local fake of the Binance USDⓈ-M public market-data API (WS + REST) for tests and load runs.

* WS: routed combined streams ``/<route>/stream?streams=...`` and SUBSCRIBE/UNSUBSCRIBE.
  A stream requested on the wrong route is accepted but never receives data (like Binance
  after the 2026 route split).
* REST: time, exchangeInfo, ticker/24hr, openInterest, klines (deterministic, consistent
  with the WS klines so backfill can be checked).
* Admin (HTTP): ``POST /__admin/drop`` (close all sockets), ``/__admin/silence?s=``
  (stop sending without closing), ``/__admin/skip_klines?symbol=&n=`` (drop the next n closed
  klines of a symbol), ``GET /__admin/stats``.

Data is synthetic; message *rates* are configurable to approximate production load.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import orjson
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

MINUTE = 60_000
ROUTE_OF = {"bookTicker": "public", "depth": "public", "kline": "market", "markPrice": "market",
            "forceOrder": "market", "aggTrade": "market"}


def _now() -> int:
    return time.time_ns() // 1_000_000


def _h(*parts: Any) -> float:
    d = hashlib.blake2b(repr(parts).encode(), digest_size=8).digest()
    return int.from_bytes(d, "big") / 2**64


def _family(stream: str) -> str | None:
    head, _, rest = stream.partition("@")
    token = head[1:] if head.startswith("!") else rest.split("@", 1)[0]
    for f in ROUTE_OF:
        if token.startswith(f):
            return f
    return None


@dataclass
class FakeConfig:
    n_symbols: int = 200
    n_extra_symbols: int = 40             # listed but outside the volume filter / non-TRADING
    book_rate_total: float = 1500.0       # bookTicker msgs/s across all subscribed symbols
    kline_update_ms: int = 250
    depth_ms: int = 500
    liquidation_rate: float = 0.5         # forceOrder msgs/s
    ws_host: str = "127.0.0.1"
    ws_port: int = 0
    http_port: int = 0
    weight_limit: int = 2400
    reuse_port: bool = False              # several fake processes can share the ports (load tests)
    control_file: str | None = None       # JSON {"book_rate_total", "drop_epoch", "silence_until"} polled 5x/s
    stats_file: str | None = None         # per-process counters written every second


@dataclass(eq=False)
class _Client:
    ws: ServerConnection
    route: str
    streams: set[str] = field(default_factory=set)


class FakeBinance:
    def __init__(self, cfg: FakeConfig | None = None) -> None:
        self.cfg = cfg or FakeConfig()
        c = self.cfg
        self.symbols = [f"S{i:03d}USDT" for i in range(c.n_symbols)]
        self.symbols[:2] = ["BTCUSDT", "ETHUSDT"]
        self.extra = [f"X{i:03d}USDT" for i in range(c.n_extra_symbols)]
        self.base_px = {s: 10 ** (1 + 3 * _h("px", s)) for s in self.symbols + self.extra}
        self.base_px["BTCUSDT"], self.base_px["ETHUSDT"] = 60_000.0, 3_000.0
        self.book_weight = {s: (30.0 if s == "BTCUSDT" else 20.0 if s == "ETHUSDT" else 0.5 + 3 * _h("bw", s))
                            for s in self.symbols}
        self.clients: set[_Client] = set()
        self.silence_until = 0
        self.skip_klines: dict[str, int] = {}
        self.stats: dict[str, int] = {"ws_msgs": 0, "rest_requests": 0, "ws_connections": 0, "subscribes": 0,
                                      "wrong_route_streams": 0}
        self._update_id = 1
        self._drop_epoch = 0
        self.total_book_weight = sum(self.book_weight.values())
        self._spread = {s: 0.00005 + 0.0002 * _h("sp", s) for s in self.symbols}
        self._mid_cache: dict[str, tuple[int, float]] = {}
        self.stats["book_msgs"] = 0
        self._tasks: list[asyncio.Task[Any]] = []
        self._ws_server: Any = None
        self._http_server: asyncio.base_events.Server | None = None
        self._weight_used = 0
        self._weight_minute = 0
        self.ws_port = 0
        self.http_port = 0

    # -- deterministic market -------------------------------------------------

    def price(self, sym: str, t: int) -> float:
        b = self.base_px[sym]
        x = t / 1000
        return b * (1 + 0.004 * math.sin(x / 97 + 10 * _h("ph", sym)) + 0.002 * math.sin(x / 13.7)
                    + 0.0005 * (_h("n", sym, t // 250) - 0.5))

    def kline(self, sym: str, open_time: int) -> dict[str, Any]:
        pts = [self.price(sym, open_time + i * 5000) for i in range(12)]
        vol = 50 + 500 * _h("v", sym, open_time)
        close = pts[-1]
        return {"t": open_time, "T": open_time + MINUTE - 1, "o": pts[0], "h": max(pts), "l": min(pts), "c": close,
                "v": vol, "q": vol * close, "n": int(100 + 900 * _h("n", sym, open_time)),
                "V": vol * 0.52, "Q": vol * 0.52 * close}

    @staticmethod
    def _fmt(x: float) -> str:
        return f"{x:.8f}"

    def rest_kline_row(self, sym: str, open_time: int) -> list[Any]:
        k = self.kline(sym, open_time)
        f = self._fmt
        return [k["t"], f(k["o"]), f(k["h"]), f(k["l"]), f(k["c"]), f(k["v"]), k["T"], f(k["q"]), k["n"],
                f(k["V"]), f(k["Q"]), "0"]

    # -- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        rp = self.cfg.reuse_port or None
        self._ws_server = await serve(self._ws_handler, self.cfg.ws_host, self.cfg.ws_port, max_queue=256,
                                      compression=None, reuse_port=rp)
        self.ws_port = self._ws_server.sockets[0].getsockname()[1]
        self._http_server = await asyncio.start_server(self._http_handler, self.cfg.ws_host, self.cfg.http_port,
                                                       reuse_port=rp)
        self.http_port = self._http_server.sockets[0].getsockname()[1]
        loops = [self._book_loop(), self._kline_loop(), self._mark_loop(), self._depth_loop(),
                 self._liquidation_loop()]
        if self.cfg.control_file:
            loops.append(self._control_loop())
        if self.cfg.stats_file:
            loops.append(self._stats_loop())
        for coro in loops:
            self._tasks.append(asyncio.create_task(coro))

    async def _control_loop(self) -> None:
        from pathlib import Path

        path = Path(self.cfg.control_file or "")
        while True:
            await asyncio.sleep(0.2)
            try:
                ctl = orjson.loads(path.read_bytes())
            except (OSError, orjson.JSONDecodeError):
                continue
            self.cfg.book_rate_total = float(ctl.get("book_rate_total", self.cfg.book_rate_total))
            self.silence_until = int(ctl.get("silence_until", self.silence_until))
            epoch = int(ctl.get("drop_epoch", 0))
            if epoch > self._drop_epoch:
                self._drop_epoch = epoch
                await self.drop_all()

    async def _stats_loop(self) -> None:
        import os
        from pathlib import Path

        path = Path(self.cfg.stats_file or "")
        while True:
            await asyncio.sleep(1.0)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(orjson.dumps({"t": _now(), "pid": os.getpid(), **self.stats,
                                          "clients": len(self.clients)}))
            os.replace(tmp, path)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
        if self._http_server is not None:
            self._http_server.close()
            await self._http_server.wait_closed()

    @property
    def ws_base(self) -> str:
        return f"ws://{self.cfg.ws_host}:{self.ws_port}"

    @property
    def rest_base(self) -> str:
        return f"http://{self.cfg.ws_host}:{self.http_port}"

    async def drop_all(self) -> None:
        for c in list(self.clients):
            await c.ws.close(code=1001, reason="fake drop")

    # -- websocket ------------------------------------------------------------------

    async def _ws_handler(self, ws: ServerConnection) -> None:
        parts = urlsplit(ws.request.path)
        segs = [p for p in parts.path.split("/") if p]
        if len(segs) != 2 or segs[0] not in ("public", "market", "private") or segs[1] != "stream":
            await ws.close(code=1008, reason="unrouted path")
            return
        client = _Client(ws, segs[0])
        qs = parse_qs(parts.query)
        if "streams" in qs:
            client.streams.update(s for s in qs["streams"][0].split("/") if s)
        self.clients.add(client)
        self.stats["ws_connections"] += 1
        try:
            async for raw in ws:
                try:
                    msg = orjson.loads(raw)
                except orjson.JSONDecodeError:
                    continue
                method, params, mid = msg.get("method"), msg.get("params") or [], msg.get("id")
                if method == "SUBSCRIBE":
                    client.streams.update(params)
                    self.stats["subscribes"] += 1
                elif method == "UNSUBSCRIBE":
                    client.streams.difference_update(params)
                await ws.send(orjson.dumps({"result": None, "id": mid}).decode())
        except ConnectionClosed:
            pass
        finally:
            self.clients.discard(client)

    def _subscribers(self, stream: str) -> list[_Client]:
        fam = _family(stream)
        return [c for c in self.clients if stream in c.streams and ROUTE_OF.get(fam or "") == c.route]

    async def _send(self, stream: str, data: Any) -> None:
        if _now() < self.silence_until:
            return
        subs = self._subscribers(stream)
        if not subs:
            return
        frame = orjson.dumps({"stream": stream, "data": data}).decode()
        for c in subs:
            try:
                await c.ws.send(frame)
                self.stats["ws_msgs"] += 1
            except ConnectionClosed:
                self.clients.discard(c)

    def _subscribed(self, suffix: str) -> list[str]:
        out = set()
        for c in self.clients:
            for s in c.streams:
                if s.endswith(suffix) and ROUTE_OF.get(_family(s) or "") == c.route:
                    out.add(s.split("@", 1)[0].upper())
        return sorted(out)

    def _next_update_id(self, t: int) -> int:
        # time-based so ids stay monotonic when a stream reconnects to another fake process
        self._update_id = max(self._update_id + 1, t * 1000)
        return self._update_id

    def _book_mid(self, s: str, t: int) -> float:
        k = t // 50
        c = self._mid_cache.get(s)
        if c is None or c[0] != k:
            c = (k, self.price(s, t))
            self._mid_cache[s] = c
        return c[1]

    async def _book_loop(self) -> None:
        tick = 0.01
        acc: dict[str, float] = {}
        while True:
            await asyncio.sleep(tick)
            syms = self._subscribed("@bookTicker")
            if not syms or _now() < self.silence_until:
                continue
            t = _now()
            per_weight = self.cfg.book_rate_total * tick / self.total_book_weight
            batches: dict[_Client, list[bytes]] = {}
            for s in syms:
                acc[s] = acc.get(s, 0.0) + per_weight * self.book_weight.get(s, 1.0)
                n = int(acc[s])
                if n <= 0:
                    continue
                acc[s] -= n
                stream = f"{s.lower()}@bookTicker"
                subs = self._subscribers(stream)
                if not subs:
                    continue
                mid = self._book_mid(s, t)
                half = mid * self._spread[s]
                bid, ask = f"{mid - half:.8f}", f"{mid + half:.8f}"
                for _ in range(n):
                    u = self._next_update_id(t)
                    frame = (f'{{"stream":"{stream}","data":{{"e":"bookTicker","u":{u},"E":{t},"T":{t - 1},'
                             f'"s":"{s}","b":"{bid}","B":"{1 + 10 * random.random():.3f}","a":"{ask}",'
                             f'"A":"{1 + 10 * random.random():.3f}"}}}}').encode()
                    for c in subs:
                        batches.setdefault(c, []).append(frame)
            for c, frames in batches.items():
                # one send_context per client and tick: frames are written back to back, then drained once
                try:
                    async with c.ws.send_context():
                        for f in frames:
                            c.ws.protocol.send_text(f)
                    self.stats["ws_msgs"] += len(frames)
                    self.stats["book_msgs"] += len(frames)
                except ConnectionClosed:
                    self.clients.discard(c)

    async def _kline_loop(self) -> None:
        last_closed = _now() // MINUTE * MINUTE - MINUTE
        while True:
            await asyncio.sleep(self.cfg.kline_update_ms / 1000)
            t = _now()
            cur_open = t // MINUTE * MINUTE
            syms = self._subscribed("@kline_1m")
            closing = []
            while last_closed + MINUTE < cur_open:
                last_closed += MINUTE
                closing.append(last_closed)
            for s in syms:
                for ot in closing:
                    if self.skip_klines.get(s, 0) > 0:
                        self.skip_klines[s] -= 1
                        continue
                    await self._send(f"{s.lower()}@kline_1m", self._kline_msg(s, ot, t, True))
                await self._send(f"{s.lower()}@kline_1m", self._kline_msg(s, cur_open, t, False))

    def _kline_msg(self, s: str, ot: int, t: int, closed: bool) -> dict[str, Any]:
        k = self.kline(s, ot)
        f = self._fmt
        return {"e": "kline", "E": t, "s": s, "k": {
            "t": ot, "T": ot + MINUTE - 1, "s": s, "i": "1m", "f": 1, "L": 2, "o": f(k["o"]), "c": f(k["c"]),
            "h": f(k["h"]), "l": f(k["l"]), "v": f(k["v"]), "n": k["n"], "x": closed, "q": f(k["q"]),
            "V": f(k["V"]), "Q": f(k["Q"]), "B": "0"}}

    async def _mark_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0 - (_now() % 1000) / 1000)
            t = _now()
            data = [{"e": "markPriceUpdate", "E": t, "s": s, "p": self._fmt(self.price(s, t)),
                     "i": self._fmt(self.price(s, t - 300)), "P": self._fmt(self.price(s, t)),
                     "r": f"{0.0001 * (_h('f', s) - 0.3):.8f}", "T": (t // 28_800_000 + 1) * 28_800_000}
                    for s in self.symbols + self.extra]
            await self._send("!markPrice@arr@1s", data)

    async def _depth_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.depth_ms / 1000)
            t = _now()
            for c_sym in self._subscribed("@500ms"):
                px = self.price(c_sym, t)
                step = px * 0.0001
                bids = [[self._fmt(px - (i + 1) * step), self._fmt(1 + 5 * random.random())] for i in range(20)]
                asks = [[self._fmt(px + (i + 1) * step), self._fmt(1 + 5 * random.random())] for i in range(20)]
                u = self._next_update_id(t)
                await self._send(f"{c_sym.lower()}@depth20@500ms", {
                    "e": "depthUpdate", "E": t, "T": t - 2, "s": c_sym, "U": u - 1,
                    "u": u, "pu": u - 2, "b": bids, "a": asks})

    async def _liquidation_loop(self) -> None:
        while True:
            await asyncio.sleep(random.expovariate(max(self.cfg.liquidation_rate, 1e-3)))
            t = _now()
            s = random.choice(self.symbols)
            px = self.price(s, t)
            await self._send("!forceOrder@arr", {"e": "forceOrder", "E": t, "o": {
                "s": s, "S": random.choice(["BUY", "SELL"]), "o": "LIMIT", "f": "IOC", "q": "1.0",
                "p": self._fmt(px), "ap": self._fmt(px), "X": "FILLED", "l": "1.0", "z": "1.0", "T": t - 1}})

    # -- REST -----------------------------------------------------------------------

    def _exchange_info(self) -> dict[str, Any]:
        t = _now()
        syms = []
        for i, s in enumerate(self.symbols + self.extra):
            status = "TRADING" if not s.startswith("X") or i % 3 else "SETTLING"
            syms.append({"symbol": s, "pair": s, "contractType": "PERPETUAL", "deliveryDate": 4133404800000,
                         "onboardDate": t - 400 * 86_400_000, "status": status, "baseAsset": s[:-4],
                         "quoteAsset": "USDT", "marginAsset": "USDT",
                         "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                                     {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                                     {"filterType": "MIN_NOTIONAL", "notional": "5"}]})
        return {"timezone": "UTC", "serverTime": t, "symbols": syms,
                "rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1,
                                "limit": self.cfg.weight_limit},
                               {"rateLimitType": "ORDERS", "interval": "MINUTE", "intervalNum": 1, "limit": 1200}]}

    def _rest(self, path: str, q: dict[str, str]) -> tuple[int, Any, int]:
        t = _now()
        if path == "/fapi/v1/time":
            return 200, {"serverTime": t}, 1
        if path == "/fapi/v1/exchangeInfo":
            return 200, self._exchange_info(), 1
        if path == "/fapi/v1/ticker/24hr":
            rows = [{"symbol": s, "quoteVolume": f"{(3e9 * (0.97 ** i)):.2f}"} for i, s in enumerate(self.symbols)]
            rows += [{"symbol": s, "quoteVolume": "1000.0"} for s in self.extra]
            return 200, rows, 40
        if path == "/fapi/v1/openInterest":
            s = q.get("symbol", "")
            if s not in self.base_px:
                return 400, {"code": -1121, "msg": "Invalid symbol."}, 1
            return 200, {"symbol": s, "openInterest": f"{1e5 * (1 + 0.01 * math.sin(t / 60000)):.3f}", "time": t}, 1
        if path == "/fapi/v1/klines":
            s = q.get("symbol", "")
            if s not in self.base_px:
                return 400, {"code": -1121, "msg": "Invalid symbol."}, 1
            start = int(q.get("startTime", t - 500 * MINUTE))
            end = int(q.get("endTime", t))
            limit = min(1500, int(q.get("limit", 500)))
            ot = -(-start // MINUTE) * MINUTE
            rows = []
            while ot <= end and len(rows) < limit and ot <= t:
                rows.append(self.rest_kline_row(s, ot))
                ot += MINUTE
            return 200, rows, 5
        return 404, {"code": -1, "msg": "not found"}, 1

    async def _http_handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                method, target, _ = line.decode().split(" ", 2)
                length = 0
                while True:
                    h = await reader.readline()
                    if h in (b"\r\n", b"\n", b""):
                        break
                    k, _, v = h.decode().partition(":")
                    if k.strip().lower() == "content-length":
                        length = int(v.strip())
                if length:
                    await reader.readexactly(length)
                parts = urlsplit(target)
                q = {k: v[0] for k, v in parse_qs(parts.query).items()}
                if parts.path.startswith("/__admin/"):
                    status, body, weight = 200, await self._admin(parts.path, q), 0
                else:
                    self.stats["rest_requests"] += 1
                    status, body, weight = self._rest(parts.path, q)
                minute = _now() // MINUTE
                if minute != self._weight_minute:
                    self._weight_minute, self._weight_used = minute, 0
                self._weight_used += weight
                payload = orjson.dumps(body)
                writer.write((f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
                              f"Content-Length: {len(payload)}\r\nX-MBX-USED-WEIGHT-1M: {self._weight_used}\r\n"
                              f"Connection: keep-alive\r\n\r\n").encode() + payload)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            writer.close()

    async def _admin(self, path: str, q: dict[str, str]) -> dict[str, Any]:
        if path == "/__admin/drop":
            n = len(self.clients)
            await self.drop_all()
            return {"dropped": n}
        if path == "/__admin/silence":
            self.silence_until = _now() + int(float(q.get("s", "30")) * 1000)
            return {"silence_until": self.silence_until}
        if path == "/__admin/skip_klines":
            self.skip_klines[q["symbol"]] = int(q.get("n", "1"))
            return {"ok": True}
        if path == "/__admin/stats":
            return {**self.stats, "clients": len(self.clients),
                    "streams": sum(len(c.streams) for c in self.clients)}
        return {"error": "unknown admin path"}


async def serve_forever(cfg: FakeConfig) -> None:
    fake = FakeBinance(cfg)
    await fake.start()
    print(orjson.dumps({"ws_base": fake.ws_base, "rest_base": fake.rest_base,
                        "symbols": len(fake.symbols)}).decode(), flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await fake.stop()
