param([string]$Region = '', [switch]$List)
. "$PSScriptRoot\Common.ps1"

$outboundsFile = Join-Path $StateDir 'sakura-outbounds.json'
if (-not (Test-Path -LiteralPath $outboundsFile)) {
    throw 'Embedded subscription is not imported. Run Import-SakuraSubscription.ps1 first.'
}
$data = Get-Content -LiteralPath $outboundsFile -Raw -Encoding UTF8 | ConvertFrom-Json
$groups = $data.region_groups
if (-not $groups) {
    throw 'The subscription does not contain region groups; refresh it first.'
}
$current = ''
try {
    $secret = (Get-Content -LiteralPath (Join-Path $StateDir 'controller-secret.txt') -Raw).Trim()
    $headers = @{ Authorization = "Bearer $secret" }
    $top = Invoke-RestMethod -Uri "http://127.0.0.1:$($Settings.ControllerPort)/proxies/sakura" -Headers $headers -TimeoutSec 5
    $current = [string]$top.now
} catch { }

$rows = @()
foreach ($item in $groups.PSObject.Properties) {
    $info = $item.Value
    $rows += [pscustomobject]@{
        Region = $item.Name
        Code = $info.code
        Group = $info.group
        Nodes = $info.count
        Active = ($info.group -eq $current)
    }
}
$rows = @($rows | Sort-Object @{Expression='Active';Descending=$true}, Region)

if ($List -or -not $Region) {
    $rows | Format-Table -AutoSize
    if (-not $Region) { Write-Output 'Use -Region <name> to switch, for example: .\Switch-SakuraRegion.ps1 -Region 美国' }
    exit 0
}

$match = $rows | Where-Object { $_.Region -eq $Region -or $_.Code -eq $Region -or $_.Group -eq $Region } | Select-Object -First 1
if (-not $match) {
    throw "Unknown region: $Region. Run .\Switch-SakuraRegion.ps1 -List to see available regions."
}

$secret = (Get-Content -LiteralPath (Join-Path $StateDir 'controller-secret.txt') -Raw).Trim()
$headers = @{ Authorization = "Bearer $secret"; 'Content-Type' = 'application/json' }
$body = @{ name = $match.Group } | ConvertTo-Json -Compress
Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:$($Settings.ControllerPort)/proxies/sakura" -Headers $headers -Body $body -TimeoutSec 5 | Out-Null

$settingsPath = Join-Path $Root 'settings.local.json'
if (-not (Test-Path -LiteralPath $settingsPath)) {
    $settingsPath = Join-Path $Root 'settings.json'
}
$settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$settings | Add-Member -NotePropertyName SakuraRegion -NotePropertyValue $match.Region -Force
$settings.PSObject.Properties.Remove('SakuraRegions')
[IO.File]::WriteAllText($settingsPath, ($settings | ConvertTo-Json -Depth 10) + "`n", [Text.UTF8Encoding]::new($false))

$data.active_region = $match.Region
$top = $data.outbounds | Where-Object { $_.tag -eq 'sakura' } | Select-Object -First 1
if ($top) { $top.default = $match.Group }
[IO.File]::WriteAllText($outboundsFile, ($data | ConvertTo-Json -Depth 20) + "`n", [Text.UTF8Encoding]::new($false))

Write-Output ("Active region set to {0} ({1} nodes)." -f $match.Region, $match.Nodes)
$pool = @($Settings.SakuraSelectionRegions | Where-Object { $_ })
if ($pool.Count -gt 1) {
    Write-Warning ("Automatic selection is active across {0}; this manual region is a temporary override and the selector may move back to another pool region." -f ($pool -join ', '))
}
& $Settings.PythonPath -B (Join-Path $Root 'gateway\latency_selector.py') --once
if ($LASTEXITCODE -ne 0) { Write-Warning 'Region switched, but the latency probe did not find a verified node yet.' }