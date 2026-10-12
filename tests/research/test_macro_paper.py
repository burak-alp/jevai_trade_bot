import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest

from jevbot import macro_paper as mp
from jevbot.research import macro

PRICES = Path("run/macro_trend/prices.json")


@pytest.mark.skipif(not PRICES.exists(), reason="cached Yahoo prices (scripts/macro_trend.py)")
def test_live_weights_equal_backtest_weights():
    """Weights computed live from data truncated at a Friday == the backtest's weights in force the next day."""
    data = json.loads(PRICES.read_text(encoding="utf-8"))
    days, P, cash = macro.align(data)
    _, W = macro.strategy(P, cash, days, (252,))
    for fri in (date(2020, 3, 13), date(2024, 6, 28), date(2026, 9, 25)):
        latest, w, _ = mp.target_weights(data, fri)
        assert latest == fri
        nxt = days.index(fri) + 1
        assert np.allclose([w[a] for a in macro.ASSETS], W[nxt], atol=1e-12)


def test_rebalance_due():
    fri, thu, sat = date(2026, 10, 2), date(2026, 10, 1), date(2026, 10, 3)
    assert mp.rebalance_due(None, thu, thu)                          # first run starts immediately
    assert not mp.rebalance_due("2026-W39", thu, thu)                 # mid-week: wait
    assert mp.rebalance_due("2026-W39", fri, sat)                    # Friday close
    assert not mp.rebalance_due("2026-W40", fri, sat)                 # done this week
    assert mp.rebalance_due("2026-W39", thu, date(2026, 10, 5))      # Friday holiday: next week's run catches it


class FakeBn(mp.Binance):
    def __init__(self, px, rate=0.0):
        self.px, self.rate = px, rate

    def prices(self):
        return dict(self.px)

    def funding(self, symbol, start, end):
        return self.rate


def test_accounting_rebalance_mark_funding():
    acc = {"balance": 1000.0, "pos": {}, "last_mark": 0}
    px = {"SPYUSDT": 100.0, "TMFUSDT": 50.0}
    cost = mp.rebalance(acc, {"SPY": 0.5, "TLT": 0.3}, 2.0, px)
    # SPY 1000 notional (0.5 x 2), TMF 0.3 x 2 / 3 = 200 notional
    assert abs(acc["pos"]["SPYUSDT"]["qty"] * 100 - 1000) < 1e-9 and abs(acc["pos"]["TMFUSDT"]["qty"] * 50 - 200) < 1e-9
    assert abs(cost - 0.001 * 1200) < 1e-9
    px2 = {"SPYUSDT": 110.0, "TMFUSDT": 50.0}
    m = mp.mark(acc, px2, FakeBn(px2, rate=0.0001), DAY := 86_400_000, 0.0)
    assert abs(m["funding"] - (-(10 * 110 + 4 * 50) * 0.0001)) < 1e-9          # longs pay positive funding
    assert abs(mp._equity(acc, px2) - (1000 - 1.2 + 100 + m["funding"])) < 1e-9


def test_run_once_end_to_end(tmp_path):
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(500)]
    days = [d for d in days if d.weekday() < 5]
    rng = np.random.default_rng(0)
    data = {}
    for k, a in enumerate(macro.ASSETS):
        p = 100 * np.exp(np.cumsum(rng.normal(0.001 if k % 2 == 0 else -0.001, 0.01, len(days))))
        data[a] = {d.isoformat(): float(v) for d, v in zip(days, p)}
    data[macro.CASH] = {d.isoformat(): 4.0 for d in days}
    px = {sym: 100.0 for sym, _ in mp.SYMBOLS.values()}
    old_start = macro.START
    macro.START = date(2024, 1, 1)
    try:
        now = int(np.datetime64(days[-1].isoformat()).astype("datetime64[ms]").astype(int)) + 22 * 3_600_000
        res = mp.run_once(tmp_path, data_fn=lambda: data, bn=FakeBn(px), now=now, headlines=None)
        assert res["rebalanced"] and res["state"]["last_rebalance_week"]
        w = res["state"]["weights"]
        assert any(v > 0 for v in w.values()) and sum(w.values()) <= 1 + 1e-9
        assert abs(res["equity"]["1x"] - (1000 - 0.001 * 1000 * sum(
            v * mp.SYMBOLS[a][1] for a, v in w.items()))) < 1e-6
        res2 = mp.run_once(tmp_path, data_fn=lambda: data, bn=FakeBn(px), now=now + 3_600_000, headlines=None)
        assert not res2["rebalanced"]                                      # same week, no second rebalance
        assert "Makro trend sanal hesap" in mp.to_text(res2)
        assert len((tmp_path / "ledger.jsonl").read_text().splitlines()) == 3
        ctx = json.loads((tmp_path / f"context-{res['state']['last_rebalance_week']}.json").read_text(encoding="utf-8"))
        assert set(ctx["assets"]) == set(macro.ASSETS) and ctx["assets"]["SPY"]["binance"] == "SPYUSDT"
        held = [a for a, v in w.items() if v > 0]
        overlay = {"assets": {held[0]: {"tilt": 1, "reason": "x"}, held[-1]: {"tilt": -1, "reason": "y"}}}
        (tmp_path / f"overlay-{res['state']['last_rebalance_week']}.json").write_text(json.dumps(overlay))
        res3 = mp.run_once(tmp_path, data_fn=lambda: data, bn=FakeBn(px), now=now + 7_200_000, headlines=None)
        wk = res["state"]["last_rebalance_week"]
        lw = res3["state"]["overlay_weights"]["llm"]
        assert res3["state"]["applied"]["llm"] == wk and sum(lw.values()) <= 1 + 1e-9
        assert "Claude yorumu" in mp.to_text(res3) and "Komite" not in mp.to_text(res3)
        committee = {"version": "committee.v1", "week": wk, "analysts": {},
                     "pm": {"assets": {held[0]: {"tilt": -1, "reason": "z"}}, "note": "not"}}
        (tmp_path / f"committee-{wk}.json").write_text(json.dumps(committee))
        res4 = mp.run_once(tmp_path, data_fn=lambda: data, bn=FakeBn(px), now=now + 10_800_000, headlines=None)
        t4 = mp.to_text(res4)
        assert "Claude yorumu" not in t4 and "Komite kararı" in t4                 # each applied once per week
        assert res4["state"]["overlay_weights"]["committee"][held[0]] < w[held[0]]
        assert set(res4["equity"]) == {"1x", "2x", "llm", "committee", "fren"}
        attr = res4["state"]["accounts"]["1x"]["pnl_sym"]                          # P&L attribution = equity change
        assert abs(sum(attr.values()) - (res4["equity"]["1x"] - 1000)) < 1e-6
    finally:
        macro.START = old_start


