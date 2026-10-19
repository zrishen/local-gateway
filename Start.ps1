param([int]$WaitSeconds = 180)
. "$PSScriptRoot\Common.ps1"
$task = Get-ScheduledTask -TaskName 'UnifiedSingBox' -ErrorAction SilentlyContinue
if ($task) {
    if (-not $task.Settings.Enabled) { throw 'Automatic startup is disabled. Use Install-Autostart.ps1 to enable it again.' }
    Start-ScheduledTask -TaskName 'UnifiedSingBox'
    Write-Output 'Persistent unified service requested. SakuraCat is optional; direct fallback is automatic.'
    exit 0
}
if (Get-OwnedProcess) { Write-Output 'Unified sing-box is already running.'; exit 0 }
& "$Root\Generate-Config.ps1"
$arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$Root\Supervisor.ps1`"", '-WaitSeconds', $WaitSeconds)
$startOptions = @{FilePath="$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"; ArgumentList=$arguments; WorkingDirectory=$Root; WindowStyle='Hidden'; PassThru=$true}
if (-not (Test-Elevated)) { $startOptions.Verb = 'RunAs' }
$supervisor = Start-Process @startOptions
Write-Output "Supervisor requested (PID $($supervisor.Id)). See state\status.json and logs\supervisor.log."
