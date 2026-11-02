# scripts

安装、订阅、诊断和一次性维护脚本。日常启动、停止、重启和状态入口仍在项目根目录。

| 文件 | 用途 |
|---|---|
| `Initialize-Project.ps1` | 检测网卡和 Python，生成 `settings.local.json` |
| `Install-Core.ps1` | 下载并校验 sing-box |
| `Install-Rules.ps1` | 下载并校验规则集 |
| `Install-Autostart.ps1` / `Disable-Autostart.ps1` | 注册/移除 `UnifiedSingBox` 计划任务 |
| `Enable-Autostart.cmd` / `Disable-Autostart.cmd` | 上述自启脚本的双击包装 |
| `Preflight.ps1` | 不修改 TUN 和系统代理的代理预检 |
| `Import-SakuraSubscription.ps1` | 导入 Sakura 订阅 |
| `Apply-Upstream.ps1` | 切换订阅/SakuraCat 上游 |
| `Switch-SakuraRegion.ps1` | 查看或手动切换地区 |
| `Configure-Codex-Switch.py` | 写入或移除 Codex 本地网关配置 |
| `Health.py` | DNS/TLS/HTTP/API 健康检查 |

`Generate-Config.ps1`、`Update-SakuraUpstream.ps1`、`Service.ps1`、`Common.ps1` 和
`Rollback-V1.ps1` 是运行时或恢复入口，保留在根目录。
