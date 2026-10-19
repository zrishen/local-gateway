param([switch]$Force)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$sourcePath = Join-Path $Root 'bin\source.json'
$source = Get-Content -LiteralPath $sourcePath -Raw -Encoding UTF8 | ConvertFrom-Json
$destination = Join-Path $Root 'bin\sing-box.exe'
if ((Test-Path -LiteralPath $destination) -and -not $Force) {
    $installed = (& $destination version 2>$null | Select-Object -First 1)
    Write-Output "Core already installed: $installed"
    exit 0
}

$work = Join-Path $Root 'work\core-install'
New-Item -ItemType Directory -Path $work -Force | Out-Null
$zip = Join-Path $work 'sing-box.zip'
$extract = Join-Path $work ([guid]::NewGuid().ToString('N'))
Invoke-WebRequest -Uri $source.url -OutFile $zip -UseBasicParsing
$hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
if ($hash -ne ([string]$source.archive_sha256).ToLowerInvariant()) {
    throw "sing-box archive checksum mismatch. Expected $($source.archive_sha256), got $hash."
}
Expand-Archive -LiteralPath $zip -DestinationPath $extract -Force
$binary = Get-ChildItem -LiteralPath $extract -Recurse -Filter sing-box.exe -File | Select-Object -First 1
if (-not $binary) { throw 'sing-box.exe was not found in the archive.' }
Copy-Item -LiteralPath $binary.FullName -Destination $destination -Force
$versionText = (& $destination version | Select-Object -First 1)
Write-Output "Installed $versionText"
Write-Output "Temporary archive kept under ignored work\core-install and may be deleted manually."
