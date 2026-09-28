import asyncio
import json

import httpx
import numpy as np

from jevbot.judgment.client import JevClient, parse_answers, pick_model
from jevbot.judgment.questions import direction_band_atr, direction_h, trade_success
from jevbot.judgment.shadow import JevShadow, auc, jev_report
from jevbot.judgment.state import canonical_state, raw_state
from jevbot.paper.engine import PaperConfig, PaperEngine, read_jsonl
from jevbot.paper.source import HistSource
from jevbot.research.data import DAY, MIN

from .test_slow import DAYS, LOOSE, START, _world

HOUR = 60 * MIN
SYMS = ["BTCUSDT"] + [f"S{i}USDT" for i in range(7)]
LISTING = {s: START - 100 * DAY for s in SYMS}
PROP = {"family": "TSM", "side": 1, "atr_pct": 0.006, "entry_ref": 100.0, "stop_dist": 1.8, "stop_dist_bps": 180.0,
        "r_tp": 3.0, "horizon_min": 2880, "cost_r": 0.04, "ret_24h": 0.03, "ret_7d": 0.08, "rel_ret_7d": 0.05,
        "run_4h_atr": 1.2, "ema_trend": 1.0, "funding_bps_8h": 1.5, "vol_rank": 12, "btc_trend": -1.0,
        "oi_chg_24h": float("nan"), "symbol": "S0USDT", "t_decision": START}


def _fake_jev(calls):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"models": [{"name": "jev-latest", "description": "", "release_date": "2026-09-15"},
                                                        {"name": "jev-1.13.0", "description": "", "release_date": "2026-09-15"}]})
        assert req.headers["authorization"] == "Bearer test-key"
        body = json.loads(req.content)
        calls.append(body)
        ans = {}
        for name, q in body["questions"].items():
            if q["type"] == "noul":
                ans[name] = {"type": "noul", "noul": 0.7 if body["state"]["proposal"]["family"] == "trend_breakout" else 0.3}
            else:
                up = 0.6 if body["state"]["asset"].get("ret_24h_atr_1h", 0) > 0 else 0.2
                ans[name] = {"type": "choice", "choice": "up", "confidence": 0.5,
                             "probabilities": {"up": up, "flat": 0.2, "down": 0.8 - up}}
        return httpx.Response(200, json={"model": "jev-1.13.0", "answers": ans,
                                         "usage": {"input_tokens": 300, "output_tokens": 5}})
    return httpx.MockTransport(handler)


def test_canonical_state_is_side_aligned_and_anonymous():
    long = canonical_state(PROP)
    short = canonical_state({**PROP, "side": -1, "ret_24h": -0.03, "ret_7d": -0.08, "rel_ret_7d": -0.05,
                             "run_4h_atr": -1.2, "ema_trend": -1.0, "funding_bps_8h": -1.5, "btc_trend": 1.0})
    assert long == short                                     # the model never sees the side
    text = json.dumps(long)
    assert "S0USDT" not in text and "symbol" not in text and "100.0" not in text
    assert long["proposal"]["stop_dist_atr_1h"] == 3.0 and long["proposal"]["tp_dist_atr_1h"] == 9.0
    assert long["asset"]["funding_paid_by_position_bps_8h"] == 1.5 and "open_interest_chg_24h_pct" not in long["asset"]
    assert long["market"]["btc_ema50_vs_ema200_sign"] == -1.0
    raw = raw_state({"ret_24h": -0.03, "atr_pct": 0.006, "ema_trend": -1.0}, None)
    assert raw["asset"]["ret_24h_atr_1h"] == -5.0 and "proposal" not in raw


def test_questions_and_answer_validation():
    st = canonical_state(PROP)
    q = {"trade_success": trade_success(st), "direction_h": direction_h(1440)}
    assert "+3.0R" in q["trade_success"]["instructions"] and direction_band_atr(1440) == 2.45
    ok = {"answers": {"trade_success": {"type": "noul", "noul": 0.4},
                      "direction_h": {"type": "choice", "probabilities": {"up": 0.5, "flat": 0.3, "down": 0.21}}}}
    probs, err = parse_answers(q, ok)
    assert err is None and np.isclose(sum(v for k, v in probs.items() if k.startswith("direction_h.")), 1.0)
    assert parse_answers(q, {"answers": {"trade_success": {"noul": 1.2}}})[1]
    bad_sum = {"answers": {**ok["answers"], "direction_h": {"probabilities": {"up": 0.9, "flat": 0.3, "down": 0.2}}}}
    assert parse_answers(q, bad_sum)[1].startswith("choice sum")
    live = [{"name": n} for n in ("laya-english", "laya-multilingual", "jev-latest", "jev-preview", "jev-1.13.0",
                                  "jev-1.9.2")]                 # the account's real listing + an older pin
    assert pick_model(live) == ("jev-1.13.0", "versioned")
    assert pick_model([{"name": "laya-english"}, {"name": "jev-preview"}, {"name": "jev-latest"}]) == \
        ("jev-latest", "alias_only")
    assert pick_model([{"name": "laya-english"}]) == (None, "none")


