# One-time Windows clock setup for the recorder (run PowerShell *as Administrator*).
# Default Windows time sync polls rarely; the PC drifted ~0.5 s per hour in the 1 h test.
# This polls NTP every 64 s so the offset stays well under 500 ms.
$ntp = "HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpClient"
$config = "HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\Config"
w32tm /config /manualpeerlist:"time.google.com,0x9 time.cloudflare.com,0x9 time.windows.com,0x9" /syncfromflags:manual /reliable:no /update
Set-ItemProperty -Path $ntp -Name SpecialPollInterval -Value 64
# Windows clamps SpecialPollInterval to 2^MinPollInterval (1024 s by default).
Set-ItemProperty -Path $config -Name MinPollInterval -Value 6
# W32Time otherwise expands the live poll interval back to hours over time.
Set-ItemProperty -Path $config -Name MaxPollInterval -Value 6
# Apply phase corrections each second (100 * 10 ms), rather than the current 1-hour interval.
Set-ItemProperty -Path $config -Name UpdateInterval -Value 100
Set-Service w32time -StartupType Automatic
Restart-Service w32time
Start-Sleep -Seconds 3
w32tm /resync /force
w32tm /query /status
w32tm /stripchart /computer:time.google.com /samples:5 /dataonly
