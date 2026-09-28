"""Fetch Hyperliquid portfolio histories for the hlp.v1 sample (public API, no key; resumable).

Sample (pre-registered): random 3,000 of accounts with allTime volume >= $1M (seed 20260929) + top 500 by allTime PnL.
Output: data/hl/sample.json, data/hl/portfolio.jsonl (one line per address).
Usage: python scripts/hl_fetch.py [--rate 2.0]
"""

from __future__ import annotations

import argparse
import asyncio
import random
from pathlib import Path

import httpx
import orjson

OUT = Path("data/hl")


def sample() -> dict[str, list[str]]:
    rows = orjson.loads((OUT / "leaderboard.json").read_bytes())["leaderboardRows"]

    def win(r: dict, k: str) -> dict:
        return dict(r["windowPerformances"]).get(k, {})
    active = [r for r in rows if float(win(r, "allTime").get("vlm") or 0) >= 1e6]
    rnd = random.Random(20260929).sample(active, min(3000, len(active)))
    top = sorted(rows, key=lambda r: -float(win(r, "allTime").get("pnl") or 0))[:500]
    s = {"random": [r["ethAddress"] for r in rnd], "top_pnl": [r["ethAddress"] for r in top],
         "frame": {"accounts": len(rows), "active_vlm_1m": len(active)}}
    (OUT / "sample.json").write_bytes(orjson.dumps(s))
    return s


async def main(rate: float) -> None:
    s = sample() if not (OUT / "sample.json").exists() else orjson.loads((OUT / "sample.json").read_bytes())
    addrs = list(dict.fromkeys(s["random"] + s["top_pnl"]))
    path = OUT / "portfolio.jsonl"
    done = set()
    if path.exists():
        done = {orjson.loads(line)["user"] for line in path.read_bytes().splitlines() if line.strip()}
    todo = [a for a in addrs if a not in done]
    print(f"sample {len(addrs)}, done {len(done)}, todo {len(todo)}", flush=True)
    async with httpx.AsyncClient(timeout=60) as c:
        with open(path, "ab") as fh:
            for i, a in enumerate(todo):
                for attempt in range(5):
                    try:
                        r = await c.post("https://api.hyperliquid.xyz/info", json={"type": "portfolio", "user": a})
                        if r.status_code == 429:
                            await asyncio.sleep(10 * (attempt + 1))
                            continue
                        r.raise_for_status()
                        keep = {k: v for k, v in r.json() if k in ("perpAllTime", "allTime")}
                        fh.write(orjson.dumps({"user": a, "portfolio": keep}) + b"\n")
                        fh.flush()
                        break
                    except (httpx.HTTPError, ValueError):
                        await asyncio.sleep(5 * (attempt + 1))
                await asyncio.sleep(1.0 / rate)
                if i % 250 == 0:
                    print(f"{i}/{len(todo)}", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=2.0)
    asyncio.run(main(ap.parse_args().rate))
