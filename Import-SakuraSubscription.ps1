param([string]$Url = '')
. "$PSScriptRoot\Common.ps1"

$urlFile = Join-Path $StateDir 'sakura-subscription-url.txt'
if (-not $Url) {
    $vortex = Join-Path $env:APPDATA 'SakuraCat\vortex.json'
    if (-not (Test-Path -LiteralPath $vortex)) {
        throw 'SakuraCat subscription was not found. Run with -Url <subscription-url>, or start SakuraCat once to create vortex.json.'
    }
    $profile = Get-Content -LiteralPath $vortex -Raw -Encoding UTF8 | ConvertFrom-Json
    $Url = [string]$profile.subscription_info.subscription_url
}
if (-not $Url -or $Url -notmatch '^https?://') {
    throw 'A valid http(s) subscription URL is required.'
}
[IO.File]::WriteAllText($urlFile, $Url.Trim(), [Text.UTF8Encoding]::new($false))

try {
    $acl = Get-Acl -LiteralPath $urlFile
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRule($rule) }
    foreach ($identity in @("$env:USERDOMAIN\$env:USERNAME", 'NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators')) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'Allow')
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $urlFile -AclObject $acl
} catch {
    Write-Warning "Could not restrict subscription file ACL: $($_.Exception.Message)"
}

& "$PSScriptRoot\Update-SakuraUpstream.ps1" -Force
if ($LASTEXITCODE -ne 0) { throw 'Subscription import did not produce usable nodes.' }
$status = Get-Content -LiteralPath (Join-Path $StateDir 'sakura-subscription.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Write-Output ("Subscription imported: {0} nodes at {1}" -f $status.count, $status.updated_at)