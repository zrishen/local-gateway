param([int]$WaitSeconds = 180)
. "$PSScriptRoot\Common.ps1"
if (-not (Test-Elevated)) { throw 'TUN requires an elevated process. Use Start.ps1.' }
$mutex = [Threading.Mutex]::new($false, 'Local\UnifiedSingBoxSupervisor')
if (-not $mutex.WaitOne(0)) { exit 0 }
$coreProcess = $null
$proxySaved = $false
$failed = $false
$statusFile = Join-Path $StateDir 'status.json'
$stopFile = Join-Path $StateDir 'stop.request'
$runtimeFile = Join-Path $StateDir 'runtime.json'
function Report([string]$Phase, [string]$Message) {
    $line = "$(Get-Date -Format o) [$Phase] $Message"
    Add-Content -LiteralPath (Join-Path $LogDir 'supervisor.log') -Value $line -Encoding UTF8
    Write-StateJson $statusFile @{Phase=$Phase;Message=$Message;UpdatedAt=(Get-Date -Format o);SupervisorPid=$PID}
}
try {
    if (Test-Path -LiteralPath $stopFile) { Remove-Item -LiteralPath $stopFile }
    if (Get-OwnedProcess) { throw 'An owned core is already running.' }
    & "$Root\Generate-Config.ps1" | Out-Null
    Report 'waiting' 'Waiting for another TUN to be off and an embedded subscription or SakuraCat local proxy to be ready.'
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    do {
        if (Test-Path -LiteralPath $stopFile) { throw 'Start cancelled.' }
        $otherTun = @(Get-OtherTun)
        $embeddedFile = Join-Path $StateDir 'sakura-outbounds.json'
        $embeddedReady = (Test-Path -LiteralPath $embeddedFile)
        $ready = $otherTun.Count -eq 0 -and ($embeddedReady -or (Test-TcpPort $Settings.SakuraHost $Settings.SakuraPort))
        if ($ready) { break }
        if ((Get-Date) -ge $deadline) { throw 'Timed out. Disable only SakuraCat enhanced mode; keep its local proxy running. No network setting was changed.' }
        Start-Sleep -Seconds 1
    } while ($true)
    $preflight = Get-OwnedProcess 'preflight.json'
    if ($preflight) {
        Stop-Process -Id $preflight.Id -Force
        [void]$preflight.WaitForExit(5000)
    }
    $releaseDeadline = (Get-Date).AddSeconds(10)
    do {
        $requiredPorts = @($Settings.MixedPort, $Settings.DnsPort, $Settings.ControllerPort)
        if ($Settings.CodexOfficialEnabled -and (Test-Path -LiteralPath (Join-Path $StateDir 'sakura-outbounds.json'))) {
            $requiredPorts += [int]$Settings.CodexOfficialPort
        }
        $occupied = @($requiredPorts | Select-Object -Unique | Where-Object { Test-TcpPort '127.0.0.1' $_ })
        if ($occupied.Count -eq 0) { break }
        if ((Get-Date) -ge $releaseDeadline) { throw "Local ports are still occupied: $($occupied -join ', ')." }
        Start-Sleep -Milliseconds 250
    } while ($true)
    Save-ProxySettings
    $proxySaved = $true
    Report 'starting' 'Starting the independent TUN and DNS router.'
    $coreProcess = Start-Process -FilePath $Core -ArgumentList @('run','-D',"`"$Root`"",'-c',"`"$Root\config.json`"") -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir 'core.stdout.log') -RedirectStandardError (Join-Path $LogDir 'core.stderr.log')
    Write-StateJson $runtimeFile @{CorePid=$coreProcess.Id;CoreStartTicks=$coreProcess.StartTime.ToUniversalTime().Ticks.ToString();SupervisorPid=$PID;StartedAt=(Get-Date -Format o)}
    $readyDeadline = (Get-Date).AddSeconds(15)
    while (-not (Test-TcpPort '127.0.0.1' $Settings.MixedPort)) {
        if ($coreProcess.HasExited) { throw "sing-box exited with code $($coreProcess.ExitCode). See logs. No SakuraCat files were changed." }
        if ((Get-Date) -gt $readyDeadline) { throw 'sing-box did not become ready in 15 seconds.' }
        Start-Sleep -Milliseconds 300
        $coreProcess.Refresh()
    }
    Set-OurProxy
    Clear-DnsClientCache
    Report 'running' 'Unified TUN is running. SakuraCat supplies the upstream; company DNS and private traffic go directly.'
    $missingBackend = 0
    while (-not (Test-Path -LiteralPath $stopFile)) {
        $coreProcess.Refresh()
        if ($coreProcess.HasExited) { throw "sing-box stopped unexpectedly with code $($coreProcess.ExitCode)." }
        if (@(Get-OtherTun).Count -gt 0) { throw 'Another TUN was enabled. Stopping our TUN to avoid conflicting network ownership.' }
        $embeddedFile = Join-Path $StateDir 'sakura-outbounds.json'
        if (-not (Test-Path -LiteralPath $embeddedFile) -and -not (Test-TcpPort $Settings.SakuraHost $Settings.SakuraPort)) {
            $missingBackend++
        } else {
            $missingBackend=0
        }
        if ($missingBackend -ge 10) { throw 'Neither an embedded subscription nor SakuraCat local proxy is available. Stopping our TUN.' }
        try { [void](Ensure-OurProxy) } catch { Report 'running' "System proxy ensure skipped: $($_.Exception.Message)" }
        Start-Sleep -Seconds 3
    }
    Report 'stopping' 'Stop requested.'
} catch {
    $failed = $true
    Report 'error' $_.Exception.Message
} finally {
    $owned = Get-OwnedProcess
    if ($owned) { Stop-Process -Id $owned.Id -Force -ErrorAction SilentlyContinue }
    if ($proxySaved) { Restore-ProxySettings }
    Clear-DnsClientCache -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $runtimeFile) { Remove-Item -LiteralPath $runtimeFile }
    if (-not $failed) { Report 'stopped' 'Independent TUN stopped; previous Windows proxy settings were restored.' }
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
