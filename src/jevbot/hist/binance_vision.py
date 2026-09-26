"""Historical downloader for data.binance.vision (USDⓈ-M futures, ``data/futures/um``).

For each (dataset, symbol, period): download ``<file>.zip`` and ``<file>.zip.CHECKSUM``,
verify sha256, parse the CSV (header or no header), normalize timestamps to ms, write
Parquet + sha256 sidecar + manifest line. HTTP 404 = file does not exist (symbol not
listed that period) and is recorded in ``_missing.jsonl`` instead of being retried.

Point-in-time listing: the public S3 bucket listing gives every symbol that ever had a
file (delisted included) and its first/last available day -> survivorship-free universe.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import random
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import orjson
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pcsv
import pyarrow.parquet as pq

from jevbot.core.config import HistConfig
from jevbot.core.logging import get_logger
from jevbot.core.time import now_ms
from jevbot.recorder.integrity import append_manifest, sha256_file, write_sidecar

log = get_logger(__name__)

PREFIX = "data/futures/um"
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"

i64, f64, b = pa.int64(), pa.float64(), pa.bool_()
KLINE_COLS = [("open_time", i64), ("open", f64), ("high", f64), ("low", f64), ("close", f64), ("volume", f64),
              ("close_time", i64), ("quote_volume", f64), ("count", i64), ("taker_buy_volume", f64),
              ("taker_buy_quote_volume", f64), ("ignore", f64)]


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    columns: list[tuple[str, pa.DataType]]
    time_columns: tuple[str, ...]
    has_interval: bool = False
    daily: bool = True
    monthly: bool = True
    datetime_columns: tuple[str, ...] = ()       # "YYYY-mm-dd HH:MM:SS" strings -> ms
    drop: tuple[str, ...] = ("ignore",)


DATASETS: dict[str, DatasetSpec] = {
    "klines": DatasetSpec("klines", KLINE_COLS, ("open_time", "close_time"), has_interval=True),
    "markPriceKlines": DatasetSpec("markPriceKlines", KLINE_COLS, ("open_time", "close_time"), has_interval=True),
    "indexPriceKlines": DatasetSpec("indexPriceKlines", KLINE_COLS, ("open_time", "close_time"), has_interval=True),
    "premiumIndexKlines": DatasetSpec("premiumIndexKlines", KLINE_COLS, ("open_time", "close_time"),
                                      has_interval=True),
    "aggTrades": DatasetSpec("aggTrades", [("agg_trade_id", i64), ("price", f64), ("quantity", f64),
                                           ("first_trade_id", i64), ("last_trade_id", i64),
                                           ("transact_time", i64), ("is_buyer_maker", b)], ("transact_time",)),
    "metrics": DatasetSpec("metrics", [("create_time", pa.string()), ("symbol", pa.string()),
                                       ("sum_open_interest", f64), ("sum_open_interest_value", f64),
                                       ("count_toptrader_long_short_ratio", f64),
                                       ("sum_toptrader_long_short_ratio", f64), ("count_long_short_ratio", f64),
                                       ("sum_taker_long_short_vol_ratio", f64)], (), monthly=False,
                           datetime_columns=("create_time",)),
    "fundingRate": DatasetSpec("fundingRate", [("calc_time", i64), ("funding_interval_hours", i64),
                                               ("last_funding_rate", f64)], ("calc_time",), daily=False),
    "bookDepth": DatasetSpec("bookDepth", [("timestamp", pa.string()), ("percentage", f64), ("depth", f64),
                                           ("notional", f64)], (), monthly=False, datetime_columns=("timestamp",)),
}


class ChecksumMismatch(Exception):
    pass


def daterange(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def months(start: date, end: date) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def file_key(spec: DatasetSpec, symbol: str, period: str, granularity: str, interval: str | None) -> str:
    """Relative object key below ``data/futures/um``."""
    if spec.has_interval:
        if not interval:
            raise ValueError(f"{spec.name} requires --interval")
        return f"{granularity}/{spec.name}/{symbol}/{interval}/{symbol}-{interval}-{period}.zip"
    return f"{granularity}/{spec.name}/{symbol}/{symbol}-{spec.name}-{period}.zip"


def parse_csv(spec: DatasetSpec, raw_csv: bytes) -> pa.Table:
    first = raw_csv.lstrip()[:1]
    has_header = bool(first) and not (first.isdigit() or first == b"-")
    names = [c for c, _ in spec.columns]
    types = {c: t for c, t in spec.columns}
    table = pcsv.read_csv(
        io.BytesIO(raw_csv),
        read_options=pcsv.ReadOptions(column_names=names, skip_rows=1 if has_header else 0),
        convert_options=pcsv.ConvertOptions(column_types=types, true_values=["true", "True", "TRUE"],
                                            false_values=["false", "False", "FALSE"]),
    )
    for col in spec.time_columns:           # some newer files use microseconds; normalize to ms
        arr = table.column(col)
        if len(arr) and pc.max(arr).as_py() > 10**14:
            table = table.set_column(table.schema.get_field_index(col), col, pc.divide(arr, 1000).cast(i64))
    for col in spec.datetime_columns:
        ts = pc.strptime(table.column(col), format="%Y-%m-%d %H:%M:%S", unit="ms")
        ms = pc.cast(ts, pa.int64())
        table = table.set_column(table.schema.get_field_index(col), col, ms)
    for col in spec.drop:
        if col in table.column_names:
            table = table.drop([col])
    return table


@dataclass
class DownloadStats:
    ok: int = 0
    skipped_existing: int = 0
    missing: int = 0
    failed: int = 0
    rows: int = 0
    bytes_parquet: int = 0
    failures: list[str] = field(default_factory=list)


class BinanceVision:
    def __init__(self, cfg: HistConfig, out_root: Path, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self.out_root = Path(out_root)
        self._client = httpx.AsyncClient(timeout=cfg.timeout_s, transport=transport, follow_redirects=True,
                                         headers={"User-Agent": "jevbot-hist/0.1"})
        self._sem = asyncio.Semaphore(cfg.concurrency)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str) -> httpx.Response | None:
        """GET with retries; returns None for 404."""
        for attempt in range(1, self.cfg.max_retries + 2):
            try:
                r = await self._client.get(url)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt > self.cfg.max_retries:
                    raise
                await asyncio.sleep(min(30, 2 ** attempt) * (1 + 0.3 * random.random()))
                log.warning("hist_retry", url=url, attempt=attempt, err=repr(e))
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                return r
            if (r.status_code >= 500 or r.status_code == 429) and attempt <= self.cfg.max_retries:
                await asyncio.sleep(min(30, 2 ** attempt) * (1 + 0.3 * random.random()))
                continue
            r.raise_for_status()
        raise RuntimeError("unreachable")

    def _out_path(self, spec: DatasetSpec, symbol: str, period: str, interval: str | None) -> Path:
        d = self.out_root / spec.name
        if spec.has_interval:
            d = d / interval
        return d / f"symbol={symbol}" / f"{symbol}-{spec.name}{'-' + interval if interval else ''}-{period}.parquet"

    def _dataset_root(self, spec: DatasetSpec, interval: str | None) -> Path:
        return self.out_root / spec.name / interval if spec.has_interval and interval else self.out_root / spec.name

    async def fetch_one(self, spec: DatasetSpec, symbol: str, period: str, granularity: str,
                        interval: str | None, force: bool, stats: DownloadStats) -> None:
        out = self._out_path(spec, symbol, period, interval)
        if not force and out.exists() and out.with_name(out.name + ".sha256").exists():
            stats.skipped_existing += 1
            return
        key = file_key(spec, symbol, period, granularity, interval)
        url = f"{self.cfg.base_url.rstrip('/')}/{PREFIX}/{key}"
        root = self._dataset_root(spec, interval)
        async with self._sem:
            try:
                r_sum = await self._get(url + ".CHECKSUM")
                r_zip = await self._get(url) if r_sum is not None else None
            except Exception as e:
                stats.failed += 1
                stats.failures.append(f"{key}: {e!r}")
                log.error("hist_download_failed", key=key, err=repr(e))
                return
        if r_sum is None or r_zip is None:
            stats.missing += 1
            root.mkdir(parents=True, exist_ok=True)
            with open(root / "_missing.jsonl", "ab") as fh:
                fh.write(orjson.dumps({"key": key, "symbol": symbol, "period": period, "t": now_ms()}) + b"\n")
            return
        try:
            table, src_sha = await asyncio.to_thread(self._convert, spec, r_zip.content, r_sum.text, key)
            await asyncio.to_thread(self._write, table, out, root, key, url, src_sha, symbol, period)
        except Exception as e:
            stats.failed += 1
            stats.failures.append(f"{key}: {e!r}")
            log.error("hist_convert_failed", key=key, err=repr(e))
            return
        stats.ok += 1
        stats.rows += table.num_rows
        stats.bytes_parquet += out.stat().st_size
        if self.cfg.keep_zip:
            zp = self.out_root / "_zips" / key
            zp.parent.mkdir(parents=True, exist_ok=True)
            zp.write_bytes(r_zip.content)

    @staticmethod
    def _convert(spec: DatasetSpec, zip_bytes: bytes, checksum_text: str, key: str) -> tuple[pa.Table, str]:
        expected = (checksum_text.split() or [""])[0].lower()
        actual = hashlib.sha256(zip_bytes).hexdigest()
        if not expected or expected != actual:
            raise ChecksumMismatch(f"{key}: expected {expected[:16]}.. got {actual[:16]}..")
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = [n for n in zf.namelist() if n.endswith(".csv")]
            if len(names) != 1:
                raise ValueError(f"{key}: expected one csv in zip, got {names}")
            raw = zf.read(names[0])
        return parse_csv(spec, raw), actual

    @staticmethod
    def _write(table: pa.Table, out: Path, root: Path, key: str, url: str, src_sha: str, symbol: str,
               period: str) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        table = table.replace_schema_metadata({"source_url": url, "source_sha256": src_sha, "symbol": symbol})
        pq.write_table(table, tmp, compression="zstd", compression_level=3)
        tmp.replace(out)
        digest = sha256_file(out)
        write_sidecar(out, digest)
        append_manifest(root, {"file": str(out.relative_to(root)), "rows": table.num_rows,
                               "bytes": out.stat().st_size, "sha256": digest, "source_key": key,
                               "source_sha256": src_sha, "symbol": symbol, "period": period,
                               "finalized_at": now_ms()})

    async def download(self, dataset: str, symbols: list[str], start: date, end: date, *,
                       interval: str | None = None, granularity: str = "daily", force: bool = False) -> DownloadStats:
        spec = DATASETS[dataset]
        if granularity == "daily" and not spec.daily:
            granularity = "monthly"
        if granularity == "monthly" and not spec.monthly:
            granularity = "daily"
        periods = [d.isoformat() for d in daterange(start, end)] if granularity == "daily" else months(start, end)
        stats = DownloadStats()
        jobs = [self.fetch_one(spec, s, p, granularity, interval, force, stats) for s in symbols for p in periods]
        log.info("hist_download_start", dataset=dataset, symbols=len(symbols), periods=len(periods),
                 granularity=granularity, interval=interval)
        await asyncio.gather(*jobs)
        log.info("hist_download_done", dataset=dataset, **{k: v for k, v in stats.__dict__.items() if k != "failures"})
        return stats

    # -- S3 listing (point-in-time universe) ------------------------------------

    async def _list(self, prefix: str, delimiter: str | None = "/") -> tuple[list[str], list[str]]:
        """Returns (common prefixes, object keys) for ``prefix``, following pagination."""
        prefixes: list[str] = []
        keys: list[str] = []
        marker = ""
        while True:
            params = {"prefix": prefix}
            if delimiter:
                params["delimiter"] = delimiter
            if marker:
                params["marker"] = marker
            url = self.cfg.listing_url + "?" + "&".join(f"{k}={v}" for k, v in params.items())
            r = await self._get(url)
            if r is None:
                break
            root = ET.fromstring(r.content)
            prefixes += [e.text or "" for e in root.iter(f"{S3_NS}Prefix") if e.text and e.text != prefix]
            keys += [e.text or "" for e in root.iter(f"{S3_NS}Key")]
            truncated = (root.findtext(f"{S3_NS}IsTruncated") or "false").lower() == "true"
            if not truncated:
                break
            marker = root.findtext(f"{S3_NS}NextMarker") or (keys[-1] if keys else (prefixes[-1] if prefixes else ""))
            if not marker:
                break
        return prefixes, keys

    async def list_symbols(self, dataset: str = "klines", granularity: str = "daily") -> list[str]:
        prefixes, _ = await self._list(f"{PREFIX}/{granularity}/{dataset}/")
        return sorted({p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes})

    async def pit_listing(self, interval: str = "1m", symbols: list[str] | None = None) -> pa.Table:
        """First/last available daily kline file per symbol (delisted symbols included)."""
        symbols = symbols or await self.list_symbols("klines", "daily")
        rows: list[dict[str, Any]] = []

        async def one(sym: str) -> None:
            async with self._sem:
                _, keys = await self._list(f"{PREFIX}/daily/klines/{sym}/{interval}/", delimiter=None)
            days = sorted({k.rsplit("/", 1)[-1][:-len(".zip")][-10:] for k in keys if k.endswith(".zip")})
            if not days:
                return
            d0, d1 = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
            rows.append({"symbol": sym, "first_date": days[0], "last_date": days[-1], "n_days": len(days),
                         "n_missing_days": (d1 - d0).days + 1 - len(days)})

        await asyncio.gather(*(one(s) for s in symbols))
        rows.sort(key=lambda r: r["symbol"])
        table = pa.Table.from_pylist(rows, schema=pa.schema([
            ("symbol", pa.string()), ("first_date", pa.string()), ("last_date", pa.string()),
            ("n_days", pa.int64()), ("n_missing_days", pa.int64())]))
        out_dir = self.out_root / "_pit"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"symbol_listing-klines-{interval}.parquet"
        pq.write_table(table.replace_schema_metadata({"generated_at": str(now_ms())}), out)
        write_sidecar(out, sha256_file(out))
        return table
