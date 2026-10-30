# local-gateway

[简体中文](README.md) | English

local-gateway is a local network gateway for Windows. It simplifies everyday networking, proxying, and optional AI service connections from one place.

## What it is for

- Connect to domestic sites and applications directly while routing other traffic through a proxy.
- Select a working route automatically and switch when a node becomes unhealthy.
- Manage the proxy settings used by browsers and applications.
- Optionally connect to company networks.
- Optionally import a Trojan subscription or work with SakuraCat.
- Optionally provide local routing for Codex and AI services.
- Optionally let Claude Code use official Codex models.
- Optionally generate or edit images with Codex CLI's built-in image generation.

The project runs only on your local machine. It does not provide a public proxy or public API.

## Before you start

- Windows 10/11 x64.
- PowerShell 5.1 or PowerShell 7.
- Python 3.14.
- Administrator privileges.
- A working SakuraCat local proxy or a compatible Trojan subscription.

## Getting started

Open PowerShell as Administrator, enter the project directory, and complete the installation:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\Initialize-Project.ps1
.\Install-Core.ps1
.\Install-Rules.ps1
```

Then choose one upstream option.

**Use SakuraCat:**

Start SakuraCat, connect to a node, and disable its TUN/enhanced mode. Then run:

```powershell
.\Start.ps1
```

**Import a subscription directly:**

```powershell
.\Import-SakuraSubscription.ps1
.\Start.ps1
```

Check the status after startup:

```powershell
.\Status.ps1
```

Once the status is healthy, you can start using it. Run `.\Stop.ps1` when you want to stop it.

## Everyday use

| Action | Command |
|---|---|
| Start | `.\Start.ps1` or `Start.cmd` |
| Stop | `.\Stop.ps1` or `Stop.cmd` |
| Show status | `.\Status.ps1` or `Status.cmd` |
| Enable automatic startup | `.\Install-Autostart.ps1` |
| Disable automatic startup | `.\Disable-Autostart.ps1` |
| List available regions | `.\Switch-SakuraRegion.ps1 -List` |
| Switch region | `.\Switch-SakuraRegion.ps1 -Region <region>` |

If the connection is unhealthy, run `.\Status.ps1` first, then see [Maintenance and diagnostics](docs/MAINTENANCE.md).

## Notes

- The first installation, TUN setup, and system proxy configuration require administrator privileges.
- Disable TUN mode in other VPN or proxy applications before enabling TUN here.
- This project is for local use only. Do not expose it to the public internet.
- Do not publish subscription URLs, API keys, configurations, or logs.

## More documentation

- [Architecture and components (Chinese)](docs/ARCHITECTURE.md)
- [Maintenance, diagnostics, and recovery (Chinese)](docs/MAINTENANCE.md)
- [Codex model routing (Chinese)](docs/CODEX.md)
- [Codex image generation](tools/codex-imagegen/README.md)
- [Verification scope (Chinese)](docs/VERIFICATION.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

## License

[GPL-3.0-or-later](LICENSE)
