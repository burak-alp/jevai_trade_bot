import hashlib
import io
import zipfile
from datetime import date

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from jevbot.core.config import HistConfig
from jevbot.hist.binance_vision import BinanceVision
from jevbot.hist.catalog import list_files
from jevbot.hist.validate import (
    Context,
    period_bounds,
    validate_book_depth,
    validate_funding,
    validate_klines,
    validate_metrics,
)
from jevbot.recorder.integrity import read_manifest

D0, _ = period_bounds("2024-01-01")
M = 60_000


def klines(n=1440, start=D0, mutate=None):
    rows = []
    for i in range(n):
        ot = start + i * M
        r = {"open_time": ot, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0,
             "close_time": ot + M - 1, "quote_volume": 1000.0, "count": 5, "taker_buy_volume": 4.0,
             "taker_buy_quote_volume": 400.0}
        if mutate:
            mutate(i, r)
        rows.append(r)
    return pa.Table.from_pylist(rows)


def codes(rep):
    return {i.split(":")[1] for i in rep.issues}


CTX = Context("BTCUSDT", "2024-01-01", "1m")


def test_klines_clean_day_is_ok():
    rep = validate_klines(klines(), CTX)
    assert rep.result()["status"] == "ok", rep.issues
    assert rep.checks["coverage"] == 1.0


def test_klines_gaps_are_reported_not_fatal():
    t = klines(mutate=None).filter(pa.array([i not in (10, 11, 12, 500) for i in range(1440)]))
    res = validate_klines(t, CTX).result()
    assert res["status"] == "warn" and res["checks"]["missing_bars"] == 4
    assert res["checks"]["missing_ranges"] == [f"{D0 + 10 * M}-{D0 + 12 * M}", f"{D0 + 500 * M}-{D0 + 500 * M}"]


def test_klines_semantic_violations_are_suspect():
    def bad(i, r):
        if i == 3:
            r["high"] = 99.5            # below open/close
        if i == 4:
            r["low"] = 100.7            # above min(open, close)
        if i == 5:
            r["volume"] = -1.0
    rep = validate_klines(klines(mutate=bad), CTX)
    assert rep.result()["status"] == "suspect"
    assert {"high_below_max_open_close", "low_above_min_open_close", "negative_volume"} <= codes(rep)


def test_klines_duplicates_and_non_monotonic():
    t = klines(5)
    rows = t.to_pylist()
    t2 = pa.Table.from_pylist(rows + [rows[2]] + [rows[0]])
    rep = validate_klines(t2, CTX)
    assert {"open_time_duplicates", "open_time_non_monotonic"} <= codes(rep)


def metrics_table(ts, symbol="BTCUSDT", oi=1.0):
    return pa.Table.from_pylist([{"create_time": t, "symbol": symbol, "sum_open_interest": oi,
                                  "sum_open_interest_value": 2.0, "count_toptrader_long_short_ratio": 1.0,
                                  "sum_toptrader_long_short_ratio": 1.0, "count_long_short_ratio": 1.0,
                                  "sum_taker_long_short_vol_ratio": 1.0} for t in ts])


def test_metrics_cadence_shift_and_duplicates():
    full = [D0 + i * 300_000 for i in range(288)]
    assert validate_metrics(metrics_table(full), CTX).result()["status"] == "ok"
    shifted = [t + 5_000 for t in full]            # every label 5 s after the grid
    rep = validate_metrics(metrics_table(shifted), CTX)
    assert "create_time_semantic_shift" in codes(rep) and rep.checks["create_time_offset_ms"] == 5000
    sparse = full[::2] + [full[0]]                 # 50 % coverage + an identical duplicate
    rep = validate_metrics(metrics_table(sorted(sparse)), CTX)
    assert {"cadence_coverage_low", "create_time_duplicates"} <= codes(rep)
    assert rep.result()["status"] == "warn"
    t = metrics_table(full[:3])
    conflict = pa.concat_tables([t, metrics_table([full[1]], oi=9.0)]).sort_by("create_time")
    assert validate_metrics(conflict, CTX).result()["status"] == "suspect"
    end_labels = [t + 300_000 for t in full]       # end-of-interval labels spill into the next day
    rep = validate_metrics(metrics_table(end_labels), CTX)
    assert "create_time_outside_period" in codes(rep)


