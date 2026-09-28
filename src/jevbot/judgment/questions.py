"""Question set ``qs.slow.v1`` (spec §7.2 for hourly proposals). No judgment words in the texts."""

from __future__ import annotations

import math
from typing import Any

from jevbot.judgment.state import sha

QS_VERSION = "qs.slow.v1"

TRADE_SUCCESS = (
    "A position has just been opened in the proposal direction. Every direction-dependent field in the "
    "state is expressed so that positive values favor this position. Exit rules: take-profit at +{tp_R}R, "
    "stop-loss at -1R (1R = {stop_atr} ATR of 1h bars), otherwise exit after {horizon_min} minutes. "
    "Will the take-profit be reached before the stop-loss and before the time exit?")
DIRECTION = (
    "Over the next {horizon_min} minutes, will the price change be above +{band} ATR of 1h bars (up), "
    "below -{band} ATR of 1h bars (down), or in between (flat)?")
PROMPT_HASH = sha({"qs": QS_VERSION, "trade_success": TRADE_SUCCESS, "direction_h": DIRECTION})[:16]


BTC_REGIME = (
    "Over the next {days} days, will the BTC price change be above +{pct}% (up), below -{pct}% (down), "
    "or in between (flat)?")
REGIME_HASH = sha({"btc_regime": BTC_REGIME})[:16]         # separate: adding it leaves PROMPT_HASH unchanged


def btc_regime(days: int = 7, pct: float = 3.0) -> dict[str, Any]:
    """Weekly BTC regime (the user's thesis: run the family that suits the coming regime)."""
    p = f"{pct:g}"
    return {"type": "choice", "instructions": BTC_REGIME.format(days=days, pct=p),
            "criteria": {"up": f"BTC price change above +{p}%", "flat": f"BTC price change between -{p}% and +{p}%",
                         "down": f"BTC price change below -{p}%"}}


def direction_band_atr(horizon_min: int) -> float:
    """Flat band: +-0.5 x the random-walk scale of the horizon in 1h ATRs (24 h -> 2.45 ATR)."""
    return round(0.5 * math.sqrt(horizon_min / 60), 2)


def trade_success(state: dict[str, Any]) -> dict[str, Any]:
    pr = state["proposal"]
    return {"type": "noul",
            "instructions": TRADE_SUCCESS.format(tp_R=pr.get("tp_R"), stop_atr=pr.get("stop_dist_atr_1h"),
                                                 horizon_min=pr.get("horizon_min")),
            "criteria": {"true": "The take-profit is reached first.",
                         "false": "The stop-loss is hit first, or neither barrier is hit before the time exit."}}


def direction_h(horizon_min: int) -> dict[str, Any]:
    b = direction_band_atr(horizon_min)
    return {"type": "choice", "instructions": DIRECTION.format(horizon_min=horizon_min, band=b),
            "criteria": {"up": f"price change above +{b} ATR of 1h bars",
                         "flat": f"price change between -{b} and +{b} ATR of 1h bars",
                         "down": f"price change below -{b} ATR of 1h bars"}}
