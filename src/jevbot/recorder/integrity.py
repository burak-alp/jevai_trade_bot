"""File integrity: sha256 sidecars, per-dataset manifests, verification, orphan quarantine.

Layout written by the sink::

    <root>/<dataset>/date=YYYY-MM-DD/<dataset>-<YYYYmmddTHHMMSS>-<n>.parquet
    <root>/<dataset>/date=YYYY-MM-DD/<...>.parquet.sha256     ("<hex>  <name>")
    <root>/<dataset>/_manifest.jsonl                           one line per finalized file

A ``.parquet.tmp`` file is a file that was open when the process died (no footer,
unreadable). It is moved to ``<data_dir>/quarantine/`` on startup.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import orjson
import pyarrow.parquet as pq

TMP_SUFFIX = ".tmp"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def write_sidecar(path: Path, digest: str) -> Path:
    side = path.with_name(path.name + ".sha256")
    side.write_text(f"{digest}  {path.name}\n")
    return side


def read_sidecar(path: Path) -> str | None:
    side = path.with_name(path.name + ".sha256")
    if not side.exists():
        return None
    parts = side.read_text().split()
    return parts[0] if parts else None


def fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append_manifest(dataset_dir: Path, entry: dict[str, Any]) -> None:
    with open(dataset_dir / "_manifest.jsonl", "ab") as fh:
        fh.write(orjson.dumps(entry) + b"\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_manifest(dataset_dir: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    mf = dataset_dir / "_manifest.jsonl"
    if not mf.exists():
        return out
    for line in mf.read_bytes().splitlines():
        if not line.strip():
            continue
        try:
            e = orjson.loads(line)
        except orjson.JSONDecodeError:
            continue            # torn last line after a crash
        if e.get("deleted"):    # tombstone written by compaction
            out.pop(e["file"], None)
        else:
            out[e["file"]] = e
    return out


def quarantine_orphans(root: Path, quarantine_dir: Path) -> list[Path]:
    moved: list[Path] = []
    if not root.exists():
        return moved
    for p in sorted(root.rglob(f"*{TMP_SUFFIX}")):
        dest = quarantine_dir / p.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest))
        moved.append(dest)
    return moved


@dataclass
class VerifyReport:
    files_ok: int = 0
    rows_ok: int = 0
    bytes_ok: int = 0
    checksum_mismatch: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    missing_sidecar: list[str] = field(default_factory=list)
    row_count_mismatch: list[str] = field(default_factory=list)
    orphans_tmp: list[str] = field(default_factory=list)
    manifest_missing_file: list[str] = field(default_factory=list)
    not_in_manifest: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.checksum_mismatch or self.unreadable or self.missing_sidecar
                    or self.row_count_mismatch or self.manifest_missing_file)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["ok"] = self.ok
        return d


def verify_tree(root: Path) -> VerifyReport:
    """Verify every finalized Parquet file below ``root`` (a dataset root such as data/raw)."""
    rep = VerifyReport()
    if not root.exists():
        return rep
    manifests: dict[Path, dict[str, dict[str, Any]]] = {}
    for mf in root.rglob("_manifest.jsonl"):
        manifests[mf.parent] = read_manifest(mf.parent)
    seen: set[tuple[Path, str]] = set()
    for p in sorted([*root.rglob("*.parquet"), *root.rglob("*.json.gz")]):
        rel = str(p.relative_to(root))
        dataset_dir = next((d for d in manifests if d in p.parents), None)
        man_key = str(p.relative_to(dataset_dir)) if dataset_dir else None
        expected = read_sidecar(p)
        if expected is None:
            rep.missing_sidecar.append(rel)
            continue
        if sha256_file(p) != expected:
            rep.checksum_mismatch.append(rel)
            continue
        if p.name.endswith(".json.gz"):
            rep.files_ok += 1
            rep.bytes_ok += p.stat().st_size
            if dataset_dir is not None:
                seen.add((dataset_dir, man_key))
            continue
        try:
            md = pq.read_metadata(p)
        except Exception:
            rep.unreadable.append(rel)
            continue
        entry = manifests.get(dataset_dir, {}).get(man_key) if dataset_dir else None
        if entry is None:
            rep.not_in_manifest.append(rel)
        else:
            seen.add((dataset_dir, man_key))
            if entry.get("rows") != md.num_rows or entry.get("sha256") != expected:
                rep.row_count_mismatch.append(rel)
                continue
        rep.files_ok += 1
        rep.rows_ok += md.num_rows
        rep.bytes_ok += p.stat().st_size
    for d, entries in manifests.items():
        for name in entries:
            if (d, name) not in seen and not (d / name).exists():
                rep.manifest_missing_file.append(str((d / name).relative_to(root)))
    rep.orphans_tmp = [str(p.relative_to(root)) for p in root.rglob(f"*{TMP_SUFFIX}")]
    return rep
