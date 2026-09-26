import pyarrow.parquet as pq

from jevbot.core.config import SinkConfig
from jevbot.recorder.integrity import quarantine_orphans, read_manifest, verify_tree
from jevbot.core.time import ms_to_date
from jevbot.recorder.sinks import SinkManager

ROW = ("BTCUSDT", 0, 59_999, 1.0, 2.0, 0.5, 1.5, 10.0, 15.0, 7, 6.0, 9.0, 60_000, 60_010, "ws")


W = 60_000                                      # rotate_s=60 in these tests
BASE = 1_800_000_000_000 - 1_800_000_000_000 % W


def kl(open_time, source="ws"):
    return ("BTCUSDT", open_time, open_time + 59_999) + ROW[3:12] + (open_time + 60_000, open_time + 60_010, source)


def files_of(root, ds="kline_1m"):
    return sorted((root / ds).rglob("*.parquet"))


def check_window_consistency(root, ds="kline_1m", tcol="open_time"):
    man = read_manifest(root / ds)
    for f in files_of(root, ds):
        e = man[str(f.relative_to(root / ds))]
        t = pq.read_table(f).column(tcol).to_pylist()
        assert e["window_start"] <= min(t) and max(t) < e["window_end"], (e, min(t), max(t))
        assert e["t_min"] == min(t) and e["t_max"] == max(t)
        assert f.parent.name == "date=" + ms_to_date(e["window_start"])
    return man


def test_rotation_checksum_manifest(tmp_path):
    sm = SinkManager(tmp_path, SinkConfig(rotate_s=60, late_grace_s=5, flush_rows=10_000),
                     datasets=["kline_1m", "gaps"])
    sm.start()
    for i in range(5):
        sm.append("kline_1m", kl(BASE + 1000 * i))
    sm.flush(BASE + 10_000)
    for i in range(3):
        sm.append("kline_1m", kl(BASE + W + 1000 * i))
    sm.flush(BASE + W + 10_000)                 # window BASE ends at BASE+W, grace 5 s -> finalized
    sm.close()
    files = files_of(tmp_path)
    assert len(files) == 2
    assert sorted(pq.read_metadata(f).num_rows for f in files) == [3, 5]
    man = check_window_consistency(tmp_path)
    assert sum(e["rows"] for e in man.values()) == 8
    rep = verify_tree(tmp_path)
    assert rep.ok and rep.files_ok == 2 and rep.rows_ok == 8
    assert sm.stats.datasets["kline_1m"].rows_written == 8
    assert pq.read_schema(files[0]).metadata[b"jevbot_schema"] == b"rec.v1"


def test_window_boundary_split_in_one_flush(tmp_path):
    sm = SinkManager(tmp_path, SinkConfig(rotate_s=60, late_grace_s=5), datasets=["kline_1m"])
    sm.start()
    for t in (BASE + W - 2, BASE + W - 1, BASE + W, BASE + W + 1):
        sm.append("kline_1m", kl(t))
    sm.flush(BASE + W + 1)                      # one batch spanning two windows
    sm.close()
    files = files_of(tmp_path)
    assert len(files) == 2
    man = check_window_consistency(tmp_path)
    assert sorted((e["window_start"], e["rows"]) for e in man.values()) == [(BASE, 2), (BASE + W, 2)]


def test_late_event_goes_to_its_event_time_window(tmp_path):
    sm = SinkManager(tmp_path, SinkConfig(rotate_s=60, late_grace_s=5), datasets=["kline_1m"])
    sm.start()
    sm.append("kline_1m", kl(BASE + 100))
    sm.flush(BASE + 1000)
    sm.flush(BASE + W + 6000)                   # window BASE finalized
    sm.append("kline_1m", kl(BASE + 200))       # late event for the finalized window
    sm.append("kline_1m", kl(BASE + 2 * W + 1)) # on-time event in a newer window
    sm.flush(BASE + 2 * W + 2000)
    sm.close()
    man = check_window_consistency(tmp_path)
    windows = sorted(e["window_start"] for e in man.values())
    assert windows == [BASE, BASE, BASE + 2 * W]      # late row -> extra part of its own window
    st = sm.stats.datasets["kline_1m"]
    assert st.late_rows == 1 and st.late_parts == 1


def test_rest_backfill_old_rows_land_in_old_partition(tmp_path):
    sm = SinkManager(tmp_path, SinkConfig(rotate_s=60, late_grace_s=5), datasets=["kline_1m"])
    sm.start()
    now = BASE + 3 * 86_400_000                  # three days later
    sm.append("kline_1m", kl(now - 5000))
    rows = [kl(BASE + i * W, source="rest") for i in range(3)]   # backfill spanning three windows
    sm.extend("kline_1m", rows)
    sm.flush(now)
    sm.close()
    man = check_window_consistency(tmp_path)
    assert sorted(e["window_start"] for e in man.values()) == [BASE, BASE + W, BASE + 2 * W, now - 5000 - (now - 5000) % W]
    old = [f for f in files_of(tmp_path) if "date=" + ms_to_date(BASE) in str(f)]
    assert sum(pq.read_metadata(f).num_rows for f in old) == 3


def test_open_windows_bounded(tmp_path):
    sm = SinkManager(tmp_path, SinkConfig(rotate_s=60, late_grace_s=5), datasets=["kline_1m"])
    sm.start()
    for i in range(20):                          # 20 distinct old windows in one flush
        sm.append("kline_1m", kl(BASE + i * W))
    sm.flush(BASE + 20 * W)
    import time
    time.sleep(0.3)
    assert sm.open_files() <= SinkManager.MAX_OPEN_WINDOWS_PER_DATASET
    sm.close()
    check_window_consistency(tmp_path)
    assert len(files_of(tmp_path)) == 20


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
    sm.append("kline_1m", ROW)                 # same batch: only the bad row is dropped
    sm.flush()
    sm.append("kline_1m", ("BTCUSDT", 0, "x") + ROW[3:])   # conversion error of a whole window batch
    sm.close()
    assert sm.stats.datasets["kline_1m"].write_errors == 2
    assert sm.stats.datasets["kline_1m"].rows_written == 1
