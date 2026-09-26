"""``jevbot record``: standalone public market-data recorder.

Independent from any trading component. Records only what cannot be downloaded later
(book aggregates, depth samples, mark/funding at 1s, OI at ~1m, liquidation snapshots,
latency) plus closed 1m klines (cheap; used for gap/latency QA and feature warmup).
"""

from __future__ import annotations

import asyncio
import gzip
import os
import time
from pathlib import Path
from typing import Any

import httpx
import orjson

from jevbot.core.config import AppConfig
from jevbot.core.events import BookTicker, DepthSnapshot, ForceOrder, Kline, MarkPrice, SchemaError
from jevbot.core.logging import get_logger
from jevbot.core.time import MINUTE_MS, ClockOffsetEstimator, mono_ms, ms_to_date, now_ms
from jevbot.marketdata import endpoints as ep
from jevbot.marketdata.binance_rest import BinanceRest, RateLimited
from jevbot.marketdata.binance_ws import WsConnection, WsPool
from jevbot.marketdata.parsers import decode_frame, parse_payload
from jevbot.marketdata.universe import select_universe
from jevbot.recorder.aggregators import BookTicker1s, DepthSampler, IntervalQuantiles, LatencyAggregator, MarkDedup
from jevbot.recorder.gaps import KlineTracker
from jevbot.recorder.health import Counters, HealthMonitor, STOPPED
from jevbot.recorder.integrity import append_manifest, quarantine_orphans, sha256_file, write_sidecar
from jevbot.recorder.schemas import SCHEMAS
from jevbot.recorder.sinks import SinkManager
from jevbot.recorder.smoke import SmokeReport, run_smoke

log = get_logger(__name__)

EXIT_OK, EXIT_SMOKE_FAILED, EXIT_STARTUP_FAILED, EXIT_WRITER_FAILED, EXIT_TASK_CRASHED = 0, 3, 4, 9, 10


def _kline_row(k: Kline) -> tuple[Any, ...]:
    return (k.symbol, k.open_time, k.close_time, k.open, k.high, k.low, k.close, k.volume, k.quote_volume,
            k.n_trades, k.taker_buy_base, k.taker_buy_quote, k.t_event, k.t_recv, k.source)


def _force_row(f: ForceOrder) -> tuple[Any, ...]:
    return (f.symbol, f.t_event, f.t_trade, f.side, f.order_type, f.time_in_force, f.qty, f.price, f.avg_price,
            f.status, f.last_filled_qty, f.filled_acc_qty, f.t_recv)


