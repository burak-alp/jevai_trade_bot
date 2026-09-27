# Local → Remote: A0 PIT pipeline active (2026-09-27 08:49 UTC)

- Pulled `3ce3dc6`; PIT daily universe change and Unicode sidecar fix received. Full suite: **95 passed**.
- The prior downloader stopped on a transient Windows file lock for `QUSDT` April funding. Retried only that archive: 1 file, 180 rows, quality ok. Six zero-byte Unicode sidecars left by the old writer were repaired by forcing those six source downloads; 249,870 rows, no failures/suspect.
- Top-50 1m history is continuing at concurrency 2, now in September daily klines. Background PID 2916; log `logs/a0-download-20260927T082220Z.log`.
- Follow-on PIT pipeline PID 17696 waits for PID 2916, then downloads all-symbol 1d (completed months monthly, September daily), builds top-60 pool, stops if pool >150 or days_short, fills pool 1m and funding, verifies, runs A0 full/h1/h2. Log `logs/pit-a0-pipeline-20260927.log`. All downloads sequential at concurrency 2; no duplicate live process.
- 24 h real Binance recorder PID 13640 and hourly compactor PID 17524 remain active; recent 08:43 health: HEALTHY, 200/200 kline, 50/50 book, p99 156 ms, RSS 88.3 MB. Initial ~1 s `/public` gaps remain for final acceptance review.
- No trading or credentials enabled. Measured A0 verdict and 24 h soak report follow when complete.
