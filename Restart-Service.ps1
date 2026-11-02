. "$PSScriptRoot\Common.ps1"
# Stop.ps1 and Start.ps1 work without elevation; the scheduled task runs
# Service.ps1 with highest privileges.
$statusPath = Join-Path $StateDir 'status.json'
$resultPath = Join-Path $StateDir 'service-restart.json'
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$startedAt = Get-Date
$oldStatus = $null
try {
    if (Test-Path -LiteralPath $statusPath) {
        $oldStatus = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
} catch { }
try {
    Write-StateJson $resultPath @{Ok=$false;Phase='stopping';StartedAt=(Get-Date -Format o)}
    $stop = Start-Process -FilePath $powershell -ArgumentList @(
        '-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$PSScriptRoot\Stop.ps1`""
    ) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -Wait -PassThru
    if ($stop.ExitCode -ne 0) { throw "Stop.ps1 failed with exit code $($stop.ExitCode)." }

    # Wait for the previous scheduled-task run to finish before starting a
    # new one; MultipleInstancesPolicy ignores starts while it is Running.
    $taskDeadline = (Get-Date).AddSeconds(20)
    do {
        $task = Get-ScheduledTask -TaskName 'UnifiedSingBox' -ErrorAction SilentlyContinue
        if (-not $task -or $task.State -ne 'Running') { break }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $taskDeadline)
    if ($task -and $task.State -eq 'Running') {
        throw 'Scheduled task is still running; refusing to start a second instance.'
    }

    Write-StateJson $resultPath @{Ok=$false;Phase='starting';StartedAt=(Get-Date -Format o)}
    $start = Start-Process -FilePath $powershell -ArgumentList @(
        '-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$PSScriptRoot\Start.ps1`""
    ) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -Wait -PassThru
    if ($start.ExitCode -ne 0) { throw "Start.ps1 failed with exit code $($start.ExitCode)." }

    $readyDeadline = (Get-Date).AddSeconds(240)
    $ready = $false
    while ((Get-Date) -lt $readyDeadline) {
        try {
            if ((Test-TcpPort '127.0.0.1' $Settings.PublicPort) -and (Test-Path -LiteralPath $statusPath)) {
                $current = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($current.Phase -eq 'running') {
                    $ready = $true
                    break
                }
            }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'Service did not become ready within 240 seconds. See logs\service.log.' }

    $newStatus = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-StateJson $resultPath @{
        Ok=$true
        Phase='running'
        PreviousCorePid=$(if ($oldStatus) { $oldStatus.CorePid } else { $null })
        CorePid=$newStatus.CorePid
        PreviousGatewayPid=$(if ($oldStatus) { $oldStatus.GatewayPid } else { $null })
        GatewayPid=$newStatus.GatewayPid
        StartedAt=$startedAt.ToString('o')
        FinishedAt=(Get-Date -Format o)
    }
    Write-Output ("Service restarted: core {0} -> {1}, gateway {2} -> {3}." -f `
        $(if ($oldStatus) { $oldStatus.CorePid } else { '?' }), $newStatus.CorePid, `
        $(if ($oldStatus) { $oldStatus.GatewayPid } else { '?' }), $newStatus.GatewayPid)
    exit 0
} catch {
    Write-StateJson $resultPath @{Ok=$false;Phase='failed';Error=$_.Exception.Message;StartedAt=$startedAt.ToString('o');FinishedAt=(Get-Date -Format o)}
    Write-Error $_.Exception.Message
    exit 1
}
