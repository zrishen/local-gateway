. "$PSScriptRoot\Common.ps1"
$serviceStatusPath = Join-Path $StateDir 'status.json'
if (Test-Path -LiteralPath $serviceStatusPath) {
    $serviceStatus = Get-Content -LiteralPath $serviceStatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($serviceStatus.ServiceVersion -ge 2) {
        Set-Content -LiteralPath (Join-Path $StateDir 'service.stop') -Value 'manual-stop' -Encoding ASCII
        $serviceDeadline = (Get-Date).AddSeconds(30)
        $stopped = $false
        while ((Get-Date) -lt $serviceDeadline) {
            $current = $null
            try {
                if (Test-Path -LiteralPath $serviceStatusPath) {
                    $current = Get-Content -LiteralPath $serviceStatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
                }
            } catch { }
            if ($current -and $current.Phase -eq 'stopped') { $stopped = $true; break }
            $supervisorAlive = $false
            if ($current -and $current.SupervisorPid) {
                $supervisorAlive = [bool](Get-Process -Id ([int]$current.SupervisorPid) -ErrorAction SilentlyContinue)
            }
            if ($current -and -not $supervisorAlive -and -not (Test-TcpPort '127.0.0.1' $Settings.PublicPort)) {
                $stopped = $true
                break
            }
            Start-Sleep -Milliseconds 250
        }
        if (-not $stopped -or (Test-TcpPort '127.0.0.1' $Settings.PublicPort)) {
            throw 'Service did not stop in time. See logs\service.log.'
        }
        Write-Output 'Unified service stopped for this session. Start.ps1 or next Windows logon starts it again.'
        exit 0
    }
}
Set-Content -LiteralPath (Join-Path $StateDir 'stop.request') -Value 'stop' -Encoding ASCII
$deadline = (Get-Date).AddSeconds(15)
while ((Test-TcpPort '127.0.0.1' $Settings.ControllerPort) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
if (Test-TcpPort '127.0.0.1' $Settings.ControllerPort) {
    $preflight = Get-OwnedProcess 'preflight.json'
    if (-not $preflight) { throw 'Supervisor did not stop in time. Check logs\supervisor.log.' }
}
$preflight = Get-OwnedProcess 'preflight.json'
if ($preflight) { Stop-Process -Id $preflight.Id -Force }
Write-Output 'Independent sing-box stopped. To return to SakuraCat TUN, enable its enhanced mode.'
