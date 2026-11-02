. "$PSScriptRoot\Common.ps1"
if (-not (Test-Elevated)) { throw 'The persistent TUN service requires administrator privileges.' }
$mutex = [Threading.Mutex]::new($false, 'Local\UnifiedSingBoxService')
if (-not $mutex.WaitOne(0)) { exit 0 }
$coreProcess = $null
$gatewayProcess = $null
$failed = $false
$coreRetryAt = Get-Date
$gatewayRetryAt = Get-Date
$gatewayStateSignature = ''
$stopPath = Join-Path $StateDir 'service.stop'
$runtimePath = Join-Path $StateDir 'runtime.json'
$statusPath = Join-Path $StateDir 'status.json'
$previousPhase = ''
$pendingCoreRefresh = $false
$coreRefreshRetryAt = Get-Date
function Service-Log([string]$Message) {
    Add-Content -LiteralPath (Join-Path $LogDir 'service.log') -Value "$(Get-Date -Format o) $Message" -Encoding UTF8
}
function Stop-Managed($Process) {
    if ($Process) {
        $Process.Refresh()
        if (-not $Process.HasExited) {
            # Kill the whole tree so an isolated probe core cannot survive a
            # gateway restart and keep competing for upstream bandwidth.
            & taskkill.exe /PID $Process.Id /T /F 2>$null | Out-Null
            if (-not $Process.WaitForExit(5000)) {
                Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
                [void]$Process.WaitForExit(3000)
            }
        }
    }
}
function Remove-StaleProbeProcesses {
    $probeRoot = Join-Path $script:Root 'work'
    if (-not (Test-Path -LiteralPath $probeRoot)) { return }
    foreach ($directory in @(Get-ChildItem -LiteralPath $probeRoot -Directory -Filter 'isolated-probes-*' -ErrorAction SilentlyContinue)) {
        $pidFile = Join-Path $directory.FullName 'pid.txt'
        if (Test-Path -LiteralPath $pidFile) {
            try {
                $probePid = [int](Get-Content -LiteralPath $pidFile -Raw).Trim()
                $probeProcess = if ($probePid -gt 0) { Get-Process -Id $probePid -ErrorAction SilentlyContinue } else { $null }
                if ($probeProcess -and $probeProcess.ProcessName -eq 'sing-box') {
                    & taskkill.exe /PID $probePid /T /F 2>$null | Out-Null
                    Service-Log "Cleaned stale isolated probe process $probePid."
                }
            } catch { }
        }
        Remove-Item -LiteralPath $directory.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
}
function Update-GatewayState {
    $signature = if ($gatewayProcess) { [string]$gatewayProcess.Id } else { '0' }
    if ($signature -eq $script:gatewayStateSignature) { return }
    $startTicks = $null
    try {
        if ($gatewayProcess) {
            $startTicks = $gatewayProcess.StartTime.ToUniversalTime().Ticks.ToString()
        }
    } catch { }
    Write-StateJson (Join-Path $StateDir 'gateway.json') @{
        Pid=$(if ($gatewayProcess) {$gatewayProcess.Id} else {$null})
        StartTicks=$startTicks
        SupervisorPid=$PID
        ServiceVersion=3
        ReloadSupported=$false
    }
    $script:gatewayStateSignature = $signature
}
try {
    if (Test-Path -LiteralPath $stopPath) { Remove-Item -LiteralPath $stopPath }
    Remove-StaleProbeProcesses
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
                if ($Settings.CodexOfficialEnabled -and (Test-Path -LiteralPath (Join-Path $StateDir 'sakura-outbounds.json')) -and
                    (Test-TcpPort '127.0.0.1' ([int]$Settings.CodexOfficialPort))) {
                    throw "Official Codex port $($Settings.CodexOfficialPort) is occupied by a different process."
                }
                # A usable cached subscription should not wait for a remote
                # refresh before restoring local API and proxy availability.
                if (-not (Test-Path -LiteralPath (Join-Path $StateDir 'sakura-outbounds.json'))) {
                    & "$Root\Update-SakuraUpstream.ps1" -MaxAgeHours 12 | Out-Null
                }
                & "$Root\Generate-Config.ps1" | Out-Null
                $coreProcess = Start-Process -FilePath $Core -ArgumentList @('run','-D',"`"$Root`"",'-c',"`"$Root\config.json`"") -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir 'core.stdout.log') -RedirectStandardError (Join-Path $LogDir 'core.stderr.log')
                Write-StateJson $runtimePath @{CorePid=$coreProcess.Id;CoreStartTicks=$coreProcess.StartTime.ToUniversalTime().Ticks.ToString();SupervisorPid=$PID;ServiceVersion=3;StartedAt=(Get-Date -Format o)}
                Clear-DnsClientCache
                $pendingCoreRefresh = $false
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
                $gatewayRetryAt = (Get-Date).AddMilliseconds(500)
            }
        }
        if (-not $gatewayProcess -and (Get-Date) -ge $gatewayRetryAt) {
            try {
                if (Test-TcpPort '127.0.0.1' $Settings.PublicPort) { throw 'The public gateway port is occupied by a different process.' }
                $arguments = @('-u',"`"$Root\gateway\unified_gateway.py`"",'--mixed-port',[string]$Settings.MixedPort)
                $gatewayProcess = Start-Process -FilePath $Settings.PythonPath -ArgumentList $arguments -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir 'gateway.stdout.log') -RedirectStandardError (Join-Path $LogDir 'gateway.stderr.log')
                Service-Log "Gateway started (PID $($gatewayProcess.Id), port $($Settings.PublicPort))."
            } catch {
                Service-Log "Gateway start postponed: $($_.Exception.Message)"
                Stop-Managed $gatewayProcess
                $gatewayProcess = $null
                $gatewayRetryAt = (Get-Date).AddSeconds(1)
            }
        }
        Update-GatewayState
        if ($coreProcess -and (Get-Date) -ge $endpointRefreshAt) {
            $endpointRefreshAt = (Get-Date).AddMinutes($endpointRefreshMinutes)
            try {
                try { & "$Root\Update-SakuraUpstream.ps1" -MaxAgeHours 12 | Out-Null } catch { Service-Log "Subscription update skipped: $($_.Exception.Message)" }
                $configPath = Join-Path $Root 'config.json'
                $beforeHash = if (Test-Path -LiteralPath $configPath) { (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash } else { '' }
                & "$Root\Generate-Config.ps1" | Out-Null
                $afterHash = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash
                if ($beforeHash -ne $afterHash) {
                    $pendingCoreRefresh = $true
                    Service-Log 'Endpoint candidates staged; preserving live connections until route repair is needed and AI is idle.'
                } else {
                    Service-Log 'Endpoint refresh completed; no core change required.'
                }
            } catch {
                Service-Log "Endpoint refresh postponed: $($_.Exception.Message)"
            }
        }
        if ($coreProcess -and $pendingCoreRefresh -and (Get-Date) -ge $coreRefreshRetryAt) {
            $coreRefreshRetryAt = (Get-Date).AddSeconds(30)
            try {
                $decisionJson = & $Settings.PythonPath -B (Join-Path $Root 'gateway\core_refresh.py')
                if ($LASTEXITCODE -ne 0) { throw 'Could not read endpoint reload policy.' }
                $decision = $decisionJson | ConvertFrom-Json
                if ($decision.reload) {
                    Stop-Managed $coreProcess
                    $coreProcess = $null
                    $coreRetryAt = Get-Date
                    Service-Log 'Applying staged endpoints after route recovery failed and AI requests finished.'
                }
            } catch {
                Service-Log "Staged endpoint reload postponed: $($_.Exception.Message)"
            }
        }
        $gatewayAvailable = [bool]$gatewayProcess
        $phase = if ($coreProcess -and $gatewayAvailable) {'running'} elseif ($otherTun.Count -gt 0) {'other-tun'} else {'recovering'}
        if ($phase -ne $previousPhase) { Service-Log "Service phase: $phase"; $previousPhase = $phase }
        if ($gatewayProcess -and (Test-TcpPort '127.0.0.1' $Settings.PublicPort)) {
            try { [void](Ensure-OurProxy) } catch { Service-Log "System proxy ensure skipped: $($_.Exception.Message)" }
        }
        Write-StateJson $statusPath @{Phase=$phase;ServiceVersion=3;SupervisorPid=$PID;CorePid=$(if ($coreProcess) {$coreProcess.Id} else {$null});GatewayPid=$(if ($gatewayProcess) {$gatewayProcess.Id} else {$null});PublicPort=$Settings.PublicPort;UpdatedAt=(Get-Date -Format o)}
        Start-Sleep -Milliseconds 750
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
    Write-StateJson $statusPath @{Phase='stopped';ServiceVersion=3;UpdatedAt=(Get-Date -Format o);Failed=$failed}
    Service-Log 'Persistent service stopped; owned processes and TUN were released.'
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
if ($failed) { exit 1 }
