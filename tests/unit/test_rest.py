import httpx
import orjson
import pytest

from jevbot.core.config import RestConfig
from jevbot.marketdata.binance_rest import BinanceRest, RateLimited


def make(handler, **kw):
    return BinanceRest("https://example.test", RestConfig(max_retries=2, **kw), transport=httpx.MockTransport(handler))


async def test_weight_header_and_exchange_limits():
    def handler(req):
        if req.url.path == "/fapi/v1/exchangeInfo":
            body = {"rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1,
                                    "limit": 1200}], "symbols": []}
            return httpx.Response(200, content=orjson.dumps(body), headers={"X-MBX-USED-WEIGHT-1M": "17"})
        return httpx.Response(404)
    rest = make(handler)
    await rest.exchange_info()
    assert rest.weights.limit_per_min == 1200
    assert rest.weights.current_used() == 17
    await rest.aclose()


async def test_retry_on_5xx_then_success():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, content=b'{"serverTime": 123}')
    rest = make(handler)
    t_send, t_server, t_recv = await rest.server_time()
    assert t_server == 123 and calls["n"] == 2
    await rest.aclose()


async def test_429_raises_and_blocks_budget():
    def handler(req):
        return httpx.Response(429, text="too many", headers={"Retry-After": "7"})
    rest = make(handler)
    with pytest.raises(RateLimited) as ei:
        await rest.get("/fapi/v1/time")
    assert ei.value.retry_after_s == 7.0
    assert not rest.weights.budget_ok(1)
    await rest.aclose()


async def test_open_interest_and_klines_paging():
    base = 1_699_999_980_000          # minute aligned

    def handler(req):
        p = req.url.path
        if p == "/fapi/v1/openInterest":
            return httpx.Response(200, content=b'{"openInterest":"10659.5","symbol":"BTCUSDT","time":1589437530011}')
        if p == "/fapi/v1/klines":
            start = int(req.url.params["startTime"])
            end = int(req.url.params["endTime"])
            limit = int(req.url.params["limit"])
            rows = []
            t = -(-start // 60_000) * 60_000   # first open_time >= start
            while t <= end and len(rows) < limit:
                rows.append([t, "1", "1", "1", "1", "1", t + 59_999, "1", 1, "0.5", "0.5", "0"])
                t += 60_000
            return httpx.Response(200, content=orjson.dumps(rows))
        return httpx.Response(404)
    rest = make(handler)
    oi = await rest.open_interest("BTCUSDT")
    assert oi.open_interest == 10659.5 and oi.t_server == 1589437530011
    ks = await rest.klines("BTCUSDT", base, base + 9 * 60_000, limit=4)
    assert [k.open_time for k in ks] == [base + i * 60_000 for i in range(10)]
    assert all(k.source == "rest" for k in ks)
    await rest.aclose()
