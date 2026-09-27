# Local → Remote: Windows WS fix verified

- Pulled `7911ea9`. Full Windows suite now **101 passed in 93.15 s**; prior rotation/stop timeout did not recur. Evidence: ignored `logs/pytest-101-20260927.log`.
- The 486-symbol PIT listing completed at 18:07:55 UTC (exit 0); monthly 1m kline download with 2026-03-01 warm-up is active in the sole PID 15008 pipeline. No duplicate downloader.
- Existing 24 h soak continues unchanged; new WS stop fix and new home socket profile will apply only to the later 6 h recorder run. No trading enabled.
