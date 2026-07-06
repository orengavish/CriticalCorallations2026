# install_scheduler.ps1
# Run once as Administrator to register all Galgo scheduled tasks.
# Right-click PowerShell → "Run as Administrator", then:
#   C:\Projects\Galgo2026\june\scripts\install_scheduler.ps1

# ── Task 1: Daily fetcher at 23:30 local ─────────────────────────────────────
$action1  = New-ScheduledTaskAction -Execute "C:\Projects\Galgo2026\june\scripts\run_fetcher.bat"
$trigger1 = New-ScheduledTaskTrigger -Daily -At "11:30PM"
$settings1 = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName    "GalaoFetcherJune" `
    -Action      $action1 `
    -Trigger     $trigger1 `
    -Settings    $settings1 `
    -Description "Galgo June fetcher - daily 23:30 local. Single computer, all 4 symbols." `
    -RunLevel    Highest `
    -Force

Write-Host "GalaoFetcherJune task registered."

# ── Task 2: Watchdog ensure — every 5 minutes ────────────────────────────────
# The watchdog script has a single-instance guard: if it's already running,
# the new invocation exits immediately (no duplicate watchdogs).
# This task is the "outer" guard — if the watchdog dies for any reason
# (PC sleep, crash, console close), this kicks it back up within 5 minutes.

$action2  = New-ScheduledTaskAction `
    -Execute "C:\Projects\Galgo2026\june\scripts\run_fetch_watchdog.bat"
$trigger2 = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -Once -At (Get-Date).Date   # fires from midnight, repeats every 5 min forever
$settings2 = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RestartCount 0

Register-ScheduledTask `
    -TaskName    "GalgoFetchWatchdog" `
    -Action      $action2 `
    -Trigger     $trigger2 `
    -Settings    $settings2 `
    -Description "Ensures fetch_watchdog.py stays alive. Fires every 5min, exits instantly if already running. Prevents 12h+ downtime from watchdog crash (G17)." `
    -RunLevel    Highest `
    -Force

Write-Host "GalgoFetchWatchdog task registered (every 5 min)."
Write-Host ""
Write-Host "Both tasks registered. Verify in Task Scheduler > Task Scheduler Library."
