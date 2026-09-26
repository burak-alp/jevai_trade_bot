import orjson
import pyarrow.parquet as pq

from jevbot.core.config import SinkConfig
from jevbot.core.time import HOUR_MS
from jevbot.recorder.compact import compact_dataset
from jevbot.recorder.integrity import read_manifest, verify_tree
from jevbot.recorder.sinks import SinkManager

H0 = 1_800_000_000_000 - 1_800_000_000_000 % HOUR_MS
M = 60_000


def book_row(sym, t):
    return (sym, t, 1.0, 1.1, 0.9, 1.0, 2.0, 1.0, 3.0, 0.99, 1.01, 5.0, 6.0, 3, 7, t, t + 5)


def make_parts(root, hours=2, per_window=5):
    sm = SinkManager(root, SinkConfig(rotate_s=180, late_grace_s=5), datasets=["book_1s"])
    sm.start()
    for h in range(hours):
        for w in range(20):                                   # 20 windows of 3 min per hour
            base = H0 + h * HOUR_MS + w * 3 * M
            for i in range(per_window):
                for sym in ("ETHUSDT", "BTCUSDT"):          # time-interleaved symbols
                    sm.append("book_1s", book_row(sym, base + i * 1000))
            sm.flush(base + 3 * M + 10_000)
    sm.close()


def parts(root):
    return sorted((root / "book_1s").rglob("*.parquet"))


def test_compaction_merges_sorts_and_tombstones(tmp_path):
    make_parts(tmp_path)
    assert len(parts(tmp_path)) == 40
    res = compact_dataset(tmp_path, "book_1s", now=H0 + 3 * HOUR_MS)
    assert (res.groups_compacted, res.files_in, res.files_out, res.rows) == (2, 40, 2, 400)
    files = parts(tmp_path)
    assert len(files) == 2
    t = pq.read_table(files[0])
    keys = list(zip(t.column("symbol").to_pylist(), t.column("t_sec").to_pylist()))
    assert keys == sorted(keys)                               # sorted by (symbol, time)
    man = read_manifest(tmp_path / "book_1s")
    assert len(man) == 2 and all(len(e["compacted_from"]) == 20 for e in man.values())
    rep = verify_tree(tmp_path)
    assert rep.ok and rep.rows_ok == 400 and not rep.manifest_missing_file
    assert not list((tmp_path / "book_1s" / "_compaction").glob("*.json"))


def test_recent_group_not_compacted(tmp_path):
    make_parts(tmp_path, hours=1)
    res = compact_dataset(tmp_path, "book_1s", now=H0 + HOUR_MS + 60_000, grace_ms=5 * M)
    assert res.groups_compacted == 0 and len(parts(tmp_path)) == 20


def test_unverified_source_is_kept_and_excluded(tmp_path):
    make_parts(tmp_path, hours=1)
    victim = parts(tmp_path)[3]
    data = bytearray(victim.read_bytes())
    data[20] ^= 0xFF
    victim.write_bytes(bytes(data))
    res = compact_dataset(tmp_path, "book_1s", now=H0 + 2 * HOUR_MS)
    assert len(res.skipped_unverified) == 1 and "checksum_mismatch" in res.skipped_unverified[0]
    assert victim.exists()                                    # never deleted
    assert res.files_in == 19 and res.rows == 190


def test_recovery_of_writing_and_committed_journals(tmp_path):
    make_parts(tmp_path, hours=1)
    ds = tmp_path / "book_1s"
    srcs = [str(p.relative_to(ds)) for p in parts(tmp_path)]
    jdir = ds / "_compaction"
    jdir.mkdir()
    # crash during writing: a half-written target exists, sources intact
    target = ds / "date=x" / "t.parquet"
    target.parent.mkdir()
    target.write_bytes(b"partial")
    (jdir / "a.json").write_bytes(orjson.dumps({"state": "writing", "target": "date=x/t.parquet",
                                                "sources": [{"file": s} for s in srcs]}))
    res = compact_dataset(tmp_path, "book_1s", now=H0 + 2 * HOUR_MS, dry_run=True)
    assert not target.exists() and res.recovered == ["a.json:writing"]
    assert len(parts(tmp_path)) == 20
    # crash after commit: remaining sources are deleted on recovery
    (jdir / "b.json").write_bytes(orjson.dumps({"state": "committed", "target": "date=x/t2.parquet",
                                                "sources": [{"file": s} for s in srcs[:2]]}))
    res = compact_dataset(tmp_path, "book_1s", now=H0 + 2 * HOUR_MS, dry_run=True)
    assert res.recovered == ["b.json:committed"]
    assert len(parts(tmp_path)) == 18
    assert srcs[0] not in read_manifest(ds)
