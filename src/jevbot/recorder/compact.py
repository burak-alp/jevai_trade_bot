"""Compaction of small recorder Parquet parts into hourly (or daily) files.

Unit of work = (dataset, time group) where the group is ``group_ms`` wide (default 1 h;
a day of book_1s for 200 symbols would not fit comfortably in RAM). For every group whose
end + grace lies in the past:

1. collect finalized parts of that group from the manifest; verify each (sha256 sidecar ==
   manifest sha256 == file bytes, Parquet footer readable, row count). Unverifiable parts
   are left untouched and reported;
2. journal ``state=writing`` (``<dataset>/_compaction/<id>.json``);
3. read, concatenate, sort by (symbol, event time), write ``.tmp``, fsync, sha256, atomic
   rename, sidecar; re-read and check rows / time range;
4. append the manifest entry of the compacted file, journal ``state=committed``;
5. delete the sources and append tombstones (``deleted: true, replaced_by``), drop the journal.

Recovery on the next run: ``writing`` -> remove the half-written target (sources are
intact); ``committed`` -> finish deleting sources. Readers that go through the manifest
never see duplicates; plain globbing may briefly see both only inside a crash window.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import orjson
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from jevbot.core.logging import get_logger
from jevbot.core.time import HOUR_MS, ms_to_date, now_ms
from jevbot.recorder.integrity import (
    TMP_SUFFIX,
    append_manifest,
    fsync_path,
    read_manifest,
    read_sidecar,
    sha256_file,
    write_sidecar,
)
from jevbot.recorder.schemas import SCHEMA_VERSION, SCHEMAS, TIME_COLUMN

log = get_logger(__name__)


@dataclass
class CompactResult:
    groups_compacted: int = 0
    files_in: int = 0
    files_out: int = 0
    rows: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    skipped_unverified: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)


def _verify_source(dataset_dir: Path, rel: str, entry: dict[str, Any]) -> str | None:
    p = dataset_dir / rel
    if not p.exists():
        return "missing"
    side = read_sidecar(p)
    if side is None or side != entry.get("sha256"):
        return "sidecar_mismatch"
    if sha256_file(p) != side:
        return "checksum_mismatch"
    try:
        if pq.read_metadata(p).num_rows != entry.get("rows"):
            return "row_count_mismatch"
    except Exception:
        return "unreadable"
    return None


def _journal_dir(dataset_dir: Path) -> Path:
    d = dataset_dir / "_compaction"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_journal(path: Path, doc: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(orjson.dumps(doc))
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _delete_sources(dataset_dir: Path, sources: list[str], target: str) -> None:
    for rel in sources:
        p = dataset_dir / rel
        side = p.with_name(p.name + ".sha256")
        if p.exists():
            p.unlink()
        if side.exists():
            side.unlink()
        append_manifest(dataset_dir, {"file": rel, "deleted": True, "replaced_by": target, "t": now_ms()})


def recover(dataset_dir: Path, result: CompactResult) -> None:
    jdir = dataset_dir / "_compaction"
    if not jdir.exists():
        return
    for jp in sorted(jdir.glob("*.json")):
        doc = orjson.loads(jp.read_bytes())
        target = dataset_dir / doc["target"]
        if doc["state"] == "writing":
            for p in (target, target.with_name(target.name + TMP_SUFFIX), target.with_name(target.name + ".sha256")):
                if p.exists():
                    p.unlink()
        elif doc["state"] == "committed":
            _delete_sources(dataset_dir, [s["file"] for s in doc["sources"]], doc["target"])
        jp.unlink()
        result.recovered.append(f"{jp.name}:{doc['state']}")
        log.warning("compaction_recovered", journal=jp.name, state=doc["state"])


def _sort_keys(dataset: str, schema: pa.Schema) -> list[tuple[str, str]]:
    keys = [("symbol", "ascending")] if "symbol" in schema.names else []
    return keys + [(TIME_COLUMN[dataset], "ascending")]


def compact_dataset(raw_root: Path, dataset: str, *, group_ms: int = HOUR_MS, grace_ms: int = 5 * 60_000,
                    now: int | None = None, dry_run: bool = False) -> CompactResult:
    now = now_ms() if now is None else now
    dataset_dir = Path(raw_root) / dataset
    result = CompactResult()
    if not dataset_dir.exists():
        return result
    lock_path = _journal_dir(dataset_dir) / ".lock"
    with open(lock_path, "w") as lock:
        if os.name != "nt":                 # single-writer lock; on Windows run one compactor at a time
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        recover(dataset_dir, result)
        manifest = read_manifest(dataset_dir)
        groups: dict[int, list[tuple[str, dict[str, Any]]]] = {}
        for rel, e in manifest.items():
            ws, we = e.get("window_start"), e.get("window_end")
            if ws is None or we is None:
                continue
            g = ws - ws % group_ms
            if we > g + group_ms:           # entry spans groups (e.g. already daily-compacted) -> leave it
                continue
            if g + group_ms + grace_ms > now:
                continue
            groups.setdefault(g, []).append((rel, e))
        for g, entries in sorted(groups.items()):
            good: list[tuple[str, dict[str, Any]]] = []
            for rel, e in entries:
                problem = _verify_source(dataset_dir, rel, e)
                if problem:
                    result.skipped_unverified.append(f"{rel}:{problem}")
                    log.error("compaction_source_unverified", dataset=dataset, file=rel, problem=problem)
                else:
                    good.append((rel, e))
            if len(good) < 2:
                continue
            if dry_run:
                result.groups_compacted += 1
                result.files_in += len(good)
                continue
            _compact_group(dataset_dir, dataset, g, group_ms, good, result)
    return result


def _compact_group(dataset_dir: Path, dataset: str, g: int, group_ms: int,
                   sources: list[tuple[str, dict[str, Any]]], result: CompactResult) -> None:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(g / 1000))
    out_dir = dataset_dir / f"date={ms_to_date(g)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{dataset}-{stamp}-compact-{os.getpid()}-{now_ms()}.parquet"
    target_rel = str(target.relative_to(dataset_dir))
    journal = _journal_dir(dataset_dir) / f"{dataset}-{stamp}-{now_ms()}.json"
    src_doc = [{"file": rel, "sha256": e["sha256"], "rows": e["rows"]} for rel, e in sources]
    _write_journal(journal, {"state": "writing", "target": target_rel, "sources": src_doc, "t": now_ms()})

    schema = SCHEMAS[dataset]
    tables = [pq.read_table(dataset_dir / rel).cast(schema) for rel, _ in sources]
    table = pa.concat_tables(tables).sort_by(_sort_keys(dataset, schema))
    rows_in = sum(e["rows"] for _, e in sources)
    if table.num_rows != rows_in:
        raise RuntimeError(f"{dataset}: row count changed while reading sources ({table.num_rows} != {rows_in})")
    tcol = TIME_COLUMN[dataset]
    t_min, t_max = pc.min(table.column(tcol)).as_py(), pc.max(table.column(tcol)).as_py()
    tmp = target.with_name(target.name + TMP_SUFFIX)
    meta = {"jevbot_schema": SCHEMA_VERSION, "dataset": dataset, "window_start": str(g),
            "window_ms": str(group_ms), "compacted_from": str(len(sources))}
    pq.write_table(table.replace_schema_metadata(meta), tmp, compression="zstd", compression_level=6,
                   row_group_size=256_000)
    fsync_path(tmp)
    digest = sha256_file(tmp)
    os.replace(tmp, target)
    write_sidecar(target, digest)
    fsync_path(out_dir)
    check = pq.read_table(target)                       # read back before touching sources
    if (check.num_rows != rows_in or pc.min(check.column(tcol)).as_py() != t_min
            or pc.max(check.column(tcol)).as_py() != t_max):
        raise RuntimeError(f"{dataset}: compacted file verification failed ({target_rel})")
    size = target.stat().st_size
    append_manifest(dataset_dir, {"file": target_rel, "rows": rows_in, "bytes": size, "sha256": digest,
                                  "t_min": t_min, "t_max": t_max, "window_start": g, "window_end": g + group_ms,
                                  "schema": SCHEMA_VERSION, "finalized_at": now_ms(),
                                  "compacted_from": [rel for rel, _ in sources]})
    _write_journal(journal, {"state": "committed", "target": target_rel, "sources": src_doc, "t": now_ms()})
    bytes_in = sum(e.get("bytes", 0) for _, e in sources)
    _delete_sources(dataset_dir, [rel for rel, _ in sources], target_rel)
    journal.unlink()
    result.groups_compacted += 1
    result.files_in += len(sources)
    result.files_out += 1
    result.rows += rows_in
    result.bytes_in += bytes_in
    result.bytes_out += size
    log.info("compaction_group_done", dataset=dataset, group=stamp, files_in=len(sources), rows=rows_in,
             bytes_in=bytes_in, bytes_out=size)


def compact_all(raw_root: Path, datasets: list[str] | None = None, **kw: Any) -> dict[str, CompactResult]:
    names = datasets or [d for d in SCHEMAS if (Path(raw_root) / d).exists()]
    return {d: compact_dataset(raw_root, d, **kw) for d in names}
