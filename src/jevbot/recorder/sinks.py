"""Buffered, event-time partitioned Parquet sinks with a dedicated writer thread.

The event loop only appends rows to in-memory buffers (cheap). Every ``flush_s`` the
buffers are split by **event-time window** (``TIME_COLUMN`` of the dataset, windows of
``rotate_s`` aligned to the epoch), converted to Arrow tables and queued to one writer
thread which owns all open ``pq.ParquetWriter`` objects, so file I/O and compression
never block the loop.

A window's file is finalized once wall clock passes ``window_end + late_grace_s``.
Rows that arrive later for that window (late events, REST backfill) go to an additional
part file of the *same* window, so a file's rows always satisfy
``window_start <= t < window_end`` and the date partition is the event date.

Finalization: close -> fsync -> sha256 -> rename ``.tmp`` -> sidecar -> manifest line.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from jevbot.core.config import SinkConfig
from jevbot.core.logging import get_logger
from jevbot.core.time import ms_to_date, now_ms
from jevbot.recorder.integrity import TMP_SUFFIX, append_manifest, fsync_path, sha256_file, write_sidecar
from jevbot.recorder.schemas import SCHEMA_VERSION, SCHEMAS, TIME_COLUMN

log = get_logger(__name__)


@dataclass
class DatasetStats:
    rows_buffered: int = 0
    rows_written: int = 0
    bytes_final: int = 0
    files_final: int = 0
    write_errors: int = 0
    late_rows: int = 0            # rows whose window was already finalized when they arrived
    late_parts: int = 0           # extra part files opened for already-finalized windows


@dataclass
class _OpenFile:
    key: int
    tmp_path: Path
    final_path: Path
    writer: pq.ParquetWriter
    rows: int = 0
    t_min: int | None = None
    t_max: int | None = None


@dataclass
class SinkStats:
    datasets: dict[str, DatasetStats] = field(default_factory=dict)

    def total(self, attr: str) -> int:
        return sum(getattr(d, attr) for d in self.datasets.values())


class SinkManager:
    MAX_OPEN_WINDOWS_PER_DATASET = 8

    def __init__(self, root: Path, cfg: SinkConfig, datasets: list[str] | None = None) -> None:
        self.root = Path(root)
        self.cfg = cfg
        self.rotate_ms = int(cfg.rotate_s * 1000)
        self.grace_ms = int(cfg.late_grace_s * 1000)
        names = datasets or list(SCHEMAS)
        unknown = set(names) - set(SCHEMAS)
        if unknown:
            raise ValueError(f"unknown datasets {sorted(unknown)}")
        self._buffers: dict[str, list[tuple[Any, ...]]] = {n: [] for n in names}
        self._time_idx = {n: SCHEMAS[n].get_field_index(TIME_COLUMN[n]) for n in names}
        self.stats = SinkStats({n: DatasetStats() for n in names})
        self._q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._thread = threading.Thread(target=self._writer_main, name="parquet-writer", daemon=True)
        self._open: dict[tuple[str, int], _OpenFile] = {}
        self._finalized: dict[str, set[int]] = {n: set() for n in names}
        self._seq = 0
        self._started = False
        self._error: BaseException | None = None
        self.last_write_ms: int = 0

    # -- loop side ------------------------------------------------------------

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._thread.start()
        self._started = True

    def append(self, dataset: str, row: tuple[Any, ...]) -> None:
        buf = self._buffers[dataset]
        buf.append(row)
        if len(buf) >= self.cfg.flush_rows:
            self._flush_one(dataset)

    def extend(self, dataset: str, rows: list[tuple[Any, ...]]) -> None:
        for r in rows:
            self.append(dataset, r)

    def buffered_rows(self) -> int:
        return sum(len(b) for b in self._buffers.values())

    def queue_depth(self) -> int:
        return self._q.qsize()

    def open_files(self) -> int:
        return len(self._open)

    def window_key(self, t: int) -> int:
        return t - (t % self.rotate_ms)

    def flush(self, now: int | None = None) -> None:
        """Queue all buffered rows and finalize windows older than ``now - grace``."""
        now = now_ms() if now is None else now
        for name in self._buffers:
            self._flush_one(name)
        self._q.put(("tick", now))

    def _flush_one(self, name: str) -> None:
        rows = self._buffers[name]
        if not rows:
            return
        self._buffers[name] = []
        ti = self._time_idx[name]
        by_window: dict[int, list[tuple[Any, ...]]] = {}
        rot = self.rotate_ms
        bad = 0
        for r in rows:
            t = r[ti]
            if type(t) is not int:
                bad += 1
                continue
            by_window.setdefault(t - t % rot, []).append(r)
        if bad:
            self.stats.datasets[name].write_errors += bad
            log.error("sink_bad_event_time", dataset=name, rows_dropped=bad)
        schema = SCHEMAS[name]
        for key in sorted(by_window):
            part = by_window[key]
            cols = list(zip(*part))
            try:
                table = pa.table([pa.array(c, type=f.type) for c, f in zip(cols, schema)], schema=schema)
            except (pa.ArrowInvalid, pa.ArrowTypeError) as e:
                self.stats.datasets[name].write_errors += 1
                log.error("sink_row_conversion_failed", dataset=name, err=repr(e), rows=len(part))
                continue
            self.stats.datasets[name].rows_buffered += len(part)
            self._q.put(("write", (name, key, table)))

    def close(self, timeout: float = 60.0) -> None:
        """Flush everything, finalize open files and stop the writer thread."""
        if not self._started:
            return
        for name in self._buffers:
            self._flush_one(name)
        self._q.put(("stop", None))
        self._thread.join(timeout)
        self._started = False
        if self._thread.is_alive():
            log.error("sink_writer_join_timeout")

    def writer_error(self) -> BaseException | None:
        return self._error

    # -- writer thread ------------------------------------------------------

    def _writer_main(self) -> None:
        while True:
            kind, payload = self._q.get()
            try:
                if kind == "write":
                    name, key, table = payload
                    self._write(name, key, table)
                elif kind == "tick":
                    self._finalize_due(payload)
                elif kind == "stop":
                    for k in sorted(self._open):
                        self._finalize(k)
                    return
            except BaseException as e:            # keep the thread alive; surface via health
                self._error = e
                log.exception("sink_writer_failed", kind=kind)

    def _path_for(self, name: str, key: int) -> tuple[Path, Path]:
        self._seq += 1
        date = ms_to_date(key)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(key / 1000))
        d = self.root / name / f"date={date}"
        d.mkdir(parents=True, exist_ok=True)
        final = d / f"{name}-{stamp}-{os.getpid()}-{self._seq}.parquet"
        return final.with_name(final.name + TMP_SUFFIX), final

    def _write(self, name: str, key: int, table: pa.Table) -> None:
        of = self._open.get((name, key))
        st = self.stats.datasets[name]
        if of is None:
            if key in self._finalized[name]:
                st.late_rows += table.num_rows
                st.late_parts += 1
            mine = sorted(k for n, k in self._open if n == name)
            if len(mine) >= self.MAX_OPEN_WINDOWS_PER_DATASET:
                self._finalize((name, mine[0]))
            tmp, final = self._path_for(name, key)
            schema = table.schema.with_metadata({"jevbot_schema": SCHEMA_VERSION, "dataset": name,
                                                 "window_start": str(key), "window_ms": str(self.rotate_ms)})
            writer = pq.ParquetWriter(str(tmp), schema, compression=self.cfg.compression,
                                      compression_level=self.cfg.compression_level)
            of = _OpenFile(key, tmp, final, writer)
            self._open[(name, key)] = of
        elif key in self._finalized[name]:
            st.late_rows += table.num_rows
        try:
            of.writer.write_table(table)
        except Exception:
            st.write_errors += 1
            raise
        of.rows += table.num_rows
        mn = pc.min(table.column(TIME_COLUMN[name])).as_py()
        mx = pc.max(table.column(TIME_COLUMN[name])).as_py()
        of.t_min = mn if of.t_min is None else min(of.t_min, mn)
        of.t_max = mx if of.t_max is None else max(of.t_max, mx)
        st.rows_written += table.num_rows
        self.last_write_ms = now_ms()

    def _finalize_due(self, now: int) -> None:
        for (name, key) in sorted(self._open):
            if key + self.rotate_ms + self.grace_ms <= now:
                self._finalize((name, key))

    def _finalize(self, k: tuple[str, int]) -> None:
        name, key = k
        of = self._open.pop(k)
        of.writer.close()
        fsync_path(of.tmp_path)
        digest = sha256_file(of.tmp_path)
        os.replace(of.tmp_path, of.final_path)
        write_sidecar(of.final_path, digest)
        fsync_path(of.final_path.parent)
        size = of.final_path.stat().st_size
        dataset_dir = self.root / name
        append_manifest(dataset_dir, {
            "file": str(of.final_path.relative_to(dataset_dir)), "rows": of.rows, "bytes": size,
            "sha256": digest, "t_min": of.t_min, "t_max": of.t_max, "window_start": key,
            "window_end": key + self.rotate_ms, "schema": SCHEMA_VERSION, "finalized_at": now_ms(),
        })
        fin = self._finalized[name]
        fin.add(key)
        if len(fin) > 10_000:
            for old in sorted(fin)[:5_000]:
                fin.discard(old)
        st = self.stats.datasets[name]
        st.files_final += 1
        st.bytes_final += size
        log.debug("sink_file_finalized", dataset=name, rows=of.rows, bytes=size, file=of.final_path.name)
