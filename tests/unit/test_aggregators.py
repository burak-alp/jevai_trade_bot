from jevbot.core.events import BookTicker, DepthSnapshot, MarkPrice
from jevbot.recorder.aggregators import BookTicker1s, DepthSampler, LatencyAggregator, MarkDedup


def bt(u, t, bid, ask, sym="BTCUSDT"):
    return BookTicker(sym, u, t, t, bid, 1.0, ask, 2.0, t + 5)


def test_book_1s_bars_and_dedup():
    agg = BookTicker1s()
    assert agg.update(bt(1, 1000, 99, 101)) is None
    assert agg.update(bt(2, 1500, 100, 102)) is None
    assert agg.update(bt(2, 1600, 1, 2)) is None                # duplicate update id
    assert agg.duplicates == 1
    row = agg.update(bt(3, 2100, 100, 101))                     # next second -> previous bar emitted
    sym, sec, o, h, l, c, sp_mean, sp_min, sp_max, bid, ask, bq, aq, n, last_u, te, tr = row
    assert (sym, sec, o, h, l, c, n, last_u) == ("BTCUSDT", 1000, 100.0, 101.0, 100.0, 101.0, 2, 2)
    assert abs(sp_mean - (200 + 2 / 101 * 1e4) / 2) < 1e-9
    assert agg.update(bt(4, 2200, 101, 100)) is None and agg.invalid == 1   # crossed book dropped
    rows = agg.flush_older_than(3000)
    assert len(rows) == 1 and rows[0][1] == 2000 and rows[0][13] == 1


def test_depth_sampler():
    ds = DepthSampler(5000)
    mk = lambda u, t: DepthSnapshot("ETHUSDT", t, t, u, (1.0,), (2.0,), (1.1,), (3.0,), t)
    assert ds.update(mk(1, 10_000)) is not None
    assert ds.update(mk(2, 10_500)) is None                     # same 5 s window
    assert ds.update(mk(2, 15_100)) is None                     # duplicate u
    assert ds.update(mk(3, 15_100))[1] == 15_100


def test_mark_dedup():
    md = MarkDedup()
    m = lambda t: MarkPrice("X", t, 1, 1, 1, 0, 0, t)
    assert md.accept(m(1000)) and not md.accept(m(1000)) and md.accept(m(2000))


def test_latency_aggregator_rolls_per_minute():
    la = LatencyAggregator(max_samples=100)
    for i in range(1000):
        assert la.add("kline", "market", 60_000 + i, 60_000 + i + (i % 100)) == []
    rows = la.add("kline", "market", 120_000, 120_010)
    assert len(rows) == 1
    t_min, fam, route, n, n_s, mn, p50, p90, p95, p99, mx = rows[0]
    assert (t_min, fam, route, n, n_s) == (60_000, "kline", "market", 1000, 100)
    assert 0 <= mn <= p50 <= p90 <= p95 <= p99 <= mx <= 99


def test_interval_quantiles_reset():
    from jevbot.recorder.aggregators import IntervalQuantiles
    q = IntervalQuantiles(max_samples=50)
    for i in range(1000):
        q.add(float(i % 100))
    r = q.take()
    assert r["n"] == 1000 and r["max"] == 99 and 0 <= r["p50"] <= r["p95"] <= r["p99"] <= 99
    assert q.take()["n"] == 0