def depth_table(n_ts=100, frozen=False, constant=False):
    rows = []
    for i in range(n_ts):
        for p in (-2.0, -1.0, 1.0, 2.0):
            base = 100.0 * abs(p)
            d = base if constant else base + (0 if frozen and p == 1.0 else (i % 7))
            rows.append({"timestamp": D0 + i * 30_000, "percentage": p, "depth": d, "notional": d * 50})
    return pa.Table.from_pylist(rows)


def test_book_depth_detectors():
    rep = validate_book_depth(depth_table(), CTX)
    assert rep.result()["status"] == "ok", rep.issues
    assert rep.checks["cadence_median_ms"] == 30_000
    rep = validate_book_depth(depth_table(frozen=True), CTX)
    assert "constant_bands" in codes(rep) or "frozen_bands" in codes(rep)
    assert validate_book_depth(depth_table(constant=True), CTX).result()["status"] == "suspect"
    t = depth_table(10)
    rows = t.to_pylist()
    rows[0]["depth"] = -5.0
    assert validate_book_depth(pa.Table.from_pylist(rows), CTX).result()["status"] == "suspect"


def test_funding_duplicates_and_post_delisting():
    d0, _ = period_bounds("2024-01")
    ts = [d0 + i * 8 * 3_600_000 for i in range(90)]
    t = pa.Table.from_pylist([{"calc_time": x, "funding_interval_hours": 8, "last_funding_rate": 0.0001} for x in ts])
    assert validate_funding(t, Context("X", "2024-01")).result()["status"] == "ok"
    rep = validate_funding(t, Context("X", "2024-01", last_listed_date="2024-01-15"))
    assert "post_delisting_observations" in codes(rep) and rep.result()["status"] == "warn"
    dup = pa.concat_tables([t, t.slice(0, 1)]).sort_by("calc_time")
    assert "calc_time_duplicates" in codes(validate_funding(dup, Context("X", "2024-01")))


async def test_suspect_file_quarantined_and_excluded_from_catalog(tmp_path):
    hdr = ("open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,"
           "taker_buy_quote_volume,ignore\n")
    good_rows = "".join(f"{D0 + i * M},1,2,0.5,1.5,1,{D0 + i * M + M - 1},1,1,0.5,0.5,0\n" for i in range(1440))
    bad_rows = good_rows.replace(f"{D0},1,2,0.5,1.5,1", f"{D0},1,0.8,0.5,1.5,1", 1)   # high < close
    files = {}
    for sym, body in (("GOODUSDT", good_rows), ("BADUSDT", bad_rows)):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("x.csv", hdr + body)
        files[sym] = buf.getvalue()

    def handler(req):
        sym = req.url.path.split("/")[-3] if "/klines/" in req.url.path else ""
        z = files.get(sym)
        if z is None:
            return httpx.Response(404)
        if req.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=hashlib.sha256(z).hexdigest())
        return httpx.Response(200, content=z)

    bv = BinanceVision(HistConfig(base_url="https://vision.test"), tmp_path, transport=httpx.MockTransport(handler))
    st = await bv.download("klines", ["GOODUSDT", "BADUSDT"], date(2024, 1, 1), date(2024, 1, 1), interval="1m")
    await bv.aclose()
    assert st.ok == 2 and st.suspect == 1
    root = tmp_path / "klines" / "1m"
    man = read_manifest(root)
    q = {e["symbol"]: e["quality"] for e in man.values()}
    assert q == {"GOODUSDT": "ok", "BADUSDT": "suspect"}
    bad_entry = next(e for e in man.values() if e["symbol"] == "BADUSDT")
    assert bad_entry["file"].startswith("_suspect/") and (root / bad_entry["file"]).exists()
    assert any("high_below_max_open_close" in i for i in bad_entry["quality_issues"])
    default = list_files(root)
    assert [p.name for p in default] == ["GOODUSDT-klines-1m-2024-01-01.parquet"]
    assert len(list_files(root, include_suspect=True)) == 2
    assert pq.read_metadata(default[0]).num_rows == 1440
