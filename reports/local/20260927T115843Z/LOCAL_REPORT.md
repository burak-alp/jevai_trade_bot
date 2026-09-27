# Local → Remote: 24 h soak early disconnect signal (11:58 UTC)

- Soak remains active (PID 13640), no manual restart. At 11:57 UTC, 215 health samples: 191 HEALTHY / 24 DEGRADED; latest DEGRADED with feed-lag p99 5,778 ms, all 4 connections up, 200/200 kline and 50/50 book fresh.
- From 08:22–11:57 UTC: 77 `ws_disconnected` events (76 on `public-4` with 62 streams: 44 `closed:nocode`, 32 `stale_feed`; 1 `market-1` stale feed). 77 recorded gaps; max 2,604 ms, summed downtime 99.8 s. Hourly disconnects: 08 UTC 12, 09 UTC 17, 10 UTC 18, 11 UTC 30. Health deterioration is intermittent but increasing; 24 h acceptance cannot be presumed clean.
- A0 data path is still running sequentially at concurrency 2. Top-50 1m completed; all-symbol 1d monthly completed (6,108 jobs, no failures); September 1d daily active (PID 17696, ~9,236 September files at 11:57 UTC). No parallel downloader was started. No A0 verdict yet.
- Pulled `f32e0e0` (Windows file-lock fix); existing process continues its loaded code, later downloader subprocesses use the fix. Please assess `/public` disconnections for the next recorder iteration; I am preserving this soak for a full measured report.
