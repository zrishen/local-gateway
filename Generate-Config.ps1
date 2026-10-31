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
$subscriptionPresent = Test-Path -LiteralPath $upstreamFile
if ($subscriptionPresent) {
    $candidate = $null
    $candidateValid = $false
    try {
        $candidate = Get-Content -LiteralPath $upstreamFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $candidateOutbounds = @($candidate.outbounds)
        $hasGroup = @($candidateOutbounds | Where-Object { $_.tag -eq 'sakura' }).Count -gt 0
        $candidateValid = $candidateOutbounds.Count -ge 2 -and $hasGroup
    } catch {
        $candidateValid = $false
    }
    if (-not $candidateValid) {
        # A subscription file is authoritative. Do not replace a working full
        # config with the SakuraCat-only fallback because the file is corrupt.
        throw 'Sakura subscription is present but invalid; keeping config.json untouched.'
    }

    $runtimeFile = Join-Path $StateDir 'sakura-outbounds.runtime.json'
    $rendered = $false
    try {
        $endpointArgs = @('-B', (Join-Path $Root 'gateway\endpoint_catalog.py'),
                          '--input', $upstreamFile, '--output', $runtimeFile,
                          '--settings', (Join-Path $Root 'settings.json'))
        & $Settings.PythonPath @endpointArgs | Out-Null
        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $runtimeFile)) {
            $candidate = Get-Content -LiteralPath $runtimeFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $renderedOutbounds = @($candidate.outbounds)
            $renderedGroup = @($renderedOutbounds | Where-Object { $_.tag -eq 'sakura' }).Count -gt 0
            $rendered = $renderedOutbounds.Count -ge 2 -and $renderedGroup
        }
    } catch {
        $rendered = $false
    }

    if (-not $rendered) {
        # Endpoint probing is network-dependent and can fail during boot or a
        # brief DNS outage. Reuse the last good rendered outbounds instead of
        # silently downgrading to SakuraCat; if neither is usable, fail the
        # generation and leave the live config untouched.
        try {
            $cached = Get-Content -LiteralPath $runtimeFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $cachedOutbounds = @($cached.outbounds)
            $cachedGroup = @($cachedOutbounds | Where-Object { $_.tag -eq 'sakura' }).Count -gt 0
            if ($cachedOutbounds.Count -ge 2 -and $cachedGroup) {
                $candidate = $cached
                $rendered = $true
                Write-Warning 'Endpoint rendering failed; using cached runtime outbounds.'
            }
        } catch {
            $rendered = $false
        }
    }
    if (-not $rendered) {
        throw 'Endpoint candidate rendering failed with no usable cached runtime; keeping config.json untouched.'
    }
    $embeddedUpstream = $candidate
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
    # No subscription file: SakuraCat is the only configured upstream.
    $outbounds += @{type='socks'; tag='sakura'; server=$Settings.SakuraHost; server_port=[int]$Settings.SakuraPort; version='5'}
}
$officialEnabled = $false
$officialSelector = [string]$Settings.CodexOfficialSelector
if ([string]::IsNullOrWhiteSpace($officialSelector)) { $officialSelector = 'codex-official' }
if ($embeddedUpstream -and [bool]$Settings.CodexOfficialEnabled) {
    $officialGroup = @($embeddedUpstream.outbounds | Where-Object {
        $_.tag -eq [string]$Settings.CodexOfficialGroup -and $_.type -eq 'selector'
    }) | Select-Object -First 1
    if ($officialGroup -and @($officialGroup.outbounds).Count -gt 0) {
        # An independent selector is required. Wrapping the global sakura
        # selector would still follow the US/Singapore browser selection.
        $outbounds += @{
            type='selector'
            tag=$officialSelector
            outbounds=@($officialGroup.outbounds)
            default=@($officialGroup.outbounds)[0]
            interrupt_exist_connections=$false
        }
        $officialEnabled = $true
    }
}
if ($officialEnabled) {
    # Insert after sniff and before the process/Direct rules so official Codex
    # keeps its own lane whenever the core is available.
    $routeRules = @(@($routeRules | Select-Object -First 3)) +
        @(@{inbound=@('codex-official-in'); action='route'; outbound=$officialSelector}) +
        @(@($routeRules | Select-Object -Skip 3))
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
if ($officialEnabled) {
    $config.inbounds = @($config.inbounds) + @{
        type='mixed'; tag='codex-official-in'; listen='127.0.0.1'
        listen_port=[int]$Settings.CodexOfficialPort
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
$staging = Join-Path $Root ($name + '.stage')
$backup = Join-Path $Root ($name + '.previous')
$json = $config | ConvertTo-Json -Depth 30
try {
    [IO.File]::WriteAllText($staging, $json, [Text.UTF8Encoding]::new($false))
    & $Settings.PythonPath -B (Join-Path $Root 'gateway\stable_json.py') $staging
    if ($LASTEXITCODE -ne 0) { throw "Could not canonicalize $name" }
    & $Core check -D $Root -c $staging
    if ($LASTEXITCODE -ne 0) { throw "sing-box rejected $name" }
    # Publish only after the staged file passes canonicalization and check.
    # A failed refresh must leave the live config.json untouched.
    if (Test-Path -LiteralPath $path) {
        [IO.File]::Replace($staging, $path, $backup)
    } else {
        Move-Item -LiteralPath $staging -Destination $path
    }
} finally {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Force -ErrorAction SilentlyContinue
    }
}
$upstreamMode = if ($embeddedUpstream) { 'subscription' } else { 'sakuracat' }
$nodeCount = if ($embeddedUpstream) { [int]$embeddedUpstream.count } else { 0 }
Write-StateJson (Join-Path $StateDir 'upstream.json') @{Mode=$upstreamMode;NodeCount=$nodeCount;UpdatedAt=(Get-Date -Format o)}
Write-Output "Configuration validated: $name"
