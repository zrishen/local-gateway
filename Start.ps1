. "$PSScriptRoot\Common.ps1"
$task = Get-ScheduledTask -TaskName 'UnifiedSingBox' -ErrorAction SilentlyContinue
if ($task) {
    if (-not $task.Settings.Enabled) { throw 'Automatic startup is disabled. Use .\scripts\Install-Autostart.ps1 to enable it again.' }
    Start-ScheduledTask -TaskName 'UnifiedSingBox'
    Write-Output 'Persistent unified service requested. SakuraCat is optional; direct fallback is automatic.'
    exit 0
}
if (Get-OwnedProcess) { Write-Output 'Unified service is already running.'; exit 0 }
& "$Root\Generate-Config.ps1"
$arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$Root\Service.ps1`"")
$startOptions = @{FilePath="$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"; ArgumentList=$arguments; WorkingDirectory=$Root; WindowStyle='Hidden'; PassThru=$true}
if (-not (Test-Elevated)) { $startOptions.Verb = 'RunAs' }
$service = Start-Process @startOptions
Write-Output "Persistent service requested (PID $($service.Id)). See state\status.json and logs\service.log."
