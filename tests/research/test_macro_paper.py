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
        res = mp.run_once(tmp_path, data_fn=lambda: data, bn=FakeBn(px), now=now)
        assert res["rebalanced"] and res["state"]["last_rebalance_week"]
        w = res["state"]["weights"]
        assert any(v > 0 for v in w.values()) and sum(w.values()) <= 1 + 1e-9
        assert abs(res["equity"]["1x"] - (1000 - 0.001 * 1000 * sum(
            v * mp.SYMBOLS[a][1] for a, v in w.items()))) < 1e-6
        res2 = mp.run_once(tmp_path, data_fn=lambda: data, bn=FakeBn(px), now=now + 3_600_000)
        assert not res2["rebalanced"]                                      # same week, no second rebalance
        assert "Makro trend sanal hesap" in mp.to_text(res2)
        assert len((tmp_path / "ledger.jsonl").read_text().splitlines()) == 3
    finally:
        macro.START = old_start


def test_binance_client_parses():
    def handler(req):
        if req.url.path.endswith("ticker/price"):
            return httpx.Response(200, json=[{"symbol": "SPYUSDT", "price": "700.5"}])
        return httpx.Response(200, json=[{"fundingRate": "0.0001"}, {"fundingRate": "-0.00005"}])
    bn = mp.Binance(transport=httpx.MockTransport(handler))
    assert bn.prices() == {"SPYUSDT": 700.5} and abs(bn.funding("SPYUSDT", 0, 1) - 0.00005) < 1e-12
