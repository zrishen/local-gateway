# local-gateway

简体中文 | [English](README.en.md)

Windows 本地统一网络入口：**TUN 分流 + 系统代理 + 节点故障切换 + 可选 AI/Codex 路由**。

- 默认入口：`127.0.0.1:8799`
- 默认 TUN：`UnifiedBox`

## 能做什么

- HTTP、HTTPS CONNECT、SOCKS5 和本地 AI API 共用 `8799`
- 国内网站和应用直连，国外流量走代理
- 可选接入公司域名和内部 DNS
- 当前地区自动选节点；节点或入口 IP 失效时切换
- 系统代理默认开启；SakuraCat 临时覆盖时不会被抢回
- 可选导入 Trojan 订阅，不依赖 SakuraCat App
- 可选 Codex 官方模型与第三方 API 模型路由

项目只在本机运行，不提供公共代理或公共 API。TUN、系统代理和自启需要管理员权限。

## 环境要求

- Windows 10/11 x64
- PowerShell 5.1 或 PowerShell 7
- Python 3.14
- 管理员权限
- 可选：SakuraCat，或一个兼容的 Trojan 订阅

## 快速开始

在**管理员 PowerShell**中运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\Initialize-Project.ps1
.\Install-Core.ps1
.\Install-Rules.ps1
```

初始化脚本会检测默认网卡和 Python，并生成不入库的 `settings.local.json`。

首次启动任选一种上游。

**使用 SakuraCat 本地代理：** 启动 SakuraCat 并连接节点，关闭它的 TUN/增强模式，然后：

```powershell
.\Start.ps1
```

**直接导入订阅：**

```powershell
.\Import-SakuraSubscription.ps1
.\Start.ps1
```

检查状态：

```powershell
.\Status.ps1
$python = (Get-Content .\settings.local.json -Raw | ConvertFrom-Json).PythonPath
& $python -B .\Health.py
```

## 日常命令

| 操作 | 命令 |
|---|---|
| 启动 | `.\Start.ps1` 或 `Start.cmd` |
| 停止 | `.\Stop.ps1` 或 `Stop.cmd` |
| 查看状态 | `.\Status.ps1` 或 `Status.cmd` |
| 开启自启 | `.\Install-Autostart.ps1` |
| 取消自启 | `.\Disable-Autostart.ps1` |
| 查看地区 | `.\Switch-SakuraRegion.ps1 -List` |
| 切换地区 | `.\Switch-SakuraRegion.ps1 -Region 日本` |

## 配置

仓库里的 `settings.json` 是安全默认值，个人配置写入：

```text
settings.local.json
```

常用字段：

| 字段 | 作用 |
|---|---|
| `PhysicalInterface` | 物理网卡名称 |
| `PythonPath` | Python 3.14 路径 |
| `PublicPort` | 总入口端口，默认 `8799` |
| `SakuraRegion` | 当前节点地区 |
| `CompanyDomains` | 公司域名后缀，默认空 |
| `CompanyDns` | 公司 DNS，默认空 |
| `CompanyProbeHosts` | 公司健康检查主机，默认空 |
| `AiUpstream` | 可选 AI 兼容上游，默认空 |

公司域名、DNS 和健康检查都是可选项；不配置时相关规则会自动省略。

以下内容不会进入版本库：

- `settings.local.json`
- `state/` 下的订阅、节点凭据、控制口令和运行状态
- `logs/`、`work/`、`config.json`
- `gateway/*.local.json`

不要公开订阅链接、API Key、完整配置或日志。

## 节点选择

项目同时使用两层选择：

1. 当前地区每 60 分钟完整测速，选择可达节点。
2. 每 5 分钟检查当前节点；失效时重新选线。

同一节点有多个入口 IP 时，项目会先在物理网卡上测试 TCP + TLS，再只把健康入口放入配置。入口每 5 分钟检查，候选全集每 60 分钟刷新。名称带“5倍消耗”的节点默认排除。

## 服务地址

| 功能 | 地址 |
|---|---|
| HTTP/HTTPS 代理 | `http://127.0.0.1:8799` |
| SOCKS5 代理 | `socks5://127.0.0.1:8799` |
| AI API | `http://127.0.0.1:8799/v1/...` |
| Codex 路由 | `http://127.0.0.1:8799/codex` |
| 健康检查 | `GET http://127.0.0.1:8799/__health` |
| 当前模式 | `GET http://127.0.0.1:8799/__status` |

## 文档

- [架构与组件](docs/ARCHITECTURE.md)
- [维护、诊断与恢复](docs/MAINTENANCE.md)
- [Codex 模型路由](docs/CODEX.md)
- [验证范围](docs/VERIFICATION.md)
- [第三方声明](THIRD_PARTY_NOTICES.md)

## 安全说明

- TUN 会接管系统路由，开启前应关闭其他 VPN/代理的 TUN。
- 项目只监听回环地址，不安装证书，也不解密 HTTPS。
- 公网暴露 `8799` 会带来未授权使用风险。
- 本项目与 SakuraCat、OpenAI、sing-box 或任何订阅服务商没有官方关联。

## License

[GPL-3.0-or-later](LICENSE)
