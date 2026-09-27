from jevbot.core.config import load_config
from jevbot.marketdata.binance_ws import WsPool

WS = load_config(["config/base.yaml"], ["binance.ws.max_streams_per_conn=3"]).binance.ws


def test_pool_packs_and_rebalances_by_route():
    pool = WsPool(WS, on_frame=lambda *a: None)
    klines = [f"s{i}usdt@kline_1m" for i in range(7)]
    books = [f"s{i}usdt@bookTicker" for i in range(2)]
    pool.set_streams(klines + books + ["!markPrice@arr@1s"])
    market = pool.conns["market"]
    public = pool.conns["public"]
    assert len(public) == 1 and public[0].streams == books
    assert sum(len(c.streams) for c in market) == 8 and all(len(c.streams) <= 3 for c in market)
    # remove 3 streams, add 1: existing connections keep their streams, freed room is reused
    first_conn_before = list(market[0].streams)
    new = klines[3:] + ["new1usdt@kline_1m"] + books + ["!markPrice@arr@1s"]
    pool.set_streams(new)
    all_market = [s for c in pool.conns["market"] for s in c.streams]
    assert sorted(all_market) == sorted(klines[3:] + ["new1usdt@kline_1m", "!markPrice@arr@1s"])
    assert all(len(c.streams) <= 3 for c in pool.conns["market"])
    assert all(s in first_conn_before or s not in klines[:3] for s in pool.conns["market"][0].streams)
    # empty route connections are dropped
    pool.set_streams(klines[3:])
    assert pool.conns["public"] == []


def test_home_profile_dedicated_family_sockets():
    ws = load_config(["config/base.yaml", "config/home.yaml"]).binance.ws
    assert ws.family_conn_max == {"bookTicker": 15, "depth": 50}
    pool = WsPool(ws, on_frame=lambda *a: None)
    books = [f"s{i}usdt@bookTicker" for i in range(30)]
    depth = [f"s{i}usdt@depth20@500ms" for i in range(12)]
    pool.set_streams(books + depth + [f"s{i}usdt@kline_1m" for i in range(5)])
    bt, dp = pool.conns["public-bookTicker"], pool.conns["public-depth"]
    assert [len(c.streams) for c in bt] == [15, 15] and [len(c.streams) for c in dp] == [12]
    assert all(c.route == "public" for c in bt + dp) and "public" not in pool.conns
    assert len(pool.conns["market"]) == 1
    pool.set_streams(books[:10] + depth)                        # shrink: emptied book socket dropped
    assert sum(len(c.streams) for c in pool.conns["public-bookTicker"]) == 10
    assert len(pool.conns["public-bookTicker"]) == 1
