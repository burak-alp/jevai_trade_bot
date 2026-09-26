"""Semantic validation of downloaded Binance Vision files.

A sha256 match only proves the bytes are what Binance published; it says nothing about
whether the data is sensible. Every converted file gets a quality report::

    {"status": "ok" | "warn" | "suspect", "issues": ["<severity>:<code>:<detail>", ...], "checks": {...}}

``suspect`` files are kept (never silently deleted) but moved under ``_suspect/`` and flagged
in the manifest; ``catalog.list_files`` excludes them by default so replay does not use them.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

DAY_MS = 86_400_000
INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
               "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000, "8h": 28_800_000,
               "12h": 43_200_000, "1d": DAY_MS, "3d": 3 * DAY_MS, "1w": 7 * DAY_MS}
METRICS_CADENCE_MS = 300_000


@dataclass
class Report:
    issues: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def warn(self, code: str, detail: Any = "") -> None:
        self.issues.append(f"warn:{code}:{detail}")

    def suspect(self, code: str, detail: Any = "") -> None:
        self.issues.append(f"suspect:{code}:{detail}")

    def result(self) -> dict[str, Any]:
        status = "suspect" if any(i.startswith("suspect:") for i in self.issues) else \
            "warn" if self.issues else "ok"
        return {"status": status, "issues": self.issues, "checks": self.checks}


@dataclass(frozen=True)
class Context:
    symbol: str
    period: str                       # YYYY-MM-DD or YYYY-MM
    interval: str | None = None
    last_listed_date: str | None = None   # from the PIT listing; funding after it is suspicious


def period_bounds(period: str) -> tuple[int, int]:
    if len(period) == 10:
        d = date.fromisoformat(period)
        start = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
        return start, start + DAY_MS
    y, m = map(int, period.split("-"))
    start = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)
    return start, start + calendar.monthrange(y, m)[1] * DAY_MS


def _col(t: pa.Table, name: str) -> list[Any]:
    return t.column(name).to_pylist()


def _monotonic_and_dups(ts: list[int], rep: Report, name: str, strict_dups: bool = True) -> list[int]:
    back = sum(1 for a, b in zip(ts, ts[1:]) if b < a)
    uniq = sorted(set(ts))
    dups = len(ts) - len(uniq)
    rep.checks[f"{name}_non_monotonic"] = back
    rep.checks[f"{name}_duplicates"] = dups
    if back:
        rep.suspect(f"{name}_non_monotonic", back)
    if dups:
        (rep.suspect if strict_dups else rep.warn)(f"{name}_duplicates", dups)
    return uniq


def _outside_period(ts: list[int], ctx: Context, rep: Report, name: str) -> None:
    lo, hi = period_bounds(ctx.period)
    out = sum(1 for t in ts if t < lo or t >= hi)
    rep.checks[f"{name}_outside_period"] = out
    if out:
        rep.suspect(f"{name}_outside_period", out)


def _gap_ranges(uniq: list[int], step: int, lo: int, hi: int, limit: int = 5) -> tuple[int, list[str]]:
    expected = range(lo - lo % step + (step if lo % step else 0), hi, step)
    have = set(uniq)
    missing = [t for t in expected if t not in have]
    ranges: list[str] = []
    if missing:
        start = prev = missing[0]
        for t in missing[1:] + [None]:
            if t is None or t != prev + step:
                ranges.append(f"{start}-{prev}")
                if t is not None:
                    start = t
            if t is not None:
                prev = t
    return len(missing), ranges[:limit]


# ---------------------------------------------------------------------------
# datasets


def validate_klines(t: pa.Table, ctx: Context, *, price_positive: bool = True, has_volume: bool = True) -> Report:
    rep = Report()
    n = t.num_rows
    rep.checks["rows"] = n
    if n == 0:
        rep.suspect("empty")
        return rep
    step = INTERVAL_MS.get(ctx.interval or "")
    ot = _col(t, "open_time")
    uniq = _monotonic_and_dups(ot, rep, "open_time")
    _outside_period(ot, ctx, rep, "open_time")
    if step:
        misaligned = sum(1 for x in ot if x % step)
        rep.checks["misaligned_open_time"] = misaligned
        if misaligned:
            rep.suspect("misaligned_open_time", misaligned)
        ct = _col(t, "close_time")
        bad_close = sum(1 for a, b in zip(ot, ct) if b != a + step - 1)
        rep.checks["close_time_mismatch"] = bad_close
        if bad_close:
            rep.warn("close_time_mismatch", bad_close)
        lo, hi = period_bounds(ctx.period)
        n_missing, ranges = _gap_ranges(uniq, step, lo, hi)
        expected = (hi - lo) // step
        rep.checks.update(expected_bars=expected, missing_bars=n_missing, missing_ranges=ranges,
                          coverage=round(len(uniq) / expected, 5) if expected else None)
        if n_missing:
            rep.warn("interval_gaps", f"{n_missing} missing, first ranges {ranges}")
    o, h, lo_, c = (_col(t, k) for k in ("open", "high", "low", "close"))
    bad_high = sum(1 for oo, hh, cc in zip(o, h, c) if hh < max(oo, cc))
    bad_low = sum(1 for oo, ll, cc in zip(o, lo_, c) if ll > min(oo, cc))
    rep.checks.update(high_below_open_close=bad_high, low_above_open_close=bad_low)
    if bad_high:
        rep.suspect("high_below_max_open_close", bad_high)
    if bad_low:
        rep.suspect("low_above_min_open_close", bad_low)
    if price_positive:
        nonpos = sum(1 for x in lo_ if x is None or x <= 0)
        rep.checks["nonpositive_price"] = nonpos
        if nonpos:
            rep.suspect("nonpositive_price", nonpos)
    if has_volume:
        for col in ("volume", "quote_volume", "taker_buy_volume", "taker_buy_quote_volume", "count"):
            neg = pc.sum(pc.less(t.column(col), 0)).as_py() or 0
            rep.checks[f"negative_{col}"] = neg
            if neg:
                rep.suspect(f"negative_{col}", neg)
        over = sum(1 for tb, v in zip(_col(t, "taker_buy_volume"), _col(t, "volume")) if tb > v * (1 + 1e-9) + 1e-12)
        rep.checks["taker_buy_gt_volume"] = over
        if over:
            rep.suspect("taker_buy_gt_volume", over)
    return rep


def validate_metrics(t: pa.Table, ctx: Context) -> Report:
    rep = Report()
    n = t.num_rows
    rep.checks["rows"] = n
    if n == 0:
        rep.suspect("empty")
        return rep
    ts = _col(t, "create_time")
    # identical duplicate rows are a warning, conflicting duplicates are suspect
    seen: dict[int, tuple[Any, ...]] = {}
    conflicting = 0
    rows = list(zip(*(t.column(c).to_pylist() for c in t.column_names)))
    for r in rows:
        k = r[t.column_names.index("create_time")]
        if k in seen and seen[k] != r:
            conflicting += 1
        seen.setdefault(k, r)
    uniq = _monotonic_and_dups(ts, rep, "create_time", strict_dups=False)
    rep.checks["create_time_conflicting_duplicates"] = conflicting
    if conflicting:
        rep.suspect("create_time_conflicting_duplicates", conflicting)
    lo, hi = period_bounds(ctx.period)
    outside = sum(1 for x in ts if x < lo or x >= hi)
    rep.checks["create_time_outside_period"] = outside
    offsets: dict[int, int] = {}
    for x in uniq:
        offsets[x % METRICS_CADENCE_MS] = offsets.get(x % METRICS_CADENCE_MS, 0) + 1
    dominant, dom_n = max(offsets.items(), key=lambda kv: kv[1])
    rep.checks["create_time_offset_ms"] = dominant
    rep.checks["create_time_offset_share"] = round(dom_n / len(uniq), 4)
    rep.checks["create_time_first"] = uniq[0] - lo
    if dominant != 0:
        rep.warn("create_time_semantic_shift", f"timestamps sit {dominant} ms after the 5m grid")
    if len(offsets) > 1:
        rep.warn("create_time_mixed_offsets", len(offsets))
    if outside:
        # a whole-file shift (e.g. end-of-interval labels crossing midnight) vs stray rows
        (rep.warn if outside <= 2 else rep.suspect)("create_time_outside_period", outside)
    expected = (hi - lo) // METRICS_CADENCE_MS
    aligned = [x - x % METRICS_CADENCE_MS for x in uniq if lo <= x < hi]
    coverage = len(set(aligned)) / expected if expected else 0
    rep.checks.update(expected_rows=expected, coverage_5m=round(coverage, 4))
    if coverage < 0.95:
        rep.warn("cadence_coverage_low", round(coverage, 4))
    syms = set(_col(t, "symbol"))
    if syms != {ctx.symbol}:
        rep.suspect("symbol_mismatch", sorted(syms)[:3])
    for col in ("sum_open_interest", "sum_open_interest_value"):
        neg = pc.sum(pc.less(t.column(col), 0)).as_py() or 0
        if neg:
            rep.suspect(f"negative_{col}", neg)
    return rep


def validate_book_depth(t: pa.Table, ctx: Context) -> Report:
    rep = Report()
    n = t.num_rows
    rep.checks["rows"] = n
    if n == 0:
        rep.suspect("empty")
        return rep
    ts, pct, depth, notional = (_col(t, k) for k in ("timestamp", "percentage", "depth", "notional"))
    neg = sum(1 for d, v in zip(depth, notional) if d < 0 or v < 0)
    rep.checks["negative_values"] = neg
    if neg:
        rep.suspect("negative_depth_or_notional", neg)
    _outside_period(ts, ctx, rep, "timestamp")
    stamps = sorted(set(ts))
    diffs = sorted(b - a for a, b in zip(stamps, stamps[1:]))
    if diffs:
        med = diffs[len(diffs) // 2]
        rep.checks.update(timestamps=len(stamps), cadence_median_ms=med, cadence_max_gap_ms=diffs[-1],
                          cadence_p95_ms=diffs[int(0.95 * (len(diffs) - 1))])
        if diffs[-1] > max(10 * med, 600_000):
            rep.warn("cadence_gap", diffs[-1])
    bands: dict[float, list[tuple[int, float]]] = {}
    for a, p, d in zip(ts, pct, depth):
        bands.setdefault(p, []).append((a, d))
    rep.checks["bands"] = sorted(bands)
    frozen, constant = [], []
    for p, series in bands.items():
        series.sort()
        vals = [d for _, d in series]
        if len(vals) < 10:
            continue
        repeats = sum(1 for a, b in zip(vals, vals[1:]) if a == b) / (len(vals) - 1)
        if len(set(vals)) == 1:
            constant.append(p)
        elif repeats > 0.5:
            frozen.append((p, round(repeats, 3)))
    rep.checks.update(frozen_bands=frozen, constant_bands=constant)
    if frozen:
        rep.warn("frozen_bands", frozen)
    if constant and len(constant) == len([b for b in bands.values() if len(b) >= 10]):
        rep.suspect("non_changing_series", "every band constant for the whole file")
    elif constant:
        rep.warn("constant_bands", constant)
    # cumulative depth must not shrink further away from mid on each side
    by_ts: dict[int, dict[float, float]] = {}
    for a, p, d in zip(ts, pct, depth):
        by_ts.setdefault(a, {})[p] = d
    bad = 0
    for snap in by_ts.values():
        for side in (1, -1):
            ps = sorted((p for p in snap if p * side > 0), key=abs)
            bad += sum(1 for x, y in zip(ps, ps[1:]) if snap[y] + 1e-12 < snap[x])
    rep.checks["non_cumulative_snapshots"] = bad
    if bad:
        rep.warn("depth_not_cumulative", bad)
    return rep


def validate_funding(t: pa.Table, ctx: Context) -> Report:
    rep = Report()
    rep.checks["rows"] = t.num_rows
    if t.num_rows == 0:
        rep.warn("empty")
        return rep
    ts = _col(t, "calc_time")
    _monotonic_and_dups(ts, rep, "calc_time")
    _outside_period(ts, ctx, rep, "calc_time")
    intervals = sorted(set(_col(t, "funding_interval_hours")))
    rep.checks["funding_interval_hours"] = intervals
    if any(h not in (1, 2, 4, 8) for h in intervals):
        rep.warn("unusual_funding_interval", intervals)
    extreme = sum(1 for r in _col(t, "last_funding_rate") if r is not None and abs(r) > 0.05)
    rep.checks["extreme_rates"] = extreme
    if extreme:
        rep.warn("extreme_funding_rate", extreme)
    if ctx.last_listed_date:
        cutoff = period_bounds(ctx.last_listed_date)[1]
        after = sum(1 for x in ts if x >= cutoff)
        rep.checks["after_last_listed_date"] = after
        if after:
            rep.warn("post_delisting_observations", f"{after} rows after {ctx.last_listed_date}")
    return rep


def validate_agg_trades_file(path: Path, ctx: Context) -> Report:
    """Streaming checks (row group by row group) for potentially very large files."""
    rep = Report()
    pf = pq.ParquetFile(path)
    last_id = last_t = None
    back_id = back_t = nonpos = rows = 0
    lo, hi = period_bounds(ctx.period)
    outside = 0
    for i in range(pf.num_row_groups):
        rg = pf.read_row_group(i, columns=["agg_trade_id", "transact_time", "price", "quantity"])
        ids, tt = rg.column("agg_trade_id").to_pylist(), rg.column("transact_time").to_pylist()
        rows += len(ids)
        prev_id, prev_t = last_id, last_t
        for a, b in zip(ids, tt):
            if prev_id is not None and a <= prev_id:
                back_id += 1
            if prev_t is not None and b < prev_t:
                back_t += 1
            if b < lo or b >= hi:
                outside += 1
            prev_id, prev_t = a, b
        last_id, last_t = prev_id, prev_t
        nonpos += (pc.sum(pc.less_equal(rg.column("price"), 0)).as_py() or 0) + \
                  (pc.sum(pc.less_equal(rg.column("quantity"), 0)).as_py() or 0)
    rep.checks.update(rows=rows, id_not_increasing=back_id, time_decreasing=back_t, nonpositive=nonpos,
                      outside_period=outside)
    if rows == 0:
        rep.suspect("empty")
    if back_id:
        rep.suspect("agg_trade_id_not_increasing", back_id)
    if back_t:
        rep.warn("transact_time_decreasing", back_t)
    if nonpos:
        rep.suspect("nonpositive_price_or_qty", nonpos)
    if outside:
        rep.suspect("transact_time_outside_period", outside)
    return rep


def validate_file(dataset: str, path: Path, ctx: Context) -> dict[str, Any]:
    if dataset == "aggTrades":
        return validate_agg_trades_file(path, ctx).result()
    t = pq.read_table(path)
    if dataset == "klines":
        rep = validate_klines(t, ctx)
    elif dataset in ("markPriceKlines", "indexPriceKlines"):
        rep = validate_klines(t, ctx, has_volume=False)
    elif dataset == "premiumIndexKlines":
        rep = validate_klines(t, ctx, price_positive=False, has_volume=False)
    elif dataset == "metrics":
        rep = validate_metrics(t, ctx)
    elif dataset == "bookDepth":
        rep = validate_book_depth(t, ctx)
    elif dataset == "fundingRate":
        rep = validate_funding(t, ctx)
    else:
        rep = Report()
        rep.checks["rows"] = t.num_rows
    return rep.result()
