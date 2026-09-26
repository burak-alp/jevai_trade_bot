# Windows (PowerShell) equivalent of scripts/local_validation.sh. Public data only: no API key, no orders, no Jev.
# Usage (repo root, venv active):  .\scripts\local_validation.ps1 [-Seconds 3600] [-NoHist] [-BaseOnly]
# Default profile: config/base.yaml + config/home.yaml (bandwidth-limited home PC profile).
param([int]$Seconds = 3600, [switch]$NoHist, [switch]$BaseOnly)
$cfg = @("--config", "config/base.yaml")
if (-not $BaseOnly) { $cfg += @("--config", "config/home.yaml") }
$ErrorActionPreference = "Continue"
$ts = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$out = "reports/local/$ts"
New-Item -ItemType Directory -Force -Path $out, logs | Out-Null
Write-Host "bundle -> $out"

& {
  "## env"; [System.Environment]::OSVersion.VersionString; python --version; $env:NUMBER_OF_PROCESSORS
  Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory | Out-String
  Get-PSDrive -PSProvider FileSystem | Out-String; git rev-parse HEAD
  "## clock"; w32tm /query /status
} *> "$out/env.txt"

python -m pytest -q *> "$out/pytest.txt"; "exit=$LASTEXITCODE" | Add-Content "$out/pytest.txt"

jevbot smoke @cfg > "$out/smoke.json" 2> "$out/smoke.stderr"; $smoke = $LASTEXITCODE
Write-Host "smoke exit=$smoke"
if ($smoke -ne 0) { Write-Host "SMOKE FAILED - stop here, report $out"; exit 3 }

$log = "logs/record-$ts.jsonl"
jevbot record @cfg --duration $Seconds --set "logging.file=$log" 2> "$out/record.stderr"; "exit=$LASTEXITCODE" | Set-Content "$out/record_exit.txt"
jevbot verify @cfg *> "$out/verify.json"; "verify exit=$LASTEXITCODE" | Add-Content "$out/record_exit.txt"
jevbot recording-report @cfg *> "$out/report.stdout"; "report exit=$LASTEXITCODE" | Add-Content "$out/record_exit.txt"
Copy-Item run/recording_report.md, run/recording_report.json, run/smoke_report.json $out -ErrorAction SilentlyContinue
Get-Content $log -Tail 200 | Set-Content "$out/record_tail.jsonl"
Select-String -Path $log -Pattern '"level":"(WARNING|ERROR)"' | Select-Object -First 300 | ForEach-Object { $_.Line } | Set-Content "$out/warnings.jsonl"
Get-ChildItem data/raw -Directory | ForEach-Object {
  "{0}`t{1:N1} MB" -f $_.Name, ((Get-ChildItem $_.FullName -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
} | Set-Content "$out/disk.txt"

if (-not $NoHist) {
  $d1 = (Get-Date).ToUniversalTime().AddDays(-4).ToString("yyyy-MM-dd")
  $d2 = (Get-Date).ToUniversalTime().AddDays(-2).ToString("yyyy-MM-dd")
  $first = (Get-Date -Day 1).ToUniversalTime()
  $m1 = $first.AddMonths(-1).ToString("yyyy-MM-dd"); $m2 = $first.AddDays(-1).ToString("yyyy-MM-dd")
  & {
    jevbot download @cfg --dataset klines --interval 1m --symbols BTCUSDT,ETHUSDT --start $d1 --end $d2
    jevbot download @cfg --dataset metrics --symbols BTCUSDT --start $d1 --end $d2
    jevbot download @cfg --dataset bookDepth --symbols BTCUSDT --start $d2 --end $d2
    jevbot download @cfg --dataset aggTrades --symbols BTCUSDT --start $d2 --end $d2
    jevbot download @cfg --dataset fundingRate --symbols BTCUSDT --start $m1 --end $m2
    jevbot verify @cfg --root data/hist/um
  } *> "$out/hist.txt"
  Get-ChildItem data/hist/um -Recurse -Filter *.quality.json | Select-Object -First 20 | ForEach-Object {
    "== $($_.FullName)"; (Get-Content $_.FullName -Raw).Substring(0, [Math]::Min(1500, $_.Length))
  } | Set-Content "$out/hist_quality.txt"
}
Write-Host "done -> $out  (now write $out/LOCAL_REPORT.md)"
