param(
    [string]$PhysicalInterface = '',
    [string]$PythonPath = '',
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$localPath = Join-Path $Root 'settings.local.json'

if ((Test-Path -LiteralPath $localPath) -and -not $Force) {
    Write-Output 'settings.local.json already exists; use -Force to regenerate detected values.'
    exit 0
}

if (-not $PhysicalInterface) {
    $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop |
        Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1
    $PhysicalInterface = (Get-NetAdapter -InterfaceIndex $route.InterfaceIndex -ErrorAction Stop).Name
}
$adapter = Get-NetAdapter -Name $PhysicalInterface -ErrorAction Stop
if ($adapter.Status -ne 'Up') { throw "Physical interface is not up: $PhysicalInterface" }

if (-not $PythonPath) {
    $command = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { $PythonPath = $command.Source }
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath)) {
    throw 'Python 3.14 was not found. Install Python, or pass -PythonPath C:\path\to\python.exe.'
}
$version = & $PythonPath -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$version -lt [version]'3.14') { throw "Python 3.14 is required; found $version." }

$local = [ordered]@{}
if (Test-Path -LiteralPath $localPath) {
    $existing = Get-Content -LiteralPath $localPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($property in $existing.PSObject.Properties) { $local[$property.Name] = $property.Value }
}
$local.PhysicalInterface = $PhysicalInterface
$local.PythonPath = $PythonPath
[IO.File]::WriteAllText($localPath, (($local | ConvertTo-Json -Depth 10) + "`n"), [Text.UTF8Encoding]::new($false))

Write-Output "Machine settings: $localPath"
Write-Output "Physical interface: $PhysicalInterface"
Write-Output "Python: $version"
