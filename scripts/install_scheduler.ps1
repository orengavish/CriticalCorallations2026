# CriticalCorallations2026 Task Scheduler Setup
# Run once as Administrator:
#   Start-Process powershell -Verb RunAs -ArgumentList '-File C:\Projects\CriticalCorallations2026\scripts\install_scheduler.ps1'

$ProjectRoot = "C:\Projects\CriticalCorallations2026"
$Python      = (Get-Command python -ErrorAction Stop).Source

# --- Task: Trading Dashboard (every 5 min, exits immediately if port 5003 already bound) ---
$Action = New-ScheduledTaskAction -Execute $Python `
            -Argument "`"$ProjectRoot\back-trading\trading_dashboard.py`"" `
            -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)

$Settings = New-ScheduledTaskSettingsSet `
                -MultipleInstances IgnoreNew `
                -ExecutionTimeLimit (New-TimeSpan -Minutes 4) `
                -RestartCount 3 `
                -RestartInterval (New-TimeSpan -Minutes 1) `
                -StartWhenAvailable $true

$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName "CC2026Dashboard" `
    -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null

Write-Host "OK: CC2026Dashboard (trading dashboard every 5 min, port 5003)"

# --- Firewall rule for port 5003 ---
if (-not (Get-NetFirewallRule -DisplayName "CC2026 Trading Dashboard" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "CC2026 Trading Dashboard" `
        -Direction Inbound -Protocol TCP -LocalPort 5003 -Action Allow | Out-Null
    Write-Host "OK: Firewall rule added for port 5003"
} else {
    Write-Host "OK: Firewall rule already exists for port 5003"
}

Write-Host ""
Write-Host "All done. CC2026 trading dashboard is now supervised by Task Scheduler."
Write-Host "Dashboard: http://localhost:5003"
Write-Host "LAN:       http://192.168.1.132:5003"
