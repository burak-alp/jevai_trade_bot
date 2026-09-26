import pytest

from jevbot.core.config import UniverseConfig, WsConfig, load_config
from jevbot.core.time import DAY_MS
from jevbot.marketdata.endpoints import combined_stream_url, route_of
from jevbot.marketdata.universe import select_universe

WS = load_config(["config/base.yaml"]).binance.ws


def test_routes_from_config():
    assert route_of("btcusdt@bookTicker", WS) == "public"
    assert route_of("btcusdt@depth20@500ms", WS) == "public"
    assert route_of("btcusdt@kline_1m", WS) == "market"
    assert route_of("!markPrice@arr@1s", WS) == "market"
    url = combined_stream_url("market", ["btcusdt@kline_1m", "!markPrice@arr@1s"], WS)
    assert url == "wss://fstream.binance.com/market/stream?streams=btcusdt@kline_1m/!markPrice@arr@1s"


def test_missing_route_mapping_raises():
    cfg = WsConfig(stream_routes={"kline": "market"})
    with pytest.raises(ValueError):
        route_of("btcusdt@bookTicker", cfg)


def _info(now, symbols):
    return {"symbols": [{
        "symbol": s, "status": st, "contractType": "PERPETUAL", "quoteAsset": "USDT",
        "onboardDate": now - age * DAY_MS, "deliveryDate": now + dd * DAY_MS,
        "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"}]}
        for s, st, age, dd in symbols]}


def test_universe_rules_and_hysteresis():
    now = 1_800_000_000_000
    info = _info(now, [("AUSDT", "TRADING", 100, 3000), ("BUSDT", "TRADING", 100, 3000),
                       ("CUSDT", "TRADING", 100, 3000), ("NEWUSDT", "TRADING", 3, 3000),
                       ("DELUSDT", "TRADING", 100, 10), ("HALTUSDT", "SETTLING", 100, 3000),
                       ("USDCUSDT", "TRADING", 100, 3000)])
    tick = [{"symbol": s, "quoteVolume": str(v)} for s, v in
            [("AUSDT", 9e8), ("BUSDT", 5e8), ("CUSDT", 4e8), ("NEWUSDT", 9e9), ("DELUSDT", 9e9),
             ("HALTUSDT", 9e9), ("USDCUSDT", 9e9)]]
    cfg = UniverseConfig(min_quote_vol_24h=1e8, max_symbols=2, exit_rank=3, min_listing_age_days=14,
                         exclude=["USDCUSDT"])
    members, rows = select_universe(info, tick, cfg, now)
    assert members == ["AUSDT", "BUSDT"]
    reasons = {r["symbol"]: r["reason"] for r in rows}
    assert reasons["NEWUSDT"] == "too_new" and reasons["DELUSDT"] == "pending_delivery"
    assert reasons["HALTUSDT"] == "status" and reasons["USDCUSDT"] == "excluded" and reasons["CUSDT"] == "rank"
    # C was a member before and is within exit_rank -> kept over newcomer B
    members2, _ = select_universe(info, tick, cfg, now, previous={"AUSDT", "CUSDT"})
    assert members2 == ["AUSDT", "CUSDT"]


def test_book_ticker_max_symbols_limits_streams(tmp_path):
    from jevbot.recorder.recorder import Recorder
    cfg = load_config(["config/base.yaml", "config/home.yaml"], [f"data_dir={tmp_path}", f"run_dir={tmp_path}"])
    rec = Recorder(cfg)
    rec.members = [f"S{i}USDT" for i in range(150)]
    streams = rec._desired_streams()
    assert 0 < cfg.recorder.book_ticker_max_symbols < len(rec.members)
    assert sum(s.endswith("@bookTicker") for s in streams) == cfg.recorder.book_ticker_max_symbols
    assert sum(s.endswith("@kline_1m") for s in streams) == 150
    assert cfg.binance.ws.compression is True
