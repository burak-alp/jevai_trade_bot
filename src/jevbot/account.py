"""Unified virtual account: Jev's crypto direction signals (C panel) + Jev's US-stock calls (llm) in one paper
account, next to always-long and random accounts with the same rules. Read-only reconstruction from the
ledgers; no new predictions. Position sizing is code, never Jev: 1x, each new position = equity / MAX_POS,
skipped when MAX_POS positions are open. Crypto: 24 h hold at the panel tick, 12 bps round trip (as the C
report; funding not included). Stocks: llm.v1 outcomes (20 bps + funding already included).
"""

from __future__ import annotations

import hashlib
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import orjson

from jevbot.judgment.state import STATE_SCHEMA
from jevbot.paper.engine import read_jsonl

START_EQUITY = 1000.0
SLOTS = {"crypto": 20, "stocks": 12}                    # per-market caps: crypto panels must not crowd out stocks
MAX_POS = sum(SLOTS.values())                          # each position = equity / MAX_POS
CRYPTO_COST = 0.0012
STOCK_COST = 0.0020                                     # as llmtrader COST (funding not in the hourly mark)
REST_BASE = "https://fapi.binance.com"
SCORE_MIN = 0.2
TR = timezone(timedelta(hours=3))
ARMS = ("jev", "jev_dyn", "always_long", "random")
# jev_dyn (pre-registered 2026-10-02, reporting only; the C test itself is unchanged): the same Jev crypto
# positions, but closed at the first later 4-hourly panel answer for the same symbol that signals the opposite side
# (|score| >= SCORE_MIN), at that panel's close; otherwise exactly as jev. Stocks: same as jev (daily decisions).


def _rand_side(key: str) -> int:
    return (int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 3) - 1        # -1 / 0 / +1


