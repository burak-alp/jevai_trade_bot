# pos.v1 + reg.v1 pipeline, one shot (public data only: no API key, no orders, no Jev).
# Part 1 (pos.v1): metrics (5 m open interest) for the dev + 180 d pools -> verify -> A0-pos dev / h1 / h2 / 180 d.
# Part 2 (reg.v1 holdout, never looked at): 2024-01-01 .. 2025-03-01 PIT pool, 1m klines/mark/funding ->
#   A0-slow holdout / h1 / h2 (ungated slow.v1 replication + BTC-trend gated "@reg" groups).
# Usage (repo root, venv active, after the recorder soak):  .\scripts\pos_reg_pipeline.ps1 [-Concurrency 8]
# Pre-registration: reports/remote/20260928T-pos-reg-prereg.md (families, parameters, pass rules are fixed).
param([int]$Concurrency = 8, [switch]$SkipDownload)
$ts = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$out = "reports/local/$ts-posreg"
$hist = "data/hist/um"
$poolDev = "data/research/pool-dev.json"
$pool180 = "data/research/pool-180d.json"
$pool24 = "data/research/pool-2024.json"
$set = @("--set", "hist.concurrency=$Concurrency")
New-Item -ItemType Directory -Force -Path $out, logs, data/research | Out-Null
function Step($name, [scriptblock]$cmd) {
  $t0 = Get-Date
  & $cmd *> "logs/posreg-$ts-$name.log"; $code = $LASTEXITCODE
  "{0}`texit={1}`t{2:N0} s" -f $name, $code, ((Get-Date) - $t0).TotalSeconds | Add-Content "$out/steps.txt"
  return $code
}

function Retry($name, [scriptblock]$cmd) {   # downloads skip finished files, so a second pass only redoes failures
  $c = Step $name $cmd
  if ($c -ne 0) { $c = Step "$name-retry" $cmd }
  return $c
}

function A0($arm, $name, $start, $end, $pool) {
  $dir = "data/research/$arm-$name"
  $c = Step "a0-$arm-$name" { jevbot research-a0 --arm $arm --symbols "@$pool" --start $start --end $end --out $dir }
  if ($c -ne 0 -or -not (Test-Path "$dir/summary.md")) {
    Write-Host "A0 $arm $name FAILED -> $out"
    exit 6
  }
  Copy-Item "$dir/summary.md" "$out/$arm-$name-summary.md" -ErrorAction Stop
}

if ((Step "pytest" { python -m pytest -q }) -ne 0) { Write-Host "TESTS FAILED -> $out"; exit 2 }

# ---- part 1: pos.v1 on the development window (+ 180 d secondary) ----
if (-not $SkipDownload) {
  if ((Retry "dl-metrics-dev" { jevbot download --dataset metrics --symbols "@$poolDev" --pit-days 2 --granularity daily --start 2025-01-01 --end 2026-03-31 @set }) -ne 0) { exit 3 }
  if ((Retry "dl-metrics-180d" { jevbot download --dataset metrics --symbols "@$pool180" --pit-days 2 --granularity daily --start 2026-03-01 --end 2026-09-27 @set }) -ne 0) { exit 3 }
}
if ((Step "verify-1" { jevbot verify --root $hist }) -ne 0) { Write-Host "VERIFY FAILED -> $out"; exit 5 }
A0 "pos" "dev" "2025-03-01" "2026-03-31" $poolDev
A0 "pos" "dev-h1" "2025-03-01" "2025-09-30" $poolDev
A0 "pos" "dev-h2" "2025-09-30" "2026-03-31" $poolDev
A0 "pos" "secondary-180d" "2026-03-31" "2026-09-27" $pool180

# ---- part 2: reg.v1 holdout 2024-01-01 .. 2025-03-01 (slow.v1 families, 30 d warm-up from 2023-12) ----
if (-not $SkipDownload) {
  if ((Retry "d1-2024" { jevbot download --dataset klines --interval 1d --symbols ALL --granularity monthly --start 2023-12-01 --end 2025-02-28 @set }) -ne 0) { exit 3 }
  if ((Retry "pit" { jevbot download --dataset pit-listing --symbols ALL @set }) -ne 0) { exit 3 }
  $c = Step "pool-2024" { jevbot universe-pool --start 2024-01-01 --end 2025-03-01 --top 60 --out $pool24 }
  if ($c -ne 0) { Write-Host "POOL exit=$c (6 = days short of top-60) -> see logs, report"; exit 4 }
  $span = @("--symbols", "@$pool24", "--granularity", "monthly", "--start", "2023-12-01", "--end", "2025-02-28") + $set
  if ((Retry "dl-klines-2024" { jevbot download --dataset klines --interval 1m @span }) -ne 0) { exit 3 }
  if ((Retry "dl-mark-2024" { jevbot download --dataset markPriceKlines --interval 1m @span }) -ne 0) { exit 3 }
  if ((Retry "dl-funding-2024" { jevbot download --dataset fundingRate @span }) -ne 0) { exit 3 }
}
if ((Step "verify-2" { jevbot verify --root $hist }) -ne 0) { Write-Host "VERIFY FAILED -> $out"; exit 5 }
A0 "slow" "holdout" "2024-01-01" "2025-03-01" $pool24
A0 "slow" "holdout-h1" "2024-01-01" "2024-08-01" $pool24
A0 "slow" "holdout-h2" "2024-08-01" "2025-03-01" $pool24

Get-Content "$out/steps.txt"
Write-Host "done -> $out (commit only the md files; proposals.parquet stays local)"
