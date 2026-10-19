# 维护指南

以下命令均在项目根目录运行。需要 TUN 或系统代理的操作请在管理员 PowerShell 中执行。

## 初始化

```powershell
.\Initialize-Project.ps1
.\Install-Core.ps1
```

初始化只写 `settings.local.json`，不会把网卡名或个人 Python 路径写入仓库文件。

## 常用命令

| 操作 | 命令 |
|---|---|
| 启动 | `.\Start.ps1` 或 `Start.cmd` |
| 停止 | `.\Stop.ps1` 或 `Stop.cmd` |
| 状态 | `.\Status.ps1` |
| 健康检查 | `$python -B .\Health.py` |
| 开启自启 | `.\Install-Autostart.ps1` |
| 取消自启 | `.\Disable-Autostart.ps1` |
| 导出/查看地区 | `.\Switch-SakuraRegion.ps1 -List` |
| 切换地区 | `.\Switch-SakuraRegion.ps1 -Region 日本` |
| 清理临时文件预览 | `.\tools\Clean-Workspace.ps1` |

## 订阅与节点

```powershell
.\Import-SakuraSubscription.ps1
.\Update-SakuraUpstream.ps1 -Force
.\Apply-Upstream.ps1
```

`Import-SakuraSubscription.ps1` 不传 `-Url` 时尝试读取本机 SakuraCat 配置。订阅地址和节点凭据只写入 `state/`。

回退到 SakuraCat 本地代理：

```powershell
.\Apply-Upstream.ps1 -Fallback
```

## 入口目录

- 目录文件：`state/sakura-endpoints.json`
- 可选部署种子：`state/sakura-endpoint-seeds.json`
- 运行配置：`state/sakura-outbounds.runtime.json`
- 清理统计：目录里的 `last_cleanup`
- 短期墓碑过滤：目录里的 `last_run.retired_filter`

维护参数：

| 参数 | 默认 | 说明 |
|---|---:|---|
| `SakuraEndpointRefreshMinutes` | 60 | 候选全集刷新周期 |
| `SakuraEndpointProbeCandidatesPerNode` | 8 | 每节点最多探测数 |
| `SakuraEndpointHistoryPerHost` | 24 | 活跃入口 + 墓碑总上限 |
| `SakuraEndpointSuccessRetentionDays` | 30 | 最近成功入口保留期 |
| `SakuraEndpointFailureRetentionDays` | 7 | 失败入口与墓碑保留期 |

订阅更新后旧节点会被清理，不会无限积累。

## 系统代理

- 关闭时自动指向项目入口；
- 已被 SakuraCat 接管时不抢回；
- 其他代理保持不动；
- 停止服务时仅在当前代理仍属于项目时恢复原设置。

状态字段在 `state/mode.json` 的 `system_proxy` 和 `system_proxy_server`。

## 日志与诊断

| 文件 | 内容 |
|---|---|
| `state/status.json` | 服务、内核和网关 PID |
| `state/mode.json` | 当前模式、节点、上游和代理归属 |
| `state/sakura-selection.json` | 最近一次选线结果 |
| `logs/service.log` | 常驻服务生命周期 |
| `logs/gateway.stderr.log` | Python 网关错误 |
| `logs/sing-box.log` | sing-box 运行日志 |

日志不要直接公开，其中可能包含访问域名、节点地址和本机时间。

## 模型目录

模型菜单在 `state/codex-models.json`，模型路由代码在 `gateway/codex_router.py`。修改后重启 Codex，并使用新聊天验证。详细限制见 [CODEX.md](CODEX.md)。

## 恢复

```powershell
.\Stop.ps1
.\Rollback-V1.ps1
```

执行恢复前先确认没有其他请求依赖当前网关。

## 测试

```powershell
$python = (Get-Content .\settings.local.json -Raw | ConvertFrom-Json).PythonPath
& $python -B -m unittest discover -s tests
```

测试使用本地模拟服务，不访问真实第三方 API，也不切换系统代理。
