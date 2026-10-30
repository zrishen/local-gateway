# 架构

local-gateway 在本机把系统代理、TUN、规则分流和可选的 AI 路由组合成一个入口。

## 数据流

```text
应用
├─ 系统代理 127.0.0.1:8799（固定入口）
│  ├─ 主后端 127.0.0.1:18801
│  └─ 热备后端 127.0.0.1:18802
│     ├─ HTTP / CONNECT / SOCKS5 → 127.0.0.1:12550 → sing-box
│     ├─ 官方 Codex → 127.0.0.1:12551 → codex-official（仅美国）
│     ├─ Claude Code 官方模型 → /v1/messages → Anthropic/Responses 桥接 → 官方 Codex
│     └─ 本地 AI API → 协议清洗/转换 → 可选上游
└─ 系统路由 → UnifiedBox TUN → sing-box
                              ├─ 国内/私有地址 → direct
                              ├─ 可选公司域名 → company-direct
                              └─ 其他流量 → sakura 节点组（美国/新加坡/日本）
```

## 主要组件

| 路径 | 职责 |
|---|---|
| `gateway/stable_entry.py` | 固定公开端口，检测主/备后端并原样转发 TCP 字节 |
| `gateway/unified_gateway.py` | 内部后端：HTTP/SOCKS 转发、AI 入口和 Codex 路由 |
| `gateway/mode_monitor.py` | 系统代理策略、Rule/Direct 模式和状态写入 |
| `Generate-Config.ps1` | 生成 sing-box 配置；不直接手改 `config.json` |
| `gateway/sakura_subscription.py` | 订阅解析、地区分组和节点出站生成 |
| `gateway/endpoint_catalog.py` | 入口 IP 发现、TCP/TLS 探测、健康选择和过期清理 |
| `gateway/route_policy.py` | 节点评分、切换迟滞和恢复退避 |
| `gateway/latency_selector.py` | 普通流量节点测速、定向恢复与切换 |
| `gateway/official_route.py` | 官方 Codex 独立美国选择器和真实请求反馈 |
| `gateway/codex_router.py` | 可选 Codex 模型路由、压缩和阶段预算 |
| `Service.ps1` | 常驻服务、内核/网关监督和配置刷新 |
| `Supervisor.ps1` | 未安装计划任务时的兼容启动入口 |

## 固定入口与后端热备

客户端只使用 `127.0.0.1:8799`。`stable_entry.py` 在这个端口上保留一个轻量 TCP 入口，并把原始字节转发到 `GatewayBackendPorts` 中的主后端；主后端退出、连接失败或被服务脚本主动重启时，它改走备用后端。两个后端执行相同的 HTTP、CONNECT、SOCKS5、AI 和 Codex 路由逻辑，因此客户端地址与系统代理配置不需要改变。

主后端负责模式监控和节点测速，备用后端不重复运行这些后台任务。入口按秒检查活动后端，主后端恢复后新连接回到主后端。已经建立的 TCP/TLS 流不迁移；后端进程在中途退出时，该条流仍会结束，但固定入口保持监听，后续请求会继续到达备用后端。后端代码刷新先等待备用和主后端的 AI 请求空闲，再依次重启两个后端；AI 维护状态会同时查询两个后端，避免其中一个仍在流式响应时重启核心。

## 配置层次

1. `settings.json`：可公开的默认值。
2. `settings.local.json`：当前机器的覆盖值，位于 `.gitignore`。
3. `config.json`：由配置生成器产出，包含本机控制口令，不提交。
4. `state/`：订阅、节点凭据、入口历史、运行状态和密钥，不提交。

Python 使用 `gateway/settings_config.py` 合并前两层；PowerShell 使用 `Common.ps1` 的相同规则。

## 系统代理状态

- 关闭：项目开启 `127.0.0.1:<PublicPort>`。
- 指向项目：保持不动。
- 指向 SakuraCat `<SakuraHost>:<SakuraPort>`：保留 SakuraCat 覆盖。
- 指向其他代理：不强行改写。

服务停止时，只有当前系统代理仍指向项目时才恢复保存的原始设置。

## 节点与入口选择

订阅中的节点先组成 `sakura → 地区组 → 节点`。若节点有多个健康入口，节点本身会变成入口级 `urltest`：

```text
sakura-region-us
└─ sakura-067 (urltest)
   ├─ sakura-067-ep1 → entry IP A
   └─ sakura-067-ep2 → entry IP B
```