def trades(state_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Per arm: {market, symbol, side, t_entry, t_exit, net (fraction of notional) or None while open}."""
    out: dict[str, list[dict[str, Any]]] = {a: [] for a in ARMS}
    led = read_jsonl(Path(state_dir) / "jev.jsonl")
    asked = {r["id"]: r.get("state", {}) for r in led if r.get("kind") == "ask" and r.get("question") == "direction_h"}
    opens = {r["id"]: r for r in led if r.get("kind") == "dir_open" and r.get("band_pct") is None}
    outs = {r["id"]: r for r in led if r.get("kind") == "dir_outcome" and r.get("status") == "ok"}
    seen: dict[str, list[tuple[int, float, float | None]]] = {}
    for j in led:
        if j.get("kind") != "judgment" or j.get("question") != "direction_h" or j.get("status") != "ok":
            continue
        o = opens.get(j["id"])
        st = asked.get(j["id"], {})
        if o is None or st.get("schema") != STATE_SCHEMA:
            continue
        p = j["probs"]
        score = p["direction_h.up"] - p["direction_h.down"]
        ret = outs[j["id"]]["ret_bps"] / 1e4 if j["id"] in outs else None
        base = {"market": "crypto", "symbol": o["symbol"], "t_entry": o["t_tick"], "entry_px": o.get("close"),
                "t_exit": o["t_tick"] + o["horizon_min"] * 60_000,
                "why": _why_crypto(p["direction_h.up"], p["direction_h.down"], st.get("asset", {}))}
        for arm, side in (("jev", 1 if score >= SCORE_MIN else -1 if score <= -SCORE_MIN else 0), ("always_long", 1),
                          ("random", _rand_side(f"{o['symbol']}|{o['t_tick']}"))):
            if side:
                out[arm].append({**base, "side": side, "net": None if ret is None else side * ret - CRYPTO_COST})
        seen.setdefault(o["symbol"], []).append((o["t_tick"], score, o.get("close")))
    for t in out["jev"]:
        flip = next(((tt, px) for tt, sc, px in sorted(seen.get(t["symbol"], [])) if t["t_entry"] < tt < t["t_exit"]
                     and sc * t["side"] <= -SCORE_MIN and px and t["entry_px"]), None)
        out["jev_dyn"].append(t if flip is None else {**t, "t_exit": flip[0], "early": True,
                                                       "net": t["side"] * math.log(flip[1] / t["entry_px"]) - CRYPTO_COST})
    lled = read_jsonl(Path(state_dir) / "llm" / "ledger.jsonl")
    louts = {r["id"]: r for r in lled if r.get("kind") == "outcome"}
    ctx: dict[int, dict[str, Any]] = {}
    for d in lled:
        if d.get("kind") != "decision" or d["arm"] not in ARMS or not d["side"]:
            continue
        o = louts.get(f"{d['arm']}|{d['t_decision']}|{d['ticker']}")
        arms = ("jev", "jev_dyn") if d["arm"] == "jev" else (d["arm"],)
        if d["t_decision"] not in ctx:
            f = Path(state_dir) / "llm" / f"context-{datetime.fromtimestamp(d['t_decision'] / 1000, timezone.utc):%Y%m%d}.json"
            try:
                ctx[d["t_decision"]] = orjson.loads(f.read_bytes()).get("symbols", {})
            except (OSError, orjson.JSONDecodeError):
                ctx[d["t_decision"]] = {}
        row = {"market": "stocks", "symbol": d["ticker"], "side": d["side"], "t_entry": d["t_entry"],
                              "t_exit": d["t_entry"] + 86_400_000, "net": None if o is None else o.get("net"),
                              "why": _why_stock(d, ctx[d["t_decision"]].get(d["ticker"], {}))}
        for a in arms:
            out[a].append(row)
    return out


def _why_crypto(up: float, down: float, a: dict[str, Any]) -> str:
    """Jev's probabilities + the main inputs it saw (state.slow.v2 asset block)."""
    parts = [f"Jev ↑%{up * 100:.0f} ↓%{down * 100:.0f}"]
    if "ret_24h_atr_1h" in a:
        parts.append(f"24s {a['ret_24h_atr_1h']:+.1f} ATR, 7g {a.get('ret_7d_atr_1h', 0):+.1f} ATR")
    if "ema50_vs_ema200_sign" in a:
        parts.append("trend " + ("yukarı" if a["ema50_vs_ema200_sign"] > 0 else "aşağı"))
    if "funding_bps_8h" in a:
        parts.append(f"funding {a['funding_bps_8h']:+.1f} bps")
    return " | ".join(parts)


def _why_stock(d: dict[str, Any], c: dict[str, Any]) -> str:
    """Probabilities + the arm's own reason when it gave one, else the context it saw (returns, trend, a headline)."""
    parts = [f"↑%{d.get('up', 0) * 100:.0f} ↓%{d.get('down', 0) * 100:.0f}"]
    if d.get("reason"):
        return " | ".join(parts + [d["reason"]])
    if "ret_1d_pct" in c:
        parts.append(f"1g {c['ret_1d_pct']:+.1f}%, 5g {c.get('ret_5d_pct', 0):+.1f}%")
    if "ema50_above_ema200" in c:
        parts.append("trend " + ("yukarı" if c["ema50_above_ema200"] else "aşağı"))
    if c.get("headlines"):
        parts.append(f"haber: {c['headlines'][0][:90]}")
    return " | ".join(parts)


def simulate(ts: list[dict[str, Any]], now: int) -> dict[str, Any]:
    """Chronological fills; realized P&L at exit; open = entered and not yet exited (or not yet settled)."""
    events = sorted([(t["t_entry"], 1, i) for i, t in enumerate(ts)] + [(t["t_exit"], 0, i) for i, t in enumerate(ts)])
    equity, peak, max_dd = START_EQUITY, START_EQUITY, 0.0
    held: dict[int, float] = {}
    realized, curve = [], []
    for t, kind, i in events:
        if t > now:
            break
        tr = ts[i]
        if kind == 1:
            if sum(ts[k]["market"] == tr["market"] for k in held) < SLOTS[tr["market"]]:
                held[i] = equity / MAX_POS
        elif i in held:
            if tr["net"] is None:
                continue                                     # past exit but not settled yet: stays open
            pnl = held.pop(i) * tr["net"]
            equity += pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, 1 - equity / peak)
            realized.append({**tr, "pnl": pnl})
            curve.append((t, equity))
    day_ago = now - 86_400_000
    last24 = [r for r in realized if r["t_exit"] > day_ago]
    last1 = [r for r in realized if r["t_exit"] > now - 3_600_000]
    return {"equity": equity, "return": equity / START_EQUITY - 1, "max_dd": max_dd, "trades": len(realized),
            "crypto": sum(r["market"] == "crypto" for r in realized), "stocks": sum(r["market"] == "stocks" for r in realized),
            "open": [{**ts[i], "size": sz} for i, sz in held.items()], "pnl_24h": sum(r["pnl"] for r in last24),
            "early": sum(bool(r.get("early")) for r in realized), "closed_1h": last1, "pnl_1h": sum(r["pnl"] for r in last1),
            "hit_24h": (sum(r["net"] > 0 for r in last24) / len(last24)) if last24 else None, "curve": curve}


