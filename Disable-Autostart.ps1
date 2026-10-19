. "$PSScriptRoot\Common.ps1"
if (-not (Test-Elevated)) {
    Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$PSCommandPath`"") -Verb RunAs -WindowStyle Hidden | Out-Null
    exit 0
}
Disable-ScheduledTask -TaskName 'UnifiedSingBox' -ErrorAction SilentlyContinue | Out-Null
Set-Content -LiteralPath (Join-Path $StateDir 'service.stop') -Value 'stop' -Encoding ASCII
Write-Output 'Automatic startup disabled; the running service has been asked to stop.'
