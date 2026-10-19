param([switch]$SkipUpdate, [switch]$Fallback)
. "$PSScriptRoot\Common.ps1"
if (-not (Test-Elevated)) { throw 'Applying the upstream requires administrator privileges.' }

$outboundsFile = Join-Path $StateDir 'sakura-outbounds.json'
$disabledFile = "$outboundsFile.disabled"
$urlFile = Join-Path $StateDir 'sakura-subscription-url.txt'
$disabledUrlFile = "$urlFile.disabled"

if ($Fallback) {
    if (-not (Test-TcpPort $Settings.SakuraHost $Settings.SakuraPort)) {
        throw 'SakuraCat local proxy is not available; start SakuraCat before falling back.'
    }
    if (Test-Path -LiteralPath $outboundsFile) {
        Move-Item -LiteralPath $outboundsFile -Destination $disabledFile -Force
    }
    if (Test-Path -LiteralPath $urlFile) {
        Move-Item -LiteralPath $urlFile -Destination $disabledUrlFile -Force
    }
} else {
    if (-not (Test-Path -LiteralPath $urlFile) -and (Test-Path -LiteralPath $disabledUrlFile)) {
        Move-Item -LiteralPath $disabledUrlFile -Destination $urlFile -Force
    }
    if (-not $SkipUpdate) {
        if (-not (Test-Path -LiteralPath $urlFile)) {
            throw 'Subscription URL is not configured. Run Import-SakuraSubscription.ps1 first.'
        }
        & "$PSScriptRoot\Update-SakuraUpstream.ps1" -Force
        if ($LASTEXITCODE -ne 0) { throw 'Subscription refresh failed; the running configuration was not changed.' }
    }
    if (-not (Test-Path -LiteralPath $outboundsFile)) {
        throw 'Embedded subscription is not imported. Run Import-SakuraSubscription.ps1 first.'
    }
    $embedded = Get-Content -LiteralPath $outboundsFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([int]$embedded.count -lt 1) { throw 'Embedded subscription contains no nodes.' }
}

$configPath = Join-Path $Root 'config.json'
$backupPath = Join-Path $StateDir 'config.before-upstream.json'
if (Test-Path -LiteralPath $configPath) { Copy-Item -LiteralPath $configPath -Destination $backupPath -Force }
try {
    & "$PSScriptRoot\Generate-Config.ps1" | Out-Null
    $generated = Get-Content -LiteralPath (Join-Path $StateDir 'upstream.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $expected = if ($Fallback) { 'sakuracat' } else { 'subscription' }
    if ($generated.Mode -ne $expected) {
        throw "Generated config selected $($generated.Mode), expected $expected."
    }
} catch {
    if ($Fallback -and (Test-Path -LiteralPath $disabledFile) -and -not (Test-Path -LiteralPath $outboundsFile)) {
        Move-Item -LiteralPath $disabledFile -Destination $outboundsFile -Force
    }
    if ($Fallback -and (Test-Path -LiteralPath $disabledUrlFile) -and -not (Test-Path -LiteralPath $urlFile)) {
        Move-Item -LiteralPath $disabledUrlFile -Destination $urlFile -Force
    }
    if (Test-Path -LiteralPath $backupPath) { Copy-Item -LiteralPath $backupPath -Destination $configPath -Force }
    throw
}

$runtimePath = Join-Path $StateDir 'runtime.json'
$runtime = Get-Content -LiteralPath $runtimePath -Raw -Encoding UTF8 | ConvertFrom-Json
$core = Get-Process -Id $runtime.CorePid -ErrorAction Stop
if ($core.Path -ne (Join-Path $Root 'bin\sing-box.exe') -or
    $core.StartTime.ToUniversalTime().Ticks.ToString() -ne [string]$runtime.CoreStartTicks) {
    throw 'Core PID identity changed; refusing to stop it.'
}
$oldPid = $core.Id
Stop-Process -Id $oldPid -Force

$deadline = (Get-Date).AddSeconds(40)
$newPid = $null
do {
    Start-Sleep -Milliseconds 500
    try {
        $new = Get-Content -LiteralPath $runtimePath -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ($new.CorePid -and $new.CorePid -ne $oldPid -and (Test-TcpPort '127.0.0.1' $Settings.MixedPort)) {
            $newPid = [int]$new.CorePid
            break
        }
    } catch { }
} while ((Get-Date) -lt $deadline)
if (-not $newPid) { throw 'Core did not restart within 40 seconds.' }

& "$PSScriptRoot\Restart-Gateway.ps1"
if ($LASTEXITCODE -ne 0) {
    throw 'Core switched, but the gateway restart failed. Run Restart-Gateway.ps1 manually.'
}
Start-Sleep -Seconds 2
$mode = Get-Content -LiteralPath (Join-Path $StateDir 'mode.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$expectedMode = if ($Fallback) { 'sakuracat' } else { 'subscription' }
if ($mode.upstream -ne $expectedMode) {
    Write-Warning "Gateway has not reported $expectedMode mode yet: $($mode.upstream)"
}

Write-StateJson (Join-Path $StateDir 'upstream-apply.json') @{Ok=$true;Mode=$expectedMode;OldCorePid=$oldPid;NewCorePid=$newPid;At=(Get-Date -Format o)}
Write-Output ("Embedded upstream applied ({0}). Core PID {1} -> {2}." -f $expectedMode, $oldPid, $newPid)