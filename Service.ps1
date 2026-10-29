. "$PSScriptRoot\Common.ps1"
if (-not (Test-Elevated)) { throw 'The persistent TUN service requires administrator privileges.' }
$mutex = [Threading.Mutex]::new($false, 'Local\UnifiedSingBoxService')
if (-not $mutex.WaitOne(0)) { exit 0 }
$coreProcess = $null
$gatewayProcess = $null
$gatewayBackupProcess = $null
$gatewayEntryProcess = $null
$failed = $false
$coreRetryAt = Get-Date
$gatewayRetryAt = Get-Date
$gatewayBackupRetryAt = Get-Date
$gatewayEntryRetryAt = Get-Date
$gatewayStateSignature = ''
$gatewayBackendPorts = @()
foreach ($configuredPort in @($Settings.GatewayBackendPorts)) {
    try { $port = [int]$configuredPort } catch { continue }
    if ($port -gt 0 -and $port -lt 65536 -and $port -ne [int]$Settings.PublicPort -and
        $gatewayBackendPorts -notcontains $port -and $gatewayBackendPorts.Count -lt 2) {
        $gatewayBackendPorts += $port
    }
}
foreach ($candidatePort in @(18801, 18802)) {
    if ($gatewayBackendPorts.Count -ge 2) { break }
    if ($gatewayBackendPorts -notcontains $candidatePort) { $gatewayBackendPorts += $candidatePort }
}
$stopPath = Join-Path $StateDir 'service.stop'
$runtimePath = Join-Path $StateDir 'runtime.json'
$statusPath = Join-Path $StateDir 'status.json'
$gatewayReloadRequestPath = Join-Path $StateDir 'gateway-reload.request'
$gatewayReloadResultPath = Join-Path $StateDir 'gateway-reload.json'
$gatewayReloadProgressPath = Join-Path $StateDir 'gateway-reload-progress.json'
$gatewayEntryRoutePath = Join-Path $StateDir 'gateway-entry-route.json'
$gatewayReloadRequestId = ''
$previousPhase = ''
$pendingCoreRefresh = $false
$coreRefreshRetryAt = Get-Date
function Service-Log([string]$Message) {
    Add-Content -LiteralPath (Join-Path $LogDir 'service.log') -Value "$(Get-Date -Format o) $Message" -Encoding UTF8
}
function Set-GatewayEntryRoute([int]$PreferredPort, [int[]]$DrainingPorts = @()) {
    Write-StateJson $gatewayEntryRoutePath @{
        PreferredPort=$PreferredPort
        DrainingPorts=@($DrainingPorts)
        UpdatedAt=(Get-Date -Format o)
    }
}
function Stop-Managed($Process) {
    if ($Process) {
        $Process.Refresh()
        if (-not $Process.HasExited) {
            # Kill the whole tree so a backend's isolated probe core cannot
            # survive a reload and keep competing for upstream bandwidth.
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
    $signature = '{0}|{1}|{2}' -f $(if ($gatewayProcess) {$gatewayProcess.Id} else {'0'}), $(if ($gatewayEntryProcess) {$gatewayEntryProcess.Id} else {'0'}), $(if ($gatewayBackupProcess) {$gatewayBackupProcess.Id} else {'0'})
    if ($signature -eq $script:gatewayStateSignature) { return }
    $primaryTicks = $null
    $entryTicks = $null
    $backupTicks = $null
    try { if ($gatewayProcess) { $primaryTicks = $gatewayProcess.StartTime.ToUniversalTime().Ticks.ToString() } } catch { }
    try { if ($gatewayEntryProcess) { $entryTicks = $gatewayEntryProcess.StartTime.ToUniversalTime().Ticks.ToString() } } catch { }
    try { if ($gatewayBackupProcess) { $backupTicks = $gatewayBackupProcess.StartTime.ToUniversalTime().Ticks.ToString() } } catch { }
    Write-StateJson (Join-Path $StateDir 'gateway.json') @{
        Pid=$(if ($gatewayProcess) {$gatewayProcess.Id} else {$null})
        StartTicks=$primaryTicks
        EntryPid=$(if ($gatewayEntryProcess) {$gatewayEntryProcess.Id} else {$null})
        EntryStartTicks=$entryTicks
        BackupPid=$(if ($gatewayBackupProcess) {$gatewayBackupProcess.Id} else {$null})
        BackupStartTicks=$backupTicks
        BackendPorts=$gatewayBackendPorts
        SupervisorPid=$PID
        ServiceVersion=2
        ReloadSupported=$true
    }
    $script:gatewayStateSignature = $signature
}
function Get-GatewayBackendActiveRequests([int]$Port) {
    $probePath = Join-Path $script:Root 'gateway\status_probe.py'
    $value = & $script:Settings.PythonPath -B $probePath ([string]$Port)
    if ($LASTEXITCODE -ne 0) { throw "Status probe failed for backend port $Port." }
    return [Math]::Max(0, [int]$value)
}
function Wait-GatewayBackendIdle([int]$Port, [string]$Role) {
    $deadline = (Get-Date).AddSeconds(600)
    $zeroSince = $null
    while ((Get-Date) -lt $deadline) {
        try { $count = Get-GatewayBackendActiveRequests $Port } catch { $count = $null }
        if ($count -eq 0) {
            if (-not $zeroSince) { $zeroSince = Get-Date }
            if (((Get-Date) - $zeroSince).TotalSeconds -ge 2) { return }
        } else {
            $zeroSince = $null
        }
        Start-Sleep -Seconds 1
    }
    throw "Timed out waiting for the $Role backend to become idle."
}
function Start-GatewayBackendForReload([string]$Role) {
    $index = if ($Role -eq 'primary') { 0 } else { 1 }
    $port = [int]$script:gatewayBackendPorts[$index]
    if (Test-TcpPort '127.0.0.1' $port) { throw "$Role backend port $port is unexpectedly occupied." }
    $arguments = @('-u',"`"$script:Root\gateway\unified_gateway.py`"",'--port',[string]$port,'--mixed-port',[string]$script:Settings.MixedPort)
    if ($Role -eq 'standby') { $arguments += '--no-monitor' }
    $stdout = if ($Role -eq 'primary') { 'gateway.stdout.log' } else { 'gateway-backup.stdout.log' }
    $stderr = if ($Role -eq 'primary') { 'gateway.stderr.log' } else { 'gateway-backup.stderr.log' }
    $process = Start-Process -FilePath $script:Settings.PythonPath -ArgumentList $arguments -WorkingDirectory $script:Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $script:LogDir $stdout) -RedirectStandardError (Join-Path $script:LogDir $stderr)
    if ($Role -eq 'primary') { $script:gatewayProcess = $process } else { $script:gatewayBackupProcess = $process }
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 250
        $process.Refresh()
        if ($process.HasExited) { throw "$Role backend exited while restarting (code $($process.ExitCode))." }
        if (Test-TcpPort '127.0.0.1' $port) {
            Service-Log "Gateway $Role backend restarted with PID $($process.Id) on port $port."
            Update-GatewayState
            return
        }
    } while ((Get-Date) -lt $deadline)
    throw "$Role backend did not listen on port $port within 20 seconds."
}
function Restart-GatewayBackendForReload([string]$Role) {
    $index = if ($Role -eq 'primary') { 0 } else { 1 }
    $otherIndex = 1 - $index
    $port = [int]$script:gatewayBackendPorts[$index]
    $otherPort = [int]$script:gatewayBackendPorts[$otherIndex]
    $primaryPort = [int]$script:gatewayBackendPorts[0]
    $process = if ($Role -eq 'primary') { $script:gatewayProcess } else { $script:gatewayBackupProcess }
    if (Test-TcpPort '127.0.0.1' $port) {
        $preferred = if ($Role -eq 'primary' -and (Test-TcpPort '127.0.0.1' $otherPort)) { $otherPort } else { $primaryPort }
        Set-GatewayEntryRoute $preferred @($port)
        Write-StateJson $script:gatewayReloadProgressPath @{RequestId=$script:gatewayReloadRequestId;Phase='draining';Role=$Role;Port=$port;PreferredPort=$preferred;At=(Get-Date -Format o)}
        Wait-GatewayBackendIdle $port $Role
    }
    if ($process) {
        Stop-Managed $process
        if ($Role -eq 'primary') { $script:gatewayProcess = $null } else { $script:gatewayBackupProcess = $null }
        Update-GatewayState
    }
    Write-StateJson $script:gatewayReloadProgressPath @{RequestId=$script:gatewayReloadRequestId;Phase='starting';Role=$Role;Port=$port;At=(Get-Date -Format o)}
    Start-GatewayBackendForReload $Role
    $restoredPreferred = if ($Role -eq 'primary') { $port } else { $primaryPort }
    Set-GatewayEntryRoute $restoredPreferred @()
}

function Invoke-GatewayReloadRequest {
    if (-not (Test-Path -LiteralPath $script:gatewayReloadRequestPath)) { return }
    try {
        $request = Get-Content -LiteralPath $script:gatewayReloadRequestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
        $requestId = [string]$request.RequestId
    } catch {
        $requestId = ''
    }
    if (-not $requestId -or $requestId -eq $script:gatewayReloadRequestId) { return }
    $script:gatewayReloadRequestId = $requestId
    $result = @{RequestId=$requestId;Ok=$false;At=(Get-Date -Format o)}
    try {
        Write-StateJson $script:gatewayReloadProgressPath @{RequestId=$requestId;Phase='starting';At=(Get-Date -Format o)}
        if (-not $script:gatewayEntryProcess -or -not (Test-TcpPort '127.0.0.1' $script:Settings.PublicPort)) {
            throw 'The stable public entry is not ready.'
        }
        Restart-GatewayBackendForReload 'standby'
        Restart-GatewayBackendForReload 'primary'
        if (-not (Test-TcpPort '127.0.0.1' $script:Settings.PublicPort)) { throw 'The stable public entry stopped during reload.' }
        $result.Ok = $true
        $result.PrimaryPid = $script:gatewayProcess.Id
        $result.BackupPid = $script:gatewayBackupProcess.Id
        Service-Log 'Both gateway backends reloaded through the stable entry.'
    } catch {
        $result.Error = $_.Exception.Message
        Service-Log "Gateway backend reload failed: $($_.Exception.Message)"
    } finally {
        Set-GatewayEntryRoute ([int]$script:gatewayBackendPorts[0]) @()
        Write-StateJson $script:gatewayReloadResultPath $result
        Write-StateJson $script:gatewayReloadProgressPath @{RequestId=$requestId;Phase='complete';Ok=[bool]$result.Ok;At=(Get-Date -Format o)}
        Remove-Item -LiteralPath $script:gatewayReloadRequestPath -Force -ErrorAction SilentlyContinue
    }
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
                Write-StateJson $runtimePath @{CorePid=$coreProcess.Id;CoreStartTicks=$coreProcess.StartTime.ToUniversalTime().Ticks.ToString();SupervisorPid=$PID;ServiceVersion=2;StartedAt=(Get-Date -Format o)}
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
        foreach ($gatewaySlot in @('entry', 'primary', 'backup')) {
            $process = switch ($gatewaySlot) {
                'entry' { $gatewayEntryProcess }
                'primary' { $gatewayProcess }
                'backup' { $gatewayBackupProcess }
            }
            if ($process) {
                $process.Refresh()
                if ($process.HasExited) {
                    Service-Log ("Gateway {0} exited (code {1}); retrying." -f $gatewaySlot, $process.ExitCode)
                    switch ($gatewaySlot) {
                        'entry' { $gatewayEntryProcess = $null; $gatewayEntryRetryAt = (Get-Date).AddMilliseconds(250) }
                        'primary' { $gatewayProcess = $null; $gatewayRetryAt = (Get-Date).AddMilliseconds(500) }
                        'backup' { $gatewayBackupProcess = $null; $gatewayBackupRetryAt = (Get-Date).AddMilliseconds(500) }
                    }
                }
            }
        }
        if (-not $gatewayEntryProcess -and (Get-Date) -ge $gatewayEntryRetryAt) {
            try {
                if (Test-TcpPort '127.0.0.1' $Settings.PublicPort) { throw 'The public entry port is occupied by a different process.' }
                $entryArguments = @('-u',"`"$Root\gateway\stable_entry.py`"",'--port',[string]$Settings.PublicPort,
                    '--backend-port',[string]$gatewayBackendPorts[0],'--backend-port',[string]$gatewayBackendPorts[1])
                $gatewayEntryProcess = Start-Process -FilePath $Settings.PythonPath -ArgumentList $entryArguments -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir 'gateway-entry.stdout.log') -RedirectStandardError (Join-Path $LogDir 'gateway-entry.stderr.log')
                Service-Log "Stable gateway entry started (PID $($gatewayEntryProcess.Id), port $($Settings.PublicPort))."
            } catch {
                Service-Log "Gateway entry start postponed: $($_.Exception.Message)"
                Stop-Managed $gatewayEntryProcess
                $gatewayEntryProcess = $null
                $gatewayEntryRetryAt = (Get-Date).AddSeconds(1)
            }
        }
        if (-not $gatewayProcess -and (Get-Date) -ge $gatewayRetryAt) {
            try {
                $backendPort = [int]$gatewayBackendPorts[0]
                if (Test-TcpPort '127.0.0.1' $backendPort) { throw "Gateway primary backend port $backendPort is occupied." }
                $arguments = @('-u',"`"$Root\gateway\unified_gateway.py`"",'--port',[string]$backendPort,'--mixed-port',[string]$Settings.MixedPort)
                $gatewayProcess = Start-Process -FilePath $Settings.PythonPath -ArgumentList $arguments -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir 'gateway.stdout.log') -RedirectStandardError (Join-Path $LogDir 'gateway.stderr.log')
                Service-Log "Gateway primary backend started (PID $($gatewayProcess.Id), port $backendPort)."
            } catch {
                Service-Log "Gateway primary start postponed: $($_.Exception.Message)"
                Stop-Managed $gatewayProcess
                $gatewayProcess = $null
                $gatewayRetryAt = (Get-Date).AddSeconds(1)
            }
        }
        if (-not $gatewayBackupProcess -and (Get-Date) -ge $gatewayBackupRetryAt) {
            try {
                $backendPort = [int]$gatewayBackendPorts[1]
                if (Test-TcpPort '127.0.0.1' $backendPort) { throw "Gateway standby backend port $backendPort is occupied." }
                $arguments = @('-u',"`"$Root\gateway\unified_gateway.py`"",'--port',[string]$backendPort,'--mixed-port',[string]$Settings.MixedPort,'--no-monitor')
                $gatewayBackupProcess = Start-Process -FilePath $Settings.PythonPath -ArgumentList $arguments -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir 'gateway-backup.stdout.log') -RedirectStandardError (Join-Path $LogDir 'gateway-backup.stderr.log')
                Service-Log "Gateway standby backend started (PID $($gatewayBackupProcess.Id), port $backendPort)."
            } catch {
                Service-Log "Gateway standby start postponed: $($_.Exception.Message)"
                Stop-Managed $gatewayBackupProcess
                $gatewayBackupProcess = $null
                $gatewayBackupRetryAt = (Get-Date).AddSeconds(1)
            }
        }
        Update-GatewayState
        Invoke-GatewayReloadRequest
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
        $gatewayAvailable = $gatewayEntryProcess -and ($gatewayProcess -or $gatewayBackupProcess)
        $phase = if ($coreProcess -and $gatewayAvailable) {'running'} elseif ($otherTun.Count -gt 0) {'other-tun'} else {'recovering'}
        if ($phase -ne $previousPhase) { Service-Log "Service phase: $phase"; $previousPhase = $phase }
        if ($gatewayEntryProcess -and (Test-TcpPort '127.0.0.1' $Settings.PublicPort)) {
            try { [void](Ensure-OurProxy) } catch { Service-Log "System proxy ensure skipped: $($_.Exception.Message)" }
        }
        Write-StateJson $statusPath @{Phase=$phase;ServiceVersion=2;SupervisorPid=$PID;CorePid=$(if ($coreProcess) {$coreProcess.Id} else {$null});GatewayPid=$(if ($gatewayProcess) {$gatewayProcess.Id} else {$null});GatewayBackupPid=$(if ($gatewayBackupProcess) {$gatewayBackupProcess.Id} else {$null});GatewayEntryPid=$(if ($gatewayEntryProcess) {$gatewayEntryProcess.Id} else {$null});GatewayBackendPorts=$gatewayBackendPorts;PublicPort=$Settings.PublicPort;UpdatedAt=(Get-Date -Format o)}
        Start-Sleep -Milliseconds 750
    }
} catch {
    $failed = $true
    Service-Log "Fatal service error: $($_.Exception.Message)"
} finally {
    Stop-Managed $gatewayEntryProcess
    Stop-Managed $gatewayProcess
    Stop-Managed $gatewayBackupProcess
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
