# install_scheduler.ps1
# Run once as Administrator to register the Galgo June fetcher task.
# Right-click PowerShell → "Run as Administrator", then:
#   C:\Projects\galgo2026\june\scripts\install_scheduler.ps1

$action  = New-ScheduledTaskAction -Execute "C:\Projects\galgo2026\june\scripts\run_fetcher.bat"
# 23:30 local time = 17:30 CT (standard) / 18:30 CT (daylight)
# Adjust if your machine clock is not in CT.
$trigger  = New-ScheduledTaskTrigger -Daily -At "11:30PM"
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName    "GalaoFetcherJune" `
    -Action      $action `
    -Trigger     $trigger `
    -Settings    $settings `
    -Description "Galgo June fetcher - daily 23:30 local. Single computer, all 4 symbols." `
    -RunLevel    Highest `
    -Force

Write-Host "GalaoFetcherJune task registered successfully."
