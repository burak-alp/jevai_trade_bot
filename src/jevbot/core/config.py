"""Typed configuration loaded from layered YAML files.

Every section is a frozen dataclass; unknown keys raise ``ConfigError`` so a typo
never silently falls back to a default.
"""

from __future__ import annotations

import dataclasses
import hashlib
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import orjson
import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    json: bool = True
    file: str | None = None


@dataclass(frozen=True)
class WsConfig:
    base: str = "wss://fstream.binance.com"
    routes: dict[str, str] = field(default_factory=lambda: {"public": "/public", "market": "/market", "private": "/private"})
    stream_routes: dict[str, str] = field(default_factory=dict)
    max_streams_per_conn: int = 100
    max_control_msgs_per_sec: float = 4.0
    subscribe_chunk: int = 50
    conn_max_age_s: float = 82800.0
    silence_timeout_s: float = 15.0      # no frames this long -> ping probe; reconnect only if the probe fails
    silence_max_s: float = 120.0         # no frames this long -> reconnect even if pings succeed
    stale_lag_ms: float = 5000.0         # per-connection EWMA of (t_recv - t_event + clock offset); 0 = off
    stale_min_interval_s: float = 60.0   # at most one stale-feed reconnect per connection per interval
    open_timeout_s: float = 10.0
    ping_interval_s: float = 30.0
    ping_timeout_s: float = 20.0
    max_message_bytes: int = 8 * 1024 * 1024
    compression: bool = False
    backoff_initial_s: float = 1.0
    backoff_max_s: float = 60.0
    backoff_jitter: float = 0.3


@dataclass(frozen=True)
class RestConfig:
    timeout_s: float = 10.0
    max_retries: int = 3
    weight_limit_per_min: int = 2400
    weight_budget_frac: float = 0.5


@dataclass(frozen=True)
class BinanceConfig:
    rest_base: str = "https://fapi.binance.com"
    ws: WsConfig = field(default_factory=WsConfig)
    rest: RestConfig = field(default_factory=RestConfig)


@dataclass(frozen=True)
class UniverseConfig:
    quote_asset: str = "USDT"
    contract_type: str = "PERPETUAL"
    min_quote_vol_24h: float = 5_000_000.0
    max_symbols: int = 200
    exit_rank: int = 230
    min_listing_age_days: float = 14.0
    exclude: list[str] = field(default_factory=list)
    refresh_s: float = 3600.0


@dataclass(frozen=True)
class DepthConfig:
    enabled: bool = True
    symbols_always: list[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    top_n_by_volume: int = 20
    levels: int = 20
    speed: str = "500ms"
    sample_interval_s: float = 5.0


@dataclass(frozen=True)
class OiConfig:
    enabled: bool = True
    cycle_s: float = 60.0


@dataclass(frozen=True)
class SmokeConfig:
    enabled: bool = True
    first_event_timeout_s: float = 20.0
    required: bool = True


@dataclass(frozen=True)
class RecorderConfig:
    kline_1m: bool = True
    mark_price: bool = True
    book_ticker: bool = True
    force_order: bool = True
    depth: DepthConfig = field(default_factory=DepthConfig)
    oi: OiConfig = field(default_factory=OiConfig)
    time_sync_s: float = 60.0
    kline_grace_s: float = 5.0
    kline_backfill_after_s: float = 20.0
    exchange_info_snapshot_s: float = 3600.0
    health_interval_s: float = 10.0
    health_log_interval_s: float = 60.0
    latency_agg_s: float = 60.0
    lag_warn_ms: float = 2000.0         # feed latency p99 above this -> DEGRADED
    loop_lag_warn_ms: float = 500.0     # event-loop lag p99 above this -> DEGRADED
    orphan_tmp_quarantine: bool = True
    smoke: SmokeConfig = field(default_factory=SmokeConfig)


@dataclass(frozen=True)
class SinkConfig:
    rotate_s: float = 180.0
    late_grace_s: float = 60.0
    flush_s: float = 5.0
    flush_rows: int = 50_000
    compression: str = "zstd"
    compression_level: int = 3


@dataclass(frozen=True)
class HistConfig:
    base_url: str = "https://data.binance.vision"
    listing_url: str = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
    concurrency: int = 8
    max_retries: int = 4
    timeout_s: float = 60.0
    keep_zip: bool = False


@dataclass(frozen=True)
class AppConfig:
    data_dir: str = "./data"
    run_dir: str = "./run"
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    binance: BinanceConfig = field(default_factory=BinanceConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    recorder: RecorderConfig = field(default_factory=RecorderConfig)
    sink: SinkConfig = field(default_factory=SinkConfig)
    hist: HistConfig = field(default_factory=HistConfig)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def config_hash(self) -> str:
        return hashlib.sha256(orjson.dumps(self.to_dict(), option=orjson.OPT_SORT_KEYS)).hexdigest()[:16]


# ---------------------------------------------------------------------------
# loading

def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _set_path(d: dict[str, Any], dotted: str, raw_value: str) -> None:
    keys = dotted.split(".")
    cur = d
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
        if not isinstance(cur, dict):
            raise ConfigError(f"--set {dotted}: '{k}' is not a section")
    cur[keys[-1]] = yaml.safe_load(raw_value)


def _build(cls: type, data: Any, path: str) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"{path or '<root>'}: expected a mapping, got {type(data).__name__}")
    hints = typing.get_type_hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ConfigError(f"{path or '<root>'}: unknown key(s) {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        tp = hints[name]
        sub = f"{path}.{name}" if path else name
        if dataclasses.is_dataclass(tp):
            kwargs[name] = _build(tp, value, sub)
        else:
            kwargs[name] = _coerce(tp, value, sub)
    return cls(**kwargs)


def _coerce(tp: Any, value: Any, path: str) -> Any:
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin in (typing.Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        non_none = [a for a in args if a is not type(None)]
        return _coerce(non_none[0], value, path)
    if tp is float and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if tp in (int, str, bool):
        if not isinstance(value, tp) or (tp is int and isinstance(value, bool)):
            raise ConfigError(f"{path}: expected {tp.__name__}, got {value!r}")
        return value
    if origin is list:
        if not isinstance(value, list):
            raise ConfigError(f"{path}: expected list, got {value!r}")
        return [_coerce(args[0], v, f"{path}[]") for v in value]
    if origin is dict:
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: expected mapping, got {value!r}")
        return {_coerce(args[0], k, path): _coerce(args[1], v, f"{path}.{k}") for k, v in value.items()}
    if tp is float:
        raise ConfigError(f"{path}: expected number, got {value!r}")
    return value


def load_config(paths: list[str | Path] | None = None, overrides: list[str] | None = None) -> AppConfig:
    merged: dict[str, Any] = {}
    for p in paths or []:
        with open(p, "rb") as fh:
            doc = yaml.safe_load(fh) or {}
        if not isinstance(doc, dict):
            raise ConfigError(f"{p}: top level must be a mapping")
        merged = _deep_merge(merged, doc)
    for item in overrides or []:
        if "=" not in item:
            raise ConfigError(f"--set expects key=value, got {item!r}")
        k, v = item.split("=", 1)
        _set_path(merged, k.strip(), v)
    return _build(AppConfig, merged, "")
