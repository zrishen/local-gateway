. "$PSScriptRoot\Common.ps1"
$restartResult = Join-Path $StateDir 'gateway-restart.json'
try {
    $state = Get-Content -LiteralPath (Join-Path $StateDir 'gateway.json') -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    if (-not $state.EntryPid -or -not $state.ReloadSupported) {
        throw 'Safe backend reload is unavailable; restart the persistent service once to load the new supervisor.'
    }
    & (Join-Path $Root 'tools\Reload-Gateway-Backends.ps1') -WaitSeconds 600
    if ($LASTEXITCODE -ne 0) { throw 'Safe backend reload failed. See logs\gateway-reload.log.' }
    $current = Get-Content -LiteralPath (Join-Path $StateDir 'gateway.json') -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    Write-StateJson $restartResult @{Ok=$true;EntryPid=$current.EntryPid;PrimaryPid=$current.Pid;BackupPid=$current.BackupPid;At=(Get-Date -Format o)}
    exit 0
} catch {
    Write-StateJson $restartResult @{Ok=$false;Error=$_.Exception.Message;At=(Get-Date -Format o)}
    Write-Error $_.Exception.Message
    exit 1
}
