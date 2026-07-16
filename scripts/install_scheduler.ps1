# CriticalCorallations2026 Task Scheduler Setup
# Run once as Administrator:
#   Start-Process powershell -Verb RunAs -ArgumentList '-File C:\Projects\CriticalCorallations2026\scripts\install_scheduler.ps1'

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    Write-Host "FAILED: this script must be run elevated (Run as Administrator)." -ForegroundColor Red
    Write-Host "Right-click PowerShell -> Run as Administrator, then re-run this script."
    exit 1
}

$ProjectRoot = "C:\Projects\CriticalCorallations2026"
$Python      = (Get-Command python -ErrorAction Stop).Source

# --- Task: Trading Dashboard (every 5 min, exits immediately if port 5003 already bound) ---
$Action = New-ScheduledTaskAction -Execute $Python `
            -Argument "`"$ProjectRoot\back-trading\trading_dashboard.py`"" `
            -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)

$SettingsArgs = @{
    MultipleInstances   = "IgnoreNew"
    ExecutionTimeLimit  = (New-TimeSpan -Minutes 4)
    RestartCount        = 3
    RestartInterval     = (New-TimeSpan -Minutes 1)
    StartWhenAvailable  = $true
}
$Settings = New-ScheduledTaskSettingsSet @SettingsArgs

$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

try {
    Register-ScheduledTask -TaskName "CC2026Dashboard" `
        -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force -ErrorAction Stop | Out-Null
    if (Get-ScheduledTask -TaskName "CC2026Dashboard" -ErrorAction SilentlyContinue) {
        Write-Host "OK: CC2026Dashboard (trading dashboard every 5 min, port 5003)"
    } else {
        Write-Host "FAILED: CC2026Dashboard registration did not stick." -ForegroundColor Red
    }
} catch {
    Write-Host "FAILED: CC2026Dashboard - $_" -ForegroundColor Red
}

# --- Firewall rule for port 5003 ---
try {
    if (-not (Get-NetFirewallRule -DisplayName "CC2026 Trading Dashboard" -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName "CC2026 Trading Dashboard" `
            -Direction Inbound -Protocol TCP -LocalPort 5003 -Action Allow -ErrorAction Stop | Out-Null
        Write-Host "OK: Firewall rule added for port 5003"
    } else {
        Write-Host "OK: Firewall rule already exists for port 5003"
    }
} catch {
    Write-Host "FAILED: Firewall rule - $_" -ForegroundColor Red
}

Write-Host ""
Write-Host "Done. Check the FAILED lines above (if any) before trusting this as installed."
Write-Host "Dashboard: http://localhost:5003"
Write-Host "LAN:       http://192.168.1.132:5003"
