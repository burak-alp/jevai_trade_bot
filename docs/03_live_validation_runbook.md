# 03 — Live validation runbook (real Binance, public data only)

Sprint 1 starts only after **(1) `jevbot smoke` passes against real Binance** and **(2) a clean ≥ 1 h
recorder run** (then a 24 h soak). No API key, no orders, no Jev calls anywhere in these steps.

## 0. Machine

- Linux VPS/PC with outbound access to `fapi.binance.com`, `fstream.binance.com` (and `data.binance.vision`,
  `s3-ap-northeast-1.amazonaws.com` for the downloader). Binance must be legally accessible from the host's
  jurisdiction.
- Python ≥ 3.11, ≥ 2 vCPU, ≥ 2 GB RAM, ≥ 20 GB free disk for the first days.
- Clock discipline: `chronyc tracking` (or `timedatectl`) must show a synchronized clock; the recorder warns
  above 250 ms offset.

## 1. Install

```bash
git clone https://github.com/burak-alp/jevai_trade_bot.git
cd jevai_trade_bot
git checkout claude/sleepy-goldberg-ypz1k2
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,fast]"
pytest -q                        # offline suite, must be green
chronyc tracking || timedatectl  # clock synchronized?
```

## 2. Smoke test (gate 1)

```bash
jevbot smoke | tee smoke_stdout.json ; echo "exit=$?"
```

Expected: `"ok": true`, exit 0, checks `rest`, `ws:public`, `ws:market` all ok. For each WS route the report
shows `subscribe_ack_ok`, `validated` event counts for the required families, `ping_rtt_ms`; the REST check shows
the exchange's actual `rate_limits`. If a route fails, **stop** and send `smoke_stdout.json`.

## 3. One-hour recorder run (gate 2)

```bash
mkdir -p logs
jevbot record --duration 3600 --set logging.file=logs/record-1h.jsonl ; echo "exit=$?"
jevbot verify ; echo "exit=$?"
jevbot recording-report          # writes run/recording_report.{md,json}
```

`jevbot record` runs its own smoke test first and exits with code 3 if it fails.
While it runs, `run/recorder_health.json` is refreshed every 10 s (status, msgs/s, latency, loop lag, backlog).

Send back: `run/recording_report.md`, `run/recording_report.json`, `run/smoke_report.json`, and the last
200 lines of `logs/record-1h.jsonl`.

The report contains everything needed for the go/no-go:

| Item | Where in the report |
|---|---|
| universe symbol count | Summary · `universe` |
| websocket connections / reconnects | Summary · `websocket` |
| msgs/s p50 / p95 / max | Summary · `throughput` |
| CPU / RAM | Summary · `process` |
| disk MB/hour per dataset | Datasets table |
| real network latency (t_recv − t_event) | "Feed latency by stream" (exchange clock vs local clock; includes clock offset) |
| schema errors, duplicates, invalid | Summary · `errors` |
| data gaps | Summary · `gaps`, `kline_completeness` |
| OI REST weight consumption | `rest.weight_used_1m`, `rest.oi_polls_per_min` |
| actual exchangeInfo limits | `exchange_rate_limits` |
| per-dataset row counts | Datasets table |
| Parquet integrity | `integrity` |

Gate 2 passes when every line of the report's **Acceptance** block is ✅
(smoke ok, integrity ok, no schema errors, no UNHEALTHY, klines complete after backfill,
feed-latency p99 < 2 s, loop-lag p99 < 500 ms). Anything else: send the report before continuing.

## 4. 24-hour soak

```bash
nohup jevbot record --set logging.file=logs/record-soak.jsonl > /dev/null 2>&1 &
echo $! > run/recorder.pid
# hourly compaction of finished hours (safe while recording)
( while true; do sleep 3600; jevbot compact >> logs/compact.jsonl 2>&1; done ) &
# after 24 h:
kill -TERM "$(cat run/recorder.pid)"     # graceful: finalizes open files
jevbot verify && jevbot recording-report
```

(For production use the systemd unit from the README instead of `nohup`.)

The soak must additionally show: at least one planned 23 h connection rotation without a gap
(`gaps` has no `ws_disconnect` rows at rotation time), stable RSS, disk MB/hour close to the 1 h run,
and a clean `jevbot compact` (no `skipped_unverified`).

## 5. Historical download smoke

```bash
jevbot download --dataset klines --interval 1m --symbols BTCUSDT,ETHUSDT --start 2026-09-01 --end 2026-09-03
jevbot download --dataset metrics --symbols BTCUSDT --start 2026-09-01 --end 2026-09-03
jevbot download --dataset fundingRate --symbols BTCUSDT --start 2026-08-01 --end 2026-08-31
jevbot download --dataset bookDepth --symbols BTCUSDT --start 2026-09-01 --end 2026-09-01
jevbot download --dataset aggTrades --symbols BTCUSDT --start 2026-09-01 --end 2026-09-01
jevbot verify --root data/hist/um
```

Check each `*.quality.json` / manifest `quality` field; the CSV layouts of `metrics` and `bookDepth` were
written from documentation knowledge and must be confirmed on these first real files.
