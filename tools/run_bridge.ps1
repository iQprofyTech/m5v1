# Start the PC bridge (Ctrl+C to stop).
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONUNBUFFERED = "1"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
Set-Location (Join-Path $root "bridge")
& (Join-Path $root ".venv\Scripts\python.exe") -m m5bridge @args
