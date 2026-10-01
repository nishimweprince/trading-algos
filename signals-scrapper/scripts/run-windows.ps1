# Task Scheduler entrypoint for signals-scrapper on the Windows MT5 host.
#
# Program:   powershell.exe
# Arguments: -NoProfile -ExecutionPolicy Bypass -File "<repo>\signals-scrapper\scripts\run-windows.ps1"
# Trigger:   At log on (same user that owns Chrome and MT5)
# Security:  "Run only when user is logged on" — the scraper attaches to that
#            user's open Chrome and the "Allow remote debugging" prompt must be
#            clickable on the interactive desktop.
# Settings:  Restart on failure; "Do not start a new instance" if already running.
$ErrorActionPreference = 'Stop'

$appDir = Split-Path -Parent $PSScriptRoot
Set-Location $appDir

$main = Join-Path $appDir 'dist\main.js'
if (-not (Test-Path $main)) {
  throw "dist\main.js not found in $appDir. Run 'npm ci' and 'npm run build' first."
}

$logDir = Join-Path $appDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format 'yyyy-MM-dd'
$log = Join-Path $logDir "scraper-$stamp.log"

# Merge stderr into stdout so Nest warnings/errors land in the same log.
& node $main *>> $log
exit $LASTEXITCODE
