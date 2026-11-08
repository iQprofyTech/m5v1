# Start the bridge automatically at Windows logon (hidden, no console window).
#
#   powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1            # install and start now
#   powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Remove    # remove
#
# Creates the scheduled task "m5bridge" for the current user (no admin rights needed).
# The bridge log goes to bridge\logs\bridge.log.
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$taskName = "m5bridge"
$root = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task '$taskName' removed."
    return
}

$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Not found: $pythonw (create the .venv first, see README)" }
$bridgeDir = Join-Path $root "bridge"
$log = Join-Path $bridgeDir "logs\bridge.log"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m m5bridge --log-file `"$log`"" -WorkingDirectory $bridgeDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "M5 Cardputer -> Claude bridge (m5v1)" -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Task '$taskName' installed and started. Log: $log"
