import asyncio
from datetime import datetime, timezone

import httpx
import orjson

from jevbot.judgment.client import JevClient
from jevbot.llmtrader import UNIVERSE, LlmTrader, is_decision_tick, llm_report, yahoo_headlines
from jevbot.paper.engine import read_jsonl
from jevbot.paper.source import HistSource
from jevbot.research.synthetic import make_symbol

DAY, HOUR, MIN = 86_400_000, 3_600_000, 60_000
T0 = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)     # a Monday
TICK = T0 + 44 * DAY + 14 * HOUR                                            # Wed 2026-07-15 14:00 UTC


def _hist(tmp_path, tickers=("NVDA", "TSLA", "SPY")):
    for i, tk in enumerate(tickers):
        make_symbol(tmp_path, f"{tk}USDT", T0, 50 * 1440, seed=i + 1, base=100 + 10 * i)
    return HistSource(tmp_path, [f"{t}USDT" for t in tickers])


def _jev():
    def handler(req):
        if req.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"models": [{"name": "jev-1.13.0"}]})
        body = orjson.loads(req.content)
        assert "headlines" in body["state"] and "ticker" in body["state"]
        return httpx.Response(200, json={"model": "jev-1.13.0", "usage": {}, "answers": {"direction_24h": {
            "type": "choice", "choice": "up", "confidence": 0.6, "probabilities": {"up": 0.6, "flat": 0.3, "down": 0.1}}}})
    return JevClient(api_key="k", transport=httpx.MockTransport(handler))


def test_decision_tick_only_weekdays_14_utc():
    assert is_decision_tick(TICK) and not is_decision_tick(TICK + HOUR)
    sat = int(datetime(2026, 7, 18, 14, tzinfo=timezone.utc).timestamp() * 1000)
    assert not is_decision_tick(sat)


def test_headlines_never_after_the_tick():
    t = int(datetime(2026, 7, 15, 14, tzinfo=timezone.utc).timestamp() * 1000)
    rss = ("<rss><channel><item><title>old news</title><pubDate>Tue, 14 Jul 2026 20:00:00 +0000</pubDate></item>"
           "<item><title>future news</title><pubDate>Wed, 15 Jul 2026 15:00:00 +0000</pubDate></item>"
           "<item><title>stale</title><pubDate>Mon, 06 Jul 2026 10:00:00 +0000</pubDate></item></channel></rss>")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=rss))) as c:
            return await yahoo_headlines(c, "NVDA", t)
    assert asyncio.run(go()) == ["old news"]


def test_llm_trader_end_to_end(tmp_path):
    src = _hist(tmp_path / "hist")
    now = {"t": TICK + 30_000}

    async def news(tk, t):
        return [f"{tk} headline"]
    jev = _jev()
    lt = LlmTrader(tmp_path / "paper", src, jev=jev, jev_model="jev-1.13.0", headlines=news, clock=lambda: now["t"])

    async def go():
        await lt.on_tick(TICK)
        await lt.on_tick(TICK + HOUR)                                       # not a decision tick
        await jev.aclose()
    asyncio.run(go())
    ctx_files = list((tmp_path / "paper" / "llm").glob("context-*.json"))
    assert len(ctx_files) == 1
    ctx = orjson.loads(ctx_files[0].read_bytes())
    assert set(ctx["symbols"]) == {"NVDA", "TSLA", "SPY"} and ctx["symbols"]["NVDA"]["headlines"] == ["NVDA headline"]
    assert ctx["symbols"]["NVDA"]["ret_1d_pct"] is not None
    # Claude arm recorded from a file-like dict; invalid probabilities and unknown tickers ignored
    rows = lt.record("claude", TICK, {"NVDA": {"up": 0.1, "flat": 0.3, "down": 0.6}, "TSLA": {"up": 0.9, "flat": 0.9, "down": 0},
                                      "XXX": {"up": 1, "flat": 0, "down": 0}}, {"NVDA": "weak guidance"}, model="claude")
    assert [r["ticker"] for r in rows] == ["NVDA"] and rows[0]["side"] == -1
    assert lt.record("claude", TICK, {"NVDA": {"up": 0.1, "flat": 0.3, "down": 0.6}}) == []          # idempotent
    led = read_jsonl(lt.ledger)
    arms = {r["arm"] for r in led if r["kind"] == "decision"}
    assert arms == {"always_long", "random", "jev", "claude"}
    assert all(r["t_entry"] >= TICK + 10 * MIN for r in led if r["kind"] == "decision")
    out = asyncio.run(lt.settle(TICK + 2 * DAY))
    assert out and all(o["ret"] == o["ret"] for o in out)                                          # finite
    rep = llm_report(tmp_path / "paper", bootstrap=50)
    assert rep["jev"]["settled"] == 3 and rep["jev"]["trades"] == 3 and rep["claude"]["trades"] == 1
    assert set(UNIVERSE) >= {"NVDA", "TSLA", "SPY"}
