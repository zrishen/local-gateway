. "$PSScriptRoot\Common.ps1"
$statusFile = Join-Path $StateDir 'status.json'
if (Test-Path -LiteralPath $statusFile) { Get-Content -LiteralPath $statusFile -Raw -Encoding UTF8 }
$modeFile = Join-Path $StateDir 'mode.json'
if (Test-Path -LiteralPath $modeFile) { Get-Content -LiteralPath $modeFile -Raw -Encoding UTF8 }
$upstreamFile = Join-Path $StateDir 'upstream.json'
$subscriptionFile = Join-Path $StateDir 'sakura-subscription.json'
$selectionFile = Join-Path $StateDir 'sakura-selection.json'
$healthFile = Join-Path $StateDir 'sakura-health.json'
$officialFile = Join-Path $StateDir 'codex-official-route.json'
$upstream = if (Test-Path -LiteralPath $upstreamFile) { Get-Content -LiteralPath $upstreamFile -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
$subscription = if (Test-Path -LiteralPath $subscriptionFile) { Get-Content -LiteralPath $subscriptionFile -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
$selection = if (Test-Path -LiteralPath $selectionFile) { Get-Content -LiteralPath $selectionFile -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
$health = if (Test-Path -LiteralPath $healthFile) { Get-Content -LiteralPath $healthFile -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
$official = if (Test-Path -LiteralPath $officialFile) { Get-Content -LiteralPath $officialFile -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
[pscustomobject]@{
    SakuraProxyReady = (Test-TcpPort $Settings.SakuraHost $Settings.SakuraPort)
    UnifiedProxyReady = (Test-TcpPort '127.0.0.1' $Settings.MixedPort)
    PublicEntryReady = (Test-TcpPort '127.0.0.1' $Settings.PublicPort)
    CodexOfficialReady = (Test-TcpPort '127.0.0.1' ([int]$Settings.CodexOfficialPort))
    Upstream = $upstream
    Subscription = $subscription
    Selection = $selection
    Health = $health
    CodexOfficial = $official
    Tun = @(Get-NetAdapter -Name $Settings.TunName -ErrorAction SilentlyContinue | Select-Object Name,Status)
    OtherTun = @(Get-OtherTun | Select-Object Name,Status)
} | ConvertTo-Json -Depth 6