- 地区池：`SakuraSelectionRegions` 当前为美国、新加坡和日本。三个地区共同测速，不设置美国优先权；其他地区不参与自动选择。
- 节点层：完整测速默认每 60 分钟；独立的无 TUN 核心为可达候选提供独立回环入口，测速不改变主选择器。初测按控制延迟排序后最多取 `SakuraCandidateProbeLimit`（20）个候选，前三名各测三次取中位数，按有效吞吐、正文吞吐、首字节和稳定性计算分数；正文速度用于诊断握手后的传输效率。质量阶段受 `SakuraScanBudgetSeconds`（180）总预算约束，超时只返回已验证结果。候选切入主路由后再验证 TUN 和 ChatGPT。失败候选冷却默认为 5 分钟（`SakuraRejectionSeconds`）。
- 节点恢复：每 5 分钟检查当前节点，默认有效吞吐下限 15 KB/s、正文下限 30 KB/s，并计算 0–1 分数；连续两次低分或 ChatGPT 不可达、一次断流后，只比较当前节点和上次完整测速的前三名（并额外纳入池内其他地区各两个），并按 60/120/300/600/1200/1800 秒退避。低分节点之间要求分数高 0.08 且有效速度高 10%；高分节点要求更大优势，避免近似节点振荡。切换继续观察两轮；原节点断流时，可连通且 ChatGPT 可达的新节点即使低于门槛也保留。
- 回滚验证：两轮观察结束后，回滚前会通过节点自己的入口和 ChatGPT 探测验证原节点与新节点。控制器不可达时保留观察、不拉黑；原节点可用则切回，只有新节点确实不可用（断流或 ChatGPT 不可达）才拉黑；原节点不可用而新节点可用时保留新节点；两边都不可用才拉黑双方并立即触发定向恢复，不再等待下一个健康周期。
- 入口层：sing-box 每 5 分钟检查候选入口；入口切换仍由单个节点的 `urltest` 完成。选择器按节点的全部入口逐个重试，单个死入口只进入 60 秒内存冷却，不会拉黑整个节点；只有所有入口都失败才拒绝节点。
- 入口目录：默认每 60 分钟刷新 DNS 和候选。更新先保存到磁盘，健康主路由不重启；只有新鲜健康记录显示恢复失败、没有待观察切换且 AI 请求空闲时才重启应用。断流且连续恢复失败达到阈值时，如果最老 AI 请求已超过 `SakuraEndpointReloadStaleRequestSeconds`（600 秒），允许有界重载以恢复入口；设为 0 关闭该放行。
- 历史清理：成功入口 30 天、失败入口 7 天、每域名合计最多 24 条。
- 重启恢复：选择器启动时如果存在待观察切换，先完成一次健康观察，30 秒后再执行完整测速，不会因为进程重启丢弃回滚保护。

## 凭据边界

- `state/controller-secret.txt`：sing-box 控制接口。
- `state/codex-router-secret.txt`：Codex 到本地网关。
- `state/sakura-subscription-url.txt`：订阅地址。
- `state/sakura-outbounds.json`：节点密码和出站配置。

这些文件只保存在本机，不进入版本库，也不应出现在日志、截图或公开 Issue 中。

## 官方 Codex 独立通道

配置生成器从 `CodexOfficialGroup`（默认 `sakura-region-us`）复制节点列表，生成独立选择器
`codex-official` 和回环入站 `codex-official-in`（默认 `12551`）。它与全局 `sakura`
完全分离，普通流量切到新加坡时不会改变官方 Codex 出口。

`gateway/official_route.py` 只在主后端运行，使用隔离探测核心测量候选的下载、ChatGPT
可达性和无认证 Codex POST 路径；每 30 分钟全量比较，每 3 分钟检查当前节点，切换后
10 分钟 sticky。真实官方请求通过 `record_request()` 回传上传/响应头/失败阶段，
上传卡死会立即降级并触发比较。状态写入 `state/codex-official-route.json`，包含
`ready/degraded`、当前节点、分数、核心 PID 和下次扫描；Router 发现状态过期或核心 PID
变化时自动回退到 `12550`、SakuraCat 或直连。非官方 lane 的连接失败只触发一次扫描，
不会把节点写入官方失败列表或让官方 lane 降级。

## Claude Code 官方模型桥接

Claude Code 使用 Anthropic `/v1/messages`。当请求模型在 `CodexAnthropicOfficialModels`
（默认 `codex/gpt-6.1-sol`、`codex/gpt-6-astra`）中时，`sanitize_proxy` 把请求交给
`gateway/anthropic_codex_bridge.py`：system 转为 `instructions`，消息、工具调用和工具结果
转为 Codex Responses 的 `input` 项，工具 schema 经 `clean_schema` 清洗，流式 Responses SSE
再转回 Anthropic SSE。桥接复用 `codex_router.official_connection()` 和
`~/.codex/auth.json` 的 ChatGPT 登录，只读凭证且不写入日志或 `state/`；其他模型仍走原有
第三方清洗和转发路径。`codex/` 是官方专用前缀；`openai/` 不在默认官方列表中，
继续走内部/第三方 API。`CodexAnthropicBridgeEnabled=false` 时官方模型返回 503。

官方请求保留原始 zstd/gzip，只有改写 JSON 后才重新压缩；连接、上传、响应头和流式空闲
分别有预算，上传预算耗尽返回 `504`，不再共用 600 秒读超时。

## AI 路由

官方模型由 `gateway/codex_catalog.py` 自动同步，与 `gateway/codex-api-models.json` 中的第三方模型合并生成 `state/codex-models.generated.json`。官方能力和模型清单不固定，同步失败时保留同一账号的最近成功缓存。官方模型与第三方模型使用独立认证路线；第三方请求头采用白名单，避免把 ChatGPT 登录凭据转发给第三方。详细说明见 [CODEX.md](CODEX.md)。
