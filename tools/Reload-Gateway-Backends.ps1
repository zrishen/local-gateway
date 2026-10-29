param([int]$WaitSeconds = 600)
. "$PSScriptRoot\..\Common.ps1"

$gatewayStatePath = Join-Path $StateDir 'gateway.json'
$requestPath = Join-Path $StateDir 'gateway-reload.request'
$resultPath = Join-Path $StateDir 'gateway-reload.json'
$progressPath = Join-Path $StateDir 'gateway-reload-progress.json'
$reloadLog = Join-Path $LogDir 'gateway-reload.log'
$requestId = [guid]::NewGuid().ToString('N')

function Write-ReloadLog([string]$Message) {
    Add-Content -LiteralPath $reloadLog -Value "$(Get-Date -Format o) $Message" -Encoding UTF8
}

try {
    $state = Get-Content -LiteralPath $gatewayStatePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    if ([int]$state.ServiceVersion -lt 2 -or -not $state.ReloadSupported) {
        throw 'The running supervisor does not support safe backend reload; restart the service once.'
    }
    if (-not $state.EntryPid -or -not (Test-TcpPort '127.0.0.1' $Settings.PublicPort)) {
        throw 'The stable public entry is not ready.'
    }
    $ports = @($state.BackendPorts | ForEach-Object { [int]$_ })
    if ($ports.Count -lt 2) { throw 'Primary and standby backend ports are not configured.' }

    Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $progressPath -Force -ErrorAction SilentlyContinue
    Write-StateJson $requestPath @{RequestId=$requestId;RequestedAt=(Get-Date -Format o)}
    Write-ReloadLog "Reload $requestId requested."

    $deadline = (Get-Date).AddSeconds([Math]::Max(30, $WaitSeconds))
    $result = $null
    while ((Get-Date) -lt $deadline) {
        try {
            $candidate = Get-Content -LiteralPath $resultPath -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
            if ([string]$candidate.RequestId -eq $requestId) {
                $result = $candidate
                break
            }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    if (-not $result) { throw 'Timed out waiting for the supervisor to reload both gateways.' }
    if (-not $result.Ok) { throw [string]$result.Error }
    if (-not (Test-TcpPort '127.0.0.1' $Settings.PublicPort)) { throw 'The public entry stopped during backend reload.' }
    foreach ($port in $ports) {
        if (-not (Test-TcpPort '127.0.0.1' $port)) { throw "Backend port $port is unavailable after reload." }
    }
    $current = Get-Content -LiteralPath $gatewayStatePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    Write-ReloadLog "Reload $requestId completed: primary $($current.Pid), standby $($current.BackupPid)."
    Write-Output "Gateway backends reloaded safely: primary $($current.Pid), standby $($current.BackupPid); public entry unchanged on port $($Settings.PublicPort)."
    exit 0
} catch {
    $message = $_.Exception.Message
    Write-ReloadLog "Reload $requestId failed: $message"
    Write-StateJson $resultPath @{RequestId=$requestId;Ok=$false;Error=$message;At=(Get-Date -Format o)}
    try {
        $request = Get-Content -LiteralPath $requestPath -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ([string]$request.RequestId -eq $requestId) { Remove-Item -LiteralPath $requestPath -Force -ErrorAction SilentlyContinue }
    } catch { }
    Write-Error $message
    exit 1
}
