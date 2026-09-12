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
# pythonw (not python) for every scheduled task below -- python.exe always opens a
# visible console for the life of the process; with CC2026Dashboard firing every 5
# minutes forever (this trigger has no RepetitionDuration cap) that's a black window
# flashing up indefinitely, all day, every day -- the same class of bug already found
# and fixed in Fetcher2026's GalgoFetcher2026 task (2026-09-11). random_gen.py already
# logs everything real through lib.logger (trader/logs/), and trading_dashboard.py's
# few startup print()s (which port, early-exit-if-already-bound) are cosmetic -- losing
# them under pythonw is an acceptable trade for not spawning a visible window on every
# trigger.
$PythonW     = Join-Path (Split-Path $Python) "pythonw.exe"

# --- Task: Trading Dashboard (every 5 min, exits immediately if port 5003 already bound) ---
$Action = New-ScheduledTaskAction -Execute $PythonW `
            -Argument "`"$ProjectRoot\back-trading\trading_dashboard.py`"" `
            -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)

# 2026-09-11: ExecutionTimeLimit was 4 minutes -- but trading_dashboard.py's
# app.run() blocks forever by design (it's a persistent Flask server, not a
# one-shot check), so Task Scheduler force-killed it every single cycle,
# 4 minutes after every 5-minute trigger. Net effect: the dashboard was only
# ever up for ~4 of every ~5-10 minutes (real-world gaps measured longer,
# since MultipleInstances=IgnoreNew also skips a trigger that lands while the
# previous instance is still mid-kill) -- this is the actual root cause of
# "5003 is broken" (intermittently), not the pythonw change made alongside
# this fix. 0 = unlimited, matching Fetcher2026's watchdog fix earlier today.
# MultipleInstances=IgnoreNew + the script's own "exit immediately if port
# 5003 already bound" check together already prevent a duplicate instance --
# nothing here needs a hard time limit to stay safe.
$SettingsArgs = @{
    MultipleInstances   = "IgnoreNew"
    ExecutionTimeLimit  = (New-TimeSpan -Minutes 0)
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

# --- Tasks: broker.py / decider.py watchdogs (2026-09-12 strategic reliability review) ---
# Real gap found: neither process has ever had any auto-restart coverage. broker.py
# gives up and exits after 5 failed IB reconnect attempts (~150s); decider.py retries
# forever but never escalates; trader/session.py's SessionManager has real restart/
# backoff logic built but isn't actually running in this environment (confirmed live,
# 2026-09-11 -- see back-trading/trading_dashboard.py's own manual-restart workaround
# code). This is what turned this week's IB Gateway blips (09-08 through 09-12) into
# multi-hour silent outages -- a direct loss against this project's only goal (make
# money in paper trading). Same watchdog shape as CC2026Dashboard above: both scripts
# already call lib.singleton_lock.acquire_singleton_lock() at startup and exit
# immediately (harmlessly) if another live instance already holds the lock -- so this
# task can just try to (re)launch every 2 minutes, unconditionally. If broker/decider
# are already running, the new attempt exits in well under a second; if either has
# died, the very next trigger relaunches it. WorkingDirectory is trader/, not the
# project root -- matches session.py's own SessionManager._spawn() (cwd=trader dir),
# since both scripts import/resolve paths assuming that CWD.
$TraderDir = Join-Path $ProjectRoot "trader"

$BrokerAction = New-ScheduledTaskAction -Execute $PythonW `
    -Argument "broker.py" -WorkingDirectory $TraderDir
$BrokerTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 2)
$BrokerSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 0) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable

try {
    Register-ScheduledTask -TaskName "CC2026Broker" `
        -Action $BrokerAction -Trigger $BrokerTrigger -Settings $BrokerSettings -Principal $Principal -Force -ErrorAction Stop | Out-Null
    if (Get-ScheduledTask -TaskName "CC2026Broker" -ErrorAction SilentlyContinue) {
        Write-Host "OK: CC2026Broker (watchdog, checks/relaunches every 2 min)"
    } else {
        Write-Host "FAILED: CC2026Broker registration did not stick." -ForegroundColor Red
    }
} catch {
    Write-Host "FAILED: CC2026Broker - $_" -ForegroundColor Red
}

$DeciderAction = New-ScheduledTaskAction -Execute $PythonW `
    -Argument "decider.py --mode session" -WorkingDirectory $TraderDir
$DeciderTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 2)
$DeciderSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 0) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable

try {
    Register-ScheduledTask -TaskName "CC2026Decider" `
        -Action $DeciderAction -Trigger $DeciderTrigger -Settings $DeciderSettings -Principal $Principal -Force -ErrorAction Stop | Out-Null
    if (Get-ScheduledTask -TaskName "CC2026Decider" -ErrorAction SilentlyContinue) {
        Write-Host "OK: CC2026Decider (watchdog, checks/relaunches every 2 min)"
    } else {
        Write-Host "FAILED: CC2026Decider registration did not stick." -ForegroundColor Red
    }
} catch {
    Write-Host "FAILED: CC2026Decider - $_" -ForegroundColor Red
}

# --- Task: Random-baseline control on MYM (mimics GevaExtract's 3x/day cadence
#     at 18:00/20:30/23:00, but on a symbol GevaExtract doesn't trade -- MES/MNQ --
#     so the two are a genuine apples-to-apples signal-vs-null-hypothesis comparison,
#     not competing for the same slots). --count 20 is a placeholder volume, not a
#     precise match to GevaExtract's per-run count (which varies with how many
#     scraped lines pass filtering) -- adjust if a closer volume match matters later. ---
$RandomAction = New-ScheduledTaskAction -Execute $PythonW `
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

# --- Task: Decider daily restart (2026-09-11 -- previously registered out-of-band,
#     not via this script, and failing every day with ERROR_FILE_NOT_FOUND: a bare
#     python.exe with no resolvable PATH/working directory in the task's own run
#     context -- the same class of bug as Fetcher2026's 19-day outage, see the
#     Principal comment above. decider.py only re-scans critical_lines and picks up a
#     new day's date once, at process startup -- this restart is what makes that
#     happen daily. Timing: 08:00 IL, well before the ~17:00 IL stock-open gate; see
#     trader/scripts/restart_decider_daily.py's own docstring for why any
#     early-morning time works. -Force overwrites the existing broken registration. ---
$DeciderRestartAction = New-ScheduledTaskAction -Execute $PythonW `
    -Argument "`"$ProjectRoot\trader\scripts\restart_decider_daily.py`"" `
    -WorkingDirectory $ProjectRoot

$DeciderRestartTrigger = New-ScheduledTaskTrigger -Daily -At "08:00"

$DeciderRestartSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -StartWhenAvailable

try {
    Register-ScheduledTask -TaskName "DeciderDailyRestart" `
        -Action $DeciderRestartAction -Trigger $DeciderRestartTrigger -Settings $DeciderRestartSettings `
        -Principal $Principal -Force -ErrorAction Stop | Out-Null
    if (Get-ScheduledTask -TaskName "DeciderDailyRestart" -ErrorAction SilentlyContinue) {
        Write-Host "OK: DeciderDailyRestart (restarts decider.py daily at 08:00 IL)"
    } else {
        Write-Host "FAILED: DeciderDailyRestart registration did not stick." -ForegroundColor Red
    }
} catch {
    Write-Host "FAILED: DeciderDailyRestart - $_" -ForegroundColor Red
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
