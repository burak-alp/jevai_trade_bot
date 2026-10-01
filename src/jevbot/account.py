"""Unified virtual account: Jev's crypto direction signals (C panel) + Jev's US-stock calls (llm) in one paper
account, next to always-long and random accounts with the same rules. Read-only reconstruction from the
ledgers; no new predictions. Position sizing is code, never Jev: 1x, each new position = equity / MAX_POS,
skipped when MAX_POS positions are open. Crypto: 24 h hold at the panel tick, 12 bps round trip (as the C
report; funding not included). Stocks: llm.v1 outcomes (20 bps + funding already included).
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from jevbot.judgment.state import STATE_SCHEMA
from jevbot.paper.engine import read_jsonl

START_EQUITY = 1000.0
SLOTS = {"crypto": 20, "stocks": 12}                    # per-market caps: crypto panels must not crowd out stocks
MAX_POS = sum(SLOTS.values())                          # each position = equity / MAX_POS
CRYPTO_COST = 0.0012
SCORE_MIN = 0.2
TR = timezone(timedelta(hours=3))
ARMS = ("jev", "always_long", "random")


def _rand_side(key: str) -> int:
    return (int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 3) - 1        # -1 / 0 / +1


def trades(state_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Per arm: {market, symbol, side, t_entry, t_exit, net (fraction of notional) or None while open}."""
    out: dict[str, list[dict[str, Any]]] = {a: [] for a in ARMS}
    led = read_jsonl(Path(state_dir) / "jev.jsonl")
    asked = {r["id"]: r.get("state", {}).get("schema") for r in led if r.get("kind") == "ask"
             and r.get("question") == "direction_h"}
    opens = {r["id"]: r for r in led if r.get("kind") == "dir_open" and r.get("band_pct") is None}
    outs = {r["id"]: r for r in led if r.get("kind") == "dir_outcome" and r.get("status") == "ok"}
    for j in led:
        if j.get("kind") != "judgment" or j.get("question") != "direction_h" or j.get("status") != "ok":
            continue
        o = opens.get(j["id"])
        if o is None or asked.get(j["id"]) != STATE_SCHEMA:
            continue
        p = j["probs"]
        score = p["direction_h.up"] - p["direction_h.down"]
        ret = outs[j["id"]]["ret_bps"] / 1e4 if j["id"] in outs else None
        base = {"market": "crypto", "symbol": o["symbol"], "t_entry": o["t_tick"],
                "t_exit": o["t_tick"] + o["horizon_min"] * 60_000}
        for arm, side in (("jev", 1 if score >= SCORE_MIN else -1 if score <= -SCORE_MIN else 0), ("always_long", 1),
                          ("random", _rand_side(f"{o['symbol']}|{o['t_tick']}"))):
            if side:
                out[arm].append({**base, "side": side, "net": None if ret is None else side * ret - CRYPTO_COST})
    lled = read_jsonl(Path(state_dir) / "llm" / "ledger.jsonl")
    louts = {r["id"]: r for r in lled if r.get("kind") == "outcome"}
    for d in lled:
        if d.get("kind") != "decision" or d["arm"] not in ARMS or not d["side"]:
            continue
        o = louts.get(f"{d['arm']}|{d['t_decision']}|{d['ticker']}")
        out[d["arm"]].append({"market": "stocks", "symbol": d["ticker"], "side": d["side"], "t_entry": d["t_entry"],
                              "t_exit": d["t_entry"] + 86_400_000, "net": None if o is None else o.get("net")})
    return out


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
    return {"pnl_1h": sum(r["pnl"] for r in last1), "closed_1h": len(last1),"equity": equity, "return": equity / START_EQUITY - 1, "max_dd": max_dd, "trades": len(realized),
            "crypto": sum(r["market"] == "crypto" for r in realized), "stocks": sum(r["market"] == "stocks" for r in realized),
            "open": [ts[i] for i in held], "pnl_24h": sum(r["pnl"] for r in last24),
            "hit_24h": (sum(r["net"] > 0 for r in last24) / len(last24)) if last24 else None, "curve": curve}


def account_report(state_dir: Path, now: int | None = None) -> dict[str, Any]:
    now = now or int(time.time() * 1000)
    tr = trades(state_dir)
    since = min((t["t_entry"] for ts in tr.values() for t in ts), default=now)
    return {"now": now, "since": since, "arms": {a: simulate(tr[a], now) for a in ARMS}}


def to_text(rep: dict[str, Any]) -> str:
    when = datetime.fromtimestamp(rep["now"] / 1000, TR).strftime("%d.%m %H:%M")
    j, al, rd = (rep["arms"][a] for a in ARMS)
    hit = f"%{j['hit_24h'] * 100:.0f}" if j["hit_24h"] is not None else "—"
    return "\n".join([
        f"🤖 Jev sanal hesap — {when}",
        f"Tek hesap: {datetime.fromtimestamp(rep.get('since', rep['now']) / 1000, TR):%d.%m}'de {START_EQUITY:,.0f}$ ile "
        f"başladı, günlük sıfırlanmaz; kâr/zarar birikir, yeni pozisyon güncel bakiyeye göre açılır (1/{MAX_POS} pay).",
        f"Jev: {j['equity']:,.2f}$ ({j['return']:+.2%}) | maks. düşüş {j['max_dd']:.1%} | işlem {j['trades']} "
        f"(kripto {j['crypto']}, hisse {j['stocks']}) | açık {len(j['open'])}",
        f"Hep long: {al['equity']:,.2f}$ ({al['return']:+.2%}) | Rastgele: {rd['equity']:,.2f}$ ({rd['return']:+.2%})",
        f"Son 24 saat: {j['pnl_24h']:+.2f}$ | isabet {hit}",
        "Sanal hesap; gerçek para yok. Karar: kripto 06.10 eleme / 27.10 karar, hisse 4. ve 8. hafta.",
    ])


def to_short(rep: dict[str, Any]) -> str:
    """One-line hourly status."""
    when = datetime.fromtimestamp(rep["now"] / 1000, TR).strftime("%d.%m %H:%M")
    j, al, rd = (rep["arms"][a] for a in ARMS)
    return (f"🕐 {when} | Jev {j['equity']:,.2f}$ ({j['return']:+.2%}) | son 1 saat: {j['closed_1h']} kapandı, "
            f"{j['pnl_1h']:+.2f}$ | açık {len(j['open'])}\nHep long {al['return']:+.2%} | Rastgele {rd['return']:+.2%}")


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
        text = to_text(rep) if daily else to_short(rep)
        out = self.dir / "account"
        out.mkdir(parents=True, exist_ok=True)
        status = await send_telegram(text)
        day = f"{datetime.fromtimestamp(t_tick / 1000, TR):%Y%m%d}"
        if daily:
            (out / f"report-{day}.txt").write_text(text + f"\n[telegram: {status}]\n", encoding="utf-8")
        else:
            with open(out / f"hourly-{day}.txt", "a", encoding="utf-8") as f:
                f.write(text + f"\n[telegram: {status}]\n")

    async def settle(self, now: int) -> None:
        return None
