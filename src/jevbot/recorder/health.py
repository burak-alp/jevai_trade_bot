"""Process + data health snapshots for the recorder."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import orjson
import psutil

from jevbot.core.time import mono_ms, now_ms

from jevbot.recorder.schemas import SCHEMAS

STARTING, HEALTHY, DEGRADED, UNHEALTHY, STOPPED = "STARTING", "HEALTHY", "DEGRADED", "UNHEALTHY", "STOPPED"

NAN = float("nan")
PERF_DEFAULTS: dict[str, Any] = {
    "lat_n": 0, "lat_p50_ms": NAN, "lat_p95_ms": NAN, "lat_p99_ms": NAN, "lat_max_ms": NAN,
    "loop_lag_p50_ms": NAN, "loop_lag_p99_ms": NAN, "loop_lag_max_ms": NAN,
    "sink_buffered_rows": 0, "open_files": 0, "writer_rows_per_s": 0.0,
    "duplicates_total": 0, "invalid_total": 0, "late_rows_total": 0, "oi_polls_total": 0,
    "rest_requests_total": 0,
}


@dataclass
class Counters:
    schema_errors: dict[str, int] = field(default_factory=dict)
    frames_by_family: dict[str, int] = field(default_factory=dict)
    control_replies: int = 0
    klines_ws: int = 0
    klines_rest: int = 0
    backfill_requests: int = 0
    backfill_failures: int = 0
    gaps_detected: int = 0
    oi_polls: int = 0
    oi_errors: int = 0
    rate_limited: int = 0

    def total_schema_errors(self) -> int:
        return sum(self.schema_errors.values())


class HealthMonitor:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.proc = psutil.Process(os.getpid())
        self.proc.cpu_percent(None)                  # prime
        self.started_mono = mono_ms()
        self._last_mono = mono_ms()
        self._last_msgs: dict[str, int] = {}
        self._last_bytes = 0
        self.smoke_ok: bool | None = None
        self.status = STARTING
        self.last_snapshot: dict[str, Any] = {}

    def snapshot(self, *, conns: list[Any], counters: Counters, symbols_universe: int, kline_fresh: int,
                 book_fresh: int, book_expected: int, sink: Any, clock_offset_ms: float | None,
                 rest_weights: Any, writer_error: BaseException | None,
                 perf: dict[str, Any] | None = None, lag_warn_ms: float = 2000.0,
                 loop_lag_warn_ms: float = 500.0) -> dict[str, Any]:
        now_m = mono_ms()
        dt = max(1e-3, (now_m - self._last_mono) / 1000)
        self._last_mono = now_m
        per_route: dict[str, float] = {}
        total_msgs_rate = 0.0
        total_bytes = 0
        for c in conns:
            prev = self._last_msgs.get(c.name, 0)
            rate = (c.stats.msgs - prev) / dt
            self._last_msgs[c.name] = c.stats.msgs
            per_route[c.route] = per_route.get(c.route, 0.0) + rate
            total_msgs_rate += rate
            total_bytes += c.stats.bytes
        bytes_rate = (total_bytes - self._last_bytes) / dt
        self._last_bytes = total_bytes
        connected = sum(1 for c in conns if c.stats.connected)
        reconnects = sum(max(0, c.stats.connects - 1) for c in conns)
        mem = self.proc.memory_info()
        problems: list[str] = []
        if self.smoke_ok is False:
            problems.append("smoke_failed")
        if writer_error is not None:
            problems.append(f"writer_error:{writer_error!r}"[:120])
        if conns and connected == 0:
            problems.append("no_connections")
        status = UNHEALTHY if problems else HEALTHY
        if status == HEALTHY:
            if self.smoke_ok is None:
                status = STARTING
            if connected < len(conns):
                problems.append(f"disconnected:{len(conns) - connected}")
            if symbols_universe and kline_fresh < 0.9 * symbols_universe:
                problems.append(f"kline_stale:{symbols_universe - kline_fresh}")
            if book_expected and book_fresh < 0.75 * book_expected:
                problems.append(f"book_stale:{book_expected - book_fresh}")
            if rest_weights is not None and not rest_weights.budget_ok(0):
                problems.append("rest_budget_exhausted")
            p = perf or {}
            if (p.get("lat_p99_ms") or 0) > lag_warn_ms:
                problems.append(f"feed_lag_p99:{p['lat_p99_ms']:.0f}ms")
            if (p.get("loop_lag_p99_ms") or 0) > loop_lag_warn_ms:
                problems.append(f"loop_lag_p99:{p['loop_lag_p99_ms']:.0f}ms")
            if problems and status == HEALTHY:
                status = DEGRADED
        self.status = status
        snap = {
            "t": now_ms(), "status": status, "uptime_s": round((now_m - self.started_mono) / 1000, 1),
            "cpu_pct": self.proc.cpu_percent(None), "rss_mb": round(mem.rss / 2**20, 1),
            "msgs_per_s": round(total_msgs_rate, 1),
            "msgs_per_s_public": round(per_route.get("public", 0.0), 1),
            "msgs_per_s_market": round(per_route.get("market", 0.0), 1),
            "bytes_per_s": round(bytes_rate, 1),
            "conns_connected": connected, "conns_total": len(conns), "reconnects_total": reconnects,
            "schema_errors_total": counters.total_schema_errors(),
            "symbols_universe": symbols_universe, "symbols_kline_fresh": kline_fresh,
            "symbols_book_fresh": book_fresh,
            "rows_written_total": sink.stats.total("rows_written"),
            "parquet_bytes_total": sink.stats.total("bytes_final"),
            "files_total": sink.stats.total("files_final"),
            "queue_depth": sink.queue_depth(),
            "clock_offset_ms": float(clock_offset_ms) if clock_offset_ms is not None else float("nan"),
            "rest_weight_used": rest_weights.current_used() if rest_weights else 0,
            "rest_weight_limit": rest_weights.limit_per_min if rest_weights else 0,
            "detail": ",".join(problems),
        }
        snap.update(PERF_DEFAULTS)
        snap.update(perf or {})
        self.last_snapshot = snap
        return snap

    def row(self, snap: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(snap[f.name] for f in SCHEMAS["health"])

    def write_status_file(self, snap: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        doc = dict(snap)
        if extra:
            doc.update(extra)
        tmp = self.run_dir / "recorder_health.json.tmp"
        tmp.write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2, default=str))
        os.replace(tmp, self.run_dir / "recorder_health.json")
