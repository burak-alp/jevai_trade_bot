import pyarrow.parquet as pq

from jevbot.core.config import SinkConfig
from jevbot.recorder.integrity import quarantine_orphans, read_manifest, verify_tree
from jevbot.recorder.sinks import SinkManager

ROW = ("BTCUSDT", 0, 59_999, 1.0, 2.0, 0.5, 1.5, 10.0, 15.0, 7, 6.0, 9.0, 60_000, 60_010, "ws")


def test_rotation_checksum_manifest(tmp_path):
    sm = SinkManager(tmp_path, SinkConfig(rotate_s=60, flush_rows=10_000), datasets=["kline_1m", "gaps"])
    sm.start()
    base = 1_800_000_000_000 - 1_800_000_000_000 % 60_000
    for i in range(5):
        sm.append("kline_1m", ROW)
    sm.flush(base + 1)
    for i in range(3):
        sm.append("kline_1m", ROW)
    sm.flush(base + 61_000)                       # new window -> first file finalized
    sm.close()
    files = sorted((tmp_path / "kline_1m").rglob("*.parquet"))
    assert len(files) == 2
    assert sorted(pq.read_metadata(f).num_rows for f in files) == [3, 5]
    man = read_manifest(tmp_path / "kline_1m")
    assert sum(e["rows"] for e in man.values()) == 8
    rep = verify_tree(tmp_path)
    assert rep.ok and rep.files_ok == 2 and rep.rows_ok == 8
    assert sm.stats.datasets["kline_1m"].rows_written == 8
    assert pq.read_schema(files[0]).metadata[b"jevbot_schema"] == b"rec.v1"


def test_corruption_detected(tmp_path):
    sm = SinkManager(tmp_path, SinkConfig(), datasets=["kline_1m"])
    sm.start()
    sm.append("kline_1m", ROW)
    sm.close()
    f = next((tmp_path / "kline_1m").rglob("*.parquet"))
    data = bytearray(f.read_bytes())
    data[10] ^= 0xFF
    f.write_bytes(bytes(data))
    rep = verify_tree(tmp_path)
    assert not rep.ok and rep.checksum_mismatch


def test_orphan_quarantine(tmp_path):
    d = tmp_path / "raw" / "kline_1m" / "date=2026-01-01"
    d.mkdir(parents=True)
    (d / "x.parquet.tmp").write_bytes(b"PAR1partial")
    moved = quarantine_orphans(tmp_path / "raw", tmp_path / "quarantine")
    assert len(moved) == 1 and moved[0].exists() and not (d / "x.parquet.tmp").exists()


def test_bad_row_does_not_kill_writer(tmp_path):
    sm = SinkManager(tmp_path, SinkConfig(), datasets=["kline_1m"])
    sm.start()
    sm.append("kline_1m", ("BTCUSDT", "not-an-int") + ROW[2:])
    sm.flush()
    sm.append("kline_1m", ROW)
    sm.close()
    assert sm.stats.datasets["kline_1m"].write_errors == 1
    assert sm.stats.datasets["kline_1m"].rows_written == 1
