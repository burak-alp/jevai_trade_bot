import json
from datetime import date, timedelta

import httpx
import numpy as np

from jevbot import macro_desk as md
from jevbot import macro_paper as mp
from jevbot import telegram_bot as tb
from jevbot.research import macro


class FakeBn(mp.Binance):
    def __init__(self, px):
        self.px = px

    def prices(self):
        return dict(self.px)

    def funding(self, symbol, start, end):
        return 0.0


def _data(n=500):
    days = [d for d in (date(2024, 1, 1) + timedelta(days=i) for i in range(n)) if d.weekday() < 5]
    rng = np.random.default_rng(1)
    data = {a: {d.isoformat(): float(v) for d, v in zip(days, 100 * np.exp(np.cumsum(rng.normal(0.001, 0.01, len(days)))))}
            for a in macro.ASSETS}
    data[macro.CASH] = {d.isoformat(): 4.0 for d in days}
    return data, days


def test_status_scorecard_two_weeks(tmp_path):
    data, days = _data()
    old = macro.START
    macro.START = date(2024, 1, 1)
    try:
        fri = [d for d in days if d.weekday() == 4][-2]
        t0 = int(np.datetime64(fri.isoformat()).astype("datetime64[ms]").astype(int)) + 26 * 3_600_000
        px = {s: 100.0 for s, _ in mp.SYMBOLS.values()}
        cut = {a: {k: v for k, v in s.items() if k <= fri.isoformat()} for a, s in data.items()}
        r1 = mp.run_once(tmp_path, data_fn=lambda: cut, bn=FakeBn(px), now=t0, headlines=None)
        wk = r1["state"]["last_rebalance_week"]
        held = [a for a, v in r1["state"]["weights"].items() if v > 0]
        (tmp_path / f"committee-{wk}.json").write_text(json.dumps({
            "week": wk, "analysts": {"makro": {"assets": {held[0]: {"view": 1}}}},
            "pm": {"assets": {held[0]: {"tilt": 1}}, "note": "Fed sabit; altın güçlü."}}))
        (tmp_path / f"gpt-{wk}.json").write_text(json.dumps({"assets": {held[0]: {"view": -1}}}))
        px2 = dict(px)
        px2[mp.SYMBOLS[held[0]][0]] = 110.0                                  # the favoured asset rises 10 %
        r2 = mp.run_once(tmp_path, data_fn=lambda: data, bn=FakeBn(px2), now=t0 + 7 * 86_400_000, headlines=None)
        assert r2["rebalanced"]
        sc = md.scorecard(tmp_path)
        assert sc["analist:makro"]["hit"] == 1.0 and abs(sc["komite (PM)"]["mean_bps"] - 1000) < 1e-6
        assert sc["gpt"]["hit"] == 0.0 and abs(sc["gpt"]["mean_bps"] + 1000) < 1e-6
        text = md.status_text(tmp_path, px2)
        assert "Varlık sınıfı" in text and "Fed sabit" in text and "Karne" in text
        s = md.status(tmp_path, px2)
        total = sum(v["pnl"] for v in s["classes"].values()) + s["cash_pnl"] + s["unattributed"]
        assert abs(total - (s["equity"]["1x"] - 1000)) < 1e-6
    finally:
        macro.START = old


def test_telegram_only_answers_own_chat():
    ups = [{"update_id": 5, "message": {"chat": {"id": 42}, "text": "durum"}},
           {"update_id": 6, "message": {"chat": {"id": 99}, "text": "durum"}},
           {"update_id": 7, "message": {"chat": {"id": 42}, "text": "merhaba"}},
           {"update_id": 8, "message": {"chat": {"id": 42}, "text": "/yardım"}}]
    nxt, out = tb.handle_updates(ups, "42", lambda: "STATUS")
    assert nxt == 9 and out == ["STATUS", tb.HELP]


def test_listen_once(tmp_path, monkeypatch):
    monkeypatch.setattr("jevbot.telegram_bot._secret", lambda v: "tok" if v.endswith("TOKEN") else "42")
    sent = []

    def handler(req):
        if req.url.path.endswith("getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": 10, "message": {"chat": {"id": 42}, "text": "Durum"}}]})
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})
    tb.listen(lambda: "S" * 4500, tmp_path / "off", transport=httpx.MockTransport(handler), once=True, poll_s=0)
    assert [len(m["text"]) for m in sent] == [4000, 500] and (tmp_path / "off").read_text() == "11"
