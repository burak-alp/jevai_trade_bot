# Local → Remote: 1 h real Binance home-profile gate

## Result: GO for Gate 2 with top-50 bookTicker scope

- Branch `claude/sleepy-goldberg-ypz1k2`; Windows/Python 3.11.9; public market data only, no credentials/orders/Jev calls.
- Config: `base.yaml` + `home.yaml`: 200 kline symbols, top-50 volume-ranked bookTicker symbols, 12 depth symbols; `PerMessageDeflate` negotiated on `/public` and `/market`.
- Fresh isolated run `data-20260926T221600Z` / `run-20260926T221600Z`; recorder 3605.1 s, exit 0; verify and report exit 0.
- Acceptance **9/9 PASS**: smoke, integrity, schema, no UNHEALTHY, kline completeness, clock <500 ms, 99% feed intervals <2 s, no stall >30 s, loop lag <500 ms.
- Integrity 172/172 files, 930,011/930,011 rows, no checksum/manifest issues. 200/200 klines complete; no gaps, reconnects, warnings, or errors.
- Feed: 359/359 steady 10 s intervals under 2 s; max interval p99 406 ms; stall episodes 0; all stream-family stall-minute counts 0.
- Clock max absolute offset 67.5 ms; local NTP stripchart near 2 ms at run end. Windows Time polls every 64 s after `MinPollInterval=6`, `UpdateInterval=100` fix.
- CPU p95 22.0%, max 25.3%; RSS max 80.3 MB; loop lag p99 max 0 ms; disk 42.8 MB/h.
- Historical metrics re-download 3/3 `ok` (864 rows); prior suspect copies remain quarantined. Other historical families passed in the previous validation.
- The script's **first** pytest artifact says 72 pass / 1 fail because its old test hard-coded top-100; this was a stale test expectation. Fixed it and made the script exit on test/recorder/verify/report/acceptance failure. Final tests: 62/62 unit + 11/11 integration PASS.
- Final tests also exposed an intermittent real downloader bug: parallel workers could overwrite a manifest append on Windows. Two files existed, one manifest entry vanished. Serialized in-process manifest appends; 20 targeted repetitions passed after the fix.

## Scope / request

- Original `docs/02_technical_spec.md` names **200 bookTicker symbols**; this successful home-PC gate covers **50**. Please reconcile the spec and Sprint 1 feature scope explicitly. Do not describe this as validated 200-symbol top-of-book coverage.
- Gate 2 is a one-hour result. It does not establish 24-hour unattended reliability. No need to rerun the hour for the manifest-only fix; the recorder run finished before that code edit, and all recorder/integration tests pass.
