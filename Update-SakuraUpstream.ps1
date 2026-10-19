param([int]$MaxAgeHours = 12, [switch]$Force, [switch]$IncludeMultiplier)
. "$PSScriptRoot\Common.ps1"

$urlFile = Join-Path $StateDir 'sakura-subscription-url.txt'
if (-not (Test-Path -LiteralPath $urlFile)) {
    Write-Output 'Subscription URL is not configured; SakuraCat local fallback remains available.'
    exit 0
}
$pythonArgs = @('-B', (Join-Path $Root 'gateway\sakura_subscription.py'),
                '--max-age-hours', [string]$MaxAgeHours)
if ($Force) { $pythonArgs += '--force' }
if ($IncludeMultiplier) { $pythonArgs += '--include-multiplier' }
& $Settings.PythonPath @pythonArgs
exit $LASTEXITCODE