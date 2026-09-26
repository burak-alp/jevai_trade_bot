# Local → Remote: home-PC diagnosis and top-50 pilot

- `git pull` completed. Fresh full suite: 73/73 pass. Real Binance smoke: both WS routes negotiate `PerMessageDeflate`.
- Previous 1 h data re-reported: 52 stalled vs 307 calm 10 s intervals; median decoded bytes/s 992,677 vs 1,023,926; latency-vs-bytes correlation **−0.086**. A high-bandwidth correlation is not observed. `bytes_per_s` counts decompressed WebSocket payloads, not on-wire traffic; bandwidth bottleneck remains unproven.
- Metrics 2026-09-22..24 re-downloaded with `--force`: 3/3 quality `ok`, 864 rows, source inversions 33/30/35 retained as diagnostic metadata.
- Wired Ethernet link reports 100 Mbps. This is link speed, not measured internet throughput.
- Windows time script initially failed its own 64 s objective: default `MinPollInterval=10` clamped polling to 1024 s. Added `MinPollInterval=6` and `UpdateInterval=100` (1 s correction); measured status now polls 64 s. A one-time clock step was needed because normal resync slewed slowly. Subsequent NTP offset stayed within a few ms; no hour-long clock result yet.
- `local_validation.ps1` now isolates each run in fresh ignored `data-<timestamp>` and `run-<timestamp>` directories so old records cannot pollute the gate.
- Top-100 home profile early stop: sealed 110 s report showed 50 s feed stall, 10% good share, clock max 66 ms, loop lag p99 max 13 ms, 200/200 klines complete; 5 `closed:nocode` + 3 `stale_feed` events in logs. NO-GO already inevitable.
- Changed home profile to top-50 bookTicker; 10 min real Binance pilot: 41/41 files, 155,438 rows verified; 200/200 klines, clock max 58 ms, loop lag p99 max 13 ms; one startup 10 s stall; good share 98.31% (58/59), longest 10 s; one reconnect. Normal minutes had p99 ~150 ms. Pilot duration is too short for the 1 h gate.
- Removed an unconditional `stall_diagnostics.reading` sentence that claimed high correlation regardless of measured sign; added note that bytes/s is decoded payload.
- A clean 1 h top-50 run is in progress. Sprint 1 remains NO-GO until it passes every acceptance criterion.
