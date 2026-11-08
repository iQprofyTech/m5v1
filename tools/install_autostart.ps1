# Start the bridge automatically at Windows logon (hidden, no console window).
#
#   powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1            # install and start now
#   powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -NoStart   # install only
#   powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Remove    # remove
#
# Creates the scheduled task "m5bridge" for the current user (no admin rights needed). The task runs
# the bridge manager (python -m m5bridge.control, http://localhost:8764): it starts the bridge and
# lets the dashboard stop and start it and switch this autostart off and on.
# Logs: bridge\logs\bridge.log (bridge), bridge\logs\control.log (manager).
param([switch]$Remove, [switch]$NoStart)

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
$log = Join-Path $bridgeDir "logs\control.log"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m m5bridge.control --log-file `"$log`"" -WorkingDirectory $bridgeDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "M5 Cardputer -> Claude bridge (m5v1)" -Force | Out-Null
if ($NoStart) { Write-Host "Task '$taskName' installed (starts at next logon)."; return }
Start-ScheduledTask -TaskName $taskName
Write-Host "Task '$taskName' installed and started. Panel: http://localhost:8764  Log: $log"
