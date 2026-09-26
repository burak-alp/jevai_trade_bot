"""Scaling properties of the historical downloader: bounded jobs, streaming, bounded memory."""

import asyncio
import hashlib
import io
import threading
import time
import zipfile
from datetime import date

import httpx
import psutil
import pyarrow.parquet as pq

from jevbot.core.config import HistConfig
from jevbot.hist.binance_vision import BinanceVision


async def test_bounded_inflight_and_lazy_job_generation(tmp_path):
    seen = {"n": 0}

    async def handler(req):
        seen["n"] += 1
        await asyncio.sleep(0)
        return httpx.Response(404)

    cfg = HistConfig(base_url="https://vision.test", concurrency=4, max_retries=0)
    bv = BinanceVision(cfg, tmp_path, transport=httpx.MockTransport(handler))
    symbols = [f"S{i}USDT" for i in range(200)]
    max_tasks = 0
    done = asyncio.Event()

    async def sampler():
        nonlocal max_tasks
        while not done.is_set():
            max_tasks = max(max_tasks, len(asyncio.all_tasks()))
            await asyncio.sleep(0.001)

    s = asyncio.create_task(sampler())
    st = await bv.download("klines", symbols, date(2024, 1, 1), date(2024, 1, 20), interval="1m")
    done.set()
    await s
    await bv.aclose()
    assert st.jobs == 4000 and st.missing == 4000
    assert bv.max_inflight <= 4
    assert max_tasks <= 4 + 1 + 3          # workers + producer + gather/test/sampler, not ~4000


def _big_aggtrades_zip(n_rows: int) -> tuple[bytes, int]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        with zf.open("BTCUSDT-aggTrades-2024-01-01.csv", "w") as fh:
            fh.write(b"agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n")
            t0 = 1704067200000
            chunk = []
            for i in range(n_rows):
                chunk.append(f"{i},{42000 + (i % 997) * 0.1:.1f},{0.001 * (1 + i % 50):.3f},{2*i},{2*i+1},"
                             f"{t0 + i // 20},{'true' if i % 3 else 'false'}\n")
                if len(chunk) == 50_000:
                    fh.write("".join(chunk).encode())
                    chunk = []
            fh.write("".join(chunk).encode())
        csv_size = zf.getinfo("BTCUSDT-aggTrades-2024-01-01.csv").file_size
    return buf.getvalue(), csv_size


_MEASURE = r"""
import asyncio, hashlib, sys, threading, time
from datetime import date
from pathlib import Path
import httpx, psutil, pyarrow.parquet as pq
sys.path.insert(0, sys.argv[3])
from test_hist_streaming import _big_aggtrades_zip
from jevbot.core.config import HistConfig
from jevbot.hist.binance_vision import BinanceVision

n_rows, out_root = int(sys.argv[1]), Path(sys.argv[2])
zbytes, csv_size = _big_aggtrades_zip(n_rows)
digest = hashlib.sha256(zbytes).hexdigest()

async def body():
    for i in range(0, len(zbytes), 256 << 10):
        yield zbytes[i:i + (256 << 10)]

def handler(req):
    if req.url.path.endswith(".CHECKSUM"):
        return httpx.Response(200, text=digest)
    return httpx.Response(200, content=body())

proc = psutil.Process(); peak = [0]; stop = threading.Event()
def sample():
    while not stop.is_set():
        peak[0] = max(peak[0], proc.memory_info().rss); time.sleep(0.003)
base = proc.memory_info().rss
th = threading.Thread(target=sample, daemon=True); th.start()

async def main():
    bv = BinanceVision(HistConfig(base_url="https://vision.test", concurrency=1), out_root,
                       transport=httpx.MockTransport(handler))
    st = await bv.download("aggTrades", ["BTCUSDT"], date(2024, 1, 1), date(2024, 1, 1))
    await bv.aclose()
    return st
st = asyncio.run(main())
stop.set(); th.join()
out = out_root / "aggTrades" / "symbol=BTCUSDT" / "BTCUSDT-aggTrades-2024-01-01.parquet"
md = pq.read_metadata(out)
assert st.ok == 1 and st.rows == n_rows == md.num_rows and md.num_row_groups > 1, st
assert not list((out_root / "_tmp").iterdir())
print(peak[0] - base, csv_size)
"""


def _measure_in_fresh_process(tmp_path, n_rows):
    import subprocess
    import sys
    from pathlib import Path

    here = str(Path(__file__).parent)
    r = subprocess.run([sys.executable, "-c", _MEASURE, str(n_rows), str(tmp_path / f"o{n_rows}"), here],
                       capture_output=True, text=True, timeout=240)
    assert r.returncode == 0, r.stderr[-2000:]
    growth, csv_size = map(int, r.stdout.split())
    return growth, csv_size


def test_large_aggtrades_streamed_with_bounded_memory(tmp_path):
    """Peak RSS must not scale with the file: doubling the csv (~80 -> ~160 MB) adds only a small delta.

    Each size runs in a fresh process so allocator/thread-pool warm-up (a fixed, one-off cost)
    is identical for both measurements.
    """
    g1, csv1 = _measure_in_fresh_process(tmp_path, 1_500_000)
    g2, csv2 = _measure_in_fresh_process(tmp_path, 3_000_000)
    assert g2 - g1 < 0.25 * (csv2 - csv1), (g1 / 2**20, g2 / 2**20, csv1 / 2**20, csv2 / 2**20)
    assert g2 < csv2, (g2 / 2**20, csv2 / 2**20)     # below the size an in-memory parse would need


async def test_interrupted_stream_is_retried(tmp_path):
    good = io.BytesIO()
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("x.csv", "1704067200000,1,1,1,1,1,1704067259999,1,1,1,1,0\n")
    good = good.getvalue()
    calls = {"n": 0}

    async def broken():
        yield good[:10]
        raise httpx.ReadError("connection reset")

    async def whole():
        yield good

    def handler(req):
        if req.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=hashlib.sha256(good).hexdigest())
        calls["n"] += 1
        return httpx.Response(200, content=broken() if calls["n"] == 1 else whole())

    import jevbot.hist.binance_vision as mod
    bv = BinanceVision(HistConfig(base_url="https://vision.test", max_retries=2), tmp_path,
                       transport=httpx.MockTransport(handler))

    async def no_wait(attempt):
        return None
    bv._backoff = no_wait
    st = await bv.download("klines", ["BTCUSDT"], date(2024, 1, 1), date(2024, 1, 1), interval="1m")
    await bv.aclose()
    assert st.ok == 1 and calls["n"] == 2 and mod is not None
