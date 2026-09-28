"""Jev shadow mode on the paper engine: ask, record, settle, measure. Never decides anything.

Ledger ``<state_dir>/jev.jsonl`` (append-only; states and questions are written *before* the call,
so nothing about the outcome can leak into what was asked):

* ``run``         model chosen via GET /v1/models (``pinning``), question set and prompt hash;
* ``ask``         one request: question, state (+ hash), proposal id or direction id, tick, entry time;
* ``judgment``    the answer (status, probabilities, model_returned, latency, ``late`` = arrived after
                  the paper entry minute -> would not have been usable by arm B);
* ``dir_open``    a direction target (symbol, tick close, 1h ATR, horizon, flat band) — arm C;
* ``dir_outcome`` realised move in 1h ATRs and class up / flat / down.

Arm B asks ``trade_success`` (canonical, side-aligned state) for every paper proposal. Arm C asks
``direction_h`` (raw state, no side) for every proposal symbol and, every 4 h, for the top-N symbols
by 24 h quote volume (the "panel": more samples for the direction question).
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from pathlib import Path
from typing import Any

import numpy as np

from jevbot.core.logging import get_logger
from jevbot.judgment.client import JevClient, pick_model
from jevbot.judgment.questions import (PROMPT_HASH, QS_VERSION, REGIME_HASH, btc_regime, direction_band_atr,
                                       direction_h, trade_success)
from jevbot.judgment.state import REGIME_SCHEMA, STATE_SCHEMA, canonical_state, raw_state, regime_state, sha
from jevbot.paper.engine import _append, read_jsonl

log = get_logger(__name__)
HOUR = 3_600_000
DAY = 86_400_000
RAW_KEYS = ("close", "atr_pct", "ret_24h", "ret_7d", "rel_ret_7d", "run_4h_atr", "ema_trend", "funding_bps_8h",
            "oi_chg_24h", "qv_24h")


def _at(sf: Any, k: str) -> float:
    v = sf.f.get(k)
    return float(v[-1]) if v is not None and len(v) else float("nan")


class JevShadow:
    def __init__(self, client: JevClient, state_dir: Path, panel_top: int = 30,
                 panel_hours: tuple[int, ...] = (0, 4, 8, 12, 16, 20), dir_horizon_min: int = 1440,
                 settle_delay_min: int = 10, regime_days: int = 7, regime_pct: float = 3.0) -> None:
        self.client, self.path = client, Path(state_dir) / "jev.jsonl"
        self.panel_top, self.panel_hours = panel_top, panel_hours
        self.dir_horizon_min, self.settle_delay_min = dir_horizon_min, settle_delay_min
        self.regime_days, self.regime_pct = regime_days, regime_pct
        self.model: str | None = None

    async def start(self, at: int) -> str:
        names: list[str] = []
        try:
            models = await self.client.models()
            names = [m.get("name") for m in models]
            self.model, pinning = pick_model(models)
        except Exception as e:                                      # noqa: BLE001 - recorded, then alias
            log.warning("jev_models_failed", error=f"{type(e).__name__}: {e}")
            self.model, pinning = None, "unverified"
        self.model = self.model or "jev-latest"
        _append(self.path, [{"kind": "run", "at": at, "model_requested": self.model, "pinning": pinning,
                             "models": names, "questionset": QS_VERSION, "prompt_hash": PROMPT_HASH,
                             "state_schema": STATE_SCHEMA, "regime_schema": REGIME_SCHEMA,
                             "regime_hash": REGIME_HASH}])
        return self.model

    def _panel(self, t_tick: int, feats: dict[str, Any]) -> list[str]:
        if (t_tick // HOUR) % 24 not in self.panel_hours:
            return []
        ok = [(s, _at(sf, "qv_24h")) for s, sf in feats.items()
              if math.isfinite(_at(sf, "qv_24h")) and math.isfinite(_at(sf, "atr_pct")) and _at(sf, "close") > 0]
        ok.sort(key=lambda x: -x[1])
        return [s for s, _ in ok[:self.panel_top]]

    async def on_tick(self, t_tick: int, t_entry: int, decisions: list[dict[str, Any]],
                      feats: dict[str, Any] | None) -> list[dict[str, Any]]:
        if self.model is None:
            await self.start(t_tick)
        asks: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []   # (ask row, state, questions)
        for d in decisions:
            st = canonical_state(d)
            q = {"trade_success": trade_success(st)}
            asks.append(({"kind": "ask", "question": "trade_success", "id": d["id"], "t_tick": t_tick,
                          "t_entry": t_entry, "family": d["family"], "state_hash": sha(st)[:16]}, st, q))
        opens = []
        if feats:
            prop_syms = {d["symbol"] for d in decisions}
            panel = self._panel(t_tick, feats)
            ranks = {s: i + 1 for i, (s, _) in enumerate(sorted(
                ((s, _at(sf, "qv_24h")) for s, sf in feats.items() if math.isfinite(_at(sf, "qv_24h"))),
                key=lambda x: -x[1]))}
            btc = {k: _at(feats["BTCUSDT"], k) for k in RAW_KEYS} if "BTCUSDT" in feats else None
            H = self.dir_horizon_min
            for s in sorted(prop_syms | set(panel)):
                if s not in feats:
                    continue
                f = {k: _at(feats[s], k) for k in RAW_KEYS}
                if not (f["close"] > 0 and math.isfinite(f["atr_pct"])):
                    continue
                f["vol_rank"] = ranks.get(s)
                src = "both" if s in prop_syms and s in panel else "proposal" if s in prop_syms else "panel"
                did = hashlib.sha256(f"dir|{s}|{t_tick}|{H}".encode()).hexdigest()[:16]
                opens.append({"kind": "dir_open", "id": did, "symbol": s, "t_tick": t_tick, "close": f["close"],
                              "atr_pct": f["atr_pct"], "horizon_min": H, "band_atr": direction_band_atr(H),
                              "source": src})
                st = raw_state(f, btc)
                asks.append(({"kind": "ask", "question": "direction_h", "id": did, "t_tick": t_tick,
                              "t_entry": t_entry, "state_hash": sha(st)[:16]}, st, {"direction_h": direction_h(H)}))
            if btc and t_tick % DAY == 0 and btc["close"] > 0:          # weekly BTC regime, once a day
                rid = hashlib.sha256(f"regime|BTCUSDT|{t_tick}|{self.regime_days}".encode()).hexdigest()[:16]
                opens.append({"kind": "dir_open", "id": rid, "symbol": "BTCUSDT", "t_tick": t_tick,
                              "close": btc["close"], "atr_pct": btc["atr_pct"], "horizon_min": self.regime_days * 1440,
                              "band_pct": self.regime_pct / 100, "question": "btc_regime_7d", "source": "regime"})
                st = regime_state(btc, [{k: _at(sf, k) for k in ("ret_24h", "ret_7d")} for sf in feats.values()])
                asks.append(({"kind": "ask", "question": "btc_regime_7d", "id": rid, "t_tick": t_tick,
                              "t_entry": t_entry, "state_hash": sha(st)[:16]}, st,
                             {"btc_regime_7d": btc_regime(self.regime_days, self.regime_pct)}))
        if not asks:
            return []
        _append(self.path, opens + [{**a, "model_requested": self.model, "state": st} for a, st, _ in asks])
        res = await asyncio.gather(*(self.client.ask(self.model, st, q) for _, st, q in asks))
        rows = []
        for (a, _, _), r in zip(asks, res):
            row = {"kind": "judgment", "question": a["question"], "id": a["id"], "t_tick": t_tick, **r}
            if a["question"] == "trade_success" and r.get("t_received") is not None:
                row["late"] = r["t_received"] > t_entry
            rows.append(row)
        _append(self.path, rows)
        ok = sum(r.get("status") == "ok" for r in rows)
        log.info("jev_shadow_tick", t_tick=t_tick, asks=len(rows), ok=ok, directions=len(opens))
        return rows

    async def settle(self, src: Any, now: int) -> list[dict[str, Any]]:
        ledger = read_jsonl(self.path)
        done = {r["id"] for r in ledger if r.get("kind") == "dir_outcome"}
        out = []
        for o in ledger:
            if o.get("kind") != "dir_open" or o["id"] in done:
                continue
            t_end = o["t_tick"] + o["horizon_min"] * 60_000
            if t_end + self.settle_delay_min * 60_000 > now:
                continue
            h1 = await src.hourly(o["symbol"], t_end - HOUR, t_end)
            c = float(h1["close"][0]) if len(h1["close"]) else float("nan")
            if not (c > 0):
                out.append({"kind": "dir_outcome", "id": o["id"], "status": "data_gap", "settled_at": now})
                continue
            ret_atr = math.log(c / o["close"]) / o["atr_pct"]
            if o.get("band_pct") is not None:                          # regime: plain percent band
                ret_pct = c / o["close"] - 1.0
                y = "up" if ret_pct > o["band_pct"] else "down" if ret_pct < -o["band_pct"] else "flat"
            else:
                y = "up" if ret_atr > o["band_atr"] else "down" if ret_atr < -o["band_atr"] else "flat"
            out.append({"kind": "dir_outcome", "id": o["id"], "status": "ok", "ret_atr": ret_atr,
                        "ret_bps": math.log(c / o["close"]) * 1e4, "y": y, "settled_at": now})
        if out:
            _append(self.path, out)
        return out


# ---------------------------------------------------------------- report

def auc(score: np.ndarray, y: np.ndarray) -> float:
    """Mann-Whitney AUC (ties averaged); NaN when a class is missing."""
    pos, neg = score[y == 1], score[y == 0]
    if not len(pos) or not len(neg):
        return float("nan")
    allv = np.concatenate([pos, neg])
    order = allv.argsort(kind="stable")
    ranks = np.empty(len(allv))
    sv = allv[order]
    i = 0
    while i < len(sv):                                            # average ranks over ties
        j = i
        while j + 1 < len(sv) and sv[j + 1] == sv[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def _block_ci(fn: Any, days: np.ndarray, n: int = 1000, seed: int = 7) -> list[float]:
    uniq = np.unique(days)
    if len(uniq) < 2:
        return [float("nan"), float("nan")]
    idx_by = [np.flatnonzero(days == d) for d in uniq]
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n):
        pick = np.concatenate([idx_by[k] for k in rng.integers(0, len(uniq), len(uniq))])
        v = fn(pick)
        if math.isfinite(v):
            vals.append(v)
    if len(vals) < n // 2:
        return [float("nan"), float("nan")]
    return [round(float(np.percentile(vals, 2.5)), 4), round(float(np.percentile(vals, 97.5)), 4)]


def jev_report(state_dir: Path, signal_min: float = 0.2, cost_bps: float = 12.0, block_days: int = 7,
               schema: str = STATE_SCHEMA) -> dict[str, Any]:
    """Operations (every answer: cost, latency, errors), arm B (trade_success vs paper outcomes) and arm C
    (direction_h vs realised moves). B and C count only answers to states of ``schema``, so a state
    revision never mixes into the evaluation. CIs resample calendar blocks of ``block_days`` (7, as the
    slow A0 arm): 24-48 h targets overlap neighbouring days and regimes persist."""
    state_dir = Path(state_dir)
    led = read_jsonl(state_dir / "jev.jsonl")
    runs = [r for r in led if r.get("kind") == "run"]
    judg = [r for r in led if r.get("kind") == "judgment"]
    asked = {(r["question"], r["id"]): r.get("state", {}).get("schema") for r in led if r.get("kind") == "ask"}
    judg_eval = [r for r in judg if asked.get((r["question"], r["id"])) == schema]
    lat = [r["latency_ms"] for r in judg if r.get("status") == "ok" and r.get("latency_ms") is not None]
    status: dict[str, int] = {}
    for r in judg:
        status[f"{r['question']}:{r.get('status')}"] = status.get(f"{r['question']}:{r.get('status')}", 0) + 1
    tokens = {"input": 0, "output": 0}
    for r in judg:
        u = r.get("usage") or {}
        tokens["input"] += int(u.get("input_tokens") or 0)
        tokens["output"] += int(u.get("output_tokens") or 0)
    rep: dict[str, Any] = {
        "runs": [{k: r.get(k) for k in ("at", "model_requested", "pinning", "questionset", "prompt_hash",
                                        "state_schema")} for r in runs],
        "state_schema_evaluated": schema, "judgments_other_schema": len(judg) - len(judg_eval),
        "models_returned": sorted({r.get("model_returned") for r in judg if r.get("model_returned")}),
        "status": status, "tokens": tokens,
        "latency_ms_p50": float(np.percentile(lat, 50)) if lat else None,
        "latency_ms_p95": float(np.percentile(lat, 95)) if lat else None,
        "late_trade_success": sum(1 for r in judg if r.get("late")),
        "ci_block_days": block_days,
    }
    # arm B
    outcomes = {o["id"]: o for o in read_jsonl(state_dir / "outcomes.jsonl") if o.get("exit_type") in ("TP", "SL", "TIME")}
    ts = [(r, outcomes[r["id"]]) for r in judg_eval if r["question"] == "trade_success" and r.get("status") == "ok"
          and not r.get("late") and r["id"] in outcomes]
    b: dict[str, Any] = {"n": len(ts)}
    if len(ts) >= 2:
        p = np.array([r["probs"]["trade_success"] for r, _ in ts])
        y = np.array([o["y_success"] for _, o in ts])
        net = np.array([o["net_r"] for _, o in ts])
        days = np.array([r["t_tick"] // DAY // block_days for r, _ in ts])
        med = float(np.median(p))
        hi = p >= med
        b.update({"blocks": int(len(np.unique(days))), "tp_rate": round(float(y.mean()), 4),
                  "mean_p": round(float(p.mean()), 4),
                  "auc": round(auc(p, y), 4), "auc_ci95": _block_ci(lambda i: auc(p[i], y[i]), days),
                  "net_r_all": round(float(net.mean()), 4),
                  "net_r_p_above_median": round(float(net[hi].mean()), 4) if hi.any() else None,
                  "net_r_p_below_median": round(float(net[~hi].mean()), 4) if (~hi).any() else None})
    rep["arm_B_trade_success"] = b
    # arm C
    opens = {r["id"]: r for r in led if r.get("kind") == "dir_open"}
    douts = {r["id"]: r for r in led if r.get("kind") == "dir_outcome" and r.get("status") == "ok"}
    dj = [(r, opens[r["id"]], douts[r["id"]]) for r in judg_eval if r["question"] == "direction_h"
          and r.get("status") == "ok" and r["id"] in douts and r["id"] in opens]
    c: dict[str, Any] = {"n": len(dj)}
    if len(dj) >= 2:
        up = np.array([r["probs"]["direction_h.up"] for r, _, _ in dj])
        dn = np.array([r["probs"]["direction_h.down"] for r, _, _ in dj])
        score = up - dn
        yc = np.array([o["y"] for _, _, o in dj])
        ret_bps = np.array([o["ret_bps"] for _, _, o in dj])
        days = np.array([op["t_tick"] // DAY // block_days for _, op, _ in dj])
        moved = yc != "flat"
        ybin = (yc == "up").astype(int)
        base = {k: round(float((yc == k).mean()), 4) for k in ("up", "flat", "down")}
        c.update({"blocks": int(len(np.unique(days))), "base_rates": base, "mean_score": round(float(score.mean()), 4),
                  "auc_up_vs_down": round(auc(score[moved], ybin[moved]), 4) if moved.any() else None,
                  "auc_ci95": _block_ci(lambda i: auc(score[i][moved[i]], ybin[i][moved[i]]), days)})
        sig = np.abs(score) >= signal_min
        if sig.any():
            pnl = np.sign(score) * ret_bps - cost_bps
            c.update({"signals": int(sig.sum()), "signal_min": signal_min, "cost_bps": cost_bps,
                      "signal_net_bps_mean": round(float(pnl[sig].mean()), 2),
                      "signal_net_bps_ci95": _block_ci(lambda i: float(pnl[i][sig[i]].mean()) if sig[i].any()
                                                       else float("nan"), days),
                      "signal_hit_rate": round(float((np.sign(score[sig]) * ret_bps[sig] > 0).mean()), 4)})
    rep["arm_C_direction"] = c
    # weekly BTC regime (user thesis): accuracy on settled calls; overlapping 7 d targets -> also every 7th day
    rj = [(r, douts[r["id"]]) for r in judg if r["question"] == "btc_regime_7d" and r.get("status") == "ok"
          and r["id"] in douts and asked.get((r["question"], r["id"])) == REGIME_SCHEMA]
    g: dict[str, Any] = {"n": len(rj)}
    if rj:
        yv = np.array([o["y"] for _, o in rj])
        pick = np.array([max(("up", "flat", "down"), key=lambda k: r["probs"][f"btc_regime_7d.{k}"]) for r, _ in rj])
        weekly = np.array([(r["t_tick"] // DAY) % 7 == 0 for r, _ in rj])
        g.update({"accuracy": round(float((pick == yv).mean()), 4),
                  "base_rates": {k: round(float((yv == k).mean()), 4) for k in ("up", "flat", "down")},
                  "n_non_overlapping": int(weekly.sum()),
                  "accuracy_non_overlapping": round(float((pick[weekly] == yv[weekly]).mean()), 4) if weekly.any()
                  else None})
    rep["btc_regime_7d"] = g
    return rep
