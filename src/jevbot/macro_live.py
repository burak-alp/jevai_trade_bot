"""macro.live.v1: runs macro.trend.v1 on the user's own Binance USD-M futures account. DRY-RUN unless all locks open.

Locks for real orders (all three): ``live=True`` (``--live`` / config mode "live"), env ``JEVBOT_LIVE=YES``, and keys
BINANCE_API_KEY / BINANCE_API_SECRET in env or the Windows user registry. Keys are never printed or logged. The key
should be trade-only, NO withdrawals, IP-restricted.

Behaviour
* Weights = the paper account's latest weekly weights (same code as the backtest). The live book rebalances once per
  paper rebalance week; failed orders (e.g. TradFi market closed) are retried on the next run until the week is done.
* Leverage = configured (max 3). Equity >= 20 % below its recorded peak -> 1x. >= 30 % -> HALT: close everything,
  alert, no new positions until ``--reset-halt``.
* One asset <= 40 % of equity x leverage. One-way position mode required. Below Binance's minimum order value -> skipped
  and reported. Market orders; reductions are reduceOnly.
* Every run without a rebalance reconciles actual positions with the last target and alerts on drift (> 10 %) or on
  positions the bot did not open.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from jevbot.macro_paper import NAMES, SYMBOLS

REST = "https://fapi.binance.com"
TR = timezone(timedelta(hours=3))
MAX_LEV, CAP_DD, HALT_DD, MAX_W, DRIFT_TOL = 3.0, 0.20, 0.30, 0.40, 0.10


class LiveError(RuntimeError):
    pass


def _secret(var: str) -> str | None:
    from jevbot.account import _secret as read
    return read(var)


class Futures:
    """Minimal Binance USD-M client: public prices/filters and the signed calls the bot needs."""

    def __init__(self, key: str | None, secret: str | None, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], int] | None = None) -> None:
        self.key, self.secret = key, secret
        self.c = httpx.Client(base_url=REST, timeout=30, transport=transport)
        self.clock = clock or (lambda: int(time.time() * 1000))

    @property
    def has_keys(self) -> bool:
        return bool(self.key and self.secret)

    def _get(self, path: str) -> Any:
        r = self.c.get(path)
        if r.status_code != 200:
            raise LiveError(f"{path}: http {r.status_code}")
        return r.json()

    def _signed(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.has_keys:
            raise LiveError("API anahtarı yok")
        qs = urlencode({**(params or {}), "timestamp": self.clock(), "recvWindow": 5000})
        sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()   # type: ignore[union-attr]
        r = self.c.request(method, f"{path}?{qs}&signature={sig}", headers={"X-MBX-APIKEY": self.key or ""})
        if r.status_code != 200:
            try:
                msg = str(r.json().get("msg", ""))[:150]
            except ValueError:
                msg = ""
            raise LiveError(f"{path}: http {r.status_code} {msg}")
        return r.json()

    def prices(self) -> dict[str, float]:
        return {x["symbol"]: float(x["price"]) for x in self._get("/fapi/v1/ticker/price")}

    def filters(self) -> dict[str, dict[str, float]]:
        out = {}
        for s in self._get("/fapi/v1/exchangeInfo")["symbols"]:
            f = {x["filterType"]: x for x in s["filters"]}
            lot = f.get("MARKET_LOT_SIZE") or f.get("LOT_SIZE") or {"stepSize": "0"}
            out[s["symbol"]] = {"step": float(lot["stepSize"]),
                                "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5))}
        return out

    def account(self) -> tuple[float, dict[str, float]]:
        a = self._signed("GET", "/fapi/v2/account")
        pos = {p["symbol"]: float(p["positionAmt"]) for p in a["positions"] if float(p["positionAmt"]) != 0}
        return float(a["totalMarginBalance"]), pos

    def one_way(self) -> bool:
        return not self._signed("GET", "/fapi/v1/positionSide/dual")["dualSidePosition"]

    def set_leverage(self, symbol: str, lev: int) -> None:
        self._signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev})

    def order(self, symbol: str, qty: float, reduce_only: bool) -> dict[str, Any]:
        p: dict[str, Any] = {"symbol": symbol, "side": "BUY" if qty > 0 else "SELL", "type": "MARKET",
                             "quantity": _fmt(abs(qty))}
        if reduce_only:
            p["reduceOnly"] = "true"
        return self._signed("POST", "/fapi/v1/order", p)


def _fmt(x: float) -> str:
    return f"{x:.8f}".rstrip("0").rstrip(".")


def _round_step(q: float, step: float) -> float:
    return round(q / step) * step if step > 0 else q


def effective_leverage(base: float, equity: float, peak: float) -> tuple[float, str]:
    """(leverage to use, note). 0.0 means HALT."""
    dd = 1 - equity / peak if peak > 0 else 0.0
    if dd >= HALT_DD:
        return 0.0, f"HALT: zirveden düşüş %{dd * 100:.1f} (sınır %{HALT_DD * 100:.0f})"
    lev = max(0.0, min(base, MAX_LEV))
    if dd >= CAP_DD and lev > 1:
        return 1.0, f"zirveden düşüş %{dd * 100:.1f} ≥ %{CAP_DD * 100:.0f} → kaldıraç 1x"
    return lev, ""


def plan(weights: dict[str, float], lev: float, equity: float, current: dict[str, float], px: dict[str, float],
         flt: dict[str, dict[str, float]]) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """(orders that move ``current`` (symbol -> signed qty) to the long-only target, target qty map). Pure."""
    tgt: dict[str, float] = {}
    for a, w in weights.items():
        sym, factor = SYMBOLS[a]
        w = min(max(w, 0.0), MAX_W)
        if w > 0 and sym in px and sym in flt:
            q = _round_step(w * lev * factor * equity / px[sym], flt[sym]["step"])
            if q * px[sym] >= flt[sym]["min_notional"]:
                tgt[sym] = q
    out = []
    for sym in sorted(set(tgt) | set(current)):
        cur, t = current.get(sym, 0.0), tgt.get(sym, 0.0)
        if sym not in px or sym not in flt:
            out.append({"symbol": sym, "current": cur, "target": t, "delta": 0.0, "notional": 0.0,
                        "reduce_only": False, "skip": "fiyat/kural yok"})
            continue
        delta = -cur if t == 0 else _round_step(t - cur, flt[sym]["step"])
        if abs(delta) < flt[sym]["step"] / 2:
            continue
        reduce = cur != 0 and abs(cur + delta) < abs(cur) and cur * (cur + delta) >= 0
        row = {"symbol": sym, "current": cur, "target": t, "delta": delta, "notional": abs(delta) * px[sym],
               "reduce_only": reduce}
        if row["notional"] < flt[sym]["min_notional"] and t != 0:
            row["skip"] = f"değişiklik çok küçük (en az {flt[sym]['min_notional']:g}$)"
        out.append(row)
    return out, tgt


def run(state_dir: Path, paper_dir: Path, *, live: bool = False, leverage: float | None = None,
        dry_equity: float = 100.0, fut: Futures | None = None, now: int | None = None,
        reset_halt: bool = False) -> dict[str, Any]:
    state_dir, paper_dir = Path(state_dir), Path(paper_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    sf = state_dir / "state.json"
    st = json.loads(sf.read_text(encoding="utf-8")) if sf.exists() else {
        "leverage": 1.0, "peak": 0.0, "halted": False, "done_week": None, "target": {}}
    if leverage is not None:
        st["leverage"] = max(0.0, min(float(leverage), MAX_LEV))
    if reset_halt:
        st["halted"], st["peak"], st["done_week"] = False, 0.0, None
    if live and os.environ.get("JEVBOT_LIVE") != "YES":
        raise LiveError("canlı mod istendi ama JEVBOT_LIVE=YES ayarlı değil")
    now = now or int(time.time() * 1000)
    paper = json.loads((paper_dir / "state.json").read_text(encoding="utf-8"))
    week, weights = paper.get("last_rebalance_week"), paper.get("weights", {})
    fut = fut or Futures(_secret("BINANCE_API_KEY"), _secret("BINANCE_API_SECRET"))
    if live and not fut.has_keys:
        raise LiveError("canlı mod istendi ama API anahtarı yok")
    px, flt = fut.prices(), fut.filters()
    if fut.has_keys:
        if not fut.one_way():
            raise LiveError("Binance'te 'hedge' pozisyon modu açık; tek yönlü moda geç")
        equity, current = fut.account()
    else:
        equity, current = float(dry_equity), {}
    st["peak"] = max(st.get("peak", 0.0), equity)
    lev, note = effective_leverage(st["leverage"], equity, st["peak"])
    alerts = [note] if note else []
    if lev == 0.0:
        st["halted"] = True
    rebalance = bool(st["halted"]) or (week is not None and week != st.get("done_week"))
    orders, target = plan({} if st["halted"] else weights, lev, equity, current, px, flt) if rebalance else ([], {})
    if st["halted"]:
        alerts.append("Sistem DURDU: pozisyonlar kapatılıyor, yeni pozisyon yok. Devam için: jevbot macro-live --reset-halt")
    if not rebalance and fut.has_keys:                      # reconciliation only (needs the real account)
        for sym, q in st.get("target", {}).items():
            a = current.get(sym, 0.0)
            if q and abs(a - q) / abs(q) > DRIFT_TOL:
                alerts.append(f"{sym}: hesapta {a:g}, beklenen {q:g}")
        for sym in sorted(set(current) - set(st.get("target", {}))):
            alerts.append(f"{sym}: botun açmadığı pozisyon ({current[sym]:g})")
    executed: list[dict[str, Any]] = []
    errors = False
    for o in orders:
        if "skip" in o:
            continue
        if not live:
            executed.append({**o, "dry_run": True})
            continue
        try:
            if not o["reduce_only"]:
                fut.set_leverage(o["symbol"], int(math.ceil(MAX_LEV)))
            r = fut.order(o["symbol"], o["delta"], o["reduce_only"])
            executed.append({**o, "order_id": r.get("orderId"), "status": r.get("status")})
        except LiveError as e:
            errors = True
            executed.append({**o, "error": str(e)})
            alerts.append(f"{o['symbol']} emri olmadı: {e} (sonraki çalışmada tekrar denenecek)")
    if rebalance and not errors and (live or not fut.has_keys):
        st["done_week"] = None if st["halted"] else week
        st["target"] = target
    res = {"at": now, "live": live, "has_keys": fut.has_keys, "equity": equity, "peak": st["peak"], "leverage": lev,
           "week": week, "weights": weights, "rebalance": rebalance, "orders": orders, "executed": executed,
           "alerts": alerts, "halted": st["halted"]}
    with open(state_dir / "ledger.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(res) + "\n")
    sf.write_text(json.dumps(st, indent=1), encoding="utf-8")
    return res


def to_text(res: dict[str, Any]) -> str:
    when = datetime.fromtimestamp(res["at"] / 1000, TR).strftime("%d.%m %H:%M")
    mode = "CANLI" if res["live"] else "KURU ÇALIŞMA — emir gönderilmedi"
    lines = [f"🤖 Makro bot ({mode}) — {when}",
             f"Bakiye {res['equity']:,.2f}$ | zirve {res['peak']:,.2f}$ | kaldıraç {res['leverage']:g}x"
             + ("" if res["has_keys"] else " | anahtar yok: örnek bakiyeyle plan")]
    w = res["weights"]
    held = [f"{NAMES[a]} %{v * 100:.0f}" for a, v in sorted(w.items(), key=lambda kv: -kv[1]) if v > 0.0005]
    lines.append("Hedef dağılım: " + (", ".join(held) or "—") + f", nakit %{(1 - sum(w.values())) * 100:.0f}")
    if res["rebalance"]:
        done = {o["symbol"]: o for o in res["executed"]}
        for o in res["orders"]:
            side = "AL" if o["delta"] > 0 else "SAT"
            if "skip" in o:
                lines.append(f"⏭ {o['symbol']}: {o['skip']}")
            elif "error" in done.get(o["symbol"], {}):
                lines.append(f"❌ {side} {o['symbol']} {abs(o['delta']):g}")
            else:
                tag = " ✅" if res["live"] else " (plan)"
                lines.append(f"{side} {o['symbol']} {abs(o['delta']):g} ≈ {o['notional']:,.2f}${tag}")
        if not res["orders"]:
            lines.append("Bu hafta değişiklik gerekmiyor.")
    else:
        lines.append("Bu haftanın dengelemesi tamam; kontrol yapıldı.")
    lines += [f"⚠️ {a}" for a in res["alerts"]]
    return "\n".join(lines)
