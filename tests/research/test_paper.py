import asyncio

import numpy as np

from jevbot.paper.engine import PaperConfig, PaperEngine, paper_report, read_jsonl
from jevbot.paper.source import HistSource
from jevbot.research.data import DAY, MIN, load_funding, load_symbol
from jevbot.research.labels import label_one
from jevbot.research.slow import add_btc_relative, compute_slow_features, scan_slow

from .test_slow import DAYS, LOOSE, START, _world

HOUR = 60 * MIN
SYMS = ["BTCUSDT"] + [f"S{i}USDT" for i in range(7)]
LISTING = {s: START - 100 * DAY for s in SYMS}
KEY = ("symbol", "family", "side", "stop_dist", "tp_price", "horizon_min")


def _replay(hist):
    feats = {}
    for s in SYMS:
        b = load_symbol(hist, s, START, START + DAYS * DAY)
        b.listing_time = LISTING[s]
        feats[s] = compute_slow_features(b, load_funding(hist, s))
    add_btc_relative(feats)
    return scan_slow(feats, LOOSE, tradable_top=6)


def test_paper_decisions_equal_replay_and_ledger_is_idempotent(tmp_path):
    hist = tmp_path / "hist"
    _world(hist)
    replay = _replay(hist)
    ticks = sorted({START + 12 * DAY + h * HOUR for h in range(1, 25)} | {START + d * DAY for d in range(13, DAYS)})
    cfg = PaperConfig(state_dir=tmp_path / "paper", slow=LOOSE, tradable_top=6, pool=len(SYMS), window_days=DAYS + 2)
    now = {"t": 0}
    eng = PaperEngine(HistSource(hist, SYMS, LISTING), cfg, clock=lambda: now["t"])

    async def go():
        got = []
        for t in ticks:
            now["t"] = t + 30_000                            # on time: 30 s after the bar close
            got += await eng.step(t)
        again = await eng.step(ticks[-1])                    # restart-safe: a recorded tick is not redone
        settled = await eng.settle(START + (DAYS + 5) * DAY)
        settled_again = await eng.settle(START + (DAYS + 5) * DAY)
        return got, again, settled, settled_again
    got, again, settled, settled_again = asyncio.run(go())

    want = [p for p in replay if p["t_decision"] in set(ticks)]
    assert want, "the synthetic world must produce proposals on these ticks"
    norm = lambda ps: sorted((p["t_decision"], *(p[k] for k in KEY)) for p in ps)   # noqa: E731
    assert norm(got) == norm(want)
    assert {p["family"] for p in got} >= {"TSM", "XSM"}
    assert again == [] and settled_again == []
    assert len(read_jsonl(cfg.state_dir / "decisions.jsonl")) == len(got) + len(ticks)

    by_id = {o["id"]: o for o in settled}
    for p in got:                          # settlement = the replay's labeler, filled at the entry minute
        assert p["t_entry"] == p["t_decision"] + MIN and p["decided_at"] < p["t_entry"]
        o = by_id[p["id"]]
        b = load_symbol(hist, p["symbol"], START, START + DAYS * DAY)
        ref = label_one({**p, "t_decision": p["t_entry"]}, b, load_funding(hist, p["symbol"]), LOOSE.cost)
        if ref["exit_type"] in ("TP", "SL", "TIME"):
            assert o["exit_type"] == ref["exit_type"] and np.isclose(o["net_r"], ref["net_r"])
        else:
            assert o["exit_type"] in ("out_of_range", "data_gap")

    rep = paper_report(cfg.state_dir, bootstrap=50)          # default gate: 6 weeks -> everything blinded
    (h, r), = rep["by_config"].items()
    assert h == eng.hash and r["settled"] > 0
    assert all(g.get("blinded") and set(g) == {"n", "blinded"} for g in r["groups"].values())
    open_rep = paper_report(cfg.state_dir, bootstrap=50, gate_days=5, gate_n=1)
    assert "mean_net_r" in next(iter(open_rep["by_config"].values()))["groups"]["all"]


