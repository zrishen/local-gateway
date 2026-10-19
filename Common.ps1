$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
function Read-ProjectSettings {
    param([string]$Path)
    return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}

$Settings = Read-ProjectSettings (Join-Path $Root 'settings.json')
$localSettingsPath = Join-Path $Root 'settings.local.json'
if (Test-Path -LiteralPath $localSettingsPath) {
    $localSettings = Read-ProjectSettings $localSettingsPath
    foreach ($property in $localSettings.PSObject.Properties) {
        $Settings | Add-Member -NotePropertyName $property.Name -NotePropertyValue $property.Value -Force
    }
}
if (-not $Settings.PhysicalInterface) {
    try {
        $defaultRoute = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop |
            Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1
        $Settings.PhysicalInterface = (Get-NetAdapter -InterfaceIndex $defaultRoute.InterfaceIndex -ErrorAction Stop).Name
    } catch { }
}
if (-not $Settings.PythonPath) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    $Settings.PythonPath = if ($pythonCommand) { $pythonCommand.Source } else { 'python' }
}
$Core = Join-Path $Root 'bin\sing-box.exe'
$StateDir = Join-Path $Root 'state'
$LogDir = Join-Path $Root 'logs'
$ProxyKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
foreach ($folder in @($StateDir, $LogDir, (Join-Path $Root 'work'))) {
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
}

function Write-StateJson($Path, $Value) {
    $temporaryPath = "$Path.tmp"
    $json = $Value | ConvertTo-Json -Depth 20
    [IO.File]::WriteAllText($temporaryPath, $json, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
}

function Test-Elevated {
    $taskIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]::new($taskIdentity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-TcpPort([string]$HostName, [int]$Port) {
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $pending = $client.ConnectAsync($HostName, $Port)
        if (-not $pending.Wait(1500)) { return $false }
        return $client.Connected
    } catch { return $false } finally { $client.Dispose() }
}

function Get-OtherTun {
    @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object {
        $_.Status -eq 'Up' -and $_.Name -ne $Settings.TunName -and
        ($_.InterfaceDescription -match '(?i)sing-tun|Meta Tunnel|Mihomo|Clash' -or $_.Name -in @('tun0', 'Meta'))
    })
}

function Get-OwnedProcess([string]$StateName = 'runtime.json') {
    $file = Join-Path $StateDir $StateName
    if (-not (Test-Path -LiteralPath $file)) { return $null }
    $saved = Get-Content -LiteralPath $file -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $saved.CorePid) { return $null }
    $process = Get-Process -Id $saved.CorePid -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    try {
        if ($process.Path -ne $Core) { return $null }
        if ($process.StartTime.ToUniversalTime().Ticks.ToString() -ne [string]$saved.CoreStartTicks) { return $null }
    } catch { return $null }
    return $process
}

function Save-ProxySettings {
    $file = Join-Path $StateDir 'proxy-before.json'
    $current = Get-ItemProperty -LiteralPath $ProxyKey
    $saved = @{}
    foreach ($name in @('ProxyEnable', 'ProxyServer', 'ProxyOverride')) {
        $property = $current.PSObject.Properties[$name]
        $saved[$name] = @{ Exists = ($null -ne $property); Value = $(if ($property) { $property.Value } else { $null }) }
    }
    Write-StateJson $file $saved
}

function Notify-ProxySettings {
    if (-not ('UnifiedProxy.WinInet' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace UnifiedProxy {
    public static class WinInet {
        [DllImport("wininet.dll", SetLastError=true)]
        public static extern bool InternetSetOption(IntPtr hInternet, int option, IntPtr buffer, int length);
    }
}
'@
    }
    [void][UnifiedProxy.WinInet]::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0)
    [void][UnifiedProxy.WinInet]::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0)
}

function Set-OurProxy {
    New-ItemProperty -LiteralPath $ProxyKey -Name ProxyServer -Value "127.0.0.1:$($Settings.PublicPort)" -PropertyType String -Force | Out-Null
    New-ItemProperty -LiteralPath $ProxyKey -Name ProxyOverride -Value '<local>;localhost;127.*;[::1]' -PropertyType String -Force | Out-Null
    New-ItemProperty -LiteralPath $ProxyKey -Name ProxyEnable -Value 1 -PropertyType DWord -Force | Out-Null
    Notify-ProxySettings
}

function Test-ProxyServerPort([string]$ProxyServer, [int]$Port) {
    return $ProxyServer.Contains("127.0.0.1:$Port") -or $ProxyServer.Contains("localhost:$Port")
}

function Get-ProjectProxyState {
    $current = Get-ItemProperty -LiteralPath $ProxyKey
    if ($current.ProxyEnable -ne 1) { return 'disabled' }
    $server = [string]$current.ProxyServer
    if ((Test-ProxyServerPort $server ([int]$Settings.PublicPort)) -or
        (Test-ProxyServerPort $server ([int]$Settings.MixedPort))) { return 'project' }
    if (Test-ProxyServerPort $server ([int]$Settings.SakuraPort)) { return 'sakuracat' }
    return 'external'
}

function Ensure-OurProxy {
    $state = Get-ProjectProxyState
    if ($state -eq 'disabled') { Set-OurProxy; return 'project' }
    return $state
}

function Restore-ProxySettings {
    $file = Join-Path $StateDir 'proxy-before.json'
    if (-not (Test-Path -LiteralPath $file)) { return }
    $current = Get-ItemProperty -LiteralPath $ProxyKey
    if ($current.ProxyServer -notin @("127.0.0.1:$($Settings.PublicPort)", "127.0.0.1:$($Settings.MixedPort)") -or $current.ProxyEnable -ne 1) { return }
    $saved = Get-Content -LiteralPath $file -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($name in @('ProxyServer', 'ProxyOverride', 'ProxyEnable')) {
        $property = $saved.$name
        if ($property.Exists) {
            $kind = if ($name -eq 'ProxyEnable') { 'DWord' } else { 'String' }
            New-ItemProperty -LiteralPath $ProxyKey -Name $name -Value $property.Value -PropertyType $kind -Force | Out-Null
        } else {
            Remove-ItemProperty -LiteralPath $ProxyKey -Name $name -ErrorAction SilentlyContinue
        }
    }
    Notify-ProxySettings
}
