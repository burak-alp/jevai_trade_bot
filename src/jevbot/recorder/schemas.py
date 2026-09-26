"""Parquet schemas of every recorder dataset (``SCHEMA_VERSION`` is stored in file metadata)."""

from __future__ import annotations

import pyarrow as pa

SCHEMA_VERSION = "rec.v1"

i64, f64, s, b, i32 = pa.int64(), pa.float64(), pa.string(), pa.bool_(), pa.int32()
lf64 = pa.list_(pa.float64())

SCHEMAS: dict[str, pa.Schema] = {
    # closed 1m klines (ws + REST backfill)
    "kline_1m": pa.schema([
        ("symbol", s), ("open_time", i64), ("close_time", i64),
        ("open", f64), ("high", f64), ("low", f64), ("close", f64),
        ("volume", f64), ("quote_volume", f64), ("n_trades", i64),
        ("taker_buy_base", f64), ("taker_buy_quote", f64),
        ("t_event", i64), ("t_recv", i64), ("source", s),
    ]),
    # !markPrice@arr@1s (universe members only)
    "mark_price": pa.schema([
        ("symbol", s), ("t_event", i64), ("mark", f64), ("index", f64), ("est_settle", f64),
        ("funding_rate", f64), ("next_funding_time", i64), ("t_recv", i64),
    ]),
    # bookTicker aggregated to 1-second bars keyed by exchange event time
    "book_1s": pa.schema([
        ("symbol", s), ("t_sec", i64),
        ("mid_open", f64), ("mid_high", f64), ("mid_low", f64), ("mid_close", f64),
        ("spread_bps_mean", f64), ("spread_bps_min", f64), ("spread_bps_max", f64),
        ("bid_last", f64), ("ask_last", f64), ("bid_qty_last", f64), ("ask_qty_last", f64),
        ("n_updates", i32), ("last_update_id", i64), ("t_event_last", i64), ("t_recv_last", i64),
    ]),
    # depth20@500ms sampled every sample_interval_s
    "depth20": pa.schema([
        ("symbol", s), ("t_event", i64), ("t_trans", i64), ("t_recv", i64), ("last_update_id", i64),
        ("bid_px", lf64), ("bid_qty", lf64), ("ask_px", lf64), ("ask_qty", lf64),
    ]),
    "open_interest": pa.schema([
        ("symbol", s), ("open_interest", f64), ("t_server", i64), ("t_req", i64), ("t_recv", i64),
    ]),
    "force_order": pa.schema([
        ("symbol", s), ("t_event", i64), ("t_trade", i64), ("side", s), ("order_type", s),
        ("time_in_force", s), ("qty", f64), ("price", f64), ("avg_price", f64), ("status", s),
        ("last_filled_qty", f64), ("filled_acc_qty", f64), ("t_recv", i64),
    ]),
    # t_recv - t_event per stream family and route, aggregated per minute
    "latency_1m": pa.schema([
        ("t_min", i64), ("family", s), ("route", s), ("n", i64), ("n_sampled", i64),
        ("lag_min_ms", f64), ("lag_p50_ms", f64), ("lag_p90_ms", f64), ("lag_p99_ms", f64), ("lag_max_ms", f64),
    ]),
    "gaps": pa.schema([
        ("symbol", s), ("stream", s), ("kind", s), ("t_start", i64), ("t_end", i64),
        ("detected_at", i64), ("backfilled", b), ("n_missing", i64), ("detail", s),
    ]),
    "health": pa.schema([
        ("t", i64), ("status", s), ("uptime_s", f64), ("cpu_pct", f64), ("rss_mb", f64),
        ("msgs_per_s", f64), ("msgs_per_s_public", f64), ("msgs_per_s_market", f64), ("bytes_per_s", f64),
        ("conns_connected", i32), ("conns_total", i32), ("reconnects_total", i64), ("schema_errors_total", i64),
        ("symbols_universe", i32), ("symbols_kline_fresh", i32), ("symbols_book_fresh", i32),
        ("rows_written_total", i64), ("parquet_bytes_total", i64), ("files_total", i64), ("queue_depth", i32),
        ("clock_offset_ms", f64), ("rest_weight_used", i32), ("rest_weight_limit", i32), ("detail", s),
    ]),
    "universe": pa.schema([
        ("t_asof", i64), ("symbol", s), ("status", s), ("contract_type", s), ("onboard_date", i64),
        ("delivery_date", i64), ("tick_size", f64), ("step_size", f64), ("min_qty", f64), ("min_notional", f64),
        ("quote_vol_24h", f64), ("vol_rank", i32), ("in_universe", b), ("reason", s),
    ]),
}

# column used for the manifest's t_min/t_max
TIME_COLUMN = {
    "kline_1m": "open_time", "mark_price": "t_event", "book_1s": "t_sec", "depth20": "t_event",
    "open_interest": "t_server", "force_order": "t_event", "latency_1m": "t_min", "gaps": "detected_at",
    "health": "t", "universe": "t_asof",
}