def test_late_tick_is_skipped_not_decided(tmp_path):
    hist = tmp_path / "hist"
    _world(hist, n=2)
    cfg = PaperConfig(state_dir=tmp_path / "paper", slow=LOOSE, tradable_top=3, pool=3, window_days=DAYS + 2)
    t = START + 13 * DAY
    late = lambda: t + 46 * MIN                              # noqa: E731  (started 46 min after the tick)
    eng = PaperEngine(HistSource(hist, SYMS[:3], LISTING), cfg, clock=late)
    assert asyncio.run(eng.step(t)) == []
    (row,) = read_jsonl(cfg.state_dir / "decisions.jsonl")
    assert row["kind"] == "tick" and row["skipped"] == "late"
    assert paper_report(cfg.state_dir)["ticks_skipped"] == 1


def test_config_hash_locks_parameters(tmp_path):
    a = PaperConfig(state_dir=tmp_path)
    b = PaperConfig(state_dir=tmp_path / "x")
    c = PaperConfig(state_dir=tmp_path, tradable_top=40)
    assert a.config_hash() == b.config_hash() != c.config_hash()


def test_rest_source_parses_binance_payloads():
    import httpx

    from jevbot.core.config import RestConfig
    from jevbot.marketdata.binance_rest import BinanceRest
    from jevbot.paper.source import RestSource

    t0 = 1_704_067_200_000

    def kl(ot, step, c):
        return [ot, str(c), str(c + 1), str(c - 1), str(c + 0.5), "10", ot + step - 1, "1000", 5, "4", "400", "0"]

    def handler(req):
        p, q = req.url.path, dict(req.url.params)
        if p == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json={"rateLimits": [], "symbols": [
                {"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": st, "onboardDate": t0 - DAY}
                for s, st in (("BTCUSDT", "TRADING"), ("AUSDT", "TRADING"), ("USDCUSDT", "TRADING"),
                              ("DEADUSDT", "SETTLING"))]})
        if p == "/fapi/v1/ticker/24hr":
            vols = (("AUSDT", "9e9"), ("BTCUSDT", "5e9"), ("USDCUSDT", "8e9"), ("DEADUSDT", "1e10"))
            return httpx.Response(200, json=[{"symbol": s, "quoteVolume": v} for s, v in vols])
        if p == "/fapi/v1/klines":
            step = HOUR if q["interval"] == "1h" else MIN
            return httpx.Response(200, json=[kl(t0 + i * step, step, 100 + i) for i in (0, 2)])  # gap at 1
        if p == "/fapi/v1/markPriceKlines":
            return httpx.Response(200, json=[[t0 + i * MIN, "100", "101.5", "98.5", "100", "0", t0 + i * MIN + 59_999,
                                              "0", 0, "0", "0", "0"] for i in range(3)])
        if p == "/fapi/v1/fundingRate":
            return httpx.Response(200, json=[{"symbol": "AUSDT", "fundingTime": t0 + 8 * HOUR, "fundingRate": "0.0002"},
                                             {"symbol": "AUSDT", "fundingTime": t0, "fundingRate": "0.0001"}])
        if p == "/fapi/v1/ticker/bookTicker":
            return httpx.Response(200, json={"symbol": "AUSDT", "bidPrice": "99.99", "askPrice": "100.01"})
        return httpx.Response(404, json={})

    async def go():
        rest = BinanceRest("https://fapi.test", RestConfig(max_retries=0), transport=httpx.MockTransport(handler))
        src = RestSource(rest)
        try:
            cands = await src.candidates(t0, 5)
            h1 = await src.hourly("AUSDT", t0, t0 + 3 * HOUR)
            ft, fr = await src.funding("AUSDT", t0, t0 + DAY)
            b = await src.minute_bars("AUSDT", t0, t0 + 3 * MIN)
            return cands, h1, (ft, fr), b, await src.spread_bps("AUSDT"), await src.listing_time("AUSDT")
        finally:
            await rest.aclose()
    cands, h1, (ft, fr), b, spread, lt = asyncio.run(go())
    assert cands == ["AUSDT", "BTCUSDT"]                      # stablecoin and non-trading excluded, volume order
    assert h1["close"][0] == 100.5 and np.isnan(h1["close"][1]) and h1["close"][2] == 102.5 and h1["qv"][0] == 1000
    assert list(ft) == [t0, t0 + 8 * HOUR] and np.allclose(fr, [0.0001, 0.0002])   # sorted
    assert b.n == 3 and b.open[0] == 100 and np.isnan(b.close[1]) and b.mark_high[1] == 101.5
    assert abs(spread - 2.0) < 1e-6 and lt == t0 - DAY
