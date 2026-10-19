# local-gateway

[简体中文](README.md) | English

A unified local network gateway for Windows: **TUN split tunneling + system proxy + node failover + optional AI/Codex routing**.

- Default endpoint: `127.0.0.1:8799`
- Default TUN interface: `UnifiedBox`

## Features

- HTTP, HTTPS CONNECT, SOCKS5, and the local AI API share port `8799`
- By default, websites and applications in mainland China connect directly; traffic to other destinations uses the proxy
- Optional routing for company domains and internal DNS
- Automatic node selection within the selected region, with failover when a node or entry IP becomes unavailable
- System proxy enabled by default, while allowing SakuraCat to temporarily override it
- Optional Trojan subscription import, without requiring the SakuraCat app
- Optional routing between official Codex models and third-party API models

The project runs only on your local machine and does not provide a public proxy or public API. TUN, system proxy configuration, and automatic startup require administrator privileges.

## Requirements

- Windows 10/11 x64
- PowerShell 5.1 or PowerShell 7
- Python 3.14
- Administrator privileges
- Optional: SakuraCat or a compatible Trojan subscription

## Quick start

Run in **PowerShell as Administrator**:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\Initialize-Project.ps1
.\Install-Core.ps1
.\Install-Rules.ps1
```

The initialization script detects the default network adapter and Python, then generates `settings.local.json`, which is excluded from version control.

For the first launch, choose one of the following upstream options.

**Use SakuraCat's local proxy:** Start SakuraCat, connect to a node, and disable its TUN/enhanced mode. Then run:

```powershell
.\Start.ps1
```

**Import a subscription directly:**

```powershell
.\Import-SakuraSubscription.ps1
.\Start.ps1
```

Check the status:

```powershell
.\Status.ps1
$python = (Get-Content .\settings.local.json -Raw | ConvertFrom-Json).PythonPath
& $python -B .\Health.py
```

## Everyday commands

| Action | Command |
|---|---|
| Start | `.\Start.ps1` or `Start.cmd` |
| Stop | `.\Stop.ps1` or `Stop.cmd` |
| Show status | `.\Status.ps1` or `Status.cmd` |
| Enable automatic startup | `.\Install-Autostart.ps1` |
| Disable automatic startup | `.\Disable-Autostart.ps1` |
| List regions | `.\Switch-SakuraRegion.ps1 -List` |
| Switch region | `.\Switch-SakuraRegion.ps1 -Region 日本` |

Region names must match the names returned by `-List`; `日本` is the name used for Japan in this example.

## Configuration

The repository's `settings.json` provides safe defaults. Put your personal configuration in:

```text
settings.local.json
```

Common fields:

| Field | Purpose |
|---|---|
| `PhysicalInterface` | Physical network adapter name |
| `PythonPath` | Path to Python 3.14 |
| `PublicPort` | Shared gateway port; defaults to `8799` |
| `SakuraRegion` | Selected node region |
| `CompanyDomains` | Company domain suffixes; empty by default |
| `CompanyDns` | Company DNS; empty by default |
| `CompanyProbeHosts` | Company health-check hosts; empty by default |
| `AiUpstream` | Optional AI-compatible upstream; empty by default |

Company domains, DNS, and health checks are optional. Their rules are omitted automatically when they are not configured.

The following are excluded from version control:

- `settings.local.json`
- Subscriptions, node credentials, control secrets, and runtime state under `state/`
- `logs/`, `work/`, and `config.json`
- `gateway/*.local.json`

Do not publish subscription URLs, API keys, full configurations, or logs.

## Node selection

The project uses two selection mechanisms together:

1. Every 60 minutes, run a full latency test within the selected region to select a reachable node.
2. Every 5 minutes, check the current node and select another route if it fails.

When a node has multiple entry IPs, the project first probes TCP + TLS through the physical network adapter and includes only healthy entries in the configuration. Entries are checked every 5 minutes, and the full candidate set is refreshed every 60 minutes. Nodes whose names contain `5倍消耗` (5× usage) are excluded by default.

## Service endpoints

| Function | Address |
|---|---|
| HTTP/HTTPS proxy | `http://127.0.0.1:8799` |
| SOCKS5 proxy | `socks5://127.0.0.1:8799` |
| AI API | `http://127.0.0.1:8799/v1/...` |
| Codex routing | `http://127.0.0.1:8799/codex` |
| Health check | `GET http://127.0.0.1:8799/__health` |
| Current mode | `GET http://127.0.0.1:8799/__status` |

## Documentation

- [Architecture and components (Chinese)](docs/ARCHITECTURE.md)
- [Maintenance, diagnostics, and recovery (Chinese)](docs/MAINTENANCE.md)
- [Codex model routing (Chinese)](docs/CODEX.md)
- [Verification scope (Chinese)](docs/VERIFICATION.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

## Security notes

- TUN takes over system routing. Disable the TUN mode of other VPNs or proxies before enabling it.
- The project listens only on loopback addresses, installs no certificates, and does not decrypt HTTPS traffic.
- Exposing port `8799` to the public internet risks unauthorized use.
- This project is not officially affiliated with SakuraCat, OpenAI, sing-box, or any subscription provider.

## License

[GPL-3.0-or-later](LICENSE)
