param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$rootPrefix = $projectRoot.TrimEnd('\') + '\'
$targets = [Collections.Generic.List[object]]::new()

function Add-CleanupFile([IO.FileInfo]$File, [string]$Reason) {
    $absolute = [IO.Path]::GetFullPath($File.FullName)
    if (-not $absolute.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Cleanup target is outside the project.'
    }
    # Never follow a linked directory or file, including links inside work/.
    $node = Get-Item -LiteralPath $absolute -Force
    while ($node -and $node.FullName -ne $projectRoot) {
        if ($node.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Cleanup encountered linked content.' }
        $node = if ($node -is [IO.FileInfo]) { $node.Directory } else { $node.Parent }
    }
    $targets.Add([pscustomobject]@{Path=$absolute;Bytes=$File.Length;Reason=$Reason})
}

# Fixed generated files; never enumerate runtime state, live logs, or backups.
foreach ($relative in @('work\sing-box-1.14.3-windows-amd64.zip','work\test_mode_switch.py',
                         'config.next.json','config.proxy.json','config.stage.json')) {
    $path = Join-Path $projectRoot $relative
    if (Test-Path -LiteralPath $path -PathType Leaf) { Add-CleanupFile (Get-Item -LiteralPath $path) 'Regenerable or superseded setup artifact' }
}
$scratch = Join-Path $projectRoot 'work\codex-switch'
if (Test-Path -LiteralPath $scratch) {
    foreach ($file in Get-ChildItem -LiteralPath $scratch -File -Force) {
        if ($file.Extension -in @('.json','.jsonl','.log','.txt') -or $file.Name -eq 'models.test.json') {
            Add-CleanupFile $file 'Generated diagnostic output; canonical evidence is in docs/verification'
        }
    }
}
foreach ($directory in @('', 'gateway','tests','tools\codex','work\codex-switch')) {
    $cache = Join-Path (Join-Path $projectRoot $directory) '__pycache__'
    if (Test-Path -LiteralPath $cache) {
        foreach ($file in Get-ChildItem -LiteralPath $cache -File -Force) {
            if ($file.Extension -eq '.pyc') { Add-CleanupFile $file 'Python bytecode cache' }
        }
    }
}

$total = ($targets | Measure-Object -Property Bytes -Sum).Sum
if ($null -eq $total) { $total = 0 }
if (-not $Apply) {
    $targets | Format-Table Path,Bytes,Reason -AutoSize
    Write-Output ('Preview: {0} files, {1:N2} MiB. Use -Apply to remove these files.' -f $targets.Count, ($total/1MB))
    exit 0
}
foreach ($target in $targets) {
    Remove-Item -LiteralPath $target.Path -Force -ErrorAction Stop
}
$manifest = [pscustomobject]@{At=(Get-Date -Format o);RemovedFiles=$targets.Count;RemovedBytes=$total;Files=@($targets)}
$reportPath = Join-Path $projectRoot 'work\cleanup-last.json'
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $reportPath -Encoding UTF8
Write-Output ('Removed {0} files, {1:N2} MiB. Report: {2}' -f $targets.Count, ($total/1MB), $reportPath)
