# local-gateway

简体中文 | [English](README.en.md)

local-gateway 是一个在本机运行的 Windows 网络入口，用来统一管理日常上网、代理和可选的 AI 服务连接。

## 有什么用

- 国内网站和应用直连，其他流量走代理。
- 自动选择可用线路，节点异常时切换。
- 统一接管浏览器和应用的代理设置。
- 可选接入公司内网。
- 可选导入 Trojan 订阅，或配合 SakuraCat 使用。
- 可选为 Codex 和 AI 服务提供本地路由。
- 可选让 Claude Code 使用 Codex 官方模型（`codex/gpt-6.1-sol`、`codex/gpt-6-astra`）。

项目只在本机运行，不提供公共代理或公共 API。

## 使用前准备

- Windows 10/11 x64。
- PowerShell 5.1 或 PowerShell 7。
- Python 3.14。
- 管理员权限。
- 一个可用的 SakuraCat 本地代理，或兼容的 Trojan 订阅。

## 开始使用

在管理员 PowerShell 中进入项目目录，先完成安装：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\Initialize-Project.ps1
.\Install-Core.ps1
.\Install-Rules.ps1
```

然后选择一种上游方式。

**使用 SakuraCat：**

启动 SakuraCat，连接节点并关闭它的 TUN/增强模式，然后运行：

```powershell
.\Start.ps1
```

**直接导入订阅：**

```powershell
.\Import-SakuraSubscription.ps1
.\Start.ps1
```

启动后查看状态：

```powershell
.\Status.ps1
```

状态正常即可开始使用。需要停止时运行 `.\Stop.ps1`。

## 日常操作

| 操作 | 命令 |
|---|---|
| 启动 | `.\Start.ps1` 或 `Start.cmd` |
| 停止 | `.\Stop.ps1` 或 `Stop.cmd` |
| 查看状态 | `.\Status.ps1` 或 `Status.cmd` |
| 开启自启 | `.\Install-Autostart.ps1` |
| 取消自启 | `.\Disable-Autostart.ps1` |
| 查看可选地区 | `.\Switch-SakuraRegion.ps1 -List` |
| 切换地区 | `.\Switch-SakuraRegion.ps1 -Region <地区名>` |

如果连接异常，先运行 `.\Status.ps1` 查看当前状态，再参考[维护与诊断](docs/MAINTENANCE.md)。

## 注意事项

- 首次安装、启用 TUN 和配置系统代理需要管理员权限。
- 开启 TUN 前，应关闭其他 VPN 或代理软件的 TUN 模式。
- 项目仅供本机使用，不要把入口暴露到公网。
- 不要公开订阅链接、API Key、配置或日志。

## 更多文档

- [架构与组件](docs/ARCHITECTURE.md)
- [维护、诊断与恢复](docs/MAINTENANCE.md)
- [Codex 模型路由](docs/CODEX.md)
- [验证范围](docs/VERIFICATION.md)
- [第三方声明](THIRD_PARTY_NOTICES.md)

## License

[GPL-3.0-or-later](LICENSE)
