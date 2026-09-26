"""Manifest-driven file listing for historical datasets (what replay should read)."""

from __future__ import annotations

from pathlib import Path

from jevbot.recorder.integrity import read_manifest


def list_files(dataset_root: Path, *, symbols: set[str] | None = None, include_suspect: bool = False,
               include_warn: bool = True) -> list[Path]:
    """Finalized files of one dataset root (e.g. ``data/hist/um/klines/1m``) per manifest.

    Suspect files are excluded unless explicitly requested; files without a quality field
    (older downloads) count as ``ok``.
    """
    out: list[Path] = []
    for rel, e in sorted(read_manifest(Path(dataset_root)).items()):
        q = e.get("quality", "ok")
        if q == "suspect" and not include_suspect:
            continue
        if q == "warn" and not include_warn:
            continue
        if symbols is not None and e.get("symbol") not in symbols:
            continue
        p = Path(dataset_root) / rel
        if p.exists():
            out.append(p)
    return out
