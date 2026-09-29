"""``llm.v1``: LLM traders (Jev vs Claude) on Binance US-stock perps, prospective paper only.

Pre-registered: reports/remote/20260929T-llm-trader-prereg.md. Every weekday at 14:00 UTC the paper process
writes one context file (identical for both arms), asks Jev, and records its probabilities; a scheduled Claude
Code task reads the same file and records its own probabilities with ``jevbot llm-record``. Code turns
probabilities into positions (score = P(up) - P(down), |score| >= 0.2), enters at 14:10 UTC or one minute after
the record (later of the two) and exits 24 h later on 1 m closes. Reference arms: always-long and random.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

import httpx
import numpy as np
import orjson

from jevbot.core.logging import get_logger
from jevbot.paper.engine import _append, read_jsonl

log = get_logger(__name__)
VERSION = "llm.v1"
UNIVERSE = ("NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "COIN", "MSTR", "PLTR", "SPY", "QQQ")
MIN, HOUR, DAY = 60_000, 3_600_000, 86_400_000
DECISION_HOUR_UTC = 14
ENTRY_OFFSET = 10 * MIN
HOLD = DAY
SCORE_MIN = 0.20
COST = 0.0020
BAND_PCT = 1.0
QUESTION = (f"Over the next 24 hours, will this stock's price change be above +{BAND_PCT:g}% (up), below "
            f"-{BAND_PCT:g}% (down), or in between (flat)? Use the price summary and the headlines.")


def is_decision_tick(t: int) -> bool:
    d = datetime.fromtimestamp(t / 1000, timezone.utc)
    return d.hour == DECISION_HOUR_UTC and d.minute == 0 and d.weekday() < 5


def _r(x: float, nd: int = 2) -> float | None:
    return round(float(x), nd) if x is not None and math.isfinite(float(x)) else None


def summarize(h1: dict[str, np.ndarray], funding_bps: float | None) -> dict[str, Any]:
    """Price summary from hourly bars ending at the decision tick (only completed bars)."""
    c = h1["close"]
    ok = np.flatnonzero(np.isfinite(c))
    if not len(ok):
        return {}
    last = float(c[ok[-1]])

    def ret(hours: int) -> float | None:
        i = ok[-1] - hours
        return _r((last / c[i] - 1) * 100) if i >= 0 and math.isfinite(c[i]) else None
    hi, lo = h1["high"], h1["low"]
    tr = np.nanmax(np.vstack([hi[1:] - lo[1:], np.abs(hi[1:] - c[:-1]), np.abs(lo[1:] - c[:-1])]), axis=0)
    atr = float(np.nanmean(tr[-14:])) if len(tr) >= 14 else float("nan")
    trend = bool(_ema(c[ok], 50) > _ema(c[ok], 200)) if len(ok) >= 200 else None
    return {"ret_1d_pct": ret(24), "ret_5d_pct": ret(120), "ret_20d_pct": ret(480),
            "atr_1h_pct": _r(atr / last * 100, 3), "ema50_above_ema200": trend,
            "funding_bps_8h": _r(funding_bps) if funding_bps is not None else None}


def _ema(x: np.ndarray, n: int) -> float:
    a, e = 2 / (n + 1), float(x[0])
    for v in x[1:]:
        e = a * float(v) + (1 - a) * e
    return e


async def yahoo_headlines(client: httpx.AsyncClient, ticker: str, t: int, hours: int = 36, n: int = 8) -> list[str]:
    try:
        r = await client.get("https://feeds.finance.yahoo.com/rss/2.0/headline",
                             params={"s": ticker, "region": "US", "lang": "en-US"},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
    except httpx.HTTPError:
        return []
    out = []
    for item in re.findall(r"<item>(.*?)</item>", r.text, flags=re.S):
        title = re.search(r"<title>(.*?)</title>", item, flags=re.S)
        pub = re.search(r"<pubDate>(.*?)</pubDate>", item, flags=re.S)
        if not title or not pub:
            continue
        try:
            ts = int(parsedate_to_datetime(pub.group(1).strip()).timestamp() * 1000)
        except (TypeError, ValueError):
            continue
        if t - hours * HOUR <= ts <= t:                                  # nothing published after the tick
            out.append(re.sub(r"<!\[CDATA\[|\]\]>", "", title.group(1)).strip())
    return out[:n]


def jev_question() -> dict[str, Any]:
    b = f"{BAND_PCT:g}"
    return {"type": "choice", "instructions": QUESTION,
            "criteria": {"up": f"price change above +{b}%", "flat": f"between -{b}% and +{b}%", "down": f"below -{b}%"}}


class LlmTrader:
    def __init__(self, state_dir: Path, src: Any, jev: Any = None, jev_model: str | None = None,
                 headlines: Callable[[str, int], Any] | None = None, clock: Callable[[], int] | None = None) -> None:
        self.dir = Path(state_dir) / "llm"
        self.src, self.jev, self.jev_model = src, jev, jev_model
        self.clock = clock or (lambda: int(time.time() * 1000))
        self._headlines = headlines
        self.ledger = self.dir / "ledger.jsonl"

    async def _news(self, ticker: str, t: int) -> list[str]:
        if self._headlines is not None:
            return await self._headlines(ticker, t)
        async with httpx.AsyncClient() as c:
            return await yahoo_headlines(c, ticker, t)

    async def build_context(self, t: int) -> dict[str, Any]:
        syms: dict[str, Any] = {}
        for tk in UNIVERSE:
            s = f"{tk}USDT"
            try:
                h1 = await self.src.hourly(s, t - 40 * DAY, t)
                ft, fr = await self.src.funding(s, t - 2 * DAY, t)
            except Exception:                                            # noqa: BLE001 - symbol skipped, logged
                log.warning("llm_context_symbol_failed", symbol=s)
                continue
            f = float(fr[-1]) * 1e4 if len(fr) else None
            summ = summarize(h1, f)
            if summ:
                syms[tk] = {**summ, "headlines": await self._news(tk, t)}
        ctx = {"version": VERSION, "t_decision": t, "iso": datetime.fromtimestamp(t / 1000, timezone.utc).isoformat(),
               "question": QUESTION, "symbols": syms}
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"context-{datetime.fromtimestamp(t / 1000, timezone.utc):%Y%m%d}.json"
        if not path.exists():
            path.write_bytes(orjson.dumps(ctx, option=orjson.OPT_INDENT_2))
        return orjson.loads(path.read_bytes())

    def record(self, arm: str, t_decision: int, probs: dict[str, dict[str, float]], reasons: dict[str, str] | None = None,
               model: str | None = None) -> list[dict[str, Any]]:
        """Append validated per-symbol probabilities of one arm (idempotent per arm x day x symbol)."""
        done = {(r["arm"], r["t_decision"], r["ticker"]) for r in read_jsonl(self.ledger) if r.get("kind") == "decision"}
        now = self.clock()
        rows = []
        for tk, p in probs.items():
            if tk not in UNIVERSE or (arm, t_decision, tk) in done:
                continue
            vals = [float(p.get(k, float("nan"))) for k in ("up", "flat", "down")]
            if not all(0 <= v <= 1 for v in vals) or abs(sum(vals) - 1) > 0.02:
                continue
            up, flat, down = (v / sum(vals) for v in vals)
            score = up - down
            side = 1 if score >= SCORE_MIN else -1 if score <= -SCORE_MIN else 0
            rows.append({"kind": "decision", "version": VERSION, "arm": arm, "model": model, "t_decision": t_decision,
                         "recorded_at": now, "t_entry": max(t_decision + ENTRY_OFFSET, now - now % MIN + MIN),
                         "ticker": tk, "up": up, "flat": flat, "down": down, "score": score, "side": side,
                         "reason": (reasons or {}).get(tk, "")[:300]})
        if rows:
            _append(self.ledger, rows)
        return rows

    async def on_tick(self, t_tick: int) -> None:
        if not is_decision_tick(t_tick):
            return
        ctx = await self.build_context(t_tick)
        # reference arms
        self.record("always_long", t_tick, {tk: {"up": 1.0, "flat": 0.0, "down": 0.0} for tk in ctx["symbols"]})
        rnd = {}
        for tk in ctx["symbols"]:
            h = int(hashlib.sha256(f"{tk}|{t_tick}".encode()).hexdigest()[:8], 16) % 3
            rnd[tk] = {"up": [1.0, 0.0, 0.5][h], "flat": [0.0, 0.0, 0.5][h], "down": [0.0, 1.0, 0.0][h]}
        self.record("random", t_tick, rnd)
        if self.jev is None or not self.jev_model:
            return
        tks = list(ctx["symbols"])
        res = await asyncio.gather(*(self.jev.ask(self.jev_model, {"ticker": tk, **ctx["symbols"][tk]},
                                                  {"direction_24h": jev_question()}) for tk in tks))
        probs = {tk: {k: r["probs"][f"direction_24h.{k}"] for k in ("up", "flat", "down")}
                 for tk, r in zip(tks, res) if r.get("status") == "ok"}
        self.record("jev", t_tick, probs, model=self.jev_model)
        log.info("llm_jev_recorded", t_tick=t_tick, n=len(probs), failed=len(tks) - len(probs))

    async def settle(self, now: int) -> list[dict[str, Any]]:
        led = read_jsonl(self.ledger)
        done = {r["id"] for r in led if r.get("kind") == "outcome"}
        cache: dict[tuple[str, int], tuple[float, float]] = {}
        out = []
        for d in led:
            if d.get("kind") != "decision":
                continue
            oid = f"{d['arm']}|{d['t_decision']}|{d['ticker']}"
            if oid in done or d["t_entry"] + HOLD + 10 * MIN > now:
                continue
            key = (d["ticker"], d["t_entry"])
            if key not in cache:
                b = await self.src.minute_bars(f"{d['ticker']}USDT", d["t_entry"] - MIN, d["t_entry"] + HOLD)
                c = b.close
                p0, p1 = (float(c[0]) if len(c) else float("nan")), (float(c[-1]) if len(c) else float("nan"))
                ft, fr = await self.src.funding(f"{d['ticker']}USDT", d["t_entry"], d["t_entry"] + HOLD)
                cache[key] = (math.log(p1 / p0) if p0 > 0 and p1 > 0 else float("nan"), float(np.sum(fr)) if len(fr) else 0.0)
            r, fsum = cache[key]
            net = d["side"] * r - (COST + d["side"] * fsum if d["side"] else 0.0) if math.isfinite(r) else None
            out.append({"kind": "outcome", "id": oid, "ret": r, "net": net, "settled_at": now})
        if out:
            _append(self.ledger, out)
        return out


def llm_report(state_dir: Path, bootstrap: int = 2000) -> dict[str, Any]:
    from jevbot.judgment.shadow import auc
    from jevbot.research.a0 import block_bootstrap_ci
    led = read_jsonl(Path(state_dir) / "llm" / "ledger.jsonl")
    outs = {r["id"]: r for r in led if r.get("kind") == "outcome" and r.get("net") is not None}
    rep: dict[str, Any] = {"version": VERSION}
    for arm in sorted({r["arm"] for r in led if r.get("kind") == "decision"}):
        ds = [(d, outs[f"{arm}|{d['t_decision']}|{d['ticker']}"]) for d in led if d.get("kind") == "decision"
              and d["arm"] == arm and f"{arm}|{d['t_decision']}|{d['ticker']}" in outs]
        g: dict[str, Any] = {"decisions": sum(1 for d in led if d.get("kind") == "decision" and d["arm"] == arm),
                             "settled": len(ds)}
        tr = [(d, o) for d, o in ds if d["side"]]
        if tr:
            days: dict[int, list[float]] = {}
            for d, o in tr:
                days.setdefault(d["t_decision"], []).append(o["net"])
            t = np.array(sorted(days), dtype=np.int64)
            daily = np.array([np.mean(days[k]) for k in t])
            lo, hi = block_bootstrap_ci(daily, t // DAY // 7, bootstrap, 7)
            g.update({"trades": len(tr), "net_per_trade_bps": round(float(np.mean([o["net"] for _, o in tr])) * 1e4, 1),
                      "daily_mean_bps": round(float(daily.mean()) * 1e4, 1), "daily_ci95_bps": [round(lo * 1e4, 1), round(hi * 1e4, 1)],
                      "trading_days": len(t), "win_rate": round(float(np.mean([o["net"] > 0 for _, o in tr])), 3)})
        if ds:
            sc = np.array([d["score"] for d, _ in ds])
            up = np.array([1 if o["ret"] > 0 else 0 for _, o in ds])
            g["auc_score_vs_up"] = round(auc(sc, up), 4)
        rep[arm] = g
    return rep
