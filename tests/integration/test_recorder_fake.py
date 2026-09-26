import asyncio
import time
from collections import Counter
from pathlib import Path

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from jevbot.marketdata.binance_ws import WsConnection
from jevbot.marketdata.parsers import decode_frame
from jevbot.recorder.integrity import verify_tree
from jevbot.recorder.recorder import EXIT_OK, EXIT_SMOKE_FAILED, Recorder
from jevbot.recorder.report import build_report, to_markdown
from jevbot.recorder.smoke import run_smoke

from .conftest import make_cfg

pytestmark = pytest.mark.integration


def read_dataset(root: Path, name: str) -> pa.Table:
    files = sorted((root / name).rglob("*.parquet"))
    return pa.concat_tables([pq.read_table(f) for f in files]) if files else pa.table({})


async def test_smoke_passes(fake, tmp_path):
    rep = await run_smoke(make_cfg(fake, tmp_path))
    assert rep.ok, rep.to_dict()
    routes = {c.name: c for c in rep.checks}
    assert set(routes) == {"rest", "ws:public", "ws:market"}
    assert routes["ws:market"].detail["subscribe_ack_ok"]
    assert routes["ws:public"].detail["ping_rtt_ms"] >= 0


async def test_smoke_detects_wrong_route_mapping(fake, tmp_path):
    # kline mapped to /public: connection works but the stream never delivers -> must fail
    cfg = make_cfg(fake, tmp_path, "binance.ws.stream_routes.kline=public")
    rep = await run_smoke(cfg)
    assert not rep.ok
    public = next(c for c in rep.checks if c.name == "ws:public")
    assert "kline" in public.detail["missing_required"]


async def test_recorder_exits_when_smoke_fails(fake, tmp_path):
    cfg = make_cfg(fake, tmp_path, "binance.ws.stream_routes.markPrice=public")
    assert await Recorder(cfg).run(duration_s=5) == EXIT_SMOKE_FAILED


async def test_recorder_end_to_end(fake, tmp_path):
    cfg = make_cfg(fake, tmp_path)
    rec = Recorder(cfg)
    task = asyncio.create_task(rec.run(duration_s=8))
    await asyncio.sleep(3)
    await fake.drop_all()                       # reconnect path during the run
    code = await task
    assert code == EXIT_OK
    raw = tmp_path / "data" / "raw"
    rep = verify_tree(raw)
    assert rep.ok and rep.files_ok > 5 and not rep.orphans_tmp, rep.to_dict()
    book = read_dataset(raw, "book_1s")
    assert book.num_rows > 20 * 4
    assert set(book.column("symbol").to_pylist()) == set(rec.members)
    keys = list(zip(book.column("symbol").to_pylist(), book.column("t_sec").to_pylist()))
    assert len(keys) == len(set(keys)), "duplicate (symbol, second) bars after reconnect"
    mark = read_dataset(raw, "mark_price")
    assert set(mark.column("symbol").to_pylist()) <= set(rec.members)   # non-universe symbols filtered
    mk = list(zip(mark.column("symbol").to_pylist(), mark.column("t_event").to_pylist()))
    assert len(mk) == len(set(mk))
    depth = read_dataset(raw, "depth20")
    assert depth.num_rows > 0 and len(depth.column("bid_px")[0].as_py()) == 20
    oi = read_dataset(raw, "open_interest")
    assert oi.num_rows >= len(rec.members)
    gaps = read_dataset(raw, "gaps")
    assert "ws_disconnect" in gaps.column("kind").to_pylist()
    health = read_dataset(raw, "health")
    assert health.column("status").to_pylist()[-1] == "STOPPED"
    assert "HEALTHY" in health.column("status").to_pylist()
    lat = read_dataset(raw, "latency_1m")
    assert lat.num_rows > 0
    uni = read_dataset(raw, "universe")
    assert uni.num_rows == len(fake.symbols) + len(fake.extra)
    status = orjson.loads((tmp_path / "run" / "recorder_health.json").read_bytes())
    assert status["status"] == "STOPPED"
    assert list((raw / "exchange_info").rglob("*.json.gz"))
    h = health.to_pylist()[-2]
    assert h["lat_n"] > 0 and h["lat_p50_ms"] >= 0 and h["loop_lag_p99_ms"] >= 0
    assert h["writer_rows_per_s"] >= 0 and h["oi_polls_total"] > 0
    rep = build_report(tmp_path / "data", tmp_path / "run")
    assert rep["acceptance"]["smoke_ok"] and rep["acceptance"]["integrity_ok"]
    assert rep["acceptance"]["no_schema_errors"] and rep["websocket"]["reconnects"] >= 1
    assert rep["datasets"]["book_1s"]["rows"] == book.num_rows
    assert rep["exchange_rate_limits"][0]["limit"] == 2400
    assert "Acceptance" in to_markdown(rep)


async def _collect(conn_cfg, fake, streams, **ws_over):
    frames = Counter()

    def on_frame(raw, t_recv, conn):
        env = decode_frame(raw)
        if env.kind == "control":
            conn.on_control_reply(env.control_id, env.error)
            frames["control"] += 1
        else:
            frames[env.stream] += 1
    import dataclasses
    ws_cfg = dataclasses.replace(conn_cfg.binance.ws, **ws_over)
    conn = WsConnection("t", "public", ws_cfg, on_frame)
    conn.add_streams(streams)
    return conn, frames


