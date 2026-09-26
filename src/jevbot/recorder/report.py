"""``jevbot recording-report``: summarize a recorder run from its own output files.

Everything is derived from the recorded datasets + manifests + run directory, so the same
report can be produced on any machine that has the data directory.
"""

from __future__ import annotations

import gzip
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import orjson
import pyarrow as pa
import pyarrow.parquet as pq

from jevbot.core.time import MINUTE_MS, ms_to_iso
from jevbot.recorder.integrity import read_manifest, verify_tree
from jevbot.recorder.schemas import SCHEMAS


def _q(vals: list[float], p: float) -> float | None:
    v = sorted(x for x in vals if x is not None and not (isinstance(x, float) and math.isnan(x)))
    if not v:
        return None
    return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]


def _entries(raw: Path, dataset: str, since: int | None, until: int | None) -> list[tuple[Path, dict[str, Any]]]:
    d = raw / dataset
    out = []
    for rel, e in read_manifest(d).items():
        ws = e.get("window_start", e.get("t_min"))
        if ws is None:
            continue
        if since is not None and e.get("t_max", ws) < since:
            continue
        if until is not None and ws >= until:
            continue
        out.append((d / rel, e))
    return out


def _read(raw: Path, dataset: str, since: int | None, until: int | None, columns: list[str] | None = None) -> pa.Table:
    files = [p for p, _ in _entries(raw, dataset, since, until) if p.exists()]
    if not files:
        return pa.table({f.name: pa.array([], type=f.type) for f in SCHEMAS[dataset]
                         if columns is None or f.name in columns})
    return pa.concat_tables([pq.read_table(p, columns=columns) for p in files], promote_options="default")


