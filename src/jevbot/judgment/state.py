"""Canonical Jev states for the slow families (spec §7.1 adapted to hourly features).

Anonymous (no symbol, date or absolute price), rounded to 2 decimals, fixed key order, units in the
key names. ``canonical``: every direction-dependent field is multiplied by the proposal side, so
positive values favor the position and the model never sees or chooses the side (arm B).
``raw``: unaligned market state without any proposal, for the direction question (arm C).
Missing values are dropped rather than sent as null.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

import orjson

STATE_SCHEMA = "state.slow.v1"
FAMILY_NEUTRAL = {"TSM": "trend_breakout", "XSM": "relative_strength_rank", "FUND": "funding_crowding_reversal",
                  "CROWD": "open_interest_crowding_reversal", "FLUSH": "deleveraging_reversal"}


def _r(x: Any, nd: int = 2) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if math.isfinite(v) else None


def _block(items: list[tuple[str, Any]]) -> dict[str, Any]:
    return {k: v for k, v in ((k, _r(v)) for k, v in items) if v is not None}


def _mul(a: Any, b: Any) -> float:
    try:
        return float(a) * float(b)
    except (TypeError, ValueError):
        return float("nan")


def canonical_state(p: dict[str, Any]) -> dict[str, Any]:
    """State for ``trade_success`` about one slow/pos proposal (side-aligned)."""
    s, atr = p["side"], p.get("atr_pct")
    stop_atr = _mul(p["stop_dist"], 1.0) / (_mul(atr, p["entry_ref"]) or float("nan"))
    proposal = {"family": FAMILY_NEUTRAL[p["family"]], "side_canonical": "with_position",
                **_block([("stop_dist_atr_1h", stop_atr), ("stop_dist_bps", p["stop_dist_bps"]),
                          ("tp_R", p["r_tp"]), ("tp_dist_atr_1h", _mul(p["r_tp"], stop_atr)),
                          ("horizon_min", p["horizon_min"]), ("cost_R", p["cost_r"])])}
    asset = _block([
        ("ret_24h_atr_1h", _mul(s, p.get("ret_24h")) / (atr or float("nan"))),
        ("ret_7d_atr_1h", _mul(s, p.get("ret_7d")) / (atr or float("nan"))),
        ("ret_7d_vs_btc_pct", _mul(s, p.get("rel_ret_7d")) * 100),
        ("ret_4h_atr_1h", _mul(s, p.get("run_4h_atr"))),
        ("ema50_vs_ema200_sign", _mul(s, p.get("ema_trend"))),
        ("funding_paid_by_position_bps_8h", _mul(s, p.get("funding_bps_8h"))),
        ("open_interest_chg_24h_pct", _mul(p.get("oi_chg_24h"), 100)),
        ("open_interest_chg_4h_pct", _mul(p.get("oi_chg_4h"), 100)),
        ("atr_1h_bps", _mul(atr, 1e4)),
        ("volume_rank", p.get("vol_rank")),
    ])
    market = _block([("btc_ema50_vs_ema200_sign", _mul(s, p.get("btc_trend")))])
    return {"schema": STATE_SCHEMA, "variant": "canonical", "proposal": proposal, "asset": asset, "market": market}


def raw_state(f: dict[str, Any], btc: dict[str, Any] | None) -> dict[str, Any]:
    """Unaligned state of one symbol at a tick for ``direction_h`` (no proposal, no side).
    ``f`` / ``btc``: slow feature values at the tick (``ret_24h``, ``atr_pct``, ...)."""
    atr = f.get("atr_pct")
    asset = _block([
        ("ret_24h_atr_1h", _mul(f.get("ret_24h"), 1.0) / (atr or float("nan"))),
        ("ret_7d_atr_1h", _mul(f.get("ret_7d"), 1.0) / (atr or float("nan"))),
        ("ret_7d_vs_btc_pct", _mul(f.get("rel_ret_7d"), 100)),
        ("ret_4h_atr_1h", f.get("run_4h_atr")),
        ("ema50_vs_ema200_sign", f.get("ema_trend")),
        ("funding_bps_8h", f.get("funding_bps_8h")),
        ("open_interest_chg_24h_pct", _mul(f.get("oi_chg_24h"), 100)),
        ("atr_1h_bps", _mul(atr, 1e4)),
        ("volume_rank", f.get("vol_rank")),
    ])
    market = {}
    if btc:
        batr = btc.get("atr_pct")
        market = _block([("btc_ret_24h_atr_1h", _mul(btc.get("ret_24h"), 1.0) / (batr or float("nan"))),
                         ("btc_ret_7d_atr_1h", _mul(btc.get("ret_7d"), 1.0) / (batr or float("nan"))),
                         ("btc_ema50_vs_ema200_sign", btc.get("ema_trend"))])
    return {"schema": STATE_SCHEMA, "variant": "raw", "asset": asset, "market": market}


def canonical_json(x: Any) -> bytes:
    return orjson.dumps(x, option=orjson.OPT_SORT_KEYS)


def sha(x: Any) -> str:
    return hashlib.sha256(x if isinstance(x, bytes) else canonical_json(x)).hexdigest()
