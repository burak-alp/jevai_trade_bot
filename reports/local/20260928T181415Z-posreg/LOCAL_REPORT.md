# LOCAL_REPORT — POS/reg one-shot (2026-09-28 18:14–22:18 UTC)

Branch `claude/sleepy-goldberg-ypz1k2`; all steps exit 0. Earlier `20260928T180449Z-posreg` stopped after pytest + partial metrics per requested `--pit-days` speedup; this is the single complete research run. No orders or Jev calls from pipeline.

## Data
- Metrics PIT jobs: dev 30,380 = 29,853 downloaded + 358 existing + 169 missing (**99.44% available**); secondary 14,681 = 14,559 + 107 + 15 (**99.90% available**). Failed 0, suspect 0; quality warnings 327/33, mostly low cadence. `verify-1`/`verify-2` exit 0; A0 proposals report data gaps 0. Metrics missing is recorded, not silently filled.
- 2024 PIT holdout: 379 symbols, 365 ever top-50 tradable, 6,719 proposals / 6,693 labelled / 0 data gaps. h1 3,318 and h2 3,352 labelled.

## Pre-registered decision
- POS.v1 development: 1,037 labelled, `pass_99=[]`. CROWD long/short and FLUSH long/short 99% net lower bounds respectively -0.208/-0.124/-0.481/-0.244 R. Even FLUSH short's +0.115 R point estimate flips to -0.191 R in 180d secondary. **NO-GO.**
- Reg.v1 untouched 2024 holdout: all six `@reg` groups fail 99% net lower >0, `reg_pass_99=[]`; no group passes the two-half gate. **NO-GO; holdout spent.**
- Ungated slow.v1 replication (information only): aggregate net +0.0033 R [95% -0.0741, +0.0913]; no `pass_99`. FUND +0.153 R 95% CI positive but 99% lower -0.018; cannot promote from this look.

Evidence: `steps.txt`, `pos-*-summary.md`, `slow-holdout*-summary.md`, `logs/posreg-20260928T181415Z-*`. User chose to keep single Jev shadow in terminal; `JevAI Paper` scheduled task remains disabled, so no second ledger writer.
