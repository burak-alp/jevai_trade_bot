"""Point-in-time trading universe selection from exchangeInfo + 24h tickers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jevbot.core.config import UniverseConfig
from jevbot.core.time import DAY_MS

PENDING_DELIVERY_WINDOW_MS = 30 * DAY_MS   # perpetual with a near deliveryDate = scheduled delisting


@dataclass(frozen=True, slots=True)
class SymbolMeta:
    symbol: str
    status: str
    contract_type: str
    quote_asset: str
    onboard_date: int
    delivery_date: int
    tick_size: float
    step_size: float
    min_qty: float
    min_notional: float


def parse_symbols(exchange_info: dict[str, Any]) -> dict[str, SymbolMeta]:
    out: dict[str, SymbolMeta] = {}
    for s in exchange_info.get("symbols", []):
        filters = {f.get("filterType"): f for f in s.get("filters", [])}
        out[s["symbol"]] = SymbolMeta(
            symbol=s["symbol"],
            status=s.get("status", ""),
            contract_type=s.get("contractType", ""),
            quote_asset=s.get("quoteAsset", ""),
            onboard_date=int(s.get("onboardDate") or 0),
            delivery_date=int(s.get("deliveryDate") or 0),
            tick_size=float(filters.get("PRICE_FILTER", {}).get("tickSize", 0) or 0),
            step_size=float(filters.get("LOT_SIZE", {}).get("stepSize", 0) or 0),
            min_qty=float(filters.get("LOT_SIZE", {}).get("minQty", 0) or 0),
            min_notional=float(filters.get("MIN_NOTIONAL", {}).get("notional", 0) or 0),
        )
    return out


def select_universe(exchange_info: dict[str, Any], tickers: list[dict[str, Any]], cfg: UniverseConfig,
                    now: int, previous: set[str] | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    """Returns (members ordered by 24h quote volume, per-symbol snapshot rows).

    Hysteresis: a previous member stays while its rank <= ``exit_rank``; newcomers need
    rank <= ``max_symbols``. The member count is capped at ``max_symbols``.
    """
    previous = previous or set()
    metas = parse_symbols(exchange_info)
    vol = {}
    for t in tickers:
        try:
            vol[t["symbol"]] = float(t.get("quoteVolume", 0) or 0)
        except (TypeError, ValueError):
            continue
    exclude = set(cfg.exclude)
    reasons: dict[str, str] = {}
    candidates: list[str] = []
    for sym, m in metas.items():
        if m.contract_type != cfg.contract_type or m.quote_asset != cfg.quote_asset:
            reasons[sym] = "contract"
        elif m.status != "TRADING":
            reasons[sym] = "status"
        elif sym in exclude:
            reasons[sym] = "excluded"
        elif m.delivery_date and m.delivery_date - now < PENDING_DELIVERY_WINDOW_MS:
            reasons[sym] = "pending_delivery"
        elif m.onboard_date and (now - m.onboard_date) < cfg.min_listing_age_days * DAY_MS:
            reasons[sym] = "too_new"
        elif vol.get(sym, 0.0) < cfg.min_quote_vol_24h:
            reasons[sym] = "low_volume"
        else:
            candidates.append(sym)
    candidates.sort(key=lambda s: (-vol.get(s, 0.0), s))
    rank = {s: i + 1 for i, s in enumerate(candidates)}
    keep = [s for s in candidates if rank[s] <= cfg.max_symbols or (s in previous and rank[s] <= cfg.exit_rank)]
    if len(keep) > cfg.max_symbols:
        # hysteresis kept old members beyond the cap: drop the weakest newcomers first
        olds = [s for s in keep if s in previous]
        news = [s for s in keep if s not in previous]
        keep = sorted((olds + news)[:cfg.max_symbols], key=lambda s: rank[s])
    members = keep
    member_set = set(members)
    for s in candidates:
        if s not in member_set:
            reasons[s] = "rank"
    rows = [{
        "t_asof": now, "symbol": sym, "status": m.status, "contract_type": m.contract_type,
        "onboard_date": m.onboard_date, "delivery_date": m.delivery_date, "tick_size": m.tick_size,
        "step_size": m.step_size, "min_qty": m.min_qty, "min_notional": m.min_notional,
        "quote_vol_24h": vol.get(sym, 0.0), "vol_rank": rank.get(sym, 0), "in_universe": sym in member_set,
        "reason": "" if sym in member_set else reasons.get(sym, ""),
    } for sym, m in sorted(metas.items())]
    return members, rows
