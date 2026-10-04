# Starts the Jev paper process (shadow + llm + account/Telegram hooks) unless it is already running, and restarts it
# after a crash. Used by the Windows scheduled task "JevAI paper" (at logon); can also be run by hand.
# Ctrl+C stops it for good (exit code 0 / interrupted -> no restart).
$ErrorActionPreference = "Continue"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'jevbot\.exe"?\s+paper' }
if ($running) {
    Write-Host "paper already running (pid $($running[0].ProcessId)); nothing to do"
    Start-Sleep -Seconds 10
    exit 0
}
$Host.UI.RawUI.WindowTitle = "JevAI paper"
while ($true) {
    Write-Host "$(Get-Date -Format 'dd.MM HH:mm:ss') starting paper"
    & .venv\Scripts\jevbot.exe paper --state-dir run/paper-jev --jev-shadow --jev-base-url https://jev-ai.pro/api `
        --set logging.file=logs/paper-jev-live.jsonl
    $code = $LASTEXITCODE
    if ($code -eq 0 -or $code -eq 130 -or $code -eq -1073741510) { break }      # normal exit / Ctrl+C
    Write-Host "$(Get-Date -Format 'dd.MM HH:mm:ss') paper exited with $code; restarting in 60 s"
    Start-Sleep -Seconds 60
}