def build_report(data_dir: Path, run_dir: Path, since: int | None = None, until: int | None = None) -> dict[str, Any]:
    raw = Path(data_dir) / "raw"
    rep: dict[str, Any] = {}
    health = sorted(_read(raw, "health", since, until).to_pylist(), key=lambda r: r["t"])
    if since is not None:
        health = [h for h in health if h["t"] >= since]
    if until is not None:
        health = [h for h in health if h["t"] < until]
    if not health:
        return {"error": "no health rows in range"}
    t0, t1 = health[0]["t"], health[-1]["t"]
    interval_s = (health[1]["t"] - health[0]["t"]) / 1000 if len(health) > 1 else 10.0
    duration_s = (t1 - t0) / 1000 + interval_s
    hours = duration_s / 3600
    live = [h for h in health if h["status"] != "STOPPED"]
    steady = [h for h in live if h["uptime_s"] > 30] or live
    rep["window"] = {"from": ms_to_iso(t0), "to": ms_to_iso(t1), "duration_s": round(duration_s, 1)}
    rep["status_counts"] = dict(Counter(h["status"] for h in health))
    rep["degraded_details"] = dict(Counter(h["detail"] for h in health if h["detail"]).most_common(10))
    last = live[-1] if live else health[-1]
    uni = _read(raw, "universe", since, until, ["t_asof", "symbol", "in_universe"]).to_pylist()
    last_asof = max((u["t_asof"] for u in uni), default=None)
    rep["universe"] = {"symbols_recorded": last["symbols_universe"],
                       "in_universe_last_snapshot": sum(1 for u in uni if u["t_asof"] == last_asof and u["in_universe"]),
                       "listed_last_snapshot": sum(1 for u in uni if u["t_asof"] == last_asof)}
    rep["websocket"] = {"connections": last["conns_total"],
                        "connected_min": min(h["conns_connected"] for h in steady),
                        "reconnects": health[-1]["reconnects_total"] - health[0]["reconnects_total"]}

    def dist(key: str, scale: float = 1.0) -> dict[str, Any]:
        v = [h[key] * scale for h in steady]
        return {"p50": _q(v, 0.5), "p95": _q(v, 0.95), "max": _q(v, 1.0),
                "mean": round(sum(v) / len(v), 2) if v else None}

    rep["throughput"] = {"msgs_per_s": dist("msgs_per_s"), "msgs_per_s_public": dist("msgs_per_s_public"),
                         "msgs_per_s_market": dist("msgs_per_s_market"), "bytes_per_s": dist("bytes_per_s"),
                         "bytes_note": "decoded WebSocket payload bytes; not on-wire bandwidth",
                         "writer_rows_per_s": dist("writer_rows_per_s")}
    rep["process"] = {"cpu_pct": dist("cpu_pct"), "rss_mb": dist("rss_mb"),
                      "loop_lag_p99_ms": dist("loop_lag_p99_ms"), "loop_lag_max_ms": _q([h["loop_lag_max_ms"] for h in steady], 1.0),
                      "sink_buffered_rows_max": _q([h["sink_buffered_rows"] for h in steady], 1.0),
                      "writer_queue_max": _q([h["queue_depth"] for h in steady], 1.0)}
    rep["feed_latency_ms_health"] = {"p50_of_p50": _q([h["lat_p50_ms"] for h in steady], 0.5),
                                     "p95_of_p95": _q([h["lat_p95_ms"] for h in steady], 0.95),
                                     "max_p99": _q([h["lat_p99_ms"] for h in steady], 1.0),
                                     "max": _q([h["lat_max_ms"] for h in steady], 1.0)}
    offs = [h["clock_offset_ms"] for h in steady]
    off_med = _q(offs, 0.5)
    rep["clock"] = {"offset_ms_median": off_med, "offset_ms_max_abs": _q([abs(o) for o in offs if o == o], 1.0),
                    "note": "offset = exchange - local; true latency ~= raw (t_recv - t_event) + offset"}
    if off_med is not None:
        f = rep["feed_latency_ms_health"]
        rep["feed_latency_ms_offset_corrected"] = {k: (round(v + off_med, 2) if v is not None else None)
                                                   for k, v in f.items()}
    # feed stalls: consecutive health intervals whose feed-latency p99 >= 2 s
    episodes, cur = [], None
    for h in steady:
        bad = (h["lat_p99_ms"] or 0) >= 2000
        if bad and cur is None:
            cur = {"from": h["t"], "to": h["t"], "max_p99_ms": h["lat_p99_ms"]}
        elif bad:
            cur["to"], cur["max_p99_ms"] = h["t"], max(cur["max_p99_ms"], h["lat_p99_ms"])
        elif cur is not None:
            episodes.append(cur)
            cur = None
    if cur is not None:
        episodes.append(cur)
    for e in episodes:
        e["duration_s"] = round((e["to"] - e["from"]) / 1000 + interval_s, 1)
        e["from"], e["to"] = ms_to_iso(e["from"]), ms_to_iso(e["to"])
    good_share = sum(1 for h in steady if (h["lat_p99_ms"] or 0) < 2000) / max(1, len(steady))
    # diagnostics: is a stall the recorder, the clock, or the network/bandwidth?
    stall = [h for h in steady if (h["lat_p99_ms"] or 0) >= 2000]
    calm = [h for h in steady if (h["lat_p99_ms"] or 0) < 2000]

    def med(rows: list[dict[str, Any]], k: str) -> float | None:
        return _q([r[k] for r in rows], 0.5)

    def pearson(xs: list[float], ys: list[float]) -> float | None:
        pts = [(x, y) for x, y in zip(xs, ys) if x == x and y == y]
        if len(pts) < 3:
            return None
        mx, my = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        sxy = sum((x - mx) * (y - my) for x, y in pts)
        sxx, syy = sum((x - mx) ** 2 for x, _ in pts), sum((y - my) ** 2 for _, y in pts)
        return round(sxy / (sxx * syy) ** 0.5, 3) if sxx and syy else None
    corrected_ok = sum(1 for h in steady if (h["lat_p99_ms"] or 0) + (h["clock_offset_ms"]
                       if h["clock_offset_ms"] == h["clock_offset_ms"] else 0) < 2000) / max(1, len(steady))
    rep["stall_diagnostics"] = {
        "intervals_stall_vs_calm": [len(stall), len(calm)],
        "median_bytes_per_s_stall_vs_calm": [med(stall, "bytes_per_s"), med(calm, "bytes_per_s")],
        "median_msgs_per_s_stall_vs_calm": [med(stall, "msgs_per_s"), med(calm, "msgs_per_s")],
        "median_cpu_stall_vs_calm": [med(stall, "cpu_pct"), med(calm, "cpu_pct")],
        "max_loop_lag_p99_in_stalls": _q([h["loop_lag_p99_ms"] for h in stall], 1.0),
        "corr_lat_p99_vs_bytes_per_s": pearson([h["bytes_per_s"] for h in steady], [h["lat_p99_ms"] for h in steady]),
        "corr_lat_p99_vs_msgs_per_s": pearson([h["msgs_per_s"] for h in steady], [h["lat_p99_ms"] for h in steady]),
        "intervals_ok_share_clock_corrected": round(corrected_ok, 4),
    }
    rep["feed_stalls"] = {"intervals_ok_share": round(good_share, 4), "episodes": episodes,
                          "longest_s": max((e["duration_s"] for e in episodes), default=0.0)}
    lat = _read(raw, "latency_1m", since, until).to_pylist()
    by = defaultdict(list)
    for r in lat:
        by[f"{r['family']}/{r['route']}"].append(r)
    rep["stall_minutes_by_stream"] = {k: sum(1 for r in rows if (r["lag_p99_ms"] or 0) >= 2000)
                                      for k, rows in sorted(by.items())}
    rep["feed_latency_ms_by_stream"] = {k: {"messages": sum(r["n"] for r in rows),
                                            "p50_median": _q([r["lag_p50_ms"] for r in rows], 0.5),
                                            "p95_median": _q([r["lag_p95_ms"] for r in rows], 0.5),
                                            "p99_max": _q([r["lag_p99_ms"] for r in rows], 1.0),
                                            "max": _q([r["lag_max_ms"] for r in rows], 1.0)}
                                        for k, rows in sorted(by.items())}
    rep["errors"] = {"schema_errors": health[-1]["schema_errors_total"] - health[0]["schema_errors_total"],
                     "duplicates_dropped": health[-1]["duplicates_total"] - health[0]["duplicates_total"],
                     "invalid_dropped": health[-1]["invalid_total"] - health[0]["invalid_total"],
                     "late_rows": health[-1]["late_rows_total"]}
    gaps = _read(raw, "gaps", since, until).to_pylist()
    rep["gaps"] = {"by_kind": dict(Counter(g["kind"] for g in gaps)),
                   "disconnect_reasons": dict(Counter(g["detail"].split("reason=")[-1] for g in gaps
                                                      if g["kind"] == "ws_disconnect" and "reason=" in g["detail"])),
                   "ws_disconnect_events": len({(g["t_start"], g["detail"]) for g in gaps if g["kind"] == "ws_disconnect"}),
                   "kline_gaps_backfilled": sum(1 for g in gaps if g["kind"].startswith("kline") and g["backfilled"]),
                   "kline_gaps_unfilled": sum(1 for g in gaps if g["kind"].startswith("kline") and not g["backfilled"]),
                   "kline_minutes_missing_before_backfill": sum(g["n_missing"] for g in gaps if g["kind"].startswith("kline"))}
    kl = _read(raw, "kline_1m", since, until, ["symbol", "open_time", "source"]).to_pylist()
    per_sym = defaultdict(set)
    for r in kl:
        per_sym[r["symbol"]].add(r["open_time"])
    minutes = sorted({r["open_time"] for r in kl})
    exp = len(minutes)
    complete = sum(1 for s in per_sym if len(per_sym[s]) >= exp)
    rep["kline_completeness"] = {"minutes": exp, "symbols": len(per_sym), "symbols_complete": complete,
                                 "sources": dict(Counter(r["source"] for r in kl)),
                                 "cells_missing": sum(exp - len(v) for v in per_sym.values())}
    rep["rest"] = {"weight_used_1m": {"p50": _q([h["rest_weight_used"] for h in steady], 0.5),
                                      "p95": _q([h["rest_weight_used"] for h in steady], 0.95),
                                      "max": _q([h["rest_weight_used"] for h in steady], 1.0)},
                   "weight_limit_1m": last["rest_weight_limit"],
                   "requests": health[-1]["rest_requests_total"] - health[0]["rest_requests_total"],
                   "oi_polls": health[-1]["oi_polls_total"] - health[0]["oi_polls_total"],
                   "oi_polls_per_min": round((health[-1]["oi_polls_total"] - health[0]["oi_polls_total"])
                                             / max(duration_s / 60, 1e-9), 1)}
    smoke = Path(run_dir) / "smoke_report.json"
    limits = None
    if smoke.exists():
        sd = orjson.loads(smoke.read_bytes())
        rep["smoke"] = {"ok": sd.get("ok"), "checks": {c["name"]: c["ok"] for c in sd.get("checks", [])}}
        limits = next((c["detail"].get("rate_limits") for c in sd.get("checks", []) if c["name"] == "rest"), None)
    snaps = sorted((raw / "exchange_info").rglob("*.json.gz"))
    if snaps:
        info = orjson.loads(gzip.decompress(snaps[-1].read_bytes()))
        limits = info.get("rateLimits", limits)
    rep["exchange_rate_limits"] = limits
    ds_stats = {}
    for ds in SCHEMAS:
        ents = _entries(raw, ds, since, until)
        rows = sum(e.get("rows", 0) for _, e in ents)
        b = sum(e.get("bytes", 0) for _, e in ents)
        ds_stats[ds] = {"files": len(ents), "rows": rows, "rows_per_hour": round(rows / hours) if hours else None,
                        "mb": round(b / 1e6, 2), "mb_per_hour": round(b / 1e6 / hours, 2) if hours else None}
    rep["datasets"] = ds_stats
    rep["disk_mb_per_hour_total"] = round(sum(v["mb_per_hour"] or 0 for v in ds_stats.values()), 1)
    v = verify_tree(raw)
    rep["integrity"] = {"ok": v.ok, "files_ok": v.files_ok, "rows_ok": v.rows_ok,
                        "problems": {k: val for k, val in v.to_dict().items()
                                     if isinstance(val, list) and val}}
    rep["acceptance"] = {
        "smoke_ok": rep.get("smoke", {}).get("ok"),
        "integrity_ok": v.ok,
        "no_schema_errors": rep["errors"]["schema_errors"] == 0,
        "no_unhealthy": rep["status_counts"].get("UNHEALTHY", 0) == 0,
        "kline_complete": rep["kline_completeness"]["cells_missing"] == 0,
        "clock_offset_under_500ms": (rep["clock"]["offset_ms_max_abs"] or 0) < 500,
        # A single socket stalling on the network is detected and reconnected (stale_feed); the gate
        # fails on sustained or frequent staleness: >= 99 % of intervals p99 < 2 s and no stall > 30 s.
        "feed_lag_p99_under_2s_99pct": good_share >= 0.99,
        "no_feed_stall_over_30s": rep["feed_stalls"]["longest_s"] <= 30,
        "loop_lag_p99_under_500ms": (rep["process"]["loop_lag_p99_ms"]["max"] or 0) < 500,
    }
    return rep


