"""Startup integration smoke test for the routed Binance WebSocket endpoints + REST.

Per route: connect -> SUBSCRIBE ack -> first event of every required family within a
timeout -> schema validation through the production parsers -> ping/pong heartbeat.
The recorder is not considered healthy until every enabled route passes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import orjson
from websockets.asyncio.client import connect

from jevbot.core.config import AppConfig
from jevbot.core.events import SchemaError
from jevbot.core.logging import get_logger
from jevbot.core.time import mono_ms, now_ms
from jevbot.marketdata import endpoints as ep
from jevbot.marketdata.binance_rest import BinanceRest
from jevbot.marketdata.parsers import decode_frame, parse_payload

log = get_logger(__name__)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class SmokeReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def failed_routes(self) -> list[str]:
        return [c.name for c in self.checks if not c.ok]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [c.__dict__ for c in self.checks]}


def smoke_plan(cfg: AppConfig) -> dict[str, dict[str, Any]]:
    """Route -> {initial streams, one extra stream to SUBSCRIBE, required families}."""
    rc = cfg.recorder
    s1, s2 = (rc.depth.symbols_always + ["BTCUSDT", "ETHUSDT"])[:2]
    streams: list[tuple[str, bool]] = []            # (stream, required-first-event)
    if rc.kline_1m:
        streams += [(ep.kline_stream(s1), True), (ep.kline_stream(s2), False)]
    if rc.mark_price:
        streams.append((ep.MARK_PRICE_ALL, True))
    if rc.force_order:
        streams.append((ep.FORCE_ORDER_ALL, False))  # liquidations may not occur within the timeout
    if rc.book_ticker:
        streams += [(ep.book_ticker_stream(s1), True), (ep.book_ticker_stream(s2), False)]
    if rc.depth.enabled:
        streams.append((ep.depth_stream(s1, rc.depth.levels, rc.depth.speed), True))
    plan: dict[str, dict[str, Any]] = {}
    for stream, required in streams:
        route = ep.route_of(stream, cfg.binance.ws)
        p = plan.setdefault(route, {"initial": [], "subscribe": [], "required": set()})
        # the second symbol's stream is added via SUBSCRIBE to exercise the control path
        (p["subscribe"] if (not required and stream.endswith(("@kline_1m", "@bookTicker"))) else p["initial"]).append(stream)
        if required:
            p["required"].add(ep.family_of(stream))
    return plan


async def _check_route(cfg: AppConfig, route: str, plan: dict[str, Any], timeout: float) -> CheckResult:
    ws_cfg = cfg.binance.ws
    url = ep.combined_stream_url(route, plan["initial"], ws_cfg)
    detail: dict[str, Any] = {"url": url, "subscribe": plan["subscribe"], "required": sorted(plan["required"])}
    t0 = mono_ms()
    try:
        async with connect(url, open_timeout=ws_cfg.open_timeout_s, max_size=ws_cfg.max_message_bytes,
                           compression=None, ping_interval=None) as ws:
            detail["connect_ms"] = mono_ms() - t0
            ack_ok = not plan["subscribe"]
            if plan["subscribe"]:
                await ws.send(orjson.dumps({"method": "SUBSCRIBE", "params": plan["subscribe"], "id": 1}).decode())
            seen: dict[str, int] = {}
            validated: dict[str, int] = {}
            errors: list[str] = []
            deadline = mono_ms() + int(timeout * 1000)
            want_families = set(plan["required"]) | {ep.family_of(s) for s in plan["subscribe"]}
            want_sub_streams = set(plan["subscribe"])
            while mono_ms() < deadline:
                remaining = (deadline - mono_ms()) / 1000
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.01, remaining))
                except asyncio.TimeoutError:
                    break
                t_recv = now_ms()
                try:
                    env = decode_frame(raw)
                    if env.kind == "control":
                        if env.control_id == 1:
                            ack_ok = env.error is None
                            detail["subscribe_ack"] = env.error or "ok"
                        continue
                    parse_payload(env, t_recv)
                    validated[env.family] = validated.get(env.family, 0) + 1
                    want_sub_streams.discard(env.stream)
                except SchemaError as e:
                    errors.append(str(e)[:200])
                    continue
                seen[env.family] = seen.get(env.family, 0) + 1
                if ack_ok and plan["required"] <= set(validated) and not want_sub_streams:
                    break
            rtt = await asyncio.wait_for(await ws.ping(), timeout=10)
            detail.update(events=seen, validated=validated, schema_errors=errors[:5], subscribe_ack_ok=ack_ok,
                          subscribed_streams_delivered=sorted(set(plan["subscribe"]) - want_sub_streams),
                          ping_rtt_ms=round(float(rtt) * 1000, 2), elapsed_ms=mono_ms() - t0)
            missing = sorted(plan["required"] - set(validated))
            detail["missing_required"] = missing
            ok = ack_ok and not missing and not errors and not want_sub_streams
            if want_families - set(validated):
                detail["families_without_events"] = sorted(want_families - set(validated))
            return CheckResult(f"ws:{route}", ok, detail)
    except Exception as e:
        detail["error"] = repr(e)
        return CheckResult(f"ws:{route}", False, detail)


async def _check_rest(rest: BinanceRest) -> CheckResult:
    detail: dict[str, Any] = {}
    try:
        t_send, t_server, t_recv = await rest.server_time()
        detail["rtt_ms"] = t_recv - t_send
        detail["clock_offset_ms"] = t_server - (t_send + t_recv) / 2
        info = await rest.exchange_info()
        detail["rate_limits"] = info.get("rateLimits")
        detail["symbols"] = len(info.get("symbols", []))
        detail["weight_used_1m"] = rest.weights.current_used()
        return CheckResult("rest", True, detail)
    except Exception as e:
        detail["error"] = repr(e)
        return CheckResult("rest", False, detail)


async def run_smoke(cfg: AppConfig, rest: BinanceRest | None = None) -> SmokeReport:
    report = SmokeReport()
    own_rest = rest is None
    rest = rest or BinanceRest(cfg.binance.rest_base, cfg.binance.rest)
    try:
        plan = smoke_plan(cfg)
        timeout = cfg.recorder.smoke.first_event_timeout_s
        results = await asyncio.gather(_check_rest(rest),
                                       *(_check_route(cfg, r, p, timeout) for r, p in sorted(plan.items())))
        report.checks.extend(results)
    finally:
        if own_rest:
            await rest.aclose()
    for c in report.checks:
        (log.info if c.ok else log.error)("smoke_check", check=c.name, ok=c.ok, **c.detail)
    return report