def test_client_errors_breaker_and_budget():
    n = {"calls": 0}

    def boom(req):
        n["calls"] += 1
        return httpx.Response(500, text="down")
    now = {"t": START}
    c = JevClient(api_key="test-key", transport=httpx.MockTransport(boom), clock=lambda: now["t"], max_calls_per_day=6)
    st = canonical_state(PROP)
    q = {"trade_success": trade_success(st)}

    async def go():
        out = [await c.ask("m", st, q) for _ in range(6)]   # 5 failures open the breaker
        now["t"] += 61_000
        out.append(await c.ask("m", st, q))                 # half-open probe fails -> open again
        out.append(await c.ask("m", st, q))
        now["t"] += 61_000
        out.append(await c.ask("m", st, q))                 # 6 calls used today
        await c.aclose()
        return out
    out = asyncio.run(go())
    assert [r["status"] for r in out] == ["error"] * 5 + ["breaker", "error", "breaker", "budget"]
    assert n["calls"] == 6 and "test-key" not in json.dumps(out)


def test_auc():
    assert auc(np.array([0.9, 0.8, 0.2, 0.1]), np.array([1, 1, 0, 0])) == 1.0
    assert auc(np.array([0.5, 0.5]), np.array([1, 0])) == 0.5
    assert np.isnan(auc(np.array([0.5]), np.array([1])))


def test_shadow_on_paper_engine_end_to_end(tmp_path):
    hist = tmp_path / "hist"
    _world(hist)
    calls = []
    cfg = PaperConfig(state_dir=tmp_path / "paper", slow=LOOSE, tradable_top=6, pool=len(SYMS), window_days=DAYS + 2)
    now = {"t": 0}
    client = JevClient(api_key="test-key", transport=_fake_jev(calls), clock=lambda: now["t"])
    shadow = JevShadow(client, cfg.state_dir, panel_top=4)
    src = HistSource(hist, SYMS, LISTING)
    eng = PaperEngine(src, cfg, clock=lambda: now["t"], shadow=shadow)
    ticks = [START + d * DAY for d in range(13, DAYS - 2)] + [START + 12 * DAY + h * HOUR for h in range(1, 12)]

    async def go():
        assert await shadow.start(START) == "jev-1.13.0"
        got = []
        for t in sorted(ticks):
            now["t"] = t + 30_000
            got += await eng.step(t)
        await eng.settle(START + (DAYS + 5) * DAY)
        await shadow.settle(src, START + (DAYS + 5) * DAY)
        again = await shadow.settle(src, START + (DAYS + 5) * DAY)
        await client.aclose()
        return got, again
    got, again = asyncio.run(go())
    assert got and again == []
    led = read_jsonl(cfg.state_dir / "jev.jsonl")
    kinds = [r["kind"] for r in led]
    assert kinds[0] == "run" and led[0]["pinning"] == "versioned"
    asks = [r for r in led if r["kind"] == "ask"]
    judg = [r for r in led if r["kind"] == "judgment"]
    assert {r["id"] for r in asks if r["question"] == "trade_success"} == {p["id"] for p in got}
    assert len(judg) == len(asks) == len(calls) and all(r["status"] == "ok" for r in judg)
    for a in asks:                                            # asked (and written) before the answer
        assert kinds.index("ask") < kinds.index("judgment")
        assert "symbol" not in json.dumps(a["state"]) and a["model_requested"] == "jev-1.13.0"
    assert all(r["late"] is False for r in judg if r["question"] == "trade_success")
    opens = [r for r in led if r["kind"] == "dir_open"]
    panel = [r for r in opens if r["source"] in ("panel", "both")]
    assert panel and all(r["t_tick"] % DAY in (0, 4 * HOUR, 8 * HOUR, 12 * HOUR, 16 * HOUR, 20 * HOUR) for r in panel)
    outs = [r for r in led if r["kind"] == "dir_outcome"]
    assert outs and {r["id"] for r in outs} <= {r["id"] for r in opens}
    assert all(r["y"] in ("up", "flat", "down") for r in outs if r["status"] == "ok")

    rep = jev_report(cfg.state_dir)
    assert rep["models_returned"] == ["jev-1.13.0"] and rep["tokens"]["input"] == 300 * len(judg)
    b, c = rep["arm_B_trade_success"], rep["arm_C_direction"]
    assert b["n"] > 0 and 0.0 <= b["mean_p"] <= 1.0
    assert c["n"] > 0 and set(c["base_rates"]) == {"up", "flat", "down"}
