. "$PSScriptRoot\Common.ps1"
if (Get-OwnedProcess) { throw 'The main TUN is already running.' }
if (Get-OwnedProcess 'preflight.json') { Write-Output 'Preflight proxy is already running.'; exit 0 }
if (-not (Test-TcpPort $Settings.SakuraHost $Settings.SakuraPort)) { throw 'SakuraCat local proxy is unavailable.' }
& "$Root\Generate-Config.ps1" -ProxyOnly
foreach ($port in @($Settings.MixedPort, $Settings.DnsPort, $Settings.ControllerPort)) {
    if (Test-TcpPort '127.0.0.1' $port) { throw "Port $port is already occupied." }
}
$process = Start-Process -FilePath $Core -ArgumentList @('run','-D',"`"$Root`"",'-c',"`"$Root\config.proxy.json`"") -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir 'preflight.stdout.log') -RedirectStandardError (Join-Path $LogDir 'preflight.stderr.log')
Write-StateJson (Join-Path $StateDir 'preflight.json') @{CorePid=$process.Id;CoreStartTicks=$process.StartTime.ToUniversalTime().Ticks.ToString()}
Start-Sleep -Seconds 1
if (-not (Test-TcpPort '127.0.0.1' $Settings.MixedPort)) { throw 'Preflight failed to start. See logs\preflight.stderr.log.' }
Write-Output 'Preflight proxy ready on 127.0.0.1:12550. TUN and Windows proxy were not changed.'
