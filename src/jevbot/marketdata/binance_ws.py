"""Resilient Binance combined-stream WebSocket connections.

``WsConnection`` owns one socket on one route (/public or /market) and a desired
set of stream names. It reconnects with jittered exponential backoff, re-subscribes
the *current* desired set on every (re)connect, detects silent stalls, and rotates
the socket before the 24 h server limit with a make-before-break overlap.

``WsPool`` packs streams into connections (<= ``max_streams_per_conn``) per route
and rebalances when the desired set changes (universe refresh).

Frames are handed to ``on_frame(raw, t_recv, conn)`` undecoded; decoding happens once
in the consumer. Consumers must tolerate duplicates around rotations/reconnects.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import orjson
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidURI

from jevbot.core.config import WsConfig
from jevbot.core.logging import get_logger
from jevbot.core.time import mono_ms, now_ms
from jevbot.marketdata.endpoints import combined_stream_url, route_of

log = get_logger(__name__)


def close_reason(e: ConnectionClosed) -> str:
    """``closed:<code>`` = server sent a close frame; ``closed:local_1011`` = our keepalive ping
    timed out (pong not seen: backlog or dead link); ``closed:nocode`` = TCP EOF/reset, no close frames."""
    if e.rcvd is not None:
        return f"closed:{e.rcvd.code}"
    if e.sent is not None:
        return f"closed:local_{e.sent.code}"
    return "closed:nocode"

FrameHandler = Callable[[bytes | str, int, "WsConnection"], None]
StateHandler = Callable[["WsConnection", str, dict[str, Any]], None]

MAX_URL_LEN = 7000


@dataclass
class ConnStats:
    msgs: int = 0
    bytes: int = 0
    connects: int = 0
    connect_failures: int = 0
    disconnects: int = 0
    rotations: int = 0
    silence_reconnects: int = 0
    silent_but_alive: int = 0
    compression: bool = False            # permessage-deflate negotiated with the server
    stale_reconnects: int = 0
    control_sent: int = 0
    control_errors: int = 0
    connected: bool = False
    connected_since_ms: int | None = None
    last_msg_mono: int = 0
    last_disconnect_ms: int | None = None
    last_disconnect_reason: str | None = None
    disconnect_log: list[tuple[int, int | None, str]] = field(default_factory=list)   # (t_down, t_up, reason)


class WsConnection:
    def __init__(self, name: str, route: str, cfg: WsConfig, on_frame: FrameHandler,
                 on_state: StateHandler | None = None) -> None:
        self.name = name
        self.route = route
        self.cfg = cfg
        self.on_frame = on_frame
        self.on_state = on_state
        self.stats = ConnStats()
        self._streams: dict[str, None] = {}          # ordered set
        self._ws: ClientConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._ctl_lock = asyncio.Lock()
        self._last_ctl_mono = 0
        self._next_id = 1
        self._pending: dict[int, tuple[str, list[str]]] = {}
        self._rotate_at_mono = 0
        self._rotate_due = False
        self._close_reason: str | None = None
        self.lag_ewma_ms = 0.0
        self._last_stale_mono = -10**12

    # -- desired stream set -------------------------------------------------

    @property
    def streams(self) -> list[str]:
        return list(self._streams)

    def room(self) -> int:
        return self.cfg.max_streams_per_conn - len(self._streams)

    def add_streams(self, streams: Iterable[str]) -> None:
        new = [s for s in streams if s not in self._streams]
        for s in new:
            self._streams[s] = None
        if new and self._ws is not None:
            self._spawn_control("SUBSCRIBE", new)

    def remove_streams(self, streams: Iterable[str]) -> None:
        gone = [s for s in streams if s in self._streams]
        for s in gone:
            del self._streams[s]
        if gone and self._ws is not None:
            self._spawn_control("UNSUBSCRIBE", gone)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"ws:{self.name}")

    async def stop(self) -> None:
        self._stopping = True
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def ping_rtt(self, timeout: float = 10.0) -> float:
        """Round trip of an explicit ping/pong on the live socket (seconds)."""
        if self._ws is None:
            raise ConnectionError(f"{self.name}: not connected")
        waiter = await self._ws.ping()
        return float(await asyncio.wait_for(waiter, timeout))

    def force_reconnect(self) -> None:
        """Test/ops hook: drop the socket; the run loop reconnects."""
        if self._ws is not None:
            asyncio.create_task(self._ws.close(code=1001, reason="forced reconnect"))

    # -- control messages ---------------------------------------------------

    def _spawn_control(self, method: str, streams: list[str]) -> None:
        asyncio.create_task(self._send_control(method, streams), name=f"ws-ctl:{self.name}")

    async def _send_control(self, method: str, streams: list[str]) -> None:
        chunk = max(1, self.cfg.subscribe_chunk)
        min_gap_ms = int(1000 / max(0.1, self.cfg.max_control_msgs_per_sec))
        for i in range(0, len(streams), chunk):
            part = streams[i:i + chunk]
            async with self._ctl_lock:
                wait = self._last_ctl_mono + min_gap_ms - mono_ms()
                if wait > 0:
                    await asyncio.sleep(wait / 1000)
                ws = self._ws
                if ws is None:
                    return          # reconnect will subscribe the desired set via URL
                msg_id = self._next_id
                self._next_id += 1
                self._pending[msg_id] = (method, part)
                try:
                    await ws.send(orjson.dumps({"method": method, "params": part, "id": msg_id}).decode())
                    self.stats.control_sent += 1
                except ConnectionClosed:
                    return
                finally:
                    self._last_ctl_mono = mono_ms()

    def on_control_reply(self, msg_id: Any, error: Any) -> None:
        req = self._pending.pop(msg_id, None) if isinstance(msg_id, int) else None
        if error:
            self.stats.control_errors += 1
            log.error("ws_control_error", conn=self.name, id=msg_id, error=error,
                      method=req[0] if req else None, n=len(req[1]) if req else None)

    # -- run loop -----------------------------------------------------------

    async def _open(self) -> ClientConnection:
        streams = self.streams
        url = combined_stream_url(self.route, streams, self.cfg)
        extra: list[str] = []
        if len(url) > MAX_URL_LEN:
            n = len(streams)
            while n > 1 and len(combined_stream_url(self.route, streams[:n], self.cfg)) > MAX_URL_LEN:
                n //= 2
            url = combined_stream_url(self.route, streams[:n], self.cfg)
            extra = streams[n:]
        ws = await connect(
            url,
            open_timeout=self.cfg.open_timeout_s,
            ping_interval=self.cfg.ping_interval_s,
            ping_timeout=self.cfg.ping_timeout_s,
            max_size=self.cfg.max_message_bytes,
            max_queue=4096,
            compression="deflate" if self.cfg.compression else None,
            close_timeout=5,
        )
        if extra:
            self._ws = ws
            await self._send_control("SUBSCRIBE", extra)
        return ws

    def _emit_state(self, state: str, **info: Any) -> None:
        if self.on_state is not None:
            try:
                self.on_state(self, state, info)
            except Exception:
                log.exception("ws_state_handler_failed", conn=self.name, state=state)

    async def _run(self) -> None:
        backoff = self.cfg.backoff_initial_s
        while not self._stopping:
            if not self._streams:
                await asyncio.sleep(1.0)
                continue
            try:
                ws = await self._open()
            except InvalidURI:
                raise                                     # configuration error: fail loudly
            except Exception as e:                        # network, TLS, proxy, handshake, timeout ...
                self.stats.connect_failures += 1
                delay = backoff * (1 + random.uniform(-self.cfg.backoff_jitter, self.cfg.backoff_jitter))
                log.warning("ws_connect_failed", conn=self.name, err=repr(e), retry_in_s=round(delay, 2))
                self._emit_state("connect_failed", error=repr(e))
                await asyncio.sleep(delay)
                backoff = min(self.cfg.backoff_max_s, backoff * 2)
                continue
            backoff = self.cfg.backoff_initial_s
            try:
                reason = await self._pump(ws)
            except Exception as e:                        # never let the connection task die silently
                log.exception("ws_pump_failed", conn=self.name)
                reason = f"error:{type(e).__name__}"
                try:
                    await ws.close()
                except Exception:
                    pass
            t_down = now_ms()
            self._ws = None
            self.stats.connected = False
            self.stats.disconnects += 1
            self.stats.last_disconnect_ms = t_down
            self.stats.last_disconnect_reason = reason
            self.stats.disconnect_log.append((t_down, None, reason))
            del self.stats.disconnect_log[:-100]
            if not self._stopping:
                log.warning("ws_disconnected", conn=self.name, reason=reason, streams=len(self._streams))
                self._emit_state("disconnected", reason=reason, t_down=t_down)
                await asyncio.sleep(random.uniform(0.1, 0.5))

    def _mark_connected(self, ws: ClientConnection, rotated: bool = False) -> None:
        self._ws = ws
        st = self.stats
        st.connected = True
        st.connects += 1
        st.connected_since_ms = now_ms()
        st.last_msg_mono = mono_ms()
        if st.disconnect_log and st.disconnect_log[-1][1] is None:
            t_down, _, r = st.disconnect_log[-1]
            st.disconnect_log[-1] = (t_down, st.connected_since_ms, r)
        ext = [type(e).__name__ for e in (getattr(ws.protocol, "extensions", None) or [])]
        st.compression = bool(ext)
        log.info("ws_connected", conn=self.name, route=self.route, streams=len(self._streams), rotated=rotated,
                 extensions=ext)
        self._emit_state("connected", rotated=rotated)

    def _deliver(self, raw: bytes | str) -> None:
        st = self.stats
        st.msgs += 1
        st.bytes += len(raw)
        try:
            self.on_frame(raw, now_ms(), self)
        except Exception:
            log.exception("ws_frame_handler_failed", conn=self.name)

    def note_lag(self, lag_ms: float) -> None:
        """Feed-latency sample for this socket (consumer computes t_recv - t_event + clock offset)."""
        self.lag_ewma_ms += 0.05 * (lag_ms - self.lag_ewma_ms)

    async def _close_for(self, reason: str, ws: ClientConnection) -> None:
        self._close_reason = reason
        await ws.close(code=1001, reason=reason)

    async def _watchdog(self) -> None:
        """Liveness and staleness checks off the per-frame hot path.

        * silence: no frames for ``silence_timeout_s`` -> ping probe; a failed probe or
          ``silence_max_s`` without frames closes the socket (sparse streams such as illiquid
          klines are legitimately quiet, a dead TCP connection is not).
        * stale feed: frames keep arriving but the socket's clock-corrected lag EWMA exceeds
          ``stale_lag_ms`` -> reconnect (at most once per ``stale_min_interval_s``).
        * rotation: raises ``_rotate_due`` at ``conn_max_age_s``.
        """
        cfg = self.cfg
        silence_ms = int(cfg.silence_timeout_s * 1000)
        silence_max_ms = int(cfg.silence_max_s * 1000)
        tick = max(0.05, min(1.0, cfg.silence_timeout_s / 4))
        last_msgs = self.stats.msgs
        last_data = mono_ms()
        next_probe = 0
        self.lag_ewma_ms = 0.0
        while True:
            await asyncio.sleep(tick)
            now = mono_ms()
            flowing = self.stats.msgs != last_msgs
            if flowing:
                last_msgs, last_data = self.stats.msgs, now
                self.stats.last_msg_mono = now
            if now >= self._rotate_at_mono:
                self._rotate_due = True
            ws = self._ws
            if ws is None or self._close_reason:
                continue
            silent_for = now - last_data
            if silent_for >= silence_max_ms:
                self.stats.silence_reconnects += 1
                log.warning("ws_silence", conn=self.name, silent_ms=silent_for, probe="skipped_max")
                await self._close_for("silence", ws)
            elif silent_for >= silence_ms and now >= next_probe:
                try:
                    await asyncio.wait_for(await ws.ping(), timeout=min(5.0, cfg.ping_timeout_s))
                    self.stats.silent_but_alive += 1
                    next_probe = mono_ms() + silence_ms
                    log.info("ws_silent_but_alive", conn=self.name, silent_ms=silent_for)
                except Exception:
                    self.stats.silence_reconnects += 1
                    log.warning("ws_silence", conn=self.name, silent_ms=silent_for, probe="failed")
                    await self._close_for("silence", ws)
            elif (cfg.stale_lag_ms > 0 and flowing and self.lag_ewma_ms > cfg.stale_lag_ms
                  and now - self._last_stale_mono >= cfg.stale_min_interval_s * 1000):
                self._last_stale_mono = now
                self.stats.stale_reconnects += 1
                log.warning("ws_stale_feed", conn=self.name, lag_ewma_ms=round(self.lag_ewma_ms))
                await self._close_for("stale_feed", ws)

    async def _pump(self, ws: ClientConnection) -> str:
        """Hot path: one ``await ws.recv()`` per frame, no per-frame timers or tasks."""
        self._mark_connected(ws)
        max_age_ms = int(self.cfg.conn_max_age_s * 1000)
        self._rotate_at_mono = mono_ms() + max_age_ms
        self._rotate_due = False
        self._close_reason = None
        watchdog = asyncio.create_task(self._watchdog(), name=f"ws-watchdog:{self.name}")
        try:
            while True:
                try:
                    raw = await ws.recv()
                except ConnectionClosed as e:
                    if self._stopping:
                        return "stopped"
                    if self._close_reason:
                        return self._close_reason
                    return close_reason(e)
                self._deliver(raw)
                if self._rotate_due:
                    self._rotate_due = False
                    new_ws = await self._rotate(ws)
                    if new_ws is not None:
                        ws = new_ws
                        self._rotate_at_mono = mono_ms() + max_age_ms
                    else:
                        self._rotate_at_mono = mono_ms() + 60_000     # retry rotation in a minute
        finally:
            watchdog.cancel()

    async def _rotate(self, old: ClientConnection) -> ClientConnection | None:
        """Make-before-break: open a new socket, wait for its first frame, drain the old one."""
        log.info("ws_rotate_start", conn=self.name)
        new: ClientConnection | None = None
        try:
            new = await self._open()
            first = await asyncio.wait_for(new.recv(), timeout=self.cfg.silence_timeout_s)
        except Exception as e:
            log.warning("ws_rotate_failed", conn=self.name, err=repr(e))
            if new is not None:
                await new.close()
            return None
        # Drain frames already buffered on the old socket. The old socket keeps receiving live
        # data until it is closed, so the drain is time-bounded; frames that overlap with the
        # new socket are duplicates and are dropped downstream (sequence/time dedup).
        drain_deadline = mono_ms() + 500
        while mono_ms() < drain_deadline:
            try:
                raw = await asyncio.wait_for(old.recv(), timeout=0.05)
            except (asyncio.TimeoutError, ConnectionClosed):
                break
            self._deliver(raw)
        await old.close()
        self.stats.rotations += 1
        self._mark_connected(new, rotated=True)
        self._deliver(first)
        return new


class WsPool:
    """Packs desired streams into per-route connections and keeps them in sync."""

    def __init__(self, cfg: WsConfig, on_frame: FrameHandler, on_state: StateHandler | None = None) -> None:
        self.cfg = cfg
        self.on_frame = on_frame
        self.on_state = on_state
        self.conns: dict[str, list[WsConnection]] = {}
        self._started = False
        self._seq = 0

    def connections(self) -> list[WsConnection]:
        return [c for lst in self.conns.values() for c in lst]

    def set_streams(self, streams: Iterable[str]) -> None:
        by_route: dict[str, list[str]] = {}
        for s in dict.fromkeys(streams):
            by_route.setdefault(route_of(s, self.cfg), []).append(s)
        for route in set(self.conns) | set(by_route):
            self._rebalance(route, by_route.get(route, []))

    def _rebalance(self, route: str, desired_list: list[str]) -> None:
        desired = set(desired_list)
        conns = self.conns.setdefault(route, [])
        have: set[str] = set()
        for c in conns:
            drop = [s for s in c.streams if s not in desired]
            if drop:
                c.remove_streams(drop)
            have.update(c.streams)
        missing = [s for s in desired_list if s not in have]
        for c in conns:
            if not missing:
                break
            take, missing = missing[:max(0, c.room())], missing[max(0, c.room()):]
            if take:
                c.add_streams(take)
        while missing:
            take, missing = missing[:self.cfg.max_streams_per_conn], missing[self.cfg.max_streams_per_conn:]
            self._seq += 1
            c = WsConnection(f"{route}-{self._seq}", route, self.cfg, self.on_frame, self.on_state)
            c.add_streams(take)
            conns.append(c)
            if self._started:
                c.start()
        for c in [c for c in conns if not c.streams]:
            conns.remove(c)
            if self._started:
                asyncio.create_task(c.stop())

    def start(self) -> None:
        self._started = True
        for c in self.connections():
            c.start()

    async def stop(self) -> None:
        await asyncio.gather(*(c.stop() for c in self.connections()), return_exceptions=True)
        self._started = False
