"""Locked prospective paper test for the slow families (no orders, no keys, no Jev).

Every hour, at the close of the hourly bar, the engine builds slow.v1 features from live
public data with the replay's own code, runs the replay's scanner and appends the tick's
proposals to an append-only decision ledger *before* any outcome exists. After a proposal's
horizon has passed, it is settled with the replay's labeler on 1m klines + 1m mark-price
klines (same pessimistic rules and cost model), into an outcome ledger. The config hash is
written on every row; a report never mixes configurations.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import orjson

from jevbot.core.logging import get_logger
from jevbot.paper.source import HOUR, MarketSource
from jevbot.research.a0 import A0Config, group_stats
from jevbot.research.data import DAY, MIN
from jevbot.research.labels import LABEL_VERSION, label_one
from jevbot.research.slow import SLOW_SCHEMA, SlowConfig, add_btc_relative, scan_slow, slow_features_from_hourly

log = get_logger(__name__)


@dataclass
class PaperConfig:
    state_dir: Path
    slow: SlowConfig = field(default_factory=SlowConfig)
    tradable_top: int = 50
    pool: int = 60                       # candidates fetched per day (top-N by 24 h quote volume)
    window_days: int = 40                # hourly history per decision (EMA200/Donchian/ATR warm-up)
    settle_delay_min: int = 10           # wait after the horizon before settling (bars final)
    tick_delay_s: float = 20.0           # wait after the hour so the closed bar is served

    def config_hash(self) -> str:
        doc = {"slow": self.slow.to_dict(), "tradable_top": self.tradable_top, "pool": self.pool,
               "window_days": self.window_days, "schema": SLOW_SCHEMA, "labels": LABEL_VERSION}
        return hashlib.sha256(orjson.dumps(doc, option=orjson.OPT_SORT_KEYS)).hexdigest()[:16]


def _clean(v: Any) -> Any:
    if isinstance(v, (float, np.floating)):
        return None if not math.isfinite(float(v)) else float(v)
    if isinstance(v, np.integer):
        return int(v)
    return v


def _append(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        for r in rows:
            fh.write(orjson.dumps({k: _clean(v) for k, v in r.items()}) + b"\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_bytes().splitlines():
        try:
            out.append(orjson.loads(line))
        except orjson.JSONDecodeError:
            continue                      # a torn last line after a crash
    return out


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


class PaperEngine:
    def __init__(self, src: MarketSource, cfg: PaperConfig) -> None:
        self.src, self.cfg = src, cfg
        self.decisions = Path(cfg.state_dir) / "decisions.jsonl"
        self.outcomes = Path(cfg.state_dir) / "outcomes.jsonl"
        self.hash = cfg.config_hash()
        self.sha = _git_sha()
        self._day: int | None = None
        self._cands: list[str] = []
        done = [r for r in read_jsonl(self.decisions) if r.get("kind") == "tick"]
        self.last_tick = max((r["t_tick"] for r in done), default=0)

    async def decide(self, t_tick: int) -> list[dict[str, Any]]:
        """Proposals whose decision time is ``t_tick`` (an hour boundary), from data before it."""
        if self._day != t_tick // DAY:
            self._cands = await self.src.candidates(t_tick, self.cfg.pool)
            self._day = t_tick // DAY
        syms = list(dict.fromkeys(["BTCUSDT", *self._cands]))
        start = t_tick - self.cfg.window_days * DAY
        feats = {}
        for s in syms:
            h1 = await self.src.hourly(s, start, t_tick)
            valid = np.flatnonzero(~np.isnan(h1["close"]))
            if not len(valid):
                continue
            fund = await self.src.funding(s, start - 3 * DAY, t_tick)
            feats[s] = slow_features_from_hourly(s, start, h1, fund, await self.src.listing_time(s),
                                                 float(valid[0]))
        if "BTCUSDT" not in feats or len(feats) < 2:
            return []
        add_btc_relative(feats)
        return [p for p in scan_slow(feats, self.cfg.slow, self.cfg.tradable_top) if p["t_decision"] == t_tick]

    async def step(self, t_tick: int) -> list[dict[str, Any]]:
        """Decide and record one tick (idempotent across restarts)."""
        if t_tick <= self.last_tick:
            return []
        props = await self.decide(t_tick)
        now = int(time.time() * 1000)
        rows = []
        for p in props:
            p = dict(p)
            p["id"] = hashlib.sha256(f"{p['family']}|{p['symbol']}|{p['side']}|{t_tick}".encode()).hexdigest()[:16]
            p.update(kind="decision", decided_at=now, config_hash=self.hash, git_sha=self.sha,
                     live_spread_bps=await self.src.spread_bps(p["symbol"]))
            rows.append(p)
        rows.append({"kind": "tick", "t_tick": t_tick, "decided_at": now, "n": len(props), "config_hash": self.hash})
        _append(self.decisions, rows)
        self.last_tick = t_tick
        log.info("paper_tick", t_tick=t_tick, proposals=len(props))
        return rows[:-1]

    async def settle(self, now: int) -> list[dict[str, Any]]:
        done = {r["id"] for r in read_jsonl(self.outcomes)}
        out = []
        for p in read_jsonl(self.decisions):
            if p.get("kind") != "decision" or p["id"] in done:
                continue
            t_end = p["t_decision"] + (p["horizon_min"] + self.cfg.settle_delay_min) * MIN
            if t_end > now:
                continue
            b = await self.src.minute_bars(p["symbol"], p["t_decision"], p["t_decision"] + (p["horizon_min"] + 1) * MIN)
            fund = await self.src.funding(p["symbol"], p["t_decision"] - DAY, t_end)
            lab = label_one(p, b, fund, self.cfg.slow.cost)
            out.append({"id": p["id"], "config_hash": p["config_hash"], "settled_at": now, **lab})
        if out:
            _append(self.outcomes, out)
            log.info("paper_settled", n=len(out))
        return out

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        await self.settle(int(time.time() * 1000))
        while not stop.is_set():
            now = int(time.time() * 1000)
            t_tick = now - now % HOUR
            if t_tick <= self.last_tick:
                wait = (t_tick + HOUR - now) / 1000 + self.cfg.tick_delay_s
                try:
                    async with asyncio.timeout(wait):
                        await stop.wait()
                except TimeoutError:
                    pass
                continue
            try:
                await self.step(t_tick)
                await self.settle(int(time.time() * 1000))
            except Exception:
                log.exception("paper_step_failed", t_tick=t_tick)
                await asyncio.sleep(60)


def paper_report(state_dir: Path, bootstrap: int = 2000) -> dict[str, Any]:
    decisions = {r["id"]: r for r in read_jsonl(Path(state_dir) / "decisions.jsonl") if r.get("kind") == "decision"}
    ticks = [r for r in read_jsonl(Path(state_dir) / "decisions.jsonl") if r.get("kind") == "tick"]
    rows_by_hash: dict[str, list[dict[str, Any]]] = {}
    for o in read_jsonl(Path(state_dir) / "outcomes.jsonl"):
        d = decisions.get(o["id"])
        if d is None or o.get("exit_type") not in ("TP", "SL", "TIME"):
            continue
        rows_by_hash.setdefault(o["config_hash"], []).append({**d, **o})
    from datetime import date
    cfg = A0Config(hist_root=Path("."), symbols=[], start=date.today(), end=date.today(), arm_kind="slow",
                   bootstrap=bootstrap)
    rep: dict[str, Any] = {"ticks": len(ticks), "decisions": len(decisions), "by_config": {}}
    for h, rows in rows_by_hash.items():
        groups = {"all": group_stats(rows, cfg)}
        for fam in sorted({r["family"] for r in rows}):
            for side, nm in ((1, "long"), (-1, "short")):
                groups[f"{fam}_{nm}"] = group_stats([r for r in rows if r["family"] == fam and r["side"] == side], cfg)
        spreads = [r["live_spread_bps"] for r in rows if r.get("live_spread_bps") is not None]
        rep["by_config"][h] = {"settled": len(rows), "groups": groups,
                               "live_spread_bps_median": float(np.median(spreads)) if spreads else None,
                               "model_spread_bps_median": float(np.median([r["spread_bps"] for r in rows]))}
    return rep
