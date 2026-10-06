import json

import httpx

from jevbot import macro_orders as mo


def _paper(d, eq=1000.0, weights=None, week="2026-W41"):
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({"history": [{"at": 0, "1x": eq}], "last_rebalance_week": week,
                                              "weights": weights or {"SPY": 0.24, "GLD": 0.11, "BTC-USD": 0.05, "TLT": 0.0}}))


PX = {"SPYUSDT": 778.0, "XAUUSDT": 4180.0, "BTCUSDT": 86000.0, "TMFUSDT": 25.0}
FLT = {"SPYUSDT": {"step": 0.01, "min_notional": 5}, "XAUUSDT": {"step": 0.001, "min_notional": 5},
       "BTCUSDT": {"step": 0.001, "min_notional": 50}, "TMFUSDT": {"step": 0.01, "min_notional": 5}}


def test_targets_rounding_and_min_notional():
    rows = {r["symbol"]: r for r in mo.targets({"SPY": 0.24, "GLD": 0.11, "BTC-USD": 0.05}, 100.0, 1.0, PX, FLT)}
    assert abs(rows["SPYUSDT"]["qty"] - 0.03) < 1e-12                 # 24$ / 778 = 0.0308 -> 0.03
    assert abs(rows["XAUUSDT"]["qty"] - 0.003) < 1e-12                # 11$ / 4180 = 0.0026 -> nearest 0.003
    assert rows["BTCUSDT"]["qty"] == 0 and "çok küçük" in rows["BTCUSDT"]["skip"]   # 5$ < 50$ minimum
    assert all(abs(r["notional"] - r["want"]) <= FLT[s]["step"] * PX[s] / 2 + 1e-9 for s, r in rows.items() if r["qty"])


def test_build_needs_equity_then_tracks_paper_and_diffs(tmp_path):
    p, s = tmp_path / "paper", tmp_path / "orders"
    _paper(p)
    assert "error" in mo.build(s, p, px=PX, flt=FLT)
    r1 = mo.build(s, p, px=PX, flt=FLT, set_equity=1000)
    assert abs(r1["equity"] - 1000) < 1e-9 and "LONG" in mo.to_text(r1)
    _paper(p, eq=1100.0, weights={"SPY": 0.24, "GLD": 0.0, "BTC-USD": 0.05})          # paper +10 %, gold dropped
    r2 = mo.build(s, p, px=PX, flt=FLT)
    assert abs(r2["equity"] - 1100) < 1e-9
    assert "XAUUSDT" in r2["close"]                                                       # tell the user to close gold
    t = mo.to_text(r2)
    assert "KAPAT" in t and "geçen haftaya göre +" in t


def test_leverage_capped_after_drawdown(tmp_path):
    p, s = tmp_path / "paper", tmp_path / "orders"
    _paper(p)
    mo.build(s, p, px=PX, flt=FLT, set_equity=1000, set_leverage=2)
    _paper(p, eq=750.0)
    r = mo.build(s, p, px=PX, flt=FLT)
    assert r["leverage"] == 1.0 and "kaldıraç 1x" in r["note"]
    assert mo.build(s, p, px=PX, flt=FLT, set_leverage=9)["leverage"] == 1.0             # still capped (dd), max 3 stored
    assert json.loads((s / "config.json").read_text())["leverage"] == 3.0


def test_market_rules_parses():
    def handler(req):
        if req.url.path.endswith("ticker/price"):
            return httpx.Response(200, json=[{"symbol": "SPYUSDT", "price": "778"}])
        return httpx.Response(200, json={"symbols": [{"symbol": "SPYUSDT", "filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.01"}, {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.01"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]})
    px, flt = mo.market_rules(transport=httpx.MockTransport(handler))
    assert px == {"SPYUSDT": 778.0} and flt["SPYUSDT"] == {"step": 0.01, "min_notional": 5.0}