class Recorder:
    def __init__(self, cfg: AppConfig, rest_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self.data_dir = Path(cfg.data_dir)
        self.raw_dir = self.data_dir / "raw"
        self.rest = BinanceRest(cfg.binance.rest_base, cfg.binance.rest, transport=rest_transport)
        self.sink = SinkManager(self.raw_dir, cfg.sink)
        self.pool = WsPool(cfg.binance.ws, self._on_frame, self._on_ws_state)
        self.health = HealthMonitor(Path(cfg.run_dir))
        self.counters = Counters()
        self.clock = ClockOffsetEstimator()
        self.klines = KlineTracker()
        self.book = BookTicker1s()
        self.depth = DepthSampler(int(cfg.recorder.depth.sample_interval_s * 1000))
        self.mark = MarkDedup()
        self.latency = LatencyAggregator()
        self.lat_interval = IntervalQuantiles()
        self.loop_lag = IntervalQuantiles(max_samples=5000)
        self._last_rows_written = 0
        self._last_health_mono = mono_ms()
        self.members: list[str] = []
        self.member_set: set[str] = set()
        self.depth_symbols: list[str] = []
        self.book_last_recv: dict[str, int] = {}
        self.smoke: SmokeReport | None = None
        self._stop = asyncio.Event()
        self._exit_code = EXIT_OK
        self._offset_ms = 0.0
        self._tasks: list[asyncio.Task[Any]] = []
        self._backfilling: set[str] = set()
        self._backfill_sem = asyncio.Semaphore(4)
        self._conn_down_at: dict[str, int] = {}
        self._conn_down_reason: dict[str, str] = {}
        self._err_log_budget: dict[str, int] = {}

    # -- public -------------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self, duration_s: float | None = None) -> int:
        rc = self.cfg.recorder
        log.info("recorder_starting", config_hash=self.cfg.config_hash(), data_dir=str(self.data_dir),
                 ws_base=self.cfg.binance.ws.base, routes=self.cfg.binance.ws.routes, rest=self.cfg.binance.rest_base)
        if rc.orphan_tmp_quarantine:
            moved = quarantine_orphans(self.raw_dir, self.data_dir / "quarantine")
            if moved:
                log.warning("orphan_files_quarantined", n=len(moved), files=[str(p) for p in moved[:10]])
        self.sink.start()
        try:
            try:
                await self._sync_time()
                await self._refresh_universe(initial=True)
            except Exception as e:
                log.exception("startup_failed", err=repr(e))
                return EXIT_STARTUP_FAILED
            if rc.smoke.enabled:
                self.smoke = await run_smoke(self.cfg, self.rest)
                self.health.smoke_ok = self.smoke.ok
                self._write_smoke_report()
                if not self.smoke.ok and rc.smoke.required:
                    log.error("smoke_test_failed_exiting", failed=self.smoke.failed_routes())
                    return EXIT_SMOKE_FAILED
            else:
                self.health.smoke_ok = True
            self.pool.set_streams(self._desired_streams())
            self.pool.start()
            self._spawn(self._flush_loop(), "flush")
            self._spawn(self._loop_lag_monitor(), "loop-lag")
            self._spawn(self._health_loop(), "health")
            self._spawn(self._kline_watchdog(), "kline-watchdog")
            self._spawn(self._time_sync_loop(), "time-sync")
            self._spawn(self._universe_loop(), "universe")
            if rc.oi.enabled:
                self._spawn(self._oi_loop(), "oi")
            if duration_s:
                self._spawn(self._stop_after(duration_s), "duration")
            await self._stop.wait()
            log.info("recorder_stopping", exit_code=self._exit_code)
        finally:
            await self._shutdown()
        return self._exit_code

    # -- lifecycle ------------------------------------------------------------

    def _spawn(self, coro: Any, name: str) -> None:
        t = asyncio.create_task(self._guard(coro, name), name=name)
        self._tasks.append(t)
        t.add_done_callback(lambda task: self._tasks.remove(task) if task in self._tasks else None)

    async def _guard(self, coro: Any, name: str) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("task_crashed", task=name)
            if self._exit_code == EXIT_OK:
                self._exit_code = EXIT_TASK_CRASHED
            self.request_stop()

    async def _stop_after(self, s: float) -> None:
        await asyncio.sleep(s)
        log.info("duration_reached", seconds=s)
        self.request_stop()

    async def _shutdown(self) -> None:
        await self.pool.stop()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        t = now_ms()
        self.sink.extend("book_1s", self.book.flush_all())
        self.sink.extend("latency_1m", self.latency.flush())
        snap = self._health_snapshot()
        snap["status"] = STOPPED
        self.sink.append("health", self.health.row(snap))
        await asyncio.to_thread(self.sink.close)
        self.health.write_status_file(snap, {"stopped_at": t})
        await self.rest.aclose()
        log.info("recorder_stopped", rows_written=self.sink.stats.total("rows_written"),
                 files=self.sink.stats.total("files_final"), bytes=self.sink.stats.total("bytes_final"))

    # -- universe & subscriptions --------------------------------------------

    def _desired_streams(self) -> list[str]:
        rc = self.cfg.recorder
        streams: list[str] = []
        if rc.mark_price:
            streams.append(ep.MARK_PRICE_ALL)
        if rc.force_order:
            streams.append(ep.FORCE_ORDER_ALL)
        for s in self.members:
            if rc.kline_1m:
                streams.append(ep.kline_stream(s))
            if rc.book_ticker:
                streams.append(ep.book_ticker_stream(s))
        if rc.depth.enabled:
            streams += [ep.depth_stream(s, rc.depth.levels, rc.depth.speed) for s in self.depth_symbols]
        return streams

    async def _refresh_universe(self, initial: bool = False) -> None:
        info = await self.rest.exchange_info()
        tickers = await self.rest.ticker_24h()
        t = now_ms()
        members, rows = select_universe(info, tickers, self.cfg.universe, t, set(self.members))
        if not members:
            raise RuntimeError("empty universe (check universe filters / exchange data)")
        added = [s for s in members if s not in self.member_set]
        removed = [s for s in self.members if s not in set(members)]
        self.members, self.member_set = members, set(members)
        for s in added:
            self.klines.add(s, t)
        for s in removed:
            self.klines.remove(s)
        dc = self.cfg.recorder.depth
        extra = [s for s in members if s not in dc.symbols_always][:dc.top_n_by_volume]
        self.depth_symbols = [s for s in dc.symbols_always if s in self.member_set] + extra
        self.sink.extend("universe", [tuple(r[f.name] for f in SCHEMAS["universe"]) for r in rows])
        await asyncio.to_thread(self._save_exchange_info, info, t)
        log.info("universe_refreshed", members=len(members), added=len(added), removed=len(removed),
                 depth_symbols=len(self.depth_symbols), rate_limits=info.get("rateLimits"))
        if not initial and (added or removed):
            self.pool.set_streams(self._desired_streams())

    def _save_exchange_info(self, info: dict[str, Any], t: int) -> None:
        d = self.raw_dir / "exchange_info" / f"date={ms_to_date(t)}"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"exchangeInfo-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(t / 1000))}.json.gz"
        tmp = path.with_name(path.name + ".tmp")
        with gzip.open(tmp, "wb") as fh:
            fh.write(orjson.dumps(info))
        os.replace(tmp, path)
        digest = sha256_file(path)
        write_sidecar(path, digest)
        append_manifest(self.raw_dir / "exchange_info", {"file": path.relative_to(self.raw_dir / "exchange_info").as_posix(),
                                                         "bytes": path.stat().st_size, "sha256": digest, "t": t})

    async def _universe_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.universe.refresh_s)
            try:
                await self._refresh_universe()
            except Exception as e:
                log.error("universe_refresh_failed", err=repr(e))

    # -- ws frames --------------------------------------------------------------

    def _on_ws_state(self, conn: WsConnection, state: str, info: dict[str, Any]) -> None:
        if state == "disconnected":
            self._conn_down_at[conn.name] = info.get("t_down", now_ms())
            self._conn_down_reason[conn.name] = info.get("reason", "")
        elif state == "connected" and not info.get("rotated"):
            t_down = self._conn_down_at.pop(conn.name, None)
            if t_down is not None:
                t_up = now_ms()
                reason = self._conn_down_reason.pop(conn.name, "")
                rows = []
                for s in conn.streams:
                    rows.append((ep.symbol_of(s) or "*", s, "ws_disconnect", t_down, t_up, t_up, False, 0,
                                 f"conn={conn.name} reason={reason}"))
                self.sink.extend("gaps", rows)
                self.counters.gaps_detected += 1
                log.warning("ws_gap_recorded", conn=conn.name, down_ms=t_up - t_down, streams=len(rows))

    def _schema_error(self, family: str, err: Exception) -> None:
        self.counters.schema_errors[family] = self.counters.schema_errors.get(family, 0) + 1
        budget = self._err_log_budget.get(family, 20)
        if budget > 0:
            self._err_log_budget[family] = budget - 1
            log.error("schema_error", family=family, err=str(err)[:300])

    def _on_frame(self, raw: bytes | str, t_recv: int, conn: WsConnection) -> None:
        try:
            env = decode_frame(raw)
        except SchemaError as e:
            self._schema_error("frame", e)
            return
        if env.kind == "control":
            self.counters.control_replies += 1
            conn.on_control_reply(env.control_id, env.error)
            return
        fam = env.family or ""
        fb = self.counters.frames_by_family
        fb[fam] = fb.get(fam, 0) + 1
        try:
            ev = parse_payload(env, t_recv)
        except SchemaError as e:
            self._schema_error(fam, e)
            return
        if fam == "bookTicker":
            self._on_book(ev, conn)
        elif fam == "kline":
            self._on_kline(ev, conn)
        elif fam == "markPrice":
            self._on_mark(ev, conn)
        elif fam == "depth":
            self._on_depth(ev, conn)
        elif fam == "forceOrder":
            self._lat(fam, conn, ev.t_event, t_recv)
            self.sink.append("force_order", _force_row(ev))

    def _lat(self, fam: str, conn: WsConnection, t_event: int, t_recv: int) -> None:
        lag = t_recv - t_event
        self.lat_interval.add(lag)
        conn.note_lag(lag + self._offset_ms)
        rows = self.latency.add(fam, conn.route, t_event, t_recv)
        if rows:
            self.sink.extend("latency_1m", rows)

    def _on_book(self, e: BookTicker, conn: WsConnection) -> None:
        self._lat("bookTicker", conn, e.t_event, e.t_recv)
        self.book_last_recv[e.symbol] = e.t_recv
        row = self.book.update(e)
        if row is not None:
            self.sink.append("book_1s", row)

    def _on_depth(self, e: DepthSnapshot, conn: WsConnection) -> None:
        self._lat("depth", conn, e.t_event, e.t_recv)
        row = self.depth.update(e)
        if row is not None:
            self.sink.append("depth20", row)

    def _on_mark(self, items: list[MarkPrice], conn: WsConnection) -> None:
        if items:
            self._lat("markPrice", conn, items[0].t_event, items[0].t_recv)
        for m in items:
            if m.symbol in self.member_set and self.mark.accept(m):
                self.sink.append("mark_price", (m.symbol, m.t_event, m.mark, m.index, m.est_settle,
                                                m.funding_rate, m.next_funding_time, m.t_recv))

    def _on_kline(self, k: Kline, conn: WsConnection) -> None:
        self._lat("kline", conn, k.t_event, k.t_recv)
        if not k.closed:
            return
        accept, gap = self.klines.on_kline(k.symbol, k.open_time)
        if not accept:
            return
        self.counters.klines_ws += 1
        self.sink.append("kline_1m", _kline_row(k))
        if gap is not None:
            self.counters.gaps_detected += 1
            self._schedule_backfill(k.symbol, gap[0], gap[1], "continuity")

    # -- kline gaps / backfill ------------------------------------------------

    def _schedule_backfill(self, symbol: str, first: int, last: int, why: str) -> None:
        key = f"{symbol}:{first}"
        if key in self._backfilling:
            return
        self._backfilling.add(key)
        self._spawn(self._backfill(symbol, first, last, why, key), f"backfill:{symbol}")

    async def _backfill(self, symbol: str, first: int, last: int, why: str, key: str) -> None:
        n_missing = (last - first) // MINUTE_MS + 1
        detected = now_ms()
        ok = False
        filled = 0
        try:
            async with self._backfill_sem:
                self.counters.backfill_requests += 1
                ks = await self.rest.klines(symbol, first, last, limit=min(1000, max(n_missing, 1)))
            ks = [k for k in ks if first <= k.open_time <= last]
            self.sink.extend("kline_1m", [_kline_row(k) for k in ks])
            filled = len(ks)
            self.counters.klines_rest += filled
            if ks:
                self.klines.mark_filled(symbol, max(k.open_time for k in ks))
            ok = filled == n_missing
            if not ok:
                self.klines.defer(symbol, now_ms() + 60_000)
        except RateLimited as e:
            self.counters.rate_limited += 1
            self.klines.defer(symbol, now_ms() + int(e.retry_after_s * 1000))
        except Exception as e:
            self.counters.backfill_failures += 1
            self.klines.defer(symbol, now_ms() + 60_000)
            log.warning("kline_backfill_failed", symbol=symbol, err=repr(e))
        finally:
            self._backfilling.discard(key)
        self.sink.append("gaps", (symbol, ep.kline_stream(symbol), f"kline_{why}", first, last + MINUTE_MS - 1,
                                  detected, ok, n_missing, f"filled={filled}"))
        log.info("kline_backfill", symbol=symbol, why=why, missing=n_missing, filled=filled, ok=ok)

    async def _kline_watchdog(self) -> None:
        rc = self.cfg.recorder
        grace = int(rc.kline_grace_s * 1000)
        after = int(rc.kline_backfill_after_s * 1000)
        while True:
            await asyncio.sleep(5)
            if not rc.kline_1m:
                continue
            for sym, first, last in self.klines.overdue(now_ms(), grace, after):
                self.counters.gaps_detected += 1
                self._schedule_backfill(sym, first, last, "missing")

    # -- REST pollers -----------------------------------------------------------

    async def _sync_time(self) -> None:
        t_send, t_server, t_recv = await self.rest.server_time()
        self.clock.add_sample(t_send, t_server, t_recv)
        self._offset_ms = float(self.clock.offset_ms or 0.0)
        off = self.clock.offset_ms
        if off is not None and abs(off) > 250:
            log.warning("clock_offset_high", offset_ms=round(off, 1), rtt_ms=self.clock.last_rtt_ms)

    async def _time_sync_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.recorder.time_sync_s)
            try:
                await self._sync_time()
            except Exception as e:
                log.warning("time_sync_failed", err=repr(e))

    async def _oi_loop(self) -> None:
        cycle = self.cfg.recorder.oi.cycle_s
        while True:
            members = list(self.members)
            if not members:
                await asyncio.sleep(1)
                continue
            gap = cycle / len(members)
            start = mono_ms()
            for i, sym in enumerate(members):
                target = start + int(i * gap * 1000)
                delay = (target - mono_ms()) / 1000
                if delay > 0:
                    await asyncio.sleep(delay)
                if sym not in self.member_set:
                    continue
                try:
                    oi = await self.rest.open_interest(sym)
                    self.counters.oi_polls += 1
                    self.sink.append("open_interest", (oi.symbol, oi.open_interest, oi.t_server, oi.t_req, oi.t_recv))
                except RateLimited as e:
                    self.counters.rate_limited += 1
                    await asyncio.sleep(e.retry_after_s)
                except Exception as e:
                    self.counters.oi_errors += 1
                    if self.counters.oi_errors % 50 == 1:
                        log.warning("oi_poll_failed", symbol=sym, err=repr(e), total_errors=self.counters.oi_errors)
            rest = cycle - (mono_ms() - start) / 1000
            if rest > 0:
                await asyncio.sleep(rest)

    # -- periodic -----------------------------------------------------------------

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.sink.flush_s)
            t = now_ms()
            self.sink.extend("book_1s", self.book.flush_older_than(t - t % 1000 - 2000))
            self.sink.flush(t)

    async def _loop_lag_monitor(self) -> None:
        """Event-loop responsiveness: overshoot of a 50 ms sleep."""
        while True:
            t0 = mono_ms()
            await asyncio.sleep(0.05)
            self.loop_lag.add(max(0, mono_ms() - t0 - 50))

    def _perf(self) -> dict[str, Any]:
        lat = self.lat_interval.take()
        ll = self.loop_lag.take()
        rows = self.sink.stats.total("rows_written")
        dt = max(1e-3, (mono_ms() - self._last_health_mono) / 1000)
        rate = (rows - self._last_rows_written) / dt
        self._last_rows_written, self._last_health_mono = rows, mono_ms()
        return {
            "lat_n": lat["n"], "lat_p50_ms": lat["p50"], "lat_p95_ms": lat["p95"], "lat_p99_ms": lat["p99"],
            "lat_max_ms": lat["max"], "loop_lag_p50_ms": ll["p50"], "loop_lag_p99_ms": ll["p99"],
            "loop_lag_max_ms": ll["max"], "sink_buffered_rows": self.sink.buffered_rows(),
            "open_files": self.sink.open_files(), "writer_rows_per_s": round(rate, 1),
            "duplicates_total": self.book.duplicates + self.depth.duplicates + self.mark.duplicates
            + self.klines.duplicates,
            "invalid_total": self.book.invalid, "late_rows_total": self.sink.stats.total("late_rows"),
            "oi_polls_total": self.counters.oi_polls, "rest_requests_total": self.rest.weights.requests,
        }

    def _health_snapshot(self) -> dict[str, Any]:
        t = now_ms()
        book_expected = len(self.members) if self.cfg.recorder.book_ticker else 0
        book_fresh = sum(1 for s in self.members if t - self.book_last_recv.get(s, 0) <= 10_000)
        return self.health.snapshot(
            conns=self.pool.connections(), counters=self.counters, symbols_universe=len(self.members),
            kline_fresh=self.klines.fresh_count(t, int(self.cfg.recorder.kline_grace_s * 1000))
            if self.cfg.recorder.kline_1m else len(self.members),
            book_fresh=book_fresh, book_expected=book_expected, sink=self.sink,
            clock_offset_ms=self.clock.offset_ms, rest_weights=self.rest.weights,
            writer_error=self.sink.writer_error(), perf=self._perf(),
            lag_warn_ms=self.cfg.recorder.lag_warn_ms, loop_lag_warn_ms=self.cfg.recorder.loop_lag_warn_ms)

    async def _health_loop(self) -> None:
        rc = self.cfg.recorder
        last_log = 0
        while True:
            await asyncio.sleep(rc.health_interval_s)
            snap = self._health_snapshot()
            self.sink.append("health", self.health.row(snap))
            err = self.sink.writer_error()
            if err is not None and self._exit_code == EXIT_OK:
                # data can no longer be persisted reliably -> stop loudly instead of recording into the void
                log.error("sink_writer_failed_stopping", err=repr(err))
                self._exit_code = EXIT_WRITER_FAILED
                self.request_stop()
            extra = {"counters": self.counters.__dict__, "latency_p99_ms": self.latency.snapshot_p99(),
                     "connections": [{"name": c.name, "route": c.route, "streams": len(c.streams),
                                      "connected": c.stats.connected, "msgs": c.stats.msgs,
                                      "connects": c.stats.connects, "rotations": c.stats.rotations,
                                      "silence_reconnects": c.stats.silence_reconnects,
                                      "silent_but_alive": c.stats.silent_but_alive,
                                      "stale_reconnects": c.stats.stale_reconnects,
                                      "lag_ewma_ms": round(c.lag_ewma_ms, 1),
                                      "last_disconnect_reason": c.stats.last_disconnect_reason}
                                     for c in self.pool.connections()],
                     "smoke_ok": self.health.smoke_ok}
            self.health.write_status_file(snap, extra)
            if mono_ms() - last_log >= rc.health_log_interval_s * 1000:
                last_log = mono_ms()
                log.info("health", **{k: snap[k] for k in ("status", "msgs_per_s", "cpu_pct", "rss_mb",
                                                          "conns_connected", "conns_total", "symbols_universe",
                                                          "symbols_kline_fresh", "symbols_book_fresh",
                                                          "lat_p50_ms", "lat_p99_ms", "loop_lag_p99_ms",
                                                          "sink_buffered_rows", "rows_written_total",
                                                          "parquet_bytes_total", "detail")})

    def _write_smoke_report(self) -> None:
        if self.smoke is None:
            return
        d = Path(self.cfg.run_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / "smoke_report.json").write_bytes(orjson.dumps({"t": now_ms(), **self.smoke.to_dict()},
                                                          option=orjson.OPT_INDENT_2, default=str))
