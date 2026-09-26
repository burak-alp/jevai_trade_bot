import orjson
import pytest

from jevbot.core.events import SchemaError
from jevbot.marketdata import parsers as P
from jevbot.marketdata.endpoints import family_of, symbol_of

KLINE = {"e": "kline", "E": 1700000060005, "s": "BTCUSDT", "k": {
    "t": 1700000000000, "T": 1700000059999, "s": "BTCUSDT", "i": "1m", "f": 1, "L": 2,
    "o": "37000.1", "c": "37010.0", "h": "37020.5", "l": "36990.0", "v": "120.5", "n": 1500,
    "x": True, "q": "4459000.2", "V": "70.1", "Q": "2594000.0", "B": "0"}}
BOOK = {"e": "bookTicker", "u": 400900217, "E": 1700000000123, "T": 1700000000120, "s": "BNBUSDT",
        "b": "25.35190000", "B": "31.21000000", "a": "25.36520000", "A": "40.66000000"}
MARK = [{"e": "markPriceUpdate", "E": 1700000000000, "s": "BTCUSDT", "p": "37000.5", "i": "36999.8",
         "P": "37001.0", "r": "0.00010000", "T": 1700006400000}]
DEPTH = {"e": "depthUpdate", "E": 1700000000500, "T": 1700000000499, "s": "BTCUSDT", "U": 1, "u": 5, "pu": 0,
         "b": [["37000.0", "1.5"], ["36999.9", "2"]], "a": [["37000.1", "0.7"]]}
FORCE = {"e": "forceOrder", "E": 1700000000999, "o": {"s": "ETHUSDT", "S": "SELL", "o": "LIMIT", "f": "IOC",
         "q": "0.5", "p": "2000", "ap": "2001", "X": "FILLED", "l": "0.5", "z": "0.5", "T": 1700000000990}}


def frame(stream, data):
    return orjson.dumps({"stream": stream, "data": data})


def test_family_and_symbol():
    assert family_of("btcusdt@kline_1m") == "kline"
    assert family_of("btcusdt@depth20@500ms") == "depth"
    assert family_of("!markPrice@arr@1s") == "markPrice"
    assert family_of("!forceOrder@arr") == "forceOrder"
    assert family_of("ethusdt@bookTicker") == "bookTicker"
    assert symbol_of("ethusdt@bookTicker") == "ETHUSDT"
    assert symbol_of("!forceOrder@arr") is None
    with pytest.raises(ValueError):
        family_of("btcusdt@weird")


def test_parse_all_families():
    k = P.parse_payload(P.decode_frame(frame("btcusdt@kline_1m", KLINE)), 1)
    assert k.closed and k.close == 37010.0 and k.taker_buy_quote == 2594000.0 and k.n_trades == 1500
    b = P.parse_payload(P.decode_frame(frame("bnbusdt@bookTicker", BOOK)), 2)
    assert b.bid == 25.3519 and b.ask_qty == 40.66 and b.update_id == 400900217
    m = P.parse_payload(P.decode_frame(frame("!markPrice@arr@1s", MARK)), 3)
    assert m[0].funding_rate == 0.0001 and m[0].next_funding_time == 1700006400000
    d = P.parse_payload(P.decode_frame(frame("btcusdt@depth20@500ms", DEPTH)), 4)
    assert d.bid_px == (37000.0, 36999.9) and d.ask_qty == (0.7,) and d.last_update_id == 5
    f = P.parse_payload(P.decode_frame(frame("!forceOrder@arr", FORCE)), 5)
    assert f.symbol == "ETHUSDT" and f.side == "SELL" and f.avg_price == 2001.0


def test_control_reply():
    env = P.decode_frame(b'{"result":null,"id":7}')
    assert env.kind == "control" and env.control_id == 7 and env.error is None
    env = P.decode_frame(b'{"error":{"code":2,"msg":"Invalid request"},"id":8}')
    assert env.error["code"] == 2


@pytest.mark.parametrize("mutate", [
    lambda d: d["k"].pop("x"),
    lambda d: d["k"].__setitem__("o", "abc"),
    lambda d: d.__setitem__("E", "123"),
    lambda d: d.__setitem__("e", "trade"),
])
def test_kline_schema_errors(mutate):
    bad = orjson.loads(orjson.dumps(KLINE))
    mutate(bad)
    with pytest.raises(SchemaError):
        P.parse_payload(P.decode_frame(frame("btcusdt@kline_1m", bad)), 1)


def test_bad_frames():
    with pytest.raises(SchemaError):
        P.decode_frame(b"not json")
    with pytest.raises(SchemaError):
        P.decode_frame(b"[1,2]")
    with pytest.raises(SchemaError):
        P.decode_frame(b'{"foo":1}')


def test_rest_kline_row():
    row = [1700000000000, "1", "2", "0.5", "1.5", "10", 1700000059999, "15", 7, "6", "9", "0"]
    k = P.parse_rest_kline("XUSDT", row, 5)
    assert k.source == "rest" and k.quote_volume == 15.0 and k.taker_buy_quote == 9.0
