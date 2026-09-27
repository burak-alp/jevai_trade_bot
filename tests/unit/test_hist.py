import hashlib
import io
import zipfile
from datetime import date

import httpx
import pyarrow.parquet as pq

from jevbot.core.config import HistConfig
from jevbot.hist.binance_vision import DATASETS, BinanceVision, file_key, months, parse_csv
from jevbot.recorder.integrity import verify_tree

KL_HDR = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n"
KL_ROW = "1704067200000,42000.1,42010,41990,42005,12.5,1704067259999,525000,300,6.1,256000,0\n"


def zipped(name, text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, text)
    return buf.getvalue()


def test_file_keys_and_months():
    assert file_key(DATASETS["klines"], "BTCUSDT", "2024-01-01", "daily", "1m") == \
        "daily/klines/BTCUSDT/1m/BTCUSDT-1m-2024-01-01.zip"
    assert file_key(DATASETS["fundingRate"], "BTCUSDT", "2024-01", "monthly", None) == \
        "monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2024-01.zip"
    assert months(date(2023, 11, 5), date(2024, 2, 1)) == ["2023-11", "2023-12", "2024-01", "2024-02"]


def test_parse_csv_header_and_no_header_and_micros():
    t1 = parse_csv(DATASETS["klines"], (KL_HDR + KL_ROW).encode())
    t2 = parse_csv(DATASETS["klines"], KL_ROW.encode())
    assert t1.equals(t2) and t1.num_rows == 1 and "ignore" not in t1.column_names
    micro = KL_ROW.replace("1704067200000", "1704067200000000").replace("1704067259999", "1704067259999000")
    t3 = parse_csv(DATASETS["klines"], micro.encode())
    assert t3.column("open_time")[0].as_py() == 1704067200000
    m = parse_csv(DATASETS["metrics"], b"create_time,symbol,sum_open_interest,sum_open_interest_value,"
                  b"count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,count_long_short_ratio,"
                  b"sum_taker_long_short_vol_ratio\n2024-01-01 00:05:00,BTCUSDT,1,2,3,4,5,6\n")
    assert m.column("create_time")[0].as_py() == 1704067500000


async def test_download_verify_missing_and_bad_checksum(tmp_path):
    good = zipped("BTCUSDT-1m-2024-01-01.csv", KL_HDR + KL_ROW)
    bad = zipped("ETHUSDT-1m-2024-01-01.csv", KL_ROW)
    flaky = {"n": 0}

    def handler(req):
        p = req.url.path
        if p.endswith("BTCUSDT-1m-2024-01-01.zip"):
            flaky["n"] += 1
            if flaky["n"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, content=good)
        if p.endswith("BTCUSDT-1m-2024-01-01.zip.CHECKSUM"):
            return httpx.Response(200, text=f"{hashlib.sha256(good).hexdigest()}  BTCUSDT-1m-2024-01-01.zip\n")
        if p.endswith("ETHUSDT-1m-2024-01-01.zip"):
            return httpx.Response(200, content=bad)
        if p.endswith("ETHUSDT-1m-2024-01-01.zip.CHECKSUM"):
            return httpx.Response(200, text="0" * 64 + "  x\n")
        return httpx.Response(404)

    cfg = HistConfig(base_url="https://vision.test", max_retries=2, concurrency=2)
    bv = BinanceVision(cfg, tmp_path, transport=httpx.MockTransport(handler))
    import jevbot.hist.binance_vision as mod
    orig = mod.asyncio.sleep

    async def fast_sleep(_):
        await orig(0)
    mod.asyncio.sleep = fast_sleep
    try:
        st = await bv.download("klines", ["BTCUSDT", "ETHUSDT"], date(2024, 1, 1), date(2024, 1, 2), interval="1m")
    finally:
        mod.asyncio.sleep = orig
    assert (st.ok, st.missing, st.failed) == (1, 2, 1)
    assert "ChecksumMismatch" in st.failures[0]
    f = tmp_path / "klines" / "1m" / "symbol=BTCUSDT" / "BTCUSDT-klines-1m-2024-01-01.parquet"
    t = pq.read_table(f)
    assert t.num_rows == 1 and t.column("close")[0].as_py() == 42005.0
    assert verify_tree(tmp_path).ok
    st2 = await bv.download("klines", ["BTCUSDT"], date(2024, 1, 1), date(2024, 1, 1), interval="1m")
    assert st2.skipped_existing == 1
    missing = (tmp_path / "klines" / "1m" / "_missing.jsonl").read_text().splitlines()
    assert len(missing) == 2
    await bv.aclose()