def test_jev_views_with_fake_client():
    class FakeJev:
        async def models(self):
            return [{"name": "jev-1.13.0"}, {"name": "laya-x"}]

        async def ask(self, model, state, q):
            assert model == "jev-1.13.0" and "direction_5d" in q and state["asset"] in ("SPY", "GLD")
            return {"status": "ok", "probs": {"direction_5d.up": 0.5, "direction_5d.flat": 0.3, "direction_5d.down": 0.2}}

    ctx = {"assets": {"SPY": {"name": "S&P", "vol_60d_pct": 15.0, "headlines": []},
                      "GLD": {"name": "Altın", "vol_60d_pct": None, "headlines": ["x"]}}}
    v = mp.jev_views(ctx, client=FakeJev())
    assert v["model"] == "jev-1.13.0" and v["SPY"]["up"] == 0.5 and v["SPY"]["band_pct"] == 1.1


def test_apply_overlay():
    w = {"SPY": 0.3, "GLD": 0.2, "TLT": 0.0, "BTC-USD": 0.1}
    o = mp.apply_overlay(w, {"SPY": 1, "GLD": -1, "TLT": 1})
    assert o == pytest.approx({"SPY": 0.45, "GLD": 0.1, "TLT": 0.0, "BTC-USD": 0.1})   # not-held stays 0
    big = mp.apply_overlay({"SPY": 0.5, "QQQ": 0.4}, {"SPY": 1, "QQQ": 1})
    assert abs(sum(big.values()) - 1) < 1e-12 and abs(big["SPY"] / big["QQQ"] - 1.25) < 1e-12


def test_binance_client_parses():
    def handler(req):
        if req.url.path.endswith("ticker/price"):
            return httpx.Response(200, json=[{"symbol": "SPYUSDT", "price": "700.5"}])
        return httpx.Response(200, json=[{"fundingRate": "0.0001"}, {"fundingRate": "-0.00005"}])
    bn = mp.Binance(transport=httpx.MockTransport(handler))
    assert bn.prices() == {"SPYUSDT": 700.5} and abs(bn.funding("SPYUSDT", 0, 1) - 0.00005) < 1e-12


def test_brake_account_freezes_crashed_asset(tmp_path):
    days = [d for d in (date(2024, 1, 1) + timedelta(days=i) for i in range(500)) if d.weekday() < 5]
    rng = np.random.default_rng(3)
    data = {a: {d.isoformat(): float(v) for d, v in zip(days, 100 * np.exp(np.cumsum(rng.normal(0.002, 0.01, len(days)))))}
            for a in macro.ASSETS}
    data[macro.CASH] = {d.isoformat(): 4.0 for d in days}
    px = {sym: 100.0 for sym, _ in mp.SYMBOLS.values()}
    old = macro.START
    macro.START = date(2024, 1, 1)
    try:
        fri = [d for d in days if d.weekday() == 4][-2]
        cut = lambda end: {a: {k: v for k, v in s.items() if k <= end.isoformat()} for a, s in data.items()}  # noqa: E731
        t0 = int(np.datetime64(fri.isoformat()).astype("datetime64[ms]").astype(int)) + 26 * 3_600_000
        r1 = mp.run_once(tmp_path, data_fn=lambda: cut(fri), bn=FakeBn(px), now=t0, headlines=None)
        held = [a for a, v in r1["state"]["weights"].items() if v > 0]
        assert r1["rebalanced"] and held and not r1["state"]["brake_note"]
        mon = fri + timedelta(days=3)
        crash = cut(mon)
        crash[held[0]][mon.isoformat()] = crash[held[0]][fri.isoformat()] * 0.85     # -15 % day (>> 3 sigma)
        r2 = mp.run_once(tmp_path, data_fn=lambda: crash, bn=FakeBn(px), now=t0 + 3 * 86_400_000, headlines=None)
        assert not r2["rebalanced"] and r2["state"]["brake"]["frozen"] == [held[0]]
        sym = mp.SYMBOLS[held[0]][0]
        assert sym not in r2["state"]["accounts"]["fren"]["pos"] and sym in r2["state"]["accounts"]["1x"]["pos"]
        assert "Fren" in mp.to_text(r2)
        r3 = mp.run_once(tmp_path, data_fn=lambda: crash, bn=FakeBn(px), now=t0 + 3 * 86_400_000 + 3_600_000,
                         headlines=None)
        assert not r3["state"]["brake_note"]                                         # same day not re-checked
    finally:
        macro.START = old
