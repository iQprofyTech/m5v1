# Read the whole 8 MB flash of the Cardputer into backups\ (read-only, does not change the device).
# Restore later with:  .venv\Scripts\esptool --port COM3 write-flash 0 backups\<file>.bin
param([string]$Port = "COM3")

$root = Split-Path -Parent $PSScriptRoot
$esptool = Join-Path $root ".venv\Scripts\esptool.exe"
$dir = Join-Path $root "backups"
New-Item -ItemType Directory -Force $dir | Out-Null
$file = Join-Path $dir ("cardputer-{0}.bin" -f (Get-Date -Format "yyyyMMdd-HHmmss"))

& $esptool --port $Port --baud 921600 read-flash 0 0x800000 $file
if ($LASTEXITCODE -ne 0) { throw "esptool failed ($LASTEXITCODE)" }
Get-FileHash -Algorithm SHA256 $file | Format-List
Write-Host "Backup saved: $file"
