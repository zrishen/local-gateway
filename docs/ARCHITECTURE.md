# 架构

local-gateway 在本机把系统代理、TUN、规则分流和可选的 AI 路由组合成一个入口。

## 数据流

```text
应用
├─ 系统代理 127.0.0.1:8799
│  ├─ HTTP / CONNECT / SOCKS5 → 127.0.0.1:12550 → sing-box
│  └─ 本地 AI API → 协议清洗/转换 → 可选上游
└─ 系统路由 → UnifiedBox TUN → sing-box
                              ├─ 国内/私有地址 → direct
                              ├─ 可选公司域名 → company-direct
                              └─ 其他流量 → sakura 节点组
```

## 主要组件

| 路径 | 职责 |
|---|---|
| `gateway/unified_gateway.py` | 单端口 HTTP/SOCKS 转发和本地 AI 入口 |
| `gateway/mode_monitor.py` | 系统代理策略、Rule/Direct 模式和状态写入 |
| `Generate-Config.ps1` | 生成 sing-box 配置；不直接手改 `config.json` |
| `gateway/sakura_subscription.py` | 订阅解析、地区分组和节点出站生成 |
| `gateway/endpoint_catalog.py` | 入口 IP 发现、TCP/TLS 探测、健康选择和过期清理 |
| `gateway/latency_selector.py` | 当前地区节点测速与切换 |
| `gateway/codex_router.py` | 可选 Codex 模型路由和请求头边界 |
| `Service.ps1` | 常驻服务、内核/网关监督和配置刷新 |
| `Supervisor.ps1` | 未安装计划任务时的兼容启动入口 |

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

- 节点层：完整测速默认每 60 分钟，健康检查默认每 5 分钟。
- 入口层：sing-box 每 5 分钟检查候选入口。
- 入口目录：默认每 60 分钟刷新 DNS 和候选。
- 历史清理：成功入口 30 天、失败入口 7 天、每域名合计最多 24 条。

## 凭据边界

- `state/controller-secret.txt`：sing-box 控制接口。
- `state/codex-router-secret.txt`：Codex 到本地网关。
- `state/sakura-subscription-url.txt`：订阅地址。
- `state/sakura-outbounds.json`：节点密码和出站配置。

这些文件只保存在本机，不进入版本库，也不应出现在日志、截图或公开 Issue 中。

## AI 路由

模型菜单位于 `state/codex-models.json`。官方模型与第三方模型使用独立认证路线；第三方请求头采用白名单，避免把 ChatGPT 登录凭据转发给第三方。详细说明见 [CODEX.md](CODEX.md)。
