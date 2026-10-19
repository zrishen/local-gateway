. "$PSScriptRoot\Common.ps1"
if (-not (Test-Elevated)) { throw 'Rollback requires administrator privileges.' }
$backup = Join-Path $Root 'work\before-unified-v2'
if (-not (Test-Path -LiteralPath (Join-Path $backup 'Supervisor.ps1'))) { throw 'Version 1 backup is missing.' }
Disable-ScheduledTask -TaskName 'UnifiedSingBox' -ErrorAction SilentlyContinue | Out-Null
Set-Content -LiteralPath (Join-Path $StateDir 'service.stop') -Value 'rollback' -Encoding ASCII
$deadline = (Get-Date).AddSeconds(15)
while ((Test-TcpPort '127.0.0.1' $Settings.ControllerPort) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 250 }
if (Test-TcpPort '127.0.0.1' $Settings.ControllerPort) { throw 'Cannot roll back while the new core still owns the network.' }
$gatewayState = Join-Path $StateDir 'gateway.json'
if (Test-Path -LiteralPath $gatewayState) {
    $saved = Get-Content -LiteralPath $gatewayState -Raw -Encoding UTF8 | ConvertFrom-Json
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($saved.Pid)" -ErrorAction SilentlyContinue
    if ($process -and $process.CommandLine -and $process.CommandLine.Contains((Join-Path $Root 'gateway\unified_gateway.py'))) {
        Stop-Process -Id $process.ProcessId -Force
    }
}
foreach ($file in @(Get-ChildItem -LiteralPath $backup -File | Where-Object { $_.Extension -ne '.xml' })) {
    Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $Root $file.Name) -Force
}
$legacyState = Join-Path $StateDir 'legacy-tasks.json'
if (Test-Path -LiteralPath $legacyState) {
    foreach ($task in @(Get-Content -LiteralPath $legacyState -Raw -Encoding UTF8 | ConvertFrom-Json)) {
        if ($task.Enabled) { Enable-ScheduledTask -TaskName $task.Name | Out-Null }
    }
}
& "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File (Join-Path $env:USERPROFILE '.claude-gemini-proxy\start.ps1') | Out-Null
Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$Root\Supervisor.ps1`"",'-WaitSeconds','180') -WorkingDirectory $Root -WindowStyle Hidden | Out-Null
Add-Content -LiteralPath (Join-Path $LogDir 'rollback.log') -Value "$(Get-Date -Format o) Restored version 1 and the original AI service."
