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
LLM = "llm"                                             # overlay.v1 account (1x, v1 weights tilted by Claude's view)
COMMITTEE = "committee"                                 # committee.v1 account (1x, tilted by the PM after analysts)
OVERLAYS = {LLM: "overlay", COMMITTEE: "committee"}     # account -> weekly tilt file prefix
TILT = {-1: 0.5, 0: 1.0, 1: 1.5}
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
    attr = acc.setdefault("pnl_sym", {})
    for s, p in acc["pos"].items():
        if p["qty"] and s in px:
            f = -p["qty"] * px[s] * bn.funding(s, since, now)              # long pays positive funding
            fund += f
            attr[s] = attr.get(s, 0.0) + f
    eq = _equity(acc, px)
    gross = sum(abs(p["qty"]) * px[s] for s, p in acc["pos"].items() if s in px)
    interest = max(eq - gross, 0.0) * cash_daily * (now - since) / DAY * 252 / 365
    attr["CASH"] = attr.get("CASH", 0.0) + interest
    acc["balance"] += fund + interest
    acc["last_mark"] = now
    return {"funding": fund, "interest": interest}


def rebalance(acc: dict[str, Any], weights: dict[str, float], lev: float, px: dict[str, float]) -> float:
    """Realize P&L at current prices and move to the target notionals; returns the cost paid."""
    attr = acc.setdefault("pnl_sym", {})
    for s, p in acc["pos"].items():
        if s in px:
            attr[s] = attr.get(s, 0.0) + p["qty"] * (px[s] - p["entry"])
    acc["balance"] = _equity(acc, px)
    old = {s: p["qty"] * px[s] for s, p in acc["pos"].items() if s in px}
    eq = acc["balance"]
    new_pos, cost = {}, 0.0
    for a, w in weights.items():
        sym, factor = SYMBOLS[a]
        notional = w * lev * factor * eq
        c = macro.COST * abs(notional - old.pop(sym, 0.0))
        cost += c
        attr[sym] = attr.get(sym, 0.0) - c
        if notional and sym in px:
            new_pos[sym] = {"qty": notional / px[sym], "entry": px[sym]}
    for sym, v in old.items():
        attr[sym] = attr.get(sym, 0.0) - macro.COST * abs(v)
        cost += macro.COST * abs(v)
    acc["balance"] -= cost
    acc["pos"] = new_pos
    return cost


def apply_overlay(weights: dict[str, float], tilts: dict[str, int]) -> dict[str, float]:
    """overlay.v1: v1 weight x {-1: 0.5, 0: 1, +1: 1.5}; assets v1 does not hold stay 0; total capped at 1."""
    out = {a: w * TILT.get(int(tilts.get(a, 0)), 1.0) for a, w in weights.items()}
    tot = sum(out.values())
    return {a: w / tot for a, w in out.items()} if tot > 1 else out


def build_context(data: dict[str, dict[str, float]], latest: date, weights: dict[str, float], now: int,
                  headlines: Callable[[str, int], list[str]] | None = None) -> dict[str, Any]:
    days, P, cash = macro.align(data, macro.ASSETS, end=latest + timedelta(days=1))
    with np.errstate(invalid="ignore", divide="ignore"):
        R = P[1:] / P[:-1] - 1
    assets = {}
    for k, a in enumerate(macro.ASSETS):
        def ret(n: int) -> float | None:
            v = P[-1, k] / P[-1 - n, k] - 1 if len(P) > n else float("nan")
            return round(float(v) * 100, 2) if np.isfinite(v) else None
        vol = np.nanstd(R[-60:, k]) * np.sqrt(252)
        assets[a] = {"name": NAMES[a], "binance": SYMBOLS[a][0], "v1_weight": round(weights[a], 4),
                     "ret_1m_pct": ret(21), "ret_3m_pct": ret(63), "ret_12m_pct": ret(252),
                     "vol_60d_pct": round(float(vol) * 100, 1) if np.isfinite(vol) else None,
                     "headlines": headlines(a, now) if headlines else []}
    return {"version": "overlay.v1", "data_date": str(latest), "week": week_of(latest),
            "cash_rate_pct": round(float(cash[-1]) * 252 * 100, 2), "assets": assets}


def _headlines(asset: str, now: int) -> list[str]:
    import asyncio

    from jevbot.llmtrader import yahoo_headlines

    async def go() -> list[str]:
        async with httpx.AsyncClient() as c:
            return await yahoo_headlines(c, asset, now, hours=72, n=8)
    try:
        return asyncio.run(go())
    except Exception:                                     # noqa: BLE001 - headlines are optional context
        return []


JEV_BASE = "https://jev-ai.pro/api"


def jev_views(ctx: dict[str, Any], client: Any = None) -> dict[str, Any]:
    """Jev as a committee member: P(up/flat/down) over the next 5 trading days per asset, band = half a weekly
    standard deviation (from the 60 d volatility). Needs JEV_API_KEY; any failure -> {} (the committee runs without)."""
    import asyncio

    from jevbot.judgment.client import JevClient, load_api_key, pick_model

    if client is None and not load_api_key():
        return {}

    async def go() -> dict[str, Any]:
        jc = client or JevClient(base_url=JEV_BASE)
        try:
            model, _ = pick_model(await jc.models())
            if not model:
                return {}
            out: dict[str, Any] = {"model": model}
            for a, v in ctx["assets"].items():
                band = max(0.5, round(0.5 * (v.get("vol_60d_pct") or 20) * (5 / 252) ** 0.5, 1))
                q = {"direction_5d": {"type": "choice",
                                      "instructions": f"Over the next 5 trading days, will this asset's price change be "
                                                      f"above +{band}% (up), below -{band}% (down), or in between (flat)?",
                                      "criteria": {"up": f"above +{band}%", "flat": f"between -{band}% and +{band}%",
                                                   "down": f"below -{band}%"}}}
                state = {"asset": a, **{k: v.get(k) for k in ("name", "ret_1m_pct", "ret_3m_pct", "ret_12m_pct",
                                                               "vol_60d_pct", "headlines")}}
                r = await jc.ask(model, state, q)
                if r.get("status") == "ok":
                    out[a] = {k: round(r["probs"][f"direction_5d.{k}"], 3) for k in ("up", "flat", "down")}
                    out[a]["band_pct"] = band
            return out
        finally:
            if client is None:
                await jc.aclose()
    try:
        return asyncio.run(go())
    except Exception:                                     # noqa: BLE001 - optional member
        return {}


