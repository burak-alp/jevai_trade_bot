"""macro.orders.v1: weekly *manual* order list for macro.trend.v1 (no API keys, nothing is sent to Binance).

The list states the TARGET position per Binance USD-M perp for the user's account size, rounded to Binance's step size
and checked against its minimum order value, plus the change from the previous list. The user enters the orders by
hand. Weights are the paper account's (same code as the backtest). Account size: set once with
``jevbot macro-orders --equity 100``; afterwards it follows the paper 1x account's growth until set again.
Leverage (``--leverage``, max 3) drops to 1x when the estimated equity is >= 20 % below its peak.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from jevbot.macro_paper import NAMES, SYMBOLS

REST = "https://fapi.binance.com"
TR = timezone(timedelta(hours=3))
MAX_LEV, CAP_DD = 3.0, 0.20


def market_rules(transport: httpx.BaseTransport | None = None) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """(prices, {symbol: {step, min_notional}}) from Binance's public endpoints."""
    with httpx.Client(base_url=REST, timeout=30, transport=transport) as c:
        px = {x["symbol"]: float(x["price"]) for x in c.get("/fapi/v1/ticker/price").json()}
        flt = {}
        for s in c.get("/fapi/v1/exchangeInfo").json()["symbols"]:
            f = {x["filterType"]: x for x in s["filters"]}
            lot = f.get("MARKET_LOT_SIZE") or f.get("LOT_SIZE") or {"stepSize": "0"}
            flt[s["symbol"]] = {"step": float(lot["stepSize"]),
                                "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5))}
    return px, flt


def _round_step(q: float, step: float) -> float:
    """Nearest step (small accounts: flooring would drop up to a whole step, e.g. half of a 14$ QQQ target)."""
    return round(q / step) * step if step > 0 else q


def targets(weights: dict[str, float], equity: float, lev: float, px: dict[str, float],
            flt: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    """Target quantity per symbol (long only), rounded to the nearest step; too-small positions flagged."""
    out = []
    for a, w in sorted(weights.items(), key=lambda kv: -kv[1]):
        if w <= 0:
            continue
        sym, factor = SYMBOLS[a]
        if sym not in px or sym not in flt:
            out.append({"asset": a, "symbol": sym, "qty": 0.0, "notional": 0.0, "skip": "fiyat yok"})
            continue
        want = w * lev * factor * equity
        qty = _round_step(want / px[sym], flt[sym]["step"])
        row = {"asset": a, "symbol": sym, "weight": w, "qty": qty, "notional": qty * px[sym], "want": want}
        if row["notional"] < flt[sym]["min_notional"]:
            row["skip"] = f"çok küçük (Binance en az {flt[sym]['min_notional']:g}$ istiyor, hedef {want:.2f}$)"
            row["qty"], row["notional"] = 0.0, 0.0
        out.append(row)
    return out


def _fmt_qty(q: float, step: float) -> str:
    dec = max(0, -int(math.floor(math.log10(step)))) if step > 0 else 6
    return f"{q:.{dec}f}"


def build(state_dir: Path, paper_dir: Path, *, px: dict[str, float] | None = None,
          flt: dict[str, dict[str, float]] | None = None, now: int | None = None,
          set_equity: float | None = None, set_leverage: float | None = None) -> dict[str, Any]:
    state_dir, paper_dir = Path(state_dir), Path(paper_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    cf = state_dir / "config.json"
    paper = json.loads((paper_dir / "state.json").read_text(encoding="utf-8"))
    paper_eq = paper["history"][-1]["1x"]
    cfg = json.loads(cf.read_text(encoding="utf-8")) if cf.exists() else {"equity": None, "leverage": 1.0}
    if set_equity is not None:
        cfg.update({"equity": float(set_equity), "paper_1x_at_set": paper_eq, "peak": float(set_equity)})
    if set_leverage is not None:
        cfg["leverage"] = max(0.0, min(float(set_leverage), MAX_LEV))
    cf.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    if not cfg.get("equity"):
        return {"error": "Hesap büyüklüğü ayarlı değil: jevbot macro-orders --equity <dolar>"}
    equity = cfg["equity"] * paper_eq / cfg["paper_1x_at_set"]
    cfg["peak"] = max(cfg.get("peak", equity), equity)
    cf.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    lev, note = cfg["leverage"], ""
    if lev > 1 and equity <= cfg["peak"] * (1 - CAP_DD):
        lev, note = 1.0, f"Tahmini bakiye zirveden %{(1 - equity / cfg['peak']) * 100:.0f} aşağıda → kaldıraç 1x"
    if px is None or flt is None:
        px, flt = market_rules()
    rows = targets(paper["weights"], equity, lev, px, flt)
    lf = state_dir / "last.json"
    prev = json.loads(lf.read_text(encoding="utf-8")) if lf.exists() else {}
    now = now or int(time.time() * 1000)
    for r in rows:
        r["prev_qty"] = prev.get("qty", {}).get(r["symbol"], 0.0)
    gone = {s: q for s, q in prev.get("qty", {}).items() if q and s not in {r["symbol"] for r in rows if r["qty"]}}
    lf.write_text(json.dumps({"at": now, "week": paper.get("last_rebalance_week"),
                              "qty": {r["symbol"]: r["qty"] for r in rows if r["qty"]}}, indent=1), encoding="utf-8")
    want = sum(r.get("want", 0.0) for r in rows)
    res = {"at": now, "week": paper.get("last_rebalance_week"), "equity": equity, "leverage": lev, "note": note,
           "coverage": (sum(r["notional"] for r in rows) / want) if want else 1.0,
           "rows": rows, "close": gone, "steps": {s: f["step"] for s, f in flt.items() if s in {r["symbol"] for r in rows} | set(gone)},
           "cash_pct": 1 - sum(v for v in paper["weights"].values())}
    with open(state_dir / "ledger.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(res) + "\n")
    return res


def to_text(res: dict[str, Any]) -> str:
    if "error" in res:
        return res["error"]
    when = datetime.fromtimestamp(res["at"] / 1000, TR).strftime("%d.%m %H:%M")
    st = res["steps"]
    lines = [f"📋 Haftalık emir listesi — {when} (hafta {res['week']})",
             f"Hesap ~{res['equity']:,.2f}$ | kaldıraç {res['leverage']:g}x | nakitte kalacak ~%{res['cash_pct'] * 100:.0f}"]
    if res["note"]:
        lines.append(f"⚠️ {res['note']}")
    if abs(res["coverage"] - 1) > 0.1:
        lines.append(f"ℹ️ Binance'in en küçük emir/adım kuralları yüzünden hedef tutarın %{res['coverage'] * 100:.0f}'i "
                     "uygulanabiliyor (küçük hesapta normal; bakiye büyüdükçe düzelir).")
    lines.append("Binance → Vadeli (USDⓈ-M) → sembol → Piyasa (Market). HEDEF pozisyon; elindeki farklıysa farkı al/sat:")
    for r in res["rows"]:
        name = NAMES[r["asset"]]
        if "skip" in r:
            lines.append(f"⏭ {r['symbol']} ({name}): {r['skip']}")
            continue
        d = r["qty"] - r["prev_qty"]
        step = st.get(r["symbol"], 0.001)
        chg = "değişiklik yok" if abs(d) < step / 2 else f"{'+' if d > 0 else '−'}{_fmt_qty(abs(d), step)} ({'AL' if d > 0 else 'SAT'})"
        lines.append(f"• {r['symbol']} ({name}): LONG {_fmt_qty(r['qty'], step)} ≈ {r['notional']:,.2f}$ — geçen haftaya göre {chg}")
    for s, q in res["close"].items():
        lines.append(f"✖ {s}: pozisyonu KAPAT ({_fmt_qty(q, st.get(s, 0.001))} SAT)")
    lines.append(f"Binance ayarı: kaldıraç {max(2, math.ceil(res['leverage']))}x (yalnız teminat; asıl risk yukarıdaki miktarlar), "
                 "tek yönlü mod, çapraz marjin. Sinyal listesidir, yatırım tavsiyesi değil; karar ve emir senin.")
    return "\n".join(lines)
