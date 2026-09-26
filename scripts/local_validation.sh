#!/usr/bin/env bash
# Local (real Binance) validation bundle. Public data only: no API key, no orders, no Jev.
# Usage (repo root, venv active):  scripts/local_validation.sh [record_seconds=3600] [--no-hist]
set -uo pipefail
DUR="${1:-3600}"
HIST=1; [[ "${2:-}" == "--no-hist" ]] && HIST=0
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="reports/local/$TS"
mkdir -p "$OUT" logs
echo "bundle -> $OUT"

{ echo "## env"; uname -a; python3 --version; nproc; free -m 2>/dev/null; df -h .; git rev-parse HEAD
  echo "## clock"; chronyc tracking 2>/dev/null || timedatectl 2>/dev/null || echo "no clock tool"; } > "$OUT/env.txt" 2>&1

pytest -q > "$OUT/pytest.txt" 2>&1; echo "exit=$?" >> "$OUT/pytest.txt"

jevbot smoke > "$OUT/smoke.json" 2> "$OUT/smoke.stderr"; SMOKE=$?
echo "smoke exit=$SMOKE"
if [[ $SMOKE -ne 0 ]]; then echo "SMOKE FAILED — stop here, report $OUT"; exit 3; fi

LOG="logs/record-$TS.jsonl"
jevbot record --duration "$DUR" --set logging.file="$LOG" 2> "$OUT/record.stderr"; echo "exit=$?" > "$OUT/record_exit.txt"
jevbot verify > "$OUT/verify.json" 2>&1; echo "exit=$?" >> "$OUT/record_exit.txt"
jevbot recording-report > /dev/null 2> "$OUT/report.stderr"
cp run/recording_report.md run/recording_report.json run/smoke_report.json "$OUT/" 2>/dev/null
tail -n 200 "$LOG" > "$OUT/record_tail.jsonl"
grep -E '"level":"(WARNING|ERROR)"' "$LOG" | head -n 300 > "$OUT/warnings.jsonl"
du -sh data/raw/* > "$OUT/disk.txt" 2>&1

if [[ $HIST -eq 1 ]]; then
  D1=$(date -u -d "4 days ago" +%F); D2=$(date -u -d "2 days ago" +%F)
  M1=$(date -u -d "$(date -u +%Y-%m-01) -1 month" +%F); M2=$(date -u -d "$(date -u +%Y-%m-01) -1 day" +%F)
  {
    jevbot download --dataset klines --interval 1m --symbols BTCUSDT,ETHUSDT --start "$D1" --end "$D2"
    jevbot download --dataset metrics --symbols BTCUSDT --start "$D1" --end "$D2"
    jevbot download --dataset bookDepth --symbols BTCUSDT --start "$D2" --end "$D2"
    jevbot download --dataset aggTrades --symbols BTCUSDT --start "$D2" --end "$D2"
    jevbot download --dataset fundingRate --symbols BTCUSDT --start "$M1" --end "$M2"
    jevbot verify --root data/hist/um
  } > "$OUT/hist.txt" 2>&1
  find data/hist/um -name '*.quality.json' | head -n 20 | while read -r f; do echo "== $f"; head -c 1500 "$f"; echo; done > "$OUT/hist_quality.txt"
fi
echo "done -> $OUT  (now write $OUT/LOCAL_REPORT.md)"
