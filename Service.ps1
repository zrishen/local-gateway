. "$PSScriptRoot\Common.ps1"
if (-not (Test-Elevated)) { throw 'The persistent TUN service requires administrator privileges.' }
$mutex = [Threading.Mutex]::new($false, 'Local\UnifiedSingBoxService')
if (-not $mutex.WaitOne(0)) { exit 0 }
$coreProcess = $null
$gatewayProcess = $null
$failed = $false
$coreRetryAt = Get-Date
$gatewayRetryAt = Get-Date
$stopPath = Join-Path $StateDir 'service.stop'
$runtimePath = Join-Path $StateDir 'runtime.json'
$statusPath = Join-Path $StateDir 'status.json'
$previousPhase = ''
function Service-Log([string]$Message) {
    Add-Content -LiteralPath (Join-Path $LogDir 'service.log') -Value "$(Get-Date -Format o) $Message" -Encoding UTF8
}
function Stop-Managed($Process) {
    if ($Process) {
        $Process.Refresh()
        if (-not $Process.HasExited) {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
            [void]$Process.WaitForExit(5000)
        }
    }
}
try {
    if (Test-Path -LiteralPath $stopPath) { Remove-Item -LiteralPath $stopPath }
    Save-ProxySettings
    $endpointRefreshMinutes = [Math]::Max(1, [double]$Settings.SakuraEndpointRefreshMinutes)
    $endpointRefreshAt = (Get-Date).AddMinutes($endpointRefreshMinutes)
    Service-Log 'Persistent service started. SakuraCat is optional; direct fallback is enabled.'
    while (-not (Test-Path -LiteralPath $stopPath)) {
        $physical = Get-NetAdapter -Name $Settings.PhysicalInterface -ErrorAction SilentlyContinue
        $otherTun = @(Get-OtherTun)
        $wantCore = $physical -and $physical.Status -eq 'Up' -and $otherTun.Count -eq 0
        if ($coreProcess) {
            $coreProcess.Refresh()
            if ($coreProcess.HasExited) {
                Service-Log "Core exited (code $($coreProcess.ExitCode)); retrying."
                $coreProcess = $null
                $coreRetryAt = (Get-Date).AddSeconds(3)
            } elseif (-not $wantCore) {
                Stop-Managed $coreProcess
                $coreProcess = $null
                Service-Log 'Independent TUN released because another TUN is active or the configured network interface is unavailable.'
            }
        }
        if ($wantCore -and -not $coreProcess -and (Get-Date) -ge $coreRetryAt) {
            try {
                if (Test-TcpPort '127.0.0.1' $Settings.ControllerPort) { throw 'Controller port is occupied by a different process.' }
                try { & "$Root\Update-SakuraUpstream.ps1" -MaxAgeHours 12 | Out-Null } catch { Service-Log "Subscription update skipped: $($_.Exception.Message)" }
                & "$Root\Generate-Config.ps1" | Out-Null
                $coreProcess = Start-Process -FilePath $Core -ArgumentList @('run','-D',"`"$Root`"",'-c',"`"$Root\config.json`"") -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir 'core.stdout.log') -RedirectStandardError (Join-Path $LogDir 'core.stderr.log')
                Write-StateJson $runtimePath @{CorePid=$coreProcess.Id;CoreStartTicks=$coreProcess.StartTime.ToUniversalTime().Ticks.ToString();SupervisorPid=$PID;ServiceVersion=2;StartedAt=(Get-Date -Format o)}
                Clear-DnsClientCache
                Service-Log "Core started (PID $($coreProcess.Id))."
            } catch {
                Service-Log "Core start postponed: $($_.Exception.Message)"
                Stop-Managed $coreProcess
                $coreProcess = $null
                $coreRetryAt = (Get-Date).AddSeconds(10)
            }
        }
        if ($gatewayProcess) {
            $gatewayProcess.Refresh()
            if ($gatewayProcess.HasExited) {
                Service-Log "Gateway exited (code $($gatewayProcess.ExitCode)); retrying."
                $gatewayProcess = $null
                $gatewayRetryAt = (Get-Date).AddSeconds(3)
            }
        }
        if (-not $gatewayProcess -and (Get-Date) -ge $gatewayRetryAt) {
            try {
                if (Test-TcpPort '127.0.0.1' $Settings.PublicPort) { throw 'The public entry port is occupied by a different process.' }
                $gatewayProcess = Start-Process -FilePath $Settings.PythonPath -ArgumentList @('-u',"`"$Root\gateway\unified_gateway.py`"") -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir 'gateway.stdout.log') -RedirectStandardError (Join-Path $LogDir 'gateway.stderr.log')
                Write-StateJson (Join-Path $StateDir 'gateway.json') @{Pid=$gatewayProcess.Id;StartTicks=$gatewayProcess.StartTime.ToUniversalTime().Ticks.ToString();SupervisorPid=$PID}
                Service-Log "Gateway started (PID $($gatewayProcess.Id))."
            } catch {
                Service-Log "Gateway start postponed: $($_.Exception.Message)"
                Stop-Managed $gatewayProcess
                $gatewayProcess = $null
                $gatewayRetryAt = (Get-Date).AddSeconds(10)
            }
        }
        if ($coreProcess -and (Get-Date) -ge $endpointRefreshAt) {
            $endpointRefreshAt = (Get-Date).AddMinutes($endpointRefreshMinutes)
            try {
                $configPath = Join-Path $Root 'config.json'
                $beforeHash = if (Test-Path -LiteralPath $configPath) { (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash } else { '' }
                & "$Root\Generate-Config.ps1" | Out-Null
                $afterHash = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash
                if ($beforeHash -ne $afterHash) {
                    Stop-Managed $coreProcess
                    $coreProcess = $null
                    $coreRetryAt = Get-Date
                    Service-Log 'Endpoint candidates changed; core restart scheduled.'
                } else {
                    Service-Log 'Endpoint refresh completed; no core change required.'
                }
            } catch {
                Service-Log "Endpoint refresh postponed: $($_.Exception.Message)"
            }
        }
        $phase = if ($coreProcess -and $gatewayProcess) {'running'} elseif ($otherTun.Count -gt 0) {'other-tun'} else {'recovering'}
        if ($phase -ne $previousPhase) { Service-Log "Service phase: $phase"; $previousPhase = $phase }
        if ($gatewayProcess -and (Test-TcpPort '127.0.0.1' $Settings.PublicPort)) {
            try { [void](Ensure-OurProxy) } catch { Service-Log "System proxy ensure skipped: $($_.Exception.Message)" }
        }
        Write-StateJson $statusPath @{Phase=$phase;ServiceVersion=2;SupervisorPid=$PID;CorePid=$(if ($coreProcess) {$coreProcess.Id} else {$null});GatewayPid=$(if ($gatewayProcess) {$gatewayProcess.Id} else {$null});PublicPort=$Settings.PublicPort;UpdatedAt=(Get-Date -Format o)}
        Start-Sleep -Seconds 2
    }
} catch {
    $failed = $true
    Service-Log "Fatal service error: $($_.Exception.Message)"
} finally {
    Stop-Managed $gatewayProcess
    Stop-Managed $coreProcess
    Restore-ProxySettings
    Clear-DnsClientCache -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $runtimePath) { Remove-Item -LiteralPath $runtimePath }
    Write-StateJson $statusPath @{Phase='stopped';ServiceVersion=2;UpdatedAt=(Get-Date -Format o);Failed=$failed}
    Service-Log 'Persistent service stopped; owned processes and TUN were released.'
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
if ($failed) { exit 1 }
