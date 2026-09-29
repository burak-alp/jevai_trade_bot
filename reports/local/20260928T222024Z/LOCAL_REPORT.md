# LOCAL_REPORT — new home 6h soak (2026-09-28 22:23–2026-09-29 04:23 UTC)

**GO for recorder operations, paper research only.** New `home.yaml`: bookTicker off, depth top30. All 9/9 acceptance flags pass; `record_exit.txt` record/verify/report exit 0. No real orders.

- 21,605 s, 200 symbols, 4 WS connections / 4 disconnects. All four were a single ~2 s silence event at 22:27 UTC: 1 depth public + 3 market. Public ≤3/h; no repeat over remaining 5h55m. Old bookTicker profile had 892 reconnects; this is a distinct run.
- 360 PIT minutes × 200 members: 72,000/72,000 kline cells, 0 REST backfill, 0 missing. 4,617,474 Parquet rows, 0 schema errors, 0 invalid/duplicates.
- Feed latency p50 143 ms, p95 161 ms; p99<2 s share 99.91%, longest episode 10 s. One minute-level p99 max 18,966 ms occurred; no >30 s feed stall. Loop lag p99 max 13 ms; clock max |offset| 69.9 ms.
- CPU p95 6.9%, RSS max 91.5 MB; REST max 486/2400 weight/min, OI 199.6 polls/min; raw disk 36.7 MB/h.
- Pre-compact `verify.json`: 861 files / 4,617,474 rows, all checks OK. `compact --group hour --grace-min 0` exit 0, raw disk 220.9→98.1 MB. `verify_after_compact.json`: 62 files / same rows, all checks OK.

Jev shadow operational snapshot 2026-09-29 09:02 UTC: trade_success 12 ok, direction_h 157 ok, btc_regime_7d 1 ok; 0 other errors, 1 late trade_success. Latency p50/p95 444/1444 ms; tokens input/output 105,329/6,418; returned model `jev-1.13.0`; settled B n=0, C n=0. User terminal single writer remains; `JevAI Paper` scheduled task disabled. No early performance inference or parameter change.

Evidence: `recording_report.md/json`, `verify.json`, `compact.json`, `verify_after_compact.json`, `logs/record-20260928T222024Z.jsonl`.