async def test_ws_reconnect_resubscribes_current_set(fake, tmp_path):
    cfg = make_cfg(fake, tmp_path)
    conn, frames = await _collect(cfg, fake, ["btcusdt@bookTicker"])
    conn.start()
    try:
        await asyncio.sleep(1.0)
        assert frames["btcusdt@bookTicker"] > 0
        conn.add_streams(["ethusdt@bookTicker"])             # dynamic SUBSCRIBE while connected
        await asyncio.sleep(1.0)
        assert frames["control"] >= 1 and frames["ethusdt@bookTicker"] > 0
        await fake.drop_all()
        await asyncio.sleep(1.5)
        before = dict(frames)
        await asyncio.sleep(1.0)
        assert conn.stats.connects == 2 and conn.stats.connected
        assert frames["ethusdt@bookTicker"] > before["ethusdt@bookTicker"]   # dynamic stream survived reconnect
    finally:
        await conn.stop()


async def test_ws_silence_triggers_reconnect(fake, tmp_path):
    cfg = make_cfg(fake, tmp_path)
    # socket alive (pings answered) but no data beyond silence_max_s -> reconnect anyway
    conn, frames = await _collect(cfg, fake, ["btcusdt@bookTicker"], silence_timeout_s=1.0, silence_max_s=2.0)
    conn.start()
    try:
        await asyncio.sleep(0.8)
        fake.silence_until = time.time_ns() // 1_000_000 + 3500
        await asyncio.sleep(5.5)
        assert conn.stats.silence_reconnects >= 1
        assert conn.stats.connected and frames["btcusdt@bookTicker"] > 0
    finally:
        await conn.stop()


async def test_ws_rotation_make_before_break(fake, tmp_path):
    cfg = make_cfg(fake, tmp_path)
    conn, frames = await _collect(cfg, fake, ["btcusdt@bookTicker"], conn_max_age_s=1.5)
    conn.start()
    try:
        await asyncio.sleep(4.0)
        assert conn.stats.rotations >= 1
        assert conn.stats.disconnects == 0                   # rotation is not a disconnect
        assert conn.stats.connected
        n = conn.stats.msgs
        await asyncio.sleep(0.5)
        assert conn.stats.msgs > n                           # data keeps flowing after rotation
    finally:
        await conn.stop()


async def test_smoke_ping_survives_busy_stream(tmp_path):
    """Regression (real Binance, 2026-09-26): the pong was never read while a busy bookTicker
    stream filled the client's frame queue during the ping -> TimeoutError at stage 'ping'."""
    from jevbot.testing.fake_binance import FakeBinance, FakeConfig
    fb = FakeBinance(FakeConfig(n_symbols=20, n_extra_symbols=5, book_rate_total=20_000.0))
    await fb.start()
    try:
        for _ in range(3):
            rep = await run_smoke(make_cfg(fb, tmp_path))
            public = next(c for c in rep.checks if c.name == "ws:public")
            assert public.ok and public.detail["stage"] == "done", public.detail
            assert public.detail["ping_rtt_ms"] < 5000
    finally:
        await fb.stop()


async def test_recorder_stops_with_exit_9_on_writer_failure(fake, tmp_path, monkeypatch):
    from jevbot.recorder.recorder import EXIT_WRITER_FAILED
    from jevbot.recorder.sinks import SinkManager

    def broken_write(self, name, key, table):
        raise OSError(9, "Bad file descriptor (simulated)")
    monkeypatch.setattr(SinkManager, "_write", broken_write)
    cfg = make_cfg(fake, tmp_path)
    code = await asyncio.wait_for(Recorder(cfg).run(duration_s=30), timeout=25)
    assert code == EXIT_WRITER_FAILED


async def test_silent_but_alive_connection_is_not_reconnected(fake, tmp_path):
    """Sparse streams (illiquid klines) can be quiet; a ping probe keeps a live socket."""
    cfg = make_cfg(fake, tmp_path)
    conn, frames = await _collect(cfg, fake, ["btcusdt@bookTicker"], silence_timeout_s=1.0, silence_max_s=30.0)
    conn.start()
    try:
        await asyncio.sleep(0.8)
        fake.silence_until = time.time_ns() // 1_000_000 + 3500      # data stops, socket stays alive
        await asyncio.sleep(4.5)
        assert conn.stats.silent_but_alive >= 1
        assert conn.stats.silence_reconnects == 0 and conn.stats.connects == 1
    finally:
        await conn.stop()


async def test_stale_feed_triggers_reconnect(fake, tmp_path):
    cfg = make_cfg(fake, tmp_path)
    conn, frames = await _collect(cfg, fake, ["btcusdt@bookTicker"], stale_lag_ms=1000.0, stale_min_interval_s=60.0)
    orig = conn.on_frame

    def late(raw, t_recv, c):                  # pretend every frame arrives 3 s after its event time
        c.note_lag(3000.0)
        orig(raw, t_recv, c)
    conn.on_frame = late
    conn.start()
    try:
        await asyncio.sleep(3.0)
        assert conn.stats.stale_reconnects == 1                 # rate-limited to one per interval
        assert conn.stats.connects == 2 and conn.stats.connected
        assert conn.stats.last_disconnect_reason == "stale_feed"
    finally:
        await conn.stop()
