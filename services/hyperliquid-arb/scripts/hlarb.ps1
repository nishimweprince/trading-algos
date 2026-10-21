# Starts hlarb with the HLA_* settings from an env file.
#   powershell -ExecutionPolicy Bypass -File C:\hlarb\hlarb.ps1 live
# A crash (exit 1) restarts after 30 s, at most 5 times an hour. Exit 2 (configuration or
# kill file) and 3 (unhedged positions at startup) need a human, so the loop stops there.
param(
  [Parameter(Position = 0)][ValidateSet("check", "dry-run", "live")][string]$Mode = "check",
  [string]$EnvFile = "C:\hlarb\hlarb.env",
  [string]$Exe = "C:\hlarb\hlarb.exe",
  [string]$Log = "C:\hlarb\journal\hlarb-console.log"
)

foreach ($line in Get-Content $EnvFile) {
  $text = ($line -replace '\s+#.*$', '').Trim()
  if ($text -eq '' -or $text.StartsWith('#')) { continue }
  $eq = $text.IndexOf('=')
  if ($eq -lt 1) { continue }
  [Environment]::SetEnvironmentVariable($text.Substring(0, $eq).Trim(), $text.Substring($eq + 1).Trim(), 'Process')
}

$restarts = @()
while ($true) {
  & $Exe $Mode 2>&1 | Tee-Object -FilePath $Log -Append
  $code = $LASTEXITCODE
  if ($Mode -ne "live" -or $code -eq 0 -or $code -eq 2 -or $code -eq 3) { exit $code }
  $now = Get-Date
  $restarts = @($restarts | Where-Object { $_ -gt $now.AddHours(-1) }) + $now
  if ($restarts.Count -gt 5) {
    Write-Output "hlarb exited $code more than 5 times in an hour; not restarting"
    exit $code
  }
  Write-Output "hlarb exited $code; restarting in 30 s"
  Start-Sleep -Seconds 30
}