def account_report(state_dir: Path, now: int | None = None) -> dict[str, Any]:
    now = now or int(time.time() * 1000)
    tr = trades(state_dir)
    since = min((t["t_entry"] for ts in tr.values() for t in ts), default=now)
    return {"now": now, "since": since, "arms": {a: simulate(tr[a], now) for a in ARMS}}


def to_text(rep: dict[str, Any]) -> str:
    when = datetime.fromtimestamp(rep["now"] / 1000, TR).strftime("%d.%m %H:%M")
    j, dy, al, rd = (rep["arms"][a] for a in ARMS)
    hit = f"%{j['hit_24h'] * 100:.0f}" if j["hit_24h"] is not None else "—"
    return "\n".join([
        f"🤖 Jev sanal hesap — {when}",
        f"Tek hesap: {datetime.fromtimestamp(rep.get('since', rep['now']) / 1000, TR):%d.%m}'de {START_EQUITY:,.0f}$ ile "
        f"başladı, günlük sıfırlanmaz; kâr/zarar birikir, yeni pozisyon güncel bakiyeye göre açılır (1/{MAX_POS} pay).",
        f"Jev: {j['equity']:,.2f}$ ({j['return']:+.2%}) | maks. düşüş {j['max_dd']:.1%} | işlem {j['trades']} "
        f"(kripto {j['crypto']}, hisse {j['stocks']}) | açık {len(j['open'])}",
        f"Jev dinamik (ters sinyalde erken çıkış): {dy['equity']:,.2f}$ ({dy['return']:+.2%}) | "
        f"erken kapanan {dy['early']}",
        f"Hep long: {al['equity']:,.2f}$ ({al['return']:+.2%}) | Rastgele: {rd['equity']:,.2f}$ ({rd['return']:+.2%})",
        f"Son 24 saat: {j['pnl_24h']:+.2f}$ | isabet {hit}",
        "Sanal hesap; gerçek para yok. Karar: kripto 06.10 eleme / 27.10 karar, hisse 4. ve 8. hafta.",
    ])


def _px(x: float) -> str:
    return f"{x:,.2f}" if x >= 100 else f"{x:.4f}" if x >= 1 else f"{x:.6g}"


async def mark_prices(opens: list[dict[str, Any]], rest_base: str = REST_BASE,
                      transport: httpx.AsyncBaseTransport | None = None) -> dict[str, float]:
    """Fills missing ``entry_px`` (stocks: close of the 1m bar before entry, as llm settle) in place and returns
    current Binance futures prices (public endpoints, no key). Errors leave prices missing."""
    try:
        async with httpx.AsyncClient(base_url=rest_base, timeout=20, transport=transport) as c:
            now = {x["symbol"]: float(x["price"]) for x in (await c.get("/fapi/v1/ticker/price")).json()}
            for p in opens:
                if p.get("entry_px") is None:
                    k = (await c.get("/fapi/v1/klines", params={"symbol": _sym(p), "interval": "1m",
                                                                "startTime": p["t_entry"] - 60_000, "limit": 1})).json()
                    p["entry_px"] = float(k[0][4]) if k else None
            return now
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return {}


def _sym(p: dict[str, Any]) -> str:
    return p["symbol"] + "USDT" if p["market"] == "stocks" else p["symbol"]


