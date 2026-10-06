import hashlib
import hmac
import json
from urllib.parse import parse_qsl

import httpx
import pytest

from jevbot import macro_live as ml

PX = {"SPYUSDT": 800.0, "XAUUSDT": 4000.0, "BTCUSDT": 80000.0, "QQQUSDT": 700.0}
FLT = {s: {"step": 0.001, "min_notional": 5.0} for s in PX}
FLT["BTCUSDT"]["min_notional"] = 50.0


class FakeFut(ml.Futures):
    def __init__(self, equity=1000.0, pos=None, keys=True, fail=()):
        self.key, self.secret = ("k", "s") if keys else (None, None)
        self.equity, self.pos, self.fail = equity, dict(pos or {}), set(fail)
        self.orders, self.levs = [], []

    def prices(self):
        return dict(PX)

    def filters(self):
        return {k: dict(v) for k, v in FLT.items()}

    def account(self):
        return self.equity, dict(self.pos)

    def one_way(self):
        return True

    def set_leverage(self, symbol, lev):
        self.levs.append((symbol, lev))

    def order(self, symbol, qty, reduce_only):
        if symbol in self.fail:
            raise ml.LiveError("market closed")
        self.orders.append((symbol, qty, reduce_only))
        self.pos[symbol] = self.pos.get(symbol, 0.0) + qty
        if abs(self.pos[symbol]) < 1e-12:
            del self.pos[symbol]
        return {"orderId": len(self.orders), "status": "FILLED"}


def _paper(d, week="2026-W41", weights=None):
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({"last_rebalance_week": week,
                                              "weights": weights or {"SPY": 0.3, "GLD": 0.2, "BTC-USD": 0.02}}))


def test_plan_targets_min_notional_and_closes():
    rows, tgt = ml.plan({"SPY": 0.3, "GLD": 0.2, "BTC-USD": 0.02}, 1.0, 1000.0, {"QQQUSDT": 0.5, "SPYUSDT": 0.1}, PX, FLT)
    o = {r["symbol"]: r for r in rows}
    assert set(tgt) == {"SPYUSDT", "XAUUSDT"}
    assert abs(o["SPYUSDT"]["target"] - 0.375) < 1e-9 and abs(o["SPYUSDT"]["delta"] - 0.275) < 1e-9
    assert not o["SPYUSDT"]["reduce_only"]
    assert o["QQQUSDT"]["delta"] == -0.5 and o["QQQUSDT"]["reduce_only"]           # not in target -> close fully
    assert "BTCUSDT" not in o                                                          # 20$ < 50$ minimum: no position
    capped = ml.plan({"SPY": 0.9}, 1.0, 1000.0, {}, PX, FLT)[0][0]
    assert abs(capped["target"] * 800 - 400) < 1                                       # one asset <= 40 %


def test_effective_leverage_rules():
    assert ml.effective_leverage(2.0, 1000, 1000) == (2.0, "")
    lev, note = ml.effective_leverage(2.0, 790, 1000)
    assert lev == 1.0 and "1x" in note
    assert ml.effective_leverage(9.0, 1000, 1000)[0] == ml.MAX_LEV
    assert ml.effective_leverage(1.0, 690, 1000)[0] == 0.0                             # halt


def test_live_requires_env_and_keys(tmp_path, monkeypatch):
    _paper(tmp_path / "p")
    monkeypatch.delenv("JEVBOT_LIVE", raising=False)
    with pytest.raises(ml.LiveError):
        ml.run(tmp_path / "l", tmp_path / "p", live=True, fut=FakeFut())
    monkeypatch.setenv("JEVBOT_LIVE", "YES")
    with pytest.raises(ml.LiveError):
        ml.run(tmp_path / "l", tmp_path / "p", live=True, fut=FakeFut(keys=False))


def test_dry_run_sends_nothing(tmp_path):
    _paper(tmp_path / "p")
    f = FakeFut()
    res = ml.run(tmp_path / "l", tmp_path / "p", live=False, fut=f)
    assert res["rebalance"] and res["orders"] and not f.orders and all(e["dry_run"] for e in res["executed"])
    assert "KURU" in ml.to_text(res)


def test_live_week_cycle_retry_and_reconcile(tmp_path, monkeypatch):
    monkeypatch.setenv("JEVBOT_LIVE", "YES")
    _paper(tmp_path / "p")
    f = FakeFut(fail={"XAUUSDT"})
    r1 = ml.run(tmp_path / "l", tmp_path / "p", live=True, fut=f)
    assert ("SPYUSDT", pytest.approx(0.375), False) in f.orders and any("XAUUSDT" in a for a in r1["alerts"])
    f.fail.clear()
    r2 = ml.run(tmp_path / "l", tmp_path / "p", live=True, fut=f)                       # retried: week not done yet
    assert r2["rebalance"] and [o[0] for o in f.orders].count("XAUUSDT") == 1
    r3 = ml.run(tmp_path / "l", tmp_path / "p", live=True, fut=f)                       # done: reconcile only
    assert not r3["rebalance"] and not r3["alerts"]
    f.pos["ETHUSDT"] = 1.0
    r4 = ml.run(tmp_path / "l", tmp_path / "p", live=True, fut=f)
    assert any("açmadığı" in a for a in r4["alerts"])
    del f.pos["ETHUSDT"]
    _paper(tmp_path / "p", week="2026-W42", weights={"SPY": 0.3})                       # next week: gold dropped
    ml.run(tmp_path / "l", tmp_path / "p", live=True, fut=f)
    assert "XAUUSDT" not in f.pos and abs(f.pos["SPYUSDT"] - 0.375) < 1e-9


def test_halt_closes_everything_until_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("JEVBOT_LIVE", "YES")
    _paper(tmp_path / "p")
    f = FakeFut()
    ml.run(tmp_path / "l", tmp_path / "p", live=True, fut=f)
    f.equity = 650.0                                                                    # -35 % from the 1000 peak
    r = ml.run(tmp_path / "l", tmp_path / "p", live=True, fut=f)
    assert r["halted"] and not f.pos and "DURDU" in ml.to_text(r)
    f.equity = 700.0
    r2 = ml.run(tmp_path / "l", tmp_path / "p", live=True, fut=f)
    assert r2["halted"] and not f.pos                                                   # stays out
    r3 = ml.run(tmp_path / "l", tmp_path / "p", live=True, fut=f, reset_halt=True)
    assert not r3["halted"] and f.pos                                                   # back in after reset


def test_signing_matches_binance_scheme():
    seen = {}

    def handler(req):
        seen["url"], seen["key"] = str(req.url), req.headers.get("X-MBX-APIKEY")
        return httpx.Response(200, json={"dualSidePosition": False})
    f = ml.Futures("KEY", "SECRET", transport=httpx.MockTransport(handler), clock=lambda: 1700000000000)
    assert f.one_way()
    qs, sig = seen["url"].split("?", 1)[1].rsplit("&signature=", 1)
    assert sig == hmac.new(b"SECRET", qs.encode(), hashlib.sha256).hexdigest() and seen["key"] == "KEY"
    assert dict(parse_qsl(qs))["timestamp"] == "1700000000000"
