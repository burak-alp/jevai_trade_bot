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
import os
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
from jevbot.hist.validate import Context, validate_file
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


CSV_BLOCK_BYTES = 4 << 20          # streaming CSV block; bounds conversion memory
DOWNLOAD_CHUNK_BYTES = 1 << 20


def _has_header(first_bytes: bytes) -> bool:
    first = first_bytes.lstrip()[:1]
    return bool(first) and not (first.isdigit() or first == b"-")


def _normalize(spec: DatasetSpec, batch: pa.RecordBatch, micros: dict[str, bool]) -> pa.RecordBatch:
    cols, names = list(batch.columns), list(batch.schema.names)
    for col in spec.time_columns:        # some files use microseconds; decided once per file (first batch)
        i = names.index(col)
        if col not in micros:
            mx = pc.max(cols[i]).as_py() if len(cols[i]) else None
            micros[col] = mx is not None and mx > 10**14
        if micros[col]:
            cols[i] = pc.divide(cols[i], 1000).cast(i64)
    for col in spec.datetime_columns:    # "YYYY-mm-dd HH:MM:SS" -> epoch ms
        i = names.index(col)
        cols[i] = pc.cast(pc.strptime(cols[i], format="%Y-%m-%d %H:%M:%S", unit="ms"), pa.int64())
    keep = [i for i, n in enumerate(names) if n not in spec.drop]
    return pa.RecordBatch.from_arrays([cols[i] for i in keep], names=[names[i] for i in keep])


@dataclass
class ConvertResult:
    rows: int = 0
    batches: int = 0


def _iter_line_chunks(fh: Any, chunk_bytes: int):
    """Yield byte chunks of ~``chunk_bytes`` that end on a line boundary."""
    carry = b""
    while True:
        block = fh.read(chunk_bytes)
        if not block:
            if carry.strip():
                yield carry
            return
        block = carry + block
        cut = block.rfind(b"\n")
        if cut < 0:
            carry = block
            continue
        carry = block[cut + 1:]
        yield block[:cut + 1]


def convert_csv_stream(spec: DatasetSpec, open_fn: Any, out_tmp: Path, metadata: dict[str, str]) -> ConvertResult:
    """Convert CSV (from an ``open_fn()`` binary file object) to Parquet chunk by chunk.

    The file is read in line-aligned chunks of ``CSV_BLOCK_BYTES`` and each chunk is parsed
    independently. (pyarrow's ``open_csv`` streaming reader reads far ahead of the consumer
    and its buffering grows with the file, so it is not used.) Peak memory is bounded by a
    few chunks, independent of the file size.
    """
    names = [c for c, _ in spec.columns]
    types = {c: t for c, t in spec.columns}
    co = pcsv.ConvertOptions(column_types=types, true_values=["true", "True", "TRUE"],
                             false_values=["false", "False", "FALSE"])
    res = ConvertResult()
    writer: pq.ParquetWriter | None = None
    micros: dict[str, bool] = {}
    first = True
    try:
        with open_fn() as fh:
            for chunk in _iter_line_chunks(fh, CSV_BLOCK_BYTES):
                skip = 1 if first and _has_header(chunk[:256]) else 0
                first = False
                ro = pcsv.ReadOptions(column_names=names, skip_rows=skip, use_threads=False,
                                      block_size=len(chunk) + 1)
                table = pcsv.read_csv(io.BytesIO(chunk), read_options=ro, convert_options=co)
                del chunk
                for batch in table.to_batches():
                    nb = _normalize(spec, batch, micros)
                    if writer is None:
                        writer = pq.ParquetWriter(str(out_tmp), nb.schema.with_metadata(metadata),
                                                  compression="zstd", compression_level=3)
                    writer.write_batch(nb)
                    res.rows += nb.num_rows
                res.batches += 1
                del table
        if writer is None:               # empty csv: write an empty file with the target schema
            fields = [pa.field(c, pa.int64() if c in spec.datetime_columns else t)
                      for c, t in spec.columns if c not in spec.drop]
            writer = pq.ParquetWriter(str(out_tmp), pa.schema(fields).with_metadata(metadata))
    finally:
        if writer is not None:
            writer.close()
    return res


