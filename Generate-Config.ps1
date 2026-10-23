param([switch]$ProxyOnly, [string]$OutputName = '')
. "$PSScriptRoot\Common.ps1"

$interface = Get-NetAdapter -Name $Settings.PhysicalInterface -ErrorAction SilentlyContinue
if (-not $interface -or $interface.Status -ne 'Up') {
    throw "Physical network interface is unavailable: $($Settings.PhysicalInterface). Run Initialize-Project.ps1 or update settings.local.json."
}
$secretFile = Join-Path $StateDir 'controller-secret.txt'
if (-not (Test-Path -LiteralPath $secretFile)) {
    ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')) | Set-Content -LiteralPath $secretFile -Encoding ASCII
}
$secret = (Get-Content -LiteralPath $secretFile -Raw).Trim()
$routerSecretFile = Join-Path $StateDir 'codex-router-secret.txt'
if (-not (Test-Path -LiteralPath $routerSecretFile)) {
    ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')) | Set-Content -LiteralPath $routerSecretFile -Encoding ASCII
}
$sakuraPaths = @(
    'C:\Program Files\SakuraCat\data\flutter_assets\assets\singbox\sing-box.exe',
    (Join-Path $env:APPDATA 'SakuraCat\SakuraCat\core.exe')
)
$companyDomains = @($Settings.CompanyDomains | Where-Object { $_ })
$companyConfigured = $companyDomains.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace([string]$Settings.CompanyDns)
$outbounds = @(
    @{type='direct'; tag='direct'; bind_interface=$Settings.PhysicalInterface; domain_resolver=@{server='china-dns'}}
)
if ($companyConfigured) {
    $outbounds += @{type='direct'; tag='company-direct'; bind_interface=$Settings.PhysicalInterface; domain_resolver=@{server='company-dns'}}
}
$dnsServers = @()
if ($companyConfigured) {
    $dnsServers += @{type='udp'; tag='company-dns'; server=$Settings.CompanyDns; server_port=53; detour='direct'}
    if ($Settings.CompanyDnsAlternative) {
        $dnsServers += @{type='udp'; tag='company-dns-alternative'; server=$Settings.CompanyDnsAlternative; server_port=53; detour='direct'}
    }
}
$dnsServers += @{type='udp'; tag='china-dns'; server='223.5.5.5'; server_port=53; detour='direct'}
$dnsServers += @{type='https'; tag='remote-dns'; server='1.1.1.1'; server_port=443; path='/dns-query'; tls=@{enabled=$true; server_name='cloudflare-dns.com'}; detour='sakura'}
$dnsRules = @(@{query_type=@('AAAA'); action='predefined'; rcode='NOERROR'})
if ($companyConfigured) {
    $dnsRules += @{domain_suffix=$companyDomains; action='route'; server='company-dns'}
    $dnsRules += @{domain_suffix=@('lan','local','arpa'); action='route'; server='company-dns'}
} else {
    $dnsRules += @{domain_suffix=@('lan','local','arpa'); action='route'; server='china-dns'}
}
$dnsRules += @{clash_mode='Direct'; action='route'; server='china-dns'}
$dnsRules += @{rule_set=@('geosite-cn'); action='route'; server='china-dns'}
$routeRules = @(
    @{inbound=@('dns-in'); action='hijack-dns'},
    @{port=53; action='hijack-dns'},
    @{action='sniff'; timeout='300ms'},
    @{process_path=$sakuraPaths; action='route'; outbound='direct'},
    @{process_name=@('com.sakuracat.app.exe','core-service.exe'); action='route'; outbound='direct'}
)
if ($companyConfigured) {
    $routeRules += @{domain_suffix=$companyDomains; action='route'; outbound='company-direct'}
}
$routeRules += @{clash_mode='Direct'; action='route'; outbound='direct'}
$routeRules += @{ip_is_private=$true; action='route'; outbound='direct'}
if ($companyConfigured) {
    $routeRules += @{domain_suffix=@('lan','local'); action='route'; outbound='company-direct'}
}
$routeRules += @{rule_set=@('geosite-cn','geoip-cn'); action='route'; outbound='direct'}
$embeddedUpstream = $null
$upstreamFile = Join-Path $StateDir 'sakura-outbounds.json'
if (Test-Path -LiteralPath $upstreamFile) {
    try {
        $candidate = Get-Content -LiteralPath $upstreamFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $candidateOutbounds = @($candidate.outbounds)
        $hasGroup = @($candidateOutbounds | Where-Object { $_.tag -eq 'sakura' }).Count -gt 0
        if ($candidateOutbounds.Count -ge 2 -and $hasGroup) {
            $runtimeFile = Join-Path $StateDir 'sakura-outbounds.runtime.json'
            $endpointArgs = @('-B', (Join-Path $Root 'gateway\endpoint_catalog.py'),
                              '--input', $upstreamFile, '--output', $runtimeFile,
                              '--settings', (Join-Path $Root 'settings.json'))
            & $Settings.PythonPath @endpointArgs | Out-Null
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $runtimeFile)) {
                throw 'Endpoint candidate rendering failed.'
            }
            $candidate = Get-Content -LiteralPath $runtimeFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $embeddedUpstream = $candidate
        }
    } catch {
        $embeddedUpstream = $null
    }
}
if ($embeddedUpstream) {
    # A node connection must leave on the real adapter. Otherwise Windows can
    # route it back into UnifiedBox, proxying the proxy connection a second time.
    foreach ($outbound in @($embeddedUpstream.outbounds)) {
        if ($outbound.type -notin @('selector', 'urltest')) {
            $outbound | Add-Member -NotePropertyName bind_interface -NotePropertyValue $Settings.PhysicalInterface -Force
        }
        $outbounds += $outbound
    }
} else {
    $outbounds += @{type='socks'; tag='sakura'; server=$Settings.SakuraHost; server_port=[int]$Settings.SakuraPort; version='5'}
}
$config = [ordered]@{
    log = @{ level = 'warn'; timestamp = $true; output = (Join-Path $LogDir 'sing-box.log') }
    dns = @{
        servers = $dnsServers
        rules = $dnsRules
        final = 'remote-dns'
        reverse_mapping = $true
        strategy = 'ipv4_only'
    }
    inbounds = @(
        @{type='mixed'; tag='mixed-in'; listen='127.0.0.1'; listen_port=[int]$Settings.MixedPort},
        @{type='direct'; tag='dns-in'; listen='127.0.0.1'; listen_port=[int]$Settings.DnsPort}
    )
    outbounds = $outbounds
    route = @{
        auto_detect_interface = $true
        rules = $routeRules
        rule_set = @(
            @{type='local'; tag='geosite-cn'; format='binary'; path=(Join-Path $Root 'rules\geosite-cn.srs')},
            @{type='local'; tag='geoip-cn'; format='binary'; path=(Join-Path $Root 'rules\geoip-cn.srs')}
        )
        final = 'sakura'
        default_domain_resolver = @{server='remote-dns'}
    }
    experimental = @{
        clash_api = @{external_controller="127.0.0.1:$($Settings.ControllerPort)"; secret=$secret; default_mode=$(if ($embeddedUpstream -or (Test-TcpPort $Settings.SakuraHost $Settings.SakuraPort)) {'Rule'} else {'Direct'})}
    }
}
if (-not $ProxyOnly) {
    # Private API traffic must not depend on the TUN lifecycle. Otherwise the
    # hourly endpoint refresh can reset requests even with a fixed DNS mapping.
    $tunExclusionJson = & $Settings.PythonPath -B (Join-Path $Root 'gateway\upstream_routing.py')
    if ($LASTEXITCODE -ne 0) { throw 'Could not calculate private API TUN exclusions.' }
    $tunExclusions = $tunExclusionJson | ConvertFrom-Json
    $config.inbounds = @(@{
        type='tun'; tag='tun-in'; interface_name=$Settings.TunName
        address=@('172.31.255.1/30','fdfe:dcba:9876::1/126')
        mtu=1500; auto_route=$true; strict_route=$false; dns_mode='native'; stack='mixed'
        route_exclude_address=@($tunExclusions)
    }) + $config.inbounds
}
$name = if ($ProxyOnly) { 'config.proxy.json' } else { 'config.json' }
if ($OutputName) {
    if ([IO.Path]::GetFileName($OutputName) -ne $OutputName) { throw 'OutputName must be a file name.' }
    $name = $OutputName
}
$path = Join-Path $Root $name
$json = $config | ConvertTo-Json -Depth 30
[IO.File]::WriteAllText($path, $json, [Text.UTF8Encoding]::new($false))
& $Settings.PythonPath -B (Join-Path $Root 'gateway\stable_json.py') $path
if ($LASTEXITCODE -ne 0) { throw "Could not canonicalize $name" }
& $Core check -D $Root -c $path
if ($LASTEXITCODE -ne 0) { throw "sing-box rejected $name" }
$upstreamMode = if ($embeddedUpstream) { 'subscription' } else { 'sakuracat' }
$nodeCount = if ($embeddedUpstream) { [int]$embeddedUpstream.count } else { 0 }
Write-StateJson (Join-Path $StateDir 'upstream.json') @{Mode=$upstreamMode;NodeCount=$nodeCount;UpdatedAt=(Get-Date -Format o)}
Write-Output "Configuration validated: $name"
