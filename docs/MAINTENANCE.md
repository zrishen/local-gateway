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
| 安全刷新网关后端 | `.\Restart-Gateway.ps1` |
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

新增的 `codex-official-in` 入站和 `codex-official` 选择器属于 sing-box 配置变更，必须执行一次完整的
`Stop.ps1` / `Start.ps1` 才会重新生成 `config.json`。`Restart-Gateway.ps1` 只重启 Python 后端，
不会应用新入站；在完整重启前 Router 会自动回退到原有代理通道。

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

当前节点每 5 分钟通过本地混合代理下载 64 KiB 健康样本，并访问 ChatGPT 验证同一出口。默认有效吞吐下限 15 KB/s、正文下限 30 KB/s，并计算 0–1 分数；低分或 ChatGPT 不可达连续出现两次，或者连接、读取中断时，只比较当前节点和上次完整测速的前三名（并额外纳入池内其他地区各两个），按 60/120/300/600/1200/1800 秒退避。低分节点之间要求分数高 0.08 且有效速度高 10%；高分节点要求更大优势。切换继续观察两轮；原节点断流时，可连通且 ChatGPT 可达的新节点即使低于门槛也保留。完整测速每 60 分钟执行一次，按评分选择，并通过 HTTPS、TUN 和 ChatGPT 验证；新加坡或日本节点更快时不需要等待美国整体失效。相关参数为 `SakuraMinimumEffectiveKBps`、`SakuraMinimumBodyKBps`、`SakuraMaxFirstByteMs`、`SakuraHealthyScore`、`SakuraSwitchScoreMargin`、`SakuraSwitchRatio`、`SakuraRecoveryBackoffSeconds` 和 `SakuraRecoveryMaxSeconds`。

回滚前会分别验证原节点和新节点：控制器不可达时保留观察且不拉黑；原节点可用时切回，只有新节点确实不可用才拉黑；原节点不可用而新节点可用则保留新节点；两边都不可用才拉黑双方并立即触发定向恢复。单个节点的多个入口会逐个重试，单入口失败只进入 60 秒内存冷却，节点所有入口都失败才被拒绝。

`SakuraSelectionRegions` 定义自动选择的地区池，当前为 `["美国", "新加坡", "日本"]`。三个地区的全部非倍消耗节点平等参与，其他地区不会自动使用。`SakuraRegion` 只作为手动切换的默认地区。订阅刷新后如果地区或节点消失，选择器会安全跳过并继续使用池内其他节点。

扫描参数：`SakuraCandidateProbeLimit`（默认 20）限制进入质量验证的候选数，`SakuraScanBudgetSeconds`（默认 180）限制质量阶段总耗时，`SakuraRejectionSeconds`（默认 300）是失败节点冷却，`SakuraRestoreVerifySeconds`（默认 15）是回滚验证预算，`SakuraProbeTimeoutSeconds` 是单次入口探测超时。完整测速候选还会过滤掉当前 `config.json` 中不存在的节点，避免订阅刷新期间探针配置缺项。

运行状态位于 `state/sakura-health.json`，包含最近一次健康结果、连续失败次数、恢复退避和待观察切换；不记录节点凭据或完整请求。`state/sakura-selection.json` 是完整测速快照，`state/mode.json` 的当前节点以 sing-box 控制器实时值为准。进程重启时若存在待观察切换，会先完成一次健康观察再全量测速；`gateway_activity.py` 的入口计时只在内存中维护，`state/` 不记录请求内容。

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
| `state/sakura-selection.json` | 最近一次普通流量选线结果 |
| `state/codex-official-route.json` | 官方 Codex 独立通道状态 |
| `logs/service.log` | 常驻服务生命周期 |
| `logs/gateway-entry.stderr.log` | 固定入口和后端切换状态 |
| `logs/gateway.stderr.log` | Python 网关错误 |
| `logs/gateway-backup.stderr.log` | 备用 Python 网关错误 |
| `logs/ai-upstream.jsonl` | AI 上游连接阶段、重试次数、状态码和请求编号（不记录地址、密钥或请求正文） |
| `logs/sing-box.log` | sing-box 运行日志 |

日志不要直接公开，其中可能包含访问域名、节点地址和本机时间。

`state/status.json` 会分别记录 `GatewayEntryPid`、`GatewayPid` 和 `GatewayBackupPid`。只检查主网关 PID 无法判断入口是否仍可用；客户端始终使用 `PublicPort`（默认 `8799`），`GatewayBackendPorts`（默认 `18801`、`18802`）只供本机入口转发。主后端退出时新连接会自动转到备用后端；已经建立的单条流无法跨进程迁移。后端刷新开始时先写入 `state/gateway-entry-route.json`，让公开入口把新连接切到待保留的后端，再排空并重建待更新后端；刷新结束后恢复首选端口。这个热切换不要求主端先完全空闲，因此持续使用不会把刷新卡住。文件缺失或损坏时会自动回到首选主端。

### API 上游连接与 502

`gateway/upstream-dns-overrides.local.json` 可配置域名到 IP 的本机映射（例如 `{"api.example.test":"192.0.2.8"}`）。配置后始终连接指定 IP，不受系统代理/TUN 的 DNS 结果影响；HTTPS 的 Host、SNI 和证书校验仍使用原域名。映射需要自行维护，IP 变化后更新并重启网关。默认映射为空，本机配置不会提交。

