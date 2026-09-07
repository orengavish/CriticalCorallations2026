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

# Principal must be the interactive user, not SYSTEM: SYSTEM can't resolve
# %USERPROFILE%-relative IBC\config.ini, the same bug that caused Fetcher2026's
# 19-day silent outage (see Fetcher2026\OPERATIONS.md and plan.md bug 1). This
# task never hit the same failure in practice only because the dashboard process
# happened to already be running from a manual launch, not via this task.
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

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

# --- Task: Random-baseline control on MYM (mimics GevaExtract's 3x/day cadence
#     at 18:00/20:30/23:00, but on a symbol GevaExtract doesn't trade -- MES/MNQ --
#     so the two are a genuine apples-to-apples signal-vs-null-hypothesis comparison,
#     not competing for the same slots). --count 20 is a placeholder volume, not a
#     precise match to GevaExtract's per-run count (which varies with how many
#     scraped lines pass filtering) -- adjust if a closer volume match matters later. ---
$RandomAction = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$ProjectRoot\trader\random_gen.py`" --symbol MYM --count 20" `
    -WorkingDirectory $ProjectRoot

$RandomTriggers = @(
    (New-ScheduledTaskTrigger -Daily -At "18:00"),
    (New-ScheduledTaskTrigger -Daily -At "20:30"),
    (New-ScheduledTaskTrigger -Daily -At "23:00")
)

$RandomSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable

try {
    Register-ScheduledTask -TaskName "CC2026RandomBaseline" `
        -Action $RandomAction -Trigger $RandomTriggers -Settings $RandomSettings `
        -Principal $Principal -Force -ErrorAction Stop | Out-Null
    if (Get-ScheduledTask -TaskName "CC2026RandomBaseline" -ErrorAction SilentlyContinue) {
        Write-Host "OK: CC2026RandomBaseline (random control on MYM, 18:00/20:30/23:00)"
    } else {
        Write-Host "FAILED: CC2026RandomBaseline registration did not stick." -ForegroundColor Red
    }
} catch {
    Write-Host "FAILED: CC2026RandomBaseline - $_" -ForegroundColor Red
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