def to_hourly(rep: dict[str, Any], prices: dict[str, float]) -> str:
    """Hourly message: account line + every open Jev position (entry, now, unrealized) + positions closed in the
    last hour. Unrealized = side x simple return - round-trip cost, on the position's size."""
    when = datetime.fromtimestamp(rep["now"] / 1000, TR).strftime("%d.%m %H:%M")
    j, dy, al, rd = (rep["arms"][a] for a in ARMS)
    lines, unreal = [], 0.0
    for mkt, title, cost in (("stocks", "📈 Hisse", STOCK_COST), ("crypto", "🪙 Kripto", CRYPTO_COST)):
        ps = sorted((p for p in j["open"] if p["market"] == mkt), key=lambda p: p["symbol"])
        if not ps:
            continue
        lines.append(f"{title} ({len(ps)} açık)")
        for p in ps:
            yon = "LONG " if p["side"] > 0 else "SHORT"
            t0 = datetime.fromtimestamp(p["t_entry"] / 1000, TR).strftime("%d.%m %H:%M")
            p0, p1 = p.get("entry_px"), prices.get(_sym(p))
            if p0 and p1:
                r = p["side"] * (p1 / p0 - 1) - cost
                unreal += p["size"] * r
                lines.append(f"{'🟢' if r > 0 else '🔴'} {yon} {p['symbol'].removesuffix('USDT')}: {_px(p0)} → {_px(p1)} "
                             f"{r:+.2%} ({p['size'] * r:+.2f}$) [{t0}]")
            else:
                lines.append(f"⚪ {yon} {p['symbol'].removesuffix('USDT')}: fiyat alınamadı [{t0}]")
            if p.get("why"):
                lines.append(f"   ↳ {p['why']}")
    closed = [f"{'✅' if r['net'] > 0 else '❌'} {'LONG' if r['side'] > 0 else 'SHORT'} {r['symbol'].removesuffix('USDT')} "
              f"{r['net']:+.2%} ({r['pnl']:+.2f}$)" for r in j["closed_1h"]]
    head = [f"🕐 {when} | Jev hesap {j['equity']:,.2f}$ ({j['return']:+.2%})",
            f"Açık pozisyonların anlık kâr/zararı: {unreal:+.2f}$",
            f"Jev dinamik {dy['return']:+.2%} | Hep long {al['return']:+.2%} | Rastgele {rd['return']:+.2%}"]
    if closed:
        head += [f"Son 1 saatte kapanan ({j['pnl_1h']:+.2f}$):", *closed]
    return "\n".join(head + [""] + lines) if lines else "\n".join(head + ["Açık pozisyon yok."])


def chunks(text: str, limit: int = 4000) -> list[str]:
    """Split on line boundaries into Telegram-sized messages (4096 max)."""
    out, cur = [], ""
    for line in text.split("\n"):
        line = line[:limit]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    return out + [cur] if cur else out


def _secret(var: str) -> str | None:
    v = os.environ.get(var)
    if v or sys.platform != "win32":
        return v or None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, var)[0]) or None
    except OSError:
        return None


async def send_telegram(text: str, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Sends ``text`` with TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (never logged). Returns a status word."""
    token, chat = _secret("TELEGRAM_BOT_TOKEN"), _secret("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return "not_configured"
    try:
        async with httpx.AsyncClient(timeout=20, transport=transport) as c:
            r = await c.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": text})
        return "sent" if r.status_code == 200 else f"http_{r.status_code}"
    except httpx.HTTPError as e:
        return f"error_{type(e).__name__}"


class DailyAccountReport:
    """Paper-engine hook: every day at 18:00 UTC (21:00 Turkey) write the full report and send it to Telegram;
    with ``hourly`` the other ticks send a one-line status (appended to hourly-YYYYMMDD.txt)."""

    def __init__(self, state_dir: Path, hour_utc: int = 18, hourly: bool = True) -> None:
        self.dir, self.hour, self.hourly = Path(state_dir), hour_utc, hourly

    async def on_tick(self, t_tick: int) -> None:
        daily = datetime.fromtimestamp(t_tick / 1000, timezone.utc).hour == self.hour
        if not daily and not self.hourly:
            return
        rep = account_report(self.dir, t_tick)
        text = to_text(rep) if daily else to_hourly(rep, await mark_prices(rep["arms"]["jev"]["open"]))
        out = self.dir / "account"
        out.mkdir(parents=True, exist_ok=True)
        status = "sent"
        for part in chunks(text):
            st = await send_telegram(part)
            status = status if st == "sent" else st
        day = f"{datetime.fromtimestamp(t_tick / 1000, TR):%Y%m%d}"
        if daily:
            (out / f"report-{day}.txt").write_text(text + f"\n[telegram: {status}]\n", encoding="utf-8")
        else:
            with open(out / f"hourly-{day}.txt", "a", encoding="utf-8") as f:
                f.write(text + f"\n[telegram: {status}]\n")

    async def settle(self, now: int) -> None:
        return None
