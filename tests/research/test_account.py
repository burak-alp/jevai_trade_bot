import asyncio

import httpx

from jevbot.account import DailyAccountReport, MAX_POS, SLOTS, START_EQUITY, account_report, chunks, mark_prices, send_telegram, simulate, to_text, trades
from jevbot.paper.engine import _append

DAY = 86_400_000
T = 1_790_000_000_000 - 1_790_000_000_000 % DAY


def _ledgers(d):
    rows = []
    for i, (sym, up, down, ret) in enumerate([("AUSDT", 0.7, 0.1, 100.0), ("BUSDT", 0.1, 0.6, 50.0), ("CUSDT", 0.3, 0.3, 80.0)]):
        rid = f"id{i}"
        rows += [{"kind": "dir_open", "id": rid, "symbol": sym, "t_tick": T, "close": 1.0, "atr_pct": 0.01,
                  "horizon_min": 1440, "band_atr": 2.45, "source": "panel"},
                 {"kind": "ask", "question": "direction_h", "id": rid, "t_tick": T, "state": {"schema": "state.slow.v2"}},
                 {"kind": "judgment", "question": "direction_h", "id": rid, "t_tick": T, "status": "ok",
                  "probs": {"direction_h.up": up, "direction_h.flat": 1 - up - down, "direction_h.down": down}},
                 {"kind": "dir_outcome", "id": rid, "status": "ok", "ret_bps": ret, "y": "up"}]
    # a regime row and an old-schema answer must be ignored
    rows += [{"kind": "dir_open", "id": "rg", "symbol": "BTCUSDT", "t_tick": T, "close": 1.0, "atr_pct": 0.01,
              "horizon_min": 10080, "band_pct": 0.03, "question": "btc_regime_7d"}]
    _append(d / "jev.jsonl", rows)
    _append(d / "llm" / "ledger.jsonl", [
        {"kind": "decision", "arm": "jev", "t_decision": T, "t_entry": T + 600_000, "ticker": "NVDA", "side": 1, "score": 0.5},
        {"kind": "outcome", "id": f"jev|{T}|NVDA", "ret": 0.02, "net": 0.018}])


def test_trades_and_account(tmp_path):
    _ledgers(tmp_path)
    tr = trades(tmp_path)
    jev = tr["jev"]
    assert [(t["market"], t["symbol"], t["side"]) for t in jev] == [("crypto", "AUSDT", 1), ("crypto", "BUSDT", -1),
                                                                     ("stocks", "NVDA", 1)]
    assert abs(jev[1]["net"] - (-0.005 - 0.0012)) < 1e-12 and len(tr["always_long"]) == 3
    rep = account_report(tmp_path, now=T + 3 * DAY)
    j = rep["arms"]["jev"]
    size = START_EQUITY / MAX_POS
    assert j["trades"] == 3 and not j["open"]
    assert abs(j["equity"] - START_EQUITY - size * (0.01 - 0.0012 - 0.005 - 0.0012) - 0) < 1.0      # compounding ~
    assert "Jev sanal hesap" in to_text(rep)


def test_open_until_settled_and_slot_cap():
    ts = [{"market": "crypto", "symbol": f"S{i}", "side": 1, "t_entry": T, "t_exit": T + DAY, "net": 0.01} for i in range(25)]
    ts += [{"market": "stocks", "symbol": "NVDA", "side": 1, "t_entry": T + 1, "t_exit": T + DAY, "net": 0.02}]
    ts[0]["net"] = None
    s = simulate(ts, now=T + 2 * DAY)
    assert s["trades"] == SLOTS["crypto"] - 1 + 1 and len(s["open"]) == 1 and s["stocks"] == 1   # stock not crowded out


def test_hourly_and_daily_hook(tmp_path, monkeypatch):
    _ledgers(tmp_path)
    sent = []

    async def fake_send(text):
        sent.append(text)
        return "sent"
    monkeypatch.setattr("jevbot.account.send_telegram", fake_send)

    async def fake_prices(opens):
        return {"AUSDT": 1.1, "BUSDT": 1.1, "NVDAUSDT": 200.0}
    monkeypatch.setattr("jevbot.account.mark_prices", fake_prices)
    hook = DailyAccountReport(tmp_path)
    asyncio.run(hook.on_tick(T + 3_600_000))                        # 01:00 UTC, positions open
    asyncio.run(hook.on_tick(T + DAY + 18 * 3_600_000))             # 18:00 UTC: full daily report
    assert sent[0].startswith("🕐") and "Jev sanal hesap" in sent[1]
    assert "LONG  A: 1.0000 → 1.1000 +9.88%" in sent[0] and "SHORT B" in sent[0]
    assert "↳ Jev ↑%70 ↓%10" in sent[0] and "📈 Hisse" in sent[0] and "↳ ↑%" in sent[0]
    assert len(list((tmp_path / "account").glob("hourly-*.txt"))) == 1
    assert len(list((tmp_path / "account").glob("report-*.txt"))) == 1
    asyncio.run(DailyAccountReport(tmp_path, hourly=False).on_tick(T + DAY + 3_600_000))
    assert len(sent) == 2


def test_chunks():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = chunks(text)
    assert all(len(p) <= 4000 for p in parts) and "\n".join(parts) == text and len(parts) > 1


def test_mark_prices_fills_stock_entry():
    def handler(req):
        if req.url.path.endswith("ticker/price"):
            return httpx.Response(200, json=[{"symbol": "NVDAUSDT", "price": "230.5"}])
        assert req.url.params["symbol"] == "NVDAUSDT"
        return httpx.Response(200, json=[[0, "1", "1", "1", "228.0", "0"]])
    opens = [{"market": "stocks", "symbol": "NVDA", "t_entry": T, "side": 1}]
    px = asyncio.run(mark_prices(opens, transport=httpx.MockTransport(handler)))
    assert px == {"NVDAUSDT": 230.5} and opens[0]["entry_px"] == 228.0


def test_telegram_not_configured_and_sent(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr("jevbot.account._secret", lambda v: None)
    assert asyncio.run(send_telegram("x")) == "not_configured"
    monkeypatch.setattr("jevbot.account._secret", lambda v: "tok" if v.endswith("TOKEN") else "42")
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), req.content
        return httpx.Response(200, json={"ok": True})
    assert asyncio.run(send_telegram("merhaba", transport=httpx.MockTransport(handler))) == "sent"
    assert seen["url"].endswith("/bottok/sendMessage") and b"merhaba" in seen["body"]