def parse_csv(spec: DatasetSpec, raw_csv: bytes) -> pa.Table:
    """Small in-memory helper (tests / tooling) using the same streaming path."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "x.parquet"
        convert_csv_stream(spec, lambda: io.BytesIO(raw_csv), out, {})
        return pq.read_table(out).replace_schema_metadata(None)


@dataclass
class DownloadStats:
    ok: int = 0
    skipped_existing: int = 0
    missing: int = 0
    failed: int = 0
    suspect: int = 0
    rows: int = 0
    bytes_downloaded: int = 0
    bytes_parquet: int = 0
    jobs: int = 0
    failures: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Job:
    spec: DatasetSpec
    symbol: str
    period: str
    granularity: str
    interval: str | None


class NotFound(Exception):
    pass


class BinanceVision:
    def __init__(self, cfg: HistConfig, out_root: Path, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self.out_root = Path(out_root)
        self.tmp_dir = self.out_root / "_tmp"
        self._client = httpx.AsyncClient(timeout=cfg.timeout_s, transport=transport, follow_redirects=True,
                                         headers={"User-Agent": "jevbot-hist/0.1"})
        self._sem = asyncio.Semaphore(cfg.concurrency)
        self.max_inflight = 0
        self._inflight = 0
        self._pit: dict[str, str] | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(min(30, 2 ** attempt) * (1 + 0.3 * random.random()))

    async def _get(self, url: str) -> httpx.Response | None:
        """Small GET (checksum/listing) with retries; returns None for 404."""
        for attempt in range(1, self.cfg.max_retries + 2):
            try:
                r = await self._client.get(url)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt > self.cfg.max_retries:
                    raise
                log.warning("hist_retry", url=url, attempt=attempt, err=repr(e))
                await self._backoff(attempt)
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                return r
            if (r.status_code >= 500 or r.status_code == 429) and attempt <= self.cfg.max_retries:
                await self._backoff(attempt)
                continue
            r.raise_for_status()
        raise RuntimeError("unreachable")

    async def _stream_to_file(self, url: str, dest: Path) -> tuple[str, int]:
        """Stream ``url`` to ``dest`` computing sha256 incrementally. Raises NotFound on 404."""
        for attempt in range(1, self.cfg.max_retries + 2):
            h = hashlib.sha256()
            size = 0
            try:
                async with self._client.stream("GET", url) as r:
                    if r.status_code == 404:
                        raise NotFound(url)
                    if r.status_code != 200:
                        if (r.status_code >= 500 or r.status_code == 429) and attempt <= self.cfg.max_retries:
                            await self._backoff(attempt)
                            continue
                        r.raise_for_status()
                    with open(dest, "wb") as fh:
                        async for chunk in r.aiter_bytes(DOWNLOAD_CHUNK_BYTES):
                            fh.write(chunk)
                            h.update(chunk)
                            size += len(chunk)
                return h.hexdigest(), size
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt > self.cfg.max_retries:
                    raise
                log.warning("hist_retry", url=url, attempt=attempt, err=repr(e))
                await self._backoff(attempt)
        raise RuntimeError("unreachable")

    def _out_path(self, spec: DatasetSpec, symbol: str, period: str, interval: str | None) -> Path:
        d = self.out_root / spec.name
        if spec.has_interval:
            d = d / interval
        return d / f"symbol={symbol}" / f"{symbol}-{spec.name}{'-' + interval if interval else ''}-{period}.parquet"

    def _dataset_root(self, spec: DatasetSpec, interval: str | None) -> Path:
        return self.out_root / spec.name / interval if spec.has_interval and interval else self.out_root / spec.name

    def _record_missing(self, root: Path, key: str, job: Job) -> None:
        root.mkdir(parents=True, exist_ok=True)
        with open(root / "_missing.jsonl", "ab") as fh:
            fh.write(orjson.dumps({"key": key, "symbol": job.symbol, "period": job.period, "t": now_ms()}) + b"\n")

    async def fetch_one(self, job: Job, force: bool, stats: DownloadStats) -> None:
        spec = job.spec
        out = self._out_path(spec, job.symbol, job.period, job.interval)
        if not force and out.exists() and out.with_name(out.name + ".sha256").exists():
            stats.skipped_existing += 1
            return
        key = file_key(spec, job.symbol, job.period, job.granularity, job.interval)
        url = f"{self.cfg.base_url.rstrip('/')}/{PREFIX}/{key}"
        root = self._dataset_root(spec, job.interval)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        zip_tmp = self.tmp_dir / (key.replace("/", "__") + f".{os.getpid()}.part")
        try:
            r_sum = await self._get(url + ".CHECKSUM")
            if r_sum is None:
                stats.missing += 1
                self._record_missing(root, key, job)
                return
            expected = (r_sum.text.split() or [""])[0].lower()
            try:
                actual, size = await self._stream_to_file(url, zip_tmp)
            except NotFound:
                stats.missing += 1
                self._record_missing(root, key, job)
                return
            stats.bytes_downloaded += size
            if not expected or expected != actual:
                raise ChecksumMismatch(f"{key}: expected {expected[:16]}.. got {actual[:16]}..")
            rows = await asyncio.to_thread(self._convert_and_publish, spec, zip_tmp, out, root, key, url, actual,
                                           job, stats)
            stats.ok += 1
            stats.rows += rows
            stats.bytes_parquet += out.stat().st_size if out.exists() else 0
            if self.cfg.keep_zip:
                zp = self.out_root / "_zips" / key
                zp.parent.mkdir(parents=True, exist_ok=True)
                os.replace(zip_tmp, zp)
        except Exception as e:
            stats.failed += 1
            stats.failures.append(f"{key}: {e!r}")
            log.error("hist_job_failed", key=key, err=repr(e))
        finally:
            if zip_tmp.exists():
                zip_tmp.unlink()

    def _convert_and_publish(self, spec: DatasetSpec, zip_path: Path, out: Path, root: Path, key: str, url: str,
                             src_sha: str, job: Job, stats: DownloadStats) -> int:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if n.endswith(".csv")]
            if len(names) != 1:
                raise ValueError(f"{key}: expected one csv in zip, got {names}")
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(out.name + ".tmp")
            meta = {"source_url": url, "source_sha256": src_sha, "symbol": job.symbol, "period": job.period}
            res = convert_csv_stream(spec, lambda: zf.open(names[0]), tmp, meta)
        quality = self.validate(spec, tmp, job)
        final = out
        if quality["status"] == "suspect":
            stats.suspect += 1
            final = root / "_suspect" / out.relative_to(root)
            final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, final)
        digest = sha256_file(final)
        write_sidecar(final, digest)
        (final.with_name(final.name + ".quality.json")).write_bytes(orjson.dumps(quality, option=orjson.OPT_INDENT_2))
        append_manifest(root, {"file": str(final.relative_to(root)), "rows": res.rows,
                               "bytes": final.stat().st_size, "sha256": digest, "source_key": key,
                               "source_sha256": src_sha, "symbol": job.symbol, "period": job.period,
                               "quality": quality["status"], "quality_issues": quality["issues"],
                               "finalized_at": now_ms()})
        if quality["status"] != "ok":
            log.warning("hist_quality", key=key, status=quality["status"], issues=quality["issues"][:5])
        return res.rows

    def _last_listed(self, symbol: str) -> str | None:
        if self._pit is None:
            self._pit = {}
            for f in sorted((self.out_root / "_pit").glob("symbol_listing-*.parquet")):
                for r in pq.read_table(f).to_pylist():
                    self._pit[r["symbol"]] = max(self._pit.get(r["symbol"], ""), r["last_date"])
        return self._pit.get(symbol)

    def validate(self, spec: DatasetSpec, path: Path, job: Job) -> dict[str, Any]:
        """Semantic validation (``jevbot.hist.validate``); never raises, a crash marks the file suspect."""
        ctx = Context(symbol=job.symbol, period=job.period, interval=job.interval,
                      last_listed_date=self._last_listed(job.symbol) if spec.name == "fundingRate" else None)
        try:
            return validate_file(spec.name, path, ctx)
        except Exception as e:
            return {"status": "suspect", "issues": [f"suspect:validator_error:{e!r}"], "checks": {}}

    def iter_jobs(self, dataset: str, symbols: list[str], start: date, end: date, interval: str | None,
                  granularity: str):
        spec = DATASETS[dataset]
        if granularity == "daily" and not spec.daily:
            granularity = "monthly"
        if granularity == "monthly" and not spec.monthly:
            granularity = "daily"
        if granularity == "daily":
            n = (end - start).days + 1
            for sym in symbols:
                for i in range(n):
                    yield Job(spec, sym, (start + timedelta(days=i)).isoformat(), granularity, interval)
        else:
            for sym in symbols:
                for m in months(start, end):
                    yield Job(spec, sym, m, granularity, interval)

    async def download(self, dataset: str, symbols: list[str], start: date, end: date, *,
                       interval: str | None = None, granularity: str = "daily", force: bool = False) -> DownloadStats:
        """Bounded producer/consumer: at most ``concurrency`` jobs in flight and ``2*concurrency`` queued."""
        stats = DownloadStats()
        n = max(1, self.cfg.concurrency)
        q: asyncio.Queue[Job | None] = asyncio.Queue(maxsize=2 * n)

        async def producer() -> None:
            for job in self.iter_jobs(dataset, symbols, start, end, interval, granularity):
                await q.put(job)
                stats.jobs += 1
            for _ in range(n):
                await q.put(None)

        async def worker() -> None:
            while True:
                job = await q.get()
                if job is None:
                    return
                self._inflight += 1
                self.max_inflight = max(self.max_inflight, self._inflight)
                try:
                    await self.fetch_one(job, force, stats)
                finally:
                    self._inflight -= 1

        log.info("hist_download_start", dataset=dataset, symbols=len(symbols), start=str(start), end=str(end),
                 granularity=granularity, interval=interval, concurrency=n)
        await asyncio.gather(producer(), *(worker() for _ in range(n)))
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
