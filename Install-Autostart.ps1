. "$PSScriptRoot\Common.ps1"
if (-not (Test-Elevated)) {
    $child = Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$PSCommandPath`"") -Verb RunAs -WindowStyle Hidden -PassThru
    Write-Output "Administrator installation requested (PID $($child.Id)). See state\install.json."
    exit 0
}
$installState = Join-Path $StateDir 'install.json'
try {
    Write-StateJson $installState @{Phase='installing';UpdatedAt=(Get-Date -Format o)}
    & "$Root\Generate-Config.ps1" -OutputName 'config.next.json' | Out-Null
    $taskName = 'UnifiedSingBox'
    $taskUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Root\Service.ps1`"" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
    $principal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Highest
    $taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $taskSettings -Description 'Unified local AI API and HTTP/SOCKS entry, split DNS, TUN, and SakuraCat/direct automatic switching.' -Force | Out-Null
    $legacyStates = @()
    foreach ($name in @('ClaudeGeminiProxy','ClaudeGeminiProxyWatchdog')) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($task) {
            $legacyStates += @{Name=$name;Enabled=$task.Settings.Enabled}
            Disable-ScheduledTask -TaskName $name | Out-Null
        }
    }
    $legacyStatePath = Join-Path $StateDir 'legacy-tasks.json'
    if (-not (Test-Path -LiteralPath $legacyStatePath)) { Write-StateJson $legacyStatePath $legacyStates }
    # Retire only the previous, explicitly owned first-version core/supervisor.
    $existing = Get-OwnedProcess
    if ($existing) {
        $runtime = Get-Content -LiteralPath (Join-Path $StateDir 'runtime.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $marker = if ($runtime.ServiceVersion -eq 2) {'service.stop'} else {'stop.request'}
        Set-Content -LiteralPath (Join-Path $StateDir $marker) -Value 'upgrade' -Encoding ASCII
        $deadline = (Get-Date).AddSeconds(20)
        while ((Test-TcpPort '127.0.0.1' $Settings.ControllerPort) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 250 }
        if (Test-TcpPort '127.0.0.1' $Settings.ControllerPort) { throw 'Previous core did not stop. Installation halted.' }
    }
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Settings.PublicPort -ErrorAction SilentlyContinue)
    foreach ($listener in $listeners) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        $oldScript = Join-Path $env:USERPROFILE '.claude-gemini-proxy\sanitize_proxy.py'
        if ($owner.CommandLine -and $owner.CommandLine.Contains($oldScript) -and $owner.Name -match '^python') {
            Stop-Process -Id $owner.ProcessId -Force
        } else { throw 'Public port is occupied by an unrelated process; refusing to stop it.' }
    }
    $releaseDeadline = (Get-Date).AddSeconds(10)
    while ((Test-TcpPort '127.0.0.1' $Settings.PublicPort) -and (Get-Date) -lt $releaseDeadline) { Start-Sleep -Milliseconds 250 }
    Start-ScheduledTask -TaskName $taskName
    $deadline = (Get-Date).AddSeconds(30)
    do {
        $ready = (Test-TcpPort '127.0.0.1' $Settings.PublicPort) -and (Test-TcpPort '127.0.0.1' $Settings.ControllerPort)
        if ($ready) { break }
        if ((Get-Date) -gt $deadline) { throw 'New service was installed but did not become ready. See logs\service.log.' }
        Start-Sleep -Milliseconds 500
    } while ($true)
    Write-StateJson $installState @{Phase='installed';Task=$taskName;PublicPort=$Settings.PublicPort;UpdatedAt=(Get-Date -Format o)}
} catch {
    $installationError = $_.Exception.Message
    Write-StateJson $installState @{Phase='error';Error=$installationError;UpdatedAt=(Get-Date -Format o)}
    try { & "$Root\Rollback-V1.ps1" | Out-Null } catch { Add-Content -LiteralPath (Join-Path $LogDir 'rollback.log') -Value $_.Exception.Message }
    throw
}