async def test_listing_and_pit(tmp_path):
    ns = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'

    def handler(req):
        prefix = req.url.params.get("prefix")
        if req.url.params.get("delimiter") and prefix.endswith("daily/klines/"):
            if req.url.params.get("marker"):
                body = (f'<ListBucketResult {ns}><Prefix>{prefix}</Prefix><IsTruncated>false</IsTruncated>'
                        f'<CommonPrefixes><Prefix>{prefix}OLDUSDT/</Prefix></CommonPrefixes></ListBucketResult>')
            else:
                body = (f'<ListBucketResult {ns}><Prefix>{prefix}</Prefix><IsTruncated>true</IsTruncated>'
                        f'<NextMarker>{prefix}BTCUSDT/</NextMarker>'
                        f'<CommonPrefixes><Prefix>{prefix}BTCUSDT/</Prefix></CommonPrefixes></ListBucketResult>')
            return httpx.Response(200, text=body)
        if prefix.endswith("OLDUSDT/1m/"):
            keys = "".join(f"<Contents><Key>{prefix}OLDUSDT-1m-2021-03-0{d}.zip</Key></Contents>"
                           f"<Contents><Key>{prefix}OLDUSDT-1m-2021-03-0{d}.zip.CHECKSUM</Key></Contents>"
                           for d in (1, 2, 4))
            return httpx.Response(200, text=f'<ListBucketResult {ns}><IsTruncated>false</IsTruncated>{keys}</ListBucketResult>')
        return httpx.Response(200, text=f'<ListBucketResult {ns}><IsTruncated>false</IsTruncated></ListBucketResult>')

    bv = BinanceVision(HistConfig(), tmp_path, transport=httpx.MockTransport(handler))
    assert await bv.list_symbols() == ["BTCUSDT", "OLDUSDT"]
    t = await bv.pit_listing("1m", ["OLDUSDT", "BTCUSDT"])
    assert t.to_pylist() == [{"symbol": "OLDUSDT", "first_date": "2021-03-01", "last_date": "2021-03-04",
                              "n_days": 3, "n_missing_days": 1}]
    await bv.aclose()


def test_chunked_conversion_matches_whole_file(monkeypatch):
    import jevbot.hist.binance_vision as mod
    rows = "".join(f"{1704067200000 + i * 60000},{i}.5,{i + 1},{i},{i}.7,{i},{1704067259999 + i * 60000},"
                   f"{i * 2},{i},{i}.1,{i}.2,0\n" for i in range(997))
    whole = parse_csv(DATASETS["klines"], (KL_HDR + rows).encode())
    monkeypatch.setattr(mod, "CSV_BLOCK_BYTES", 333)          # forces many line-split chunks
    chunked = parse_csv(DATASETS["klines"], (KL_HDR + rows).encode())
    assert chunked.num_rows == 997 and chunked.equals(whole)
    no_trailing_newline = parse_csv(DATASETS["klines"], (KL_HDR + rows.rstrip("\n")).encode())
    assert no_trailing_newline.equals(whole)


async def test_transient_windows_file_locks_do_not_stop_download(tmp_path, monkeypatch):
    """AV/indexer locks: os.replace retried; a temp file that stays locked never crashes the run."""
    import os
    from pathlib import Path

    import jevbot.hist.binance_vision as mod

    good = zipped("BTCUSDT-1m-2024-01-01.csv", KL_HDR + KL_ROW)

    def handler(req):
        p = req.url.path
        if p.endswith(".zip"):
            return httpx.Response(200, content=good)
        if p.endswith(".zip.CHECKSUM"):
            return httpx.Response(200, text=f"{hashlib.sha256(good).hexdigest()}  x.zip\n")
        return httpx.Response(404)

    fails = {"replace": 2}
    real_replace, real_unlink = os.replace, Path.unlink

    def flaky_replace(a, b):
        if fails["replace"] > 0:
            fails["replace"] -= 1
            raise PermissionError(32, "being used by another process")
        return real_replace(a, b)

    def locked_unlink(self, *a, **k):
        if self.name.endswith(".part"):
            raise PermissionError(32, "being used by another process")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(mod.os, "replace", flaky_replace)
    monkeypatch.setattr(Path, "unlink", locked_unlink)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    bv = BinanceVision(HistConfig(base_url="https://vision.test", concurrency=1), tmp_path,
                       transport=httpx.MockTransport(handler))
    st = await bv.download("klines", ["BTCUSDT", "ETHUSDT"], date(2024, 1, 1), date(2024, 1, 1), interval="1m")
    await bv.aclose()
    assert (st.ok, st.failed) == (2, 0) and fails["replace"] == 0