def to_markdown(rep: dict[str, Any]) -> str:
    if "error" in rep:
        return f"**error:** {rep['error']}\n"
    L = [f"# Recording report {rep['window']['from']} → {rep['window']['to']} ({rep['window']['duration_s']:.0f} s)", ""]
    a = rep["acceptance"]
    L.append("## Acceptance")
    L += [f"- {'✅' if ok else '❌' if ok is False else '·'} {k}" for k, ok in a.items()]
    L += ["", "## Summary", "| metric | value |", "|---|---|"]
    t, p = rep["throughput"], rep["process"]
    rows = [
        ("universe symbols", rep["universe"]["symbols_recorded"]),
        ("ws connections / reconnects", f"{rep['websocket']['connections']} / {rep['websocket']['reconnects']}"),
        ("msgs/s p50 / p95 / max", f"{t['msgs_per_s']['p50']} / {t['msgs_per_s']['p95']} / {t['msgs_per_s']['max']}"),
        ("CPU % mean / p95 / max", f"{p['cpu_pct']['mean']} / {p['cpu_pct']['p95']} / {p['cpu_pct']['max']}"),
        ("RSS MB p50 / max", f"{p['rss_mb']['p50']} / {p['rss_mb']['max']}"),
        ("event-loop lag p99 max (ms)", p["loop_lag_p99_ms"]["max"]),
        ("feed latency p50 / p95 / max-p99 (ms)", f"{rep['feed_latency_ms_health']['p50_of_p50']} / "
         f"{rep['feed_latency_ms_health']['p95_of_p95']} / {rep['feed_latency_ms_health']['max_p99']}"),
        ("clock offset median / max abs (ms)", f"{rep['clock']['offset_ms_median']} / {rep['clock']['offset_ms_max_abs']}"),
        ("feed latency offset-corrected p50 / max-p99 (ms)",
         f"{rep.get('feed_latency_ms_offset_corrected', {}).get('p50_of_p50')} / "
         f"{rep.get('feed_latency_ms_offset_corrected', {}).get('max_p99')}"),
        ("feed stalls (p99 >= 2 s): ok-share / longest s / episodes",
         f"{rep['feed_stalls']['intervals_ok_share']} / {rep['feed_stalls']['longest_s']} / "
         f"{len(rep['feed_stalls']['episodes'])}"),
        ("schema errors", rep["errors"]["schema_errors"]),
        ("duplicates / invalid dropped", f"{rep['errors']['duplicates_dropped']} / {rep['errors']['invalid_dropped']}"),
        ("gaps", rep["gaps"]["by_kind"]),
        ("kline cells missing (after backfill)", rep["kline_completeness"]["cells_missing"]),
        ("REST weight used/min p50 / max (limit)", f"{rep['rest']['weight_used_1m']['p50']} / "
         f"{rep['rest']['weight_used_1m']['max']} ({rep['rest']['weight_limit_1m']})"),
        ("OI polls/min", rep["rest"]["oi_polls_per_min"]),
        ("disk MB/hour total", rep["disk_mb_per_hour_total"]),
        ("Parquet integrity", "ok" if rep["integrity"]["ok"] else rep["integrity"]["problems"]),
        ("exchange rate limits", rep["exchange_rate_limits"]),
    ]
    L += [f"| {k} | {v} |" for k, v in rows]
    L += ["", "## Datasets", "| dataset | files | rows | rows/h | MB | MB/h |", "|---|---|---|---|---|---|"]
    L += [f"| {k} | {v['files']} | {v['rows']} | {v['rows_per_hour']} | {v['mb']} | {v['mb_per_hour']} |"
          for k, v in rep["datasets"].items()]
    L += ["", "## Feed latency by stream (ms)", "| stream | messages | p50 | p95 | p99 max | max |", "|---|---|---|---|---|---|"]
    L += [f"| {k} | {v['messages']} | {v['p50_median']} | {v['p95_median']} | {v['p99_max']} | {v['max']} |"
          for k, v in rep["feed_latency_ms_by_stream"].items()]
    return "\n".join(L) + "\n"


def minutes_ago(n: float, now: int) -> int:
    return now - int(n * MINUTE_MS)
