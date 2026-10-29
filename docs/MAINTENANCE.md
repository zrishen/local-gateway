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

当前节点每 5 分钟通过本地混合代理下载 64 KiB 健康样本，并访问 ChatGPT 验证同一出口。低吞吐门槛为 50 KB/s（400 Kbps）；低吞吐或 ChatGPT 不可达连续出现两次，或者连接、读取中断时，会重新测速整个美国和新加坡候选池；故障触发的切换继续观察两轮，未恢复服务或没有明显改善时回到原节点。完整测速每 60 分钟执行一次，按实际下载吞吐选择，并通过 HTTPS、TUN 和 ChatGPT 验证；新加坡节点更快时不需要等待美国整体失效。

`SakuraSelectionRegions` 定义自动选择的地区池，当前为 `["美国", "新加坡"]`。两个地区的全部非倍消耗节点平等参与，其他地区不会自动使用。`SakuraRegion` 只作为手动切换的默认地区。订阅刷新后如果地区或节点消失，选择器会安全跳过并继续使用池内其他节点。

运行状态位于 `state/sakura-health.json`，包含最近一次健康结果和连续失败次数；不记录节点凭据或完整请求。`state/sakura-selection.json` 是完整测速快照，`state/mode.json` 的当前节点以 sing-box 控制器实时值为准。

## 系统代理

- 关闭时自动指向项目入口；
- 已被 SakuraCat 接管时不抢回；
- 其他代理保持不动；
- 停止服务时仅在当前代理仍属于项目时恢复原设置。

状态字段在 `state/mode.json` 的 `system_proxy` 和 `system_proxy_server`。

订阅模式的控制接口偶尔超时不会被立即当成不可用：项目会暂时保持 `Rule`，默认容忍 30 秒，避免正常代理流量被错误切到 `Direct`。`state/mode.json` 的 `embedded_state` 会显示 `ready`、`grace` 或 `unavailable`，可用参数 `EmbeddedStateGraceSeconds` 调整容忍时间。

## 日志与诊断

| 文件 | 内容 |
|---|---|
| `state/status.json` | 服务、内核和网关 PID |
| `state/mode.json` | 当前模式、节点、上游和代理归属 |
| `state/sakura-selection.json` | 最近一次选线结果 |
| `logs/service.log` | 常驻服务生命周期 |
| `logs/gateway.stderr.log` | Python 网关错误 |
| `logs/ai-upstream.jsonl` | AI 上游连接阶段、重试次数、状态码和请求编号（不记录地址、密钥或请求正文） |
| `logs/sing-box.log` | sing-box 运行日志 |

日志不要直接公开，其中可能包含访问域名、节点地址和本机时间。

### API 上游连接与 502

`gateway/upstream-dns-overrides.local.json` 可配置域名到 IP 的本机映射（例如 `{"api.example.test":"192.0.2.8"}`）。配置后始终连接指定 IP，不受系统代理/TUN 的 DNS 结果影响；HTTPS 的 Host、SNI 和证书校验仍使用原域名。映射需要自行维护，IP 变化后更新并重启网关。默认映射为空，本机配置不会提交。

当前 AI 上游显式配置的入口属于内网（RFC1918 或 IPv6 ULA）时，生成配置会将该 IP 的 `/32` 或 `/128` 加入 TUN 排除列表，让它使用原有物理网络路由，避免 TUN 自动更新重启打断 API 连接。仅排除当前上游的精确地址，不会排除整段内网、其他映射或公网 API。此项修改需重新生成配置并重启项目服务，只重启 Python 网关不能改变 Windows 路由。

AI 上游连接超时为 12 秒，连接成功后的读超时为 600 秒。连接阶段失败，或上游明确返回 502/503/504 时，最多重试一次，间隔 0.5 秒；401/403/429 不重试。请求发送后断线、等待响应超时以及已经开始的流式输出都不会自动重放，避免在结果不明确时重复生成。持续连接失败或空体 502/503/504 会返回带请求编号的 JSON 错误，可在 `logs/ai-upstream.jsonl` 对照原因与阶段。

有限重试只能缓解瞬时故障，不能修复持续的上游服务或网络故障。

## 模型目录

官方模型按北京时间每天 09:10、18:00 自动同步，停机错过时段后补同步一次；第三方模型在 `gateway/codex-api-models.json` 维护，合并菜单生成到 `state/codex-models.generated.json`。同步状态见 `state/codex-catalog-sync.json`，需要立即刷新可运行 `python -B .\gateway\codex_catalog.py --force`。Codex 的 `model_catalog_json` 应指向生成菜单，更新后重启 Codex，并使用新聊天验证。详细限制见 [CODEX.md](CODEX.md)。

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
