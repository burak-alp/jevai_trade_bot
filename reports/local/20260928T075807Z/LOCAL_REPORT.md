# Local → Remote: A0 result and power-interrupted soak (07:58 UTC)

- Power returned; Windows reboot 07:49 UTC. Before outage the 486-symbol historical pipeline, verify, and A0 full/h1/h2 all finished (03:57 local). Main branch unchanged. Three `summary.md` files are attached in this report directory; proposals Parquet remains local.
- Historical verify after reboot: 62,073 files, 279,838,938 rows, 10.368 GB, zero checksum/read/row/sidecar errors; `_pit` listing is the sole non-manifest artifact (`ok=true`). Quality: kline 1m 15,389 ok/72 warn, mark 1m 14,817/525, funding 3,194/13, kline 1d 27,853/201; **0 suspect**. Source 404 records reflect missing listing periods. A0: 486 pool, 437 ever tradable, 49 never tradable, 6 × 30 d chunks, 0 proposal data gaps.

| A0 period | labelled | gross mean R [95% CI] | net mean R [95% CI] | median cost R | portfolio trades/day · PF · sum net R · max DD R |
|---|---:|---|---|---:|---|
| full 180 d | 3,979 | -0.0286 [-0.0780, 0.0221] | -0.1882 [-0.2380, -0.1367] | 0.1506 | 17.65 · 0.728 · -553.27 · 562.48 |
| h1 90 d | 2,054 | 0.0159 [-0.0479, 0.0785] | -0.1414 [-0.2054, -0.0787] | 0.1480 | 18.18 · 0.782 · -221.56 · 235.83 |
| h2 90 d | 1,924 | -0.0769 [-0.1508, -0.0109] | -0.2390 [-0.3141, -0.1714] | 0.1538 | 17.11 · 0.674 · -333.11 · 334.43 |

- Family signals (full): BRK long n=1,867 gross +0.0061 CI crosses 0, net -0.1555; BRK short n=1,784 gross -0.0709 CI entirely <0, net -0.2298; PB long n=130 gross +0.1218 CI crosses 0, net -0.0322; PB short n=198 gross -0.0735 CI crosses 0, net -0.2247. Net negative in both halves; gross sign not stable (h1 +, h2 -). Per preregistered rule **redesign deterministic setups before A1/B/Jev**; this is not evidence of profitable trading.
- Cost challenge: real recorder median book spread bps (1 s bars): BTC rank1 0.012, SOL rank4 0.819, DOGE rank11 1.032, TAO rank12 0.321, 2Z rank33 2.180 (n=16,753–71,923). Current fixed spread tiers 1.5/3/5 bps are conservative against these samples; slippage/fill costs remain unmeasured. Gross result already lacks a positive CI lower bound.
- Old home-profile soak was interrupted by power at ~05:19 UTC after 75,322 s (20 h 55 m), ~3 h short of target. Partial report: 567 reconnects, 217 feed-stall episodes (longest 180 s), 31,400 kline cells missing after backfill; acceptance no_unhealthy/kline_complete/feed-lag/no-stall all false. Finalized Parquet verifies, but 8 unfinalized `.parquet.tmp` files from outage are preserved and excluded; no 24 h GO claim.
- After reboot: clock offset vs time.windows.com ~2 ms, new home-profile live smoke 3/3 passed. PID 13440 waits until 12:00 UTC today, then runs tests + smoke + isolated **6 h** new-profile recorder (covers 13:30–17:30 UTC). No current recorder/trading process.
