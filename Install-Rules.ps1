param([switch]$Force)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$rulesDir = Join-Path $Root 'rules'
$sourcePath = Join-Path $rulesDir 'source.json'
$sources = Get-Content -LiteralPath $sourcePath -Raw -Encoding UTF8 | ConvertFrom-Json
$temp = Join-Path (Join-Path $Root 'work') 'rules-install'
New-Item -ItemType Directory -Path $temp -Force | Out-Null

$valid = $true
foreach ($item in $sources) {
    $target = Join-Path $rulesDir $item.file
    if (-not $Force -and (Test-Path -LiteralPath $target)) {
        $actual = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -eq ([string]$item.sha256).ToLowerInvariant()) { continue }
    }
    $valid = $false
    $download = Join-Path $temp $item.file
    Invoke-WebRequest -Uri $item.url -OutFile $download -UseBasicParsing
    $actual = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne ([string]$item.sha256).ToLowerInvariant()) {
        throw "Rule checksum mismatch for $($item.file). Expected $($item.sha256), got $actual."
    }
    Copy-Item -LiteralPath $download -Destination $target -Force
    Write-Output "Installed $($item.file) from $($item.project)"
}
if (-not $valid) { Write-Output 'Rule installation complete.' }
else { Write-Output 'Pinned rule sets already installed and verified.' }
