"""Desk view of the macro accounts: "durum" status report and the committee scorecard (read-only).

Scorecard: every weekly view (committee analysts, the PM, the llm overlay, Jev) is scored on that week's realized
return from one rebalance to the next (Binance prices recorded at rebalance): signed return = view x return.
Informational until the pre-registered review (docs/committee_prompt.md, reports/remote/*committee-prereg.md).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jevbot.macro_paper import ACCOUNTS, OVERLAYS, START_EQUITY, SYMBOLS, week_of

TR = timezone(timedelta(hours=3))
CLASSES = {"ABD hisse": ["SPY", "QQQ"], "Değerli metal": ["GLD", "SLV"], "Enerji": ["USO"], "Tahvil": ["TLT"],
           "Kripto": ["BTC-USD", "ETH-USD"]}


def _ledger(state_dir: Path) -> list[dict[str, Any]]:
    f = Path(state_dir) / "ledger.jsonl"
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] if f.exists() else []


def weekly_returns(state_dir: Path) -> dict[str, dict[str, float]]:
    """{week: {asset: return to the next rebalance}} from consecutive rebalance price snapshots."""
    reb = [r for r in _ledger(state_dir) if r.get("kind") == "rebalance"]
    out: dict[str, dict[str, float]] = {}
    for a, b in zip(reb, reb[1:]):
        wk = week_of(date.fromisoformat(a["data_date"]))
        rets = {}
        for asset, (sym, _) in SYMBOLS.items():
            p0, p1 = a["prices"].get(sym), b["prices"].get(sym)
            if p0 and p1:
                rets[asset] = p1 / p0 - 1
        out[wk] = rets
    return out


def member_views(state_dir: Path, week: str) -> dict[str, dict[str, int]]:
    """{member: {asset: -1/0/+1}} for one week from the committee / overlay / context files."""
    d = Path(state_dir)
    views: dict[str, dict[str, int]] = {}
    cf = d / f"committee-{week}.json"
    if cf.exists():
        c = json.loads(cf.read_text(encoding="utf-8"))
        for name, an in (c.get("analysts") or {}).items():
            views[f"analist:{name}"] = {a: int(v.get("view", 0)) for a, v in (an.get("assets") or {}).items()}
        views["komite (PM)"] = {a: int(v.get("tilt", 0)) for a, v in (c.get("pm", {}).get("assets") or {}).items()}
    of = d / f"overlay-{week}.json"
    if of.exists():
        views["claude (llm)"] = {a: int(v.get("tilt", 0))
                                 for a, v in (json.loads(of.read_text(encoding="utf-8")).get("assets") or {}).items()}
    gf = d / f"gpt-{week}.json"
    if gf.exists():
        views["gpt"] = {a: int(v.get("view", 0))
                        for a, v in (json.loads(gf.read_text(encoding="utf-8")).get("assets") or {}).items()}
    xf = d / f"context-{week}.json"
    if xf.exists():
        jev = json.loads(xf.read_text(encoding="utf-8")).get("jev") or {}
        js = {a: (1 if v["up"] - v["down"] >= 0.2 else -1 if v["up"] - v["down"] <= -0.2 else 0)
              for a, v in jev.items() if isinstance(v, dict) and "up" in v}
        if js:
            views["jev"] = js
    return views


def scorecard(state_dir: Path) -> dict[str, dict[str, float]]:
    """Per member over all finished weeks: n non-zero views, hit rate, mean signed return (bps)."""
    acc: dict[str, list[float]] = {}
    for wk, rets in weekly_returns(state_dir).items():
        for member, vs in member_views(state_dir, wk).items():
            for a, v in vs.items():
                if v and a in rets:
                    acc.setdefault(member, []).append(v * rets[a])
    return {m: {"n": len(x), "hit": sum(y > 0 for y in x) / len(x), "mean_bps": sum(x) / len(x) * 1e4}
            for m, x in acc.items() if x}


def status(state_dir: Path, px: dict[str, float]) -> dict[str, Any]:
    st = json.loads((Path(state_dir) / "state.json").read_text(encoding="utf-8"))
    eq = {}
    for n, acc in st["accounts"].items():
        eq[n] = acc["balance"] + sum(p["qty"] * (px[s] - p["entry"]) for s, p in acc["pos"].items() if s in px)
    acc = st["accounts"]["1x"]
    attr = dict(acc.get("pnl_sym", {}))
    for s, p in acc["pos"].items():
        if s in px:
            attr[s] = attr.get(s, 0.0) + p["qty"] * (px[s] - p["entry"])
    classes = {}
    for cls, assets in CLASSES.items():
        syms = [SYMBOLS[a][0] for a in assets]
        classes[cls] = {"pnl": sum(attr.get(s, 0.0) for s in syms),
                        "weight": sum(st["weights"].get(a, 0.0) for a in assets)}
    explained = sum(attr.values())
    return {"st": st, "equity": eq, "classes": classes, "cash_pnl": attr.get("CASH", 0.0),
            "unattributed": eq["1x"] - START_EQUITY - explained, "score": scorecard(state_dir)}


def latest_note(state_dir: Path) -> tuple[str | None, str | None]:
    files = sorted(Path(state_dir).glob("committee-*.json"))
    if not files:
        return None, None
    c = json.loads(files[-1].read_text(encoding="utf-8"))
    return c.get("week"), (c.get("pm") or {}).get("note")


def status_text(state_dir: Path, px: dict[str, float]) -> str:
    s = status(state_dir, px)
    st = s["st"]
    when = datetime.now(TR).strftime("%d.%m %H:%M")
    lines = [f"📊 Durum — {when}"]
    lines.append(" | ".join(f"{n} {v:,.2f}$ ({v / START_EQUITY - 1:+.2%})"
                            for n, v in s["equity"].items() if n in (*ACCOUNTS, *OVERLAYS)))
    lines.append("Varlık sınıfı (1x hesap, başlangıçtan beri):")
    for cls, v in s["classes"].items():
        tag = f"şu an %{v['weight'] * 100:.0f}" if v["weight"] > 0.0005 else "şu an pozisyon yok"
        lines.append(f"  {'🟢' if v['pnl'] >= 0 else '🔴'} {cls}: {v['pnl']:+.2f}$ "
                     f"({v['pnl'] / START_EQUITY:+.2%} hesaba katkı) — {tag}")
    lines.append(f"  💵 Nakit faizi: {s['cash_pnl']:+.2f}$ | nakit oranı %{(1 - sum(st['weights'].values())) * 100:.0f}")
    if abs(s["unattributed"]) >= 0.01:
        lines.append(f"  (takip başlamadan önceki: {s['unattributed']:+.2f}$)")
    wk, note = latest_note(state_dir)
    if note:
        lines.append(f"🏛 Komite notu ({wk}):\n{note[:1500]}")
    else:
        lines.append("🏛 Komite notu henüz yok (cumartesi yazılır).")
    if s["score"]:
        lines.append("🎯 Karne (biten haftalar; isabet / ort. getiri):")
        for m, v in sorted(s["score"].items(), key=lambda kv: -kv[1]["mean_bps"]):
            lines.append(f"  {m}: {v['n']} görüş, isabet %{v['hit'] * 100:.0f}, {v['mean_bps']:+.0f} bps")
    lines.append("Sanal hesap; gerçek para yok.")
    return "\n".join(lines)