当前 AI 上游显式配置的入口属于内网（RFC1918 或 IPv6 ULA）时，生成配置会将该 IP 的 `/32` 或 `/128` 加入 TUN 排除列表，让它使用原有物理网络路由，避免 TUN 自动更新重启打断 API 连接。仅排除当前上游的精确地址，不会排除整段内网、其他映射或公网 API。此项修改需重新生成配置并重启项目服务，只重启 Python 网关不能改变 Windows 路由。

AI 上游连接超时为 12 秒；响应头等待默认 360 秒，流式空闲默认 360 秒，分别可用 `AiUpstreamHeadersTimeoutSeconds` 和 `AiUpstreamStreamIdleSeconds` 调整。连接阶段失败，或上游明确返回 502/503/504 时，最多重试一次，间隔 0.5 秒；401/403/429 不重试。请求发送后断线、等待响应超时以及已经开始的流式输出都不会自动重放，避免在结果不明确时重复生成。持续连接失败或空体 502/503/504 会返回带请求编号的 JSON 错误，可在 `logs/ai-upstream.jsonl` 对照原因与阶段。若上游返回 `x-process-time`，日志会记录为 `upstream_process_seconds`（毫秒自动换算为秒），用于区分公司上游排队和本地代理耗时。

Codex 官方路由的 `Selected upstream route unavailable` 是本地返回的 502 兜底：它表示 TLS/代理建连、请求发送或读取官方响应头失败，不是模型内容错误。上传总预算耗尽会返回 504，并记录 `upload_timed_out: true`。建连在正文发送前失败时安全重试一次；发送或读取开始后不再重放。`logs/codex-routes.jsonl` 的 `failure_stage` 会标明 `connect`、`send`、`headers`、`client_write` 或 `response_body`；`route_lane`/`route_node` 显示是否走独立官方通道，`request_bytes`/`decoded_request_bytes`/`forwarded_request_bytes` 显示压缩前后大小。`connect_seconds: null` 指向建连阶段，`upstream_connect_attempts` 显示尝试次数，`connect_retry_error` 显示被安全重试前的建连异常，`error` 显示最终异常类型。若官方请求持续失败，先查看 `state/codex-official-route.json` 的 `ready/degraded` 和当前节点。

有限重试只能缓解瞬时故障，不能修复持续的上游服务或网络故障。

## 模型目录

官方模型按北京时间每天 09:10、18:00 自动同步，停机错过时段后补同步一次；第三方模型在 `gateway/codex-api-models.json` 维护，合并菜单生成到 `state/codex-models.generated.json`。同步状态见 `state/codex-catalog-sync.json`，需要立即刷新可运行 `python -B .\gateway\codex_catalog.py --force`。Codex 的 `model_catalog_json` 应指向生成菜单，更新后重启 Codex，并使用新聊天验证。详细限制见 [CODEX.md](CODEX.md)。

### Claude Code 官方模型

Claude Code 仍使用 `127.0.0.1:8799/v1/messages`。模型名在 `CodexAnthropicOfficialModels`
（默认 `["openai/gpt-6.1-sol"]`）中时，网关把 Anthropic 请求转换为官方 Codex Responses
并转换回 Anthropic SSE；其他模型不受影响。凭证只读取 `CODEX_HOME/auth.json`
（默认 `~/.codex/auth.json`），要求 ChatGPT 登录，网关和日志都不复制或记录 token。

`CodexAnthropicBridgeEnabled=false` 时官方模型返回 503；
`CodexAnthropicBridgeMaxBodyBytes` 控制请求体上限（默认 32 MiB），
`CodexAnthropicBridgeClientVersion` 可覆盖发出的 Codex CLI 版本号（留空自动探测）。
把模型从列表移除即可回到原有第三方转发路径，无需改代码。

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

## 测速与维护的连接保护

完整选速使用 `gateway/isolated_probes.py` 启动短暂的无 TUN 核心，各候选使用独立回环端口。
下载依次执行，避免本机带宽竞争。初测前三名分别取三次样本的中位数，之后才在真实路径验证最终候选。
探测进程和含节点配置的临时目录会在退出时清理；探测失败时保留主路由。

健康检查与 TUN 检查都按 HTTP 解码后的完整正文计算有效吞吐，并计入建连和首字节等待。
TUN 检查拒绝截断正文，支持 HTTP chunked 传输解码。`effective_throughput_kbps` 用于健康门槛和节点排序；
`throughput_kbps` 只表示握手后的正文速度，用于判断慢在建连还是传输。完整测速也遵守失败候选的
`SakuraRejectionSeconds` 冷却（默认 5 分钟）。

入口目录变化只暂存新配置，不再为定时更新立即重启健康核心。
`gateway/core_refresh.py` 只有在 15 分钟内的健康记录显示恢复失败、没有待观察切换、
且 `/__status` 的 `active_ai_requests` 为 0 时才允许应用待更新配置。
缺少健康记录或网关不可达时延后处理；手动重启和下次启动也会加载磁盘上的新配置。

断流时有一条有界放行路径：健康记录为 `disconnected`、恢复失败次数达到
`SakuraEndpointReloadRecoveryFailures`（默认 2）、且两个后端中最老的 AI 请求已超过
`SakuraEndpointReloadStaleRequestSeconds`（默认 600 秒）时，允许应用配置并重启核心。
该操作会中断所有 TUN 流量约 93 秒；设为 0 可关闭此放行。`/__status` 通过
`oldest_ai_request_age_seconds` 暴露最老请求年龄。

`Generate-Config.ps1` 先把新配置写入 `.stage`，经 `stable_json.py` 规范化和
`sing-box check` 校验后才原子替换；校验或写入失败保留旧 `config.json`。
