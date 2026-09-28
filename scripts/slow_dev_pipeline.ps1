# slow.v1 development pipeline, one shot (public data only: no API key, no orders, no Jev).
# download (development window, PIT pool) -> verify -> A0-slow dev / dev-h1 / dev-h2 + 180 d secondary -> bundle.
# Usage (repo root, venv active):  .\scripts\slow_dev_pipeline.ps1 [-Concurrency 8] [-SkipDownload]
# Pre-registration: reports/remote/20260928T-slow-families.md (families, parameters, pass rule are fixed).
param([int]$Concurrency = 8, [switch]$SkipDownload)
$ts = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$out = "reports/local/$ts-slow"
$hist = "data/hist/um"
$pool = "data/research/pool-dev.json"
$set = @("--set", "hist.concurrency=$Concurrency")
New-Item -ItemType Directory -Force -Path $out, logs, data/research | Out-Null
function Step($name, [scriptblock]$cmd) {
  $t0 = Get-Date
  & $cmd *> "logs/slow-$ts-$name.log"; $code = $LASTEXITCODE
  "{0}`texit={1}`t{2:N0} s" -f $name, $code, ((Get-Date) - $t0).TotalSeconds | Add-Content "$out/steps.txt"
  return $code
}

function Retry($name, [scriptblock]$cmd) {   # downloads skip finished files, so a second pass only redoes failures
  $c = Step $name $cmd
  if ($c -ne 0) { $c = Step "$name-retry" $cmd }
  return $c
}

if ((Step "pytest" { python -m pytest -q }) -ne 0) { Write-Host "TESTS FAILED -> $out"; exit 2 }

if (-not $SkipDownload) {
  # 1d klines of every symbol (delisted included) for the pool; 2025-02 gives the prior day of 2025-03-01
  if ((Retry "d1" { jevbot download --dataset klines --interval 1d --symbols ALL --granularity monthly --start 2025-02-01 --end 2026-03-31 @set }) -ne 0) { exit 3 }
  if ((Retry "pit" { jevbot download --dataset pit-listing --symbols ALL @set }) -ne 0) { exit 3 }
  $c = Step "pool" { jevbot universe-pool --start 2025-03-01 --end 2026-03-31 --top 60 --out $pool }
  if ($c -ne 0) { Write-Host "POOL exit=$c (6 = days short of top-60) -> see logs, report"; exit 4 }
  # 1m history from 2025-01 (30 d feature warm-up before 2025-03-01); all months complete -> monthly archives
  $span = @("--symbols", "@$pool", "--granularity", "monthly", "--start", "2025-01-01", "--end", "2026-03-31") + $set
  if ((Retry "dl-klines" { jevbot download --dataset klines --interval 1m @span }) -ne 0) { exit 3 }
  if ((Retry "dl-mark" { jevbot download --dataset markPriceKlines --interval 1m @span }) -ne 0) { exit 3 }
  if ((Retry "dl-funding" { jevbot download --dataset fundingRate @span }) -ne 0) { exit 3 }
}
if ((Step "verify" { jevbot verify --root $hist }) -ne 0) { Write-Host "VERIFY FAILED -> $out"; exit 5 }

$runs = @(
  @("dev", "2025-03-01", "2026-03-31", $pool),
  @("dev-h1", "2025-03-01", "2025-09-30", $pool),
  @("dev-h2", "2025-09-30", "2026-03-31", $pool),
  @("secondary-180d", "2026-03-31", "2026-09-27", "data/research/pool-180d.json")
)
foreach ($r in $runs) {
  $dir = "data/research/slow-$($r[0])"
  Step "a0-$($r[0])" { jevbot research-a0 --arm slow --symbols "@$($r[3])" --start $r[1] --end $r[2] --out $dir } | Out-Null
  Copy-Item "$dir/summary.md" "$out/$($r[0])-summary.md" -ErrorAction SilentlyContinue
}
Get-Content "$out/steps.txt"
Write-Host "done -> $out (commit only the md files; proposals.parquet stays local)"
