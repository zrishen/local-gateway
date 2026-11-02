. "$PSScriptRoot\Common.ps1"
$gatewayStatePath = Join-Path $StateDir 'gateway.json'
$requestPath = Join-Path $StateDir 'gateway-restart.request'
$restartResult = Join-Path $StateDir 'gateway-restart.json'
try {
    $state = Get-Content -LiteralPath $gatewayStatePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    if ([int]$state.ServiceVersion -lt 3 -or -not $state.Pid) {
        throw 'Single gateway restart requires ServiceVersion 3. Run Start.ps1 once to load the new service.'
    }
    $oldPid = [int]$state.Pid
    $oldTicks = [string]$state.StartTicks
    $process = Get-Process -Id $oldPid -ErrorAction Stop
    if ($oldTicks -and $process.StartTime.ToUniversalTime().Ticks.ToString() -ne $oldTicks) {
        throw 'Gateway PID identity changed; refusing to stop it.'
    }

    # Ask the privileged supervisor to restart its own child. The caller may
    # be unelevated and cannot terminate the gateway process directly.
    Write-StateJson $requestPath @{Pid=$oldPid;StartTicks=$oldTicks;RequestedAt=(Get-Date -Format o)}
    $deadline = (Get-Date).AddSeconds(60)
    $newPid = $null
    do {
        Start-Sleep -Milliseconds 250
        try {
            $current = Get-Content -LiteralPath $gatewayStatePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
            if ($current.Pid -and [int]$current.Pid -ne $oldPid) {
                $newPid = (Get-Process -Id ([int]$current.Pid) -ErrorAction Stop).Id
                break
            }
        } catch { }
    } while ((Get-Date) -lt $deadline)
    if (-not $newPid) { throw 'Gateway did not restart within 60 seconds. See logs\gateway.stderr.log.' }
    $portDeadline = (Get-Date).AddSeconds(10)
    while (-not (Test-TcpPort '127.0.0.1' $Settings.PublicPort) -and (Get-Date) -lt $portDeadline) {
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-TcpPort '127.0.0.1' $Settings.PublicPort)) {
        throw 'Gateway restarted but the public port is not listening.'
    }
    Write-StateJson $restartResult @{Ok=$true;Pid=$newPid;PreviousPid=$oldPid;At=(Get-Date -Format o)}
    Write-Output ("Gateway restarted: PID {0} -> {1}; core and TUN untouched." -f $oldPid, $newPid)
    exit 0
} catch {
    if (Test-Path -LiteralPath $requestPath) { Remove-Item -LiteralPath $requestPath -Force -ErrorAction SilentlyContinue }
    Write-StateJson $restartResult @{Ok=$false;Error=$_.Exception.Message;At=(Get-Date -Format o)}
    Write-Error $_.Exception.Message
    exit 1
}