def _read_tilts(path: Path, kind: str) -> dict[str, int] | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        assets = d["pm"]["assets"] if kind == "committee" else d["assets"]
        return {a: int(v["tilt"]) for a, v in assets.items() if a in macro.ASSETS and int(v["tilt"]) in TILT}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def run_once(state_dir: Path, *, data_fn: Callable[[], dict] | None = None, bn: Binance | None = None,
             now: int | None = None, headlines: Callable[[str, int], list[str]] | None = _headlines,
             jev: bool = False) -> dict[str, Any]:
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
    applied = st.setdefault("applied", {LLM: st.get("llm_applied")} if "llm_applied" in st else {})
    for name in OVERLAYS:
        if name not in st["accounts"]:
            st["accounts"][name] = {"balance": START_EQUITY, "pos": {}, "last_mark": now}
            st.setdefault("since", {})[name], applied[name] = now, None
            if st["last_rebalance_week"]:                  # added mid-week: start on v1's current weights
                rebalance(st["accounts"][name], st["weights"], 1.0, px)
                cf = state_dir / f"context-{st['last_rebalance_week']}.json"
                if not cf.exists():
                    ctx = build_context(data, latest, st["weights"], now, headlines)
                    ctx["week"] = st["last_rebalance_week"]
                    cf.write_text(json.dumps(ctx, indent=1, ensure_ascii=False), encoding="utf-8")
    rows: list[dict[str, Any]] = []
    marks = {n: mark(st["accounts"][n], px, bn, now, cash_daily) for n in st["accounts"]}
    did = rebalance_due(st["last_rebalance_week"], latest, today)
    costs = {}
    if did:
        for n, lev in ACCOUNTS.items():
            costs[n] = rebalance(st["accounts"][n], weights, lev, px)
        for name in OVERLAYS:                              # v1 until this week's tilt file arrives
            costs[name] = rebalance(st["accounts"][name], weights, 1.0, px)
            applied[name] = None
        st["last_rebalance_week"] = week_of(latest)
        ctx = build_context(data, latest, weights, now, headlines)
        if jev:
            ctx["jev"] = jev_views(ctx)
        (state_dir / f"context-{ctx['week']}.json").write_text(json.dumps(ctx, indent=1, ensure_ascii=False),
                                                               encoding="utf-8")
        prev = st.get("weights", {})
        st["weights"] = weights
        rows.append({"kind": "rebalance", "at": now, "data_date": str(latest), "weights": weights, "prev": prev,
                     "costs": costs, "prices": {SYMBOLS[a][0]: px.get(SYMBOLS[a][0]) for a in weights}})
    wk = st["last_rebalance_week"]
    st["notes"], st["committee_note"] = {}, None
    for name, prefix in OVERLAYS.items():
        of = state_dir / f"{prefix}-{wk}.json"
        if not wk or applied.get(name) == wk or not of.exists():
            continue
        tilts = _read_tilts(of, prefix)
        if tilts is None:
            continue
        lw = apply_overlay(st["weights"], tilts)
        c = rebalance(st["accounts"][name], lw, 1.0, px)
        applied[name] = wk
        st.setdefault("overlay_weights", {})[name] = lw
        st["notes"][name] = tilts
        if name == COMMITTEE:
            try:
                st["committee_note"] = str(json.loads(of.read_text(encoding="utf-8"))["pm"].get("note", ""))[:1500]
            except (OSError, ValueError, KeyError):
                st["committee_note"] = ""
        rows.append({"kind": "overlay", "account": name, "at": now, "week": wk, "tilts": tilts, "weights": lw,
                     "cost": c})
    eqs = {n: _equity(st["accounts"][n], px) for n in st["accounts"]}
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
    for n in [*ACCOUNTS, *OVERLAYS]:
        if n not in res["equity"]:
            continue
        eq = res["equity"][n]
        prev = hist[-2].get(n, START_EQUITY) if len(hist) > 1 else START_EQUITY
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
    labels = {LLM: "🧠 Claude yorumu uygulandı (llm hesabı)", COMMITTEE: "🏛 Komite kararı uygulandı (committee hesabı)"}
    for name, tilts in (st.get("notes") or {}).items():
        t = {1: "↑", -1: "↓"}
        moves = [f"{NAMES[a]} {t[v]}" for a, v in tilts.items() if v]
        lines.append(f"{labels[name]}: " + (", ".join(moves) if moves else "değişiklik yok"))
    if st.get("committee_note"):
        lines.append("🏛 Komite notu:\n" + st["committee_note"])
    lines.append("Sanal hesap; gerçek para yok. Kural: 1 yıllık getirisi nakitten iyi olanı tut, ters oynaklıkla dağıt.")
    return "\n".join(lines)
