"""macro.paper.v1: live paper account for macro.trend.v1 on Binance perps (public data only, no keys, no orders).

Once a day (scheduled): fetch Yahoo daily closes -> the *same* ``jevbot.research.macro.strategy`` as the backtest ->
on the week's last close, rebalance to its weights; every run marks to Binance prices with real funding.
Accounts: ``1x`` = v1 exactly, ``2x`` = the same weights doubled (leverage; financing via funding).
Perp mapping: SPY->SPYUSDT, QQQ->QQQUSDT, GLD->XAUUSDT, SLV->XAGUSDT, USO->CLUSDT, TLT->TMFUSDT (3x ETF, 1/3 notional),
BTC->BTCUSDT, ETH->ETHUSDT. Idle equity earns the 3-month T-bill rate (Simple Earn assumption).
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import numpy as np

from jevbot.research import macro

SYMBOLS = {"SPY": ("SPYUSDT", 1.0), "QQQ": ("QQQUSDT", 1.0), "GLD": ("XAUUSDT", 1.0), "SLV": ("XAGUSDT", 1.0),
           "USO": ("CLUSDT", 1.0), "TLT": ("TMFUSDT", 1 / 3), "BTC-USD": ("BTCUSDT", 1.0), "ETH-USD": ("ETHUSDT", 1.0)}
NAMES = {"SPY": "S&P 500", "QQQ": "Nasdaq", "GLD": "Altın", "SLV": "Gümüş", "USO": "Petrol", "TLT": "ABD tahvili",
         "BTC-USD": "BTC", "ETH-USD": "ETH"}
ACCOUNTS = {"1x": 1.0, "2x": 2.0}
START_EQUITY = 1000.0
LOOKBACK = (252,)
REST = "https://fapi.binance.com"
TR = timezone(timedelta(hours=3))
DAY = 86_400_000


def target_weights(data: dict[str, dict[str, float]], today: date) -> tuple[date, dict[str, float], float]:
    """(last data date, weights per asset from the backtest code, daily cash rate)."""
    days, P, cash = macro.align(data, macro.ASSETS, end=today + timedelta(days=1))
    _, _, w = macro.strategy(P, cash, days, LOOKBACK, return_last=True)
    return days[-1], {a: float(w[k]) for k, a in enumerate(macro.ASSETS)}, float(cash[-1])


def rebalance_due(last_week: str | None, latest: date, today: date) -> bool:
    wk = f"{latest.isocalendar()[0]}-W{latest.isocalendar()[1]:02d}"
    if last_week == wk:
        return False
    return last_week is None or latest.weekday() == 4 or today.isocalendar()[:2] != latest.isocalendar()[:2]


def week_of(d: date) -> str:
    return f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"


class Binance:
    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.c = httpx.Client(base_url=REST, timeout=30, transport=transport)

    def prices(self) -> dict[str, float]:
        return {x["symbol"]: float(x["price"]) for x in self.c.get("/fapi/v1/ticker/price").json()}

    def funding(self, symbol: str, start: int, end: int) -> float:
        rows = self.c.get("/fapi/v1/fundingRate", params={"symbol": symbol, "startTime": start + 1, "endTime": end,
                                                          "limit": 1000}).json()
        return float(sum(float(r["fundingRate"]) for r in rows)) if isinstance(rows, list) else 0.0


def _equity(acc: dict[str, Any], px: dict[str, float]) -> float:
    return acc["balance"] + sum(p["qty"] * (px[s] - p["entry"]) for s, p in acc["pos"].items() if s in px)


def mark(acc: dict[str, Any], px: dict[str, float], bn: Binance, now: int, cash_daily: float) -> dict[str, float]:
    """Funding + idle-cash interest since the last mark, booked into the balance."""
    since = acc["last_mark"]
    fund = 0.0
    for s, p in acc["pos"].items():
        if p["qty"] and s in px:
            fund -= p["qty"] * px[s] * bn.funding(s, since, now)          # long pays positive funding
    eq = _equity(acc, px)
    gross = sum(abs(p["qty"]) * px[s] for s, p in acc["pos"].items() if s in px)
    interest = max(eq - gross, 0.0) * cash_daily * (now - since) / DAY * 252 / 365
    acc["balance"] += fund + interest
    acc["last_mark"] = now
    return {"funding": fund, "interest": interest}


def rebalance(acc: dict[str, Any], weights: dict[str, float], lev: float, px: dict[str, float]) -> float:
    """Realize P&L at current prices and move to the target notionals; returns the cost paid."""
    acc["balance"] = _equity(acc, px)
    old = {s: p["qty"] * px[s] for s, p in acc["pos"].items() if s in px}
    eq = acc["balance"]
    new_pos, cost = {}, 0.0
    for a, w in weights.items():
        sym, factor = SYMBOLS[a]
        notional = w * lev * factor * eq
        cost += macro.COST * abs(notional - old.pop(sym, 0.0))
        if notional and sym in px:
            new_pos[sym] = {"qty": notional / px[sym], "entry": px[sym]}
    cost += macro.COST * sum(abs(v) for v in old.values())
    acc["balance"] -= cost
    acc["pos"] = new_pos
    return cost


def run_once(state_dir: Path, *, data_fn: Callable[[], dict] | None = None, bn: Binance | None = None,
             now: int | None = None) -> dict[str, Any]:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    sf = state_dir / "state.json"
    now = now or int(time.time() * 1000)
    today = datetime.fromtimestamp(now / 1000, timezone.utc).date()
    data = (data_fn or (lambda: {t: macro.fetch(t) for t in macro.ASSETS + [macro.CASH]}))()
    latest, weights, cash_daily = target_weights(data, today)
    bn = bn or Binance()
    px = bn.prices()
    st = json.loads(sf.read_text(encoding="utf-8")) if sf.exists() else {
        "accounts": {n: {"balance": START_EQUITY, "pos": {}, "last_mark": now} for n in ACCOUNTS},
        "last_rebalance_week": None, "weights": {}, "history": []}
    rows: list[dict[str, Any]] = []
    marks = {n: mark(st["accounts"][n], px, bn, now, cash_daily) for n in ACCOUNTS}
    did = rebalance_due(st["last_rebalance_week"], latest, today)
    costs = {}
    if did:
        for n, lev in ACCOUNTS.items():
            costs[n] = rebalance(st["accounts"][n], weights, lev, px)
        st["last_rebalance_week"] = week_of(latest)
        prev = st.get("weights", {})
        st["weights"] = weights
        rows.append({"kind": "rebalance", "at": now, "data_date": str(latest), "weights": weights, "prev": prev,
                     "costs": costs, "prices": {SYMBOLS[a][0]: px.get(SYMBOLS[a][0]) for a in weights}})
    eqs = {n: _equity(st["accounts"][n], px) for n in ACCOUNTS}
    st["history"].append({"at": now, **eqs})
    rows.append({"kind": "mark", "at": now, "data_date": str(latest), "equity": eqs, "marks": marks})
    with open(state_dir / "ledger.jsonl", "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    sf.write_text(json.dumps(st, indent=1), encoding="utf-8")
    return {"state": st, "rebalanced": did, "latest": latest, "prices": px, "equity": eqs}


def to_text(res: dict[str, Any]) -> str:
    st, px = res["state"], res["prices"]
    when = datetime.fromtimestamp(st["history"][-1]["at"] / 1000, TR).strftime("%d.%m %H:%M")
    hist = st["history"]
    lines = [f"🌍 Makro trend sanal hesap — {when}"]
    for n in ACCOUNTS:
        eq = res["equity"][n]
        prev = hist[-2][n] if len(hist) > 1 else START_EQUITY
        lines.append(f"{n}: {eq:,.2f}$ ({eq / START_EQUITY - 1:+.2%} toplam, {eq / prev - 1:+.2%} son çalışmadan beri)")
    w = st["weights"]
    held = [f"{NAMES[a]} %{v * 100:.0f}" for a, v in sorted(w.items(), key=lambda kv: -kv[1]) if v > 0.0005]
    lines.append("Dağılım (1x): " + (", ".join(held) if held else "—") + f", nakit %{(1 - sum(w.values())) * 100:.0f}")
    acc = st["accounts"]["1x"]
    for s, p in sorted(acc["pos"].items()):
        if s in px:
            r = px[s] / p["entry"] - 1
            lines.append(f"{'🟢' if r >= 0 else '🔴'} {s.removesuffix('USDT')}: {p['entry']:,.4g} → {px[s]:,.4g} {r:+.2%} "
                         f"({p['qty'] * (px[s] - p['entry']):+.2f}$)")
    if res["rebalanced"]:
        lines.append(f"🔄 Haftalık yeniden dengeleme yapıldı (veri {res['latest']:%d.%m}).")
    lines.append("Sanal hesap; gerçek para yok. Kural: 1 yıllık getirisi nakitten iyi olanı tut, ters oynaklıkla dağıt.")
    return "\n".join(lines)
