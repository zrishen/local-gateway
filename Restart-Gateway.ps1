. "$PSScriptRoot\Common.ps1"
$restartResult = Join-Path $StateDir 'gateway-restart.json'
try {
    $gatewayState = Get-Content -LiteralPath (Join-Path $StateDir 'gateway.json') -Raw | ConvertFrom-Json
    $gatewayProcess = Get-Process -Id $gatewayState.Pid -ErrorAction Stop
    if ($gatewayProcess.StartTime.ToUniversalTime().Ticks.ToString() -ne $gatewayState.StartTicks) {
        throw 'Gateway PID identity changed; refusing to stop it.'
    }
    $gatewayCommand = (Get-CimInstance Win32_Process -Filter "ProcessId=$($gatewayState.Pid)" -ErrorAction SilentlyContinue).CommandLine
    if ($gatewayCommand -and $gatewayCommand -notlike "*$Root\gateway\unified_gateway.py*") {
        throw 'Gateway command does not match this project.'
    }
    if (-not $gatewayCommand) {
        $owner = @(Get-NetTCPConnection -State Listen -LocalPort $Settings.PublicPort -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess)
        if ($owner -notcontains $gatewayState.Pid) {
            throw 'Gateway PID does not own the project public port.'
        }
    }
    Stop-Process -Id $gatewayProcess.Id -Force -ErrorAction Stop
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 500
        # The supervisor may briefly hold the state file while publishing its PID.
        try {
            $current = Get-Content -LiteralPath (Join-Path $StateDir 'gateway.json') -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        } catch {
            continue
        }
        if ($current.Pid -ne $gatewayState.Pid -and (Test-TcpPort '127.0.0.1' $Settings.PublicPort)) {
            Write-StateJson $restartResult @{Ok=$true;OldPid=$gatewayState.Pid;NewPid=$current.Pid;At=(Get-Date -Format o)}
            exit 0
        }
    } while ((Get-Date) -lt $deadline)
    throw 'Gateway restart timed out.'
} catch {
    Write-StateJson $restartResult @{Ok=$false;Error=$_.Exception.Message;At=(Get-Date -Format o)}
    exit 1
}
