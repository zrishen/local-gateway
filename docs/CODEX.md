# Codex 模型路由（可选）

基础 TUN、系统代理和订阅功能不依赖本页配置。

## 模型目录

| API 模型 ID | 菜单名称 | 图片输入 |
|---|---|---|
| `ds/deepseek-v4.1-flash` | DeepSeek-V4.1-Flash (API) | 支持 |
| `glm-req/glm-5.3` | GLM-5.3 (API) | 不支持 |
| `glm-req/glm-5.3-flash` | GLM-5.3-Flash (API) | 支持 |
| `google/gemini-3.8-flash` | Gemini-3.8-Flash (API) | 支持 |
| `openai/gpt-6-luna` | GPT-6-Luna (API) | 支持 |

官方模型目录从当前 ChatGPT 账号的官方接口自动同步，名称、排序、上下文窗口、思考档位、默认值和工具能力保留官方配置，不固定模型清单。第三方模型在 `gateway/codex-api-models.json` 中维护，默认使用 300k 原始窗口、285k 有效窗口。官方模型和第三方模型使用不同认证路径：

- 官方模型：使用 Codex/ChatGPT 本地认证，经官方路由访问；
- 第三方模型：只使用本地配置的第三方凭据，不转发 ChatGPT token、Cookie 或账号标识。

## 相关文件

- `gateway/codex-api-models.json`：仅维护第三方模型的菜单和能力；
- `gateway/codex_catalog.py`：官方目录同步与合并；
- `state/codex-models.generated.json`：生成的模型菜单，供 Codex 的 `model_catalog_json` 和网关读取；
- `state/codex-official-models.json`：最近一次成功的官方目录缓存；
- `state/codex-catalog-sync.json`：同步时间、模型数量和错误类型，不包含凭据；
- `gateway/codex_router.py`：路由和凭据边界；
- `gateway/responses_bridge.py`：Responses 与 Chat Completions 转换；
- `gateway/tool_search_compat.py`：部分供应商的工具搜索兼容；
- `settings.local.json` 的 `AiUpstream`：可选 AI 兼容上游地址。

## 凭据

第三方 API 凭据不会写进仓库。不同环境中凭据来源可能不同，使用前请检查 `gateway/codex_router.py` 的 `api_credential()` 是否匹配你的本地配置。

Codex 与网关之间还有一层独立回环口令，保存在 `state/codex-router-secret.txt`。

## 官方模型自动同步

官方目录按北京时间（Asia/Shanghai）每天 09:10、18:00 在后台同步；如果关机或停服务错过时段，恢复后补同步一次。每分钟检查登录账号、已安装的 Codex 版本和本地第三方配置。首次使用、账号或版本变化立即重新获取官方目录，失败后每分钟重试。同一账号同步失败时保留最近一次成功目录；切换账号时清除旧账号的菜单，避免把旧账号的可用模型当作新账号的权限。模型请求仍由官方服务最终验证权限。

同步使用本机 `CODEX_HOME/auth.json`（默认 `~/.codex/auth.json`）中的 ChatGPT 登录，只向固定的官方主机发送认证。不会刷新或重写登录凭据。官方新增和下架模型会自动反映到生成目录，第三方条目保持独立。生成目录和缓存不纳入 Git。

首次安装或需要立即同步时运行：

```powershell
python -B .\gateway\codex_catalog.py --force
```

Codex 启动时读取生成目录；更新后重启 Codex 以刷新模型菜单。网关不会因菜单缓存过期而拒绝新的官方模型 ID，未知第三方模型仍会被拒绝。

## 官方接口透传

`/codex` 下除本地模型列表和模型分流请求外的接口，统一转发到固定的
`https://chatgpt.com/backend-api/codex`，不再按接口名称维护白名单。
图片生成、文件上传下载及后续新增的官方辅助接口可以直接到达官方服务；
实际支持情况和账号权限仍由官方响应决定。

- 支持 GET、POST、PUT、PATCH、DELETE、HEAD、OPTIONS 和 WebSocket 升级。
- 辅助接口保留原始查询参数、Content-Type、压缩和二进制请求体，并流式返回响应。
- 所有请求先验证本地口令；官方透传还要求 ChatGPT Bearer 认证。
- 官方认证仅发送到固定的官方主机，本地口令不会转发；第三方对话继续使用独立凭据。
- 只有 POST `/responses` 和 `/responses/compact` 进入现有模型路由；图片模型不受文本模型目录校验。
- `logs/codex-routes.jsonl` 中的 `official-passthrough` 记录方法、路径、状态和耗时，省略查询参数和请求体。

## 官方 Codex 独立通道

配置生成器在订阅模式且找到 `CodexOfficialGroup` 地区组时，创建独立选择器
`codex-official` 和回环入站 `codex-official-in`（默认 `127.0.0.1:12551`）。
它只包含美国组节点，不受全局 `sakura` 的美国和新加坡选择影响。普通浏览器和
其他应用仍走 `sakura`/TUN。

`gateway/official_route.py` 只管理这个选择器：

- 每 30 分钟用隔离探测核心测速候选；每 3 分钟检查当前节点，连续两次低分/不可达才重新比较；
- 探测包含 Cloudflare 64 KiB 下载、ChatGPT `/robots.txt` 和无认证的 Codex
  `/backend-api/codex/responses` POST，覆盖真实上传路径；
- 使用与普通节点相同的评分和迟滞策略；切换后 10 分钟 sticky；
- 真实官方请求的上传、响应头和流错误通过 `record_request()` 回传，上传超时立即降级并触发比较；
- 状态写入 `state/codex-official-route.json`（active、ready/degraded、分数、core PID、下次扫描），不记录请求正文；
- 状态超过 120 秒或核心 PID 变化即视为过期，Router 自动回退到项目代理、SakuraCat 或直连；
- `CodexOfficialEnabled=false` 时不生成入站和选择器，Router 不会尝试 12551。

## 请求压缩与阶段预算

官方请求保留原始 zstd/gzip 压缩体；只有过滤第三方推理块等确实改写 JSON 时才重新压缩。
`CodexRequestCompression=false` 可关闭重新压缩并回退未压缩上传。第三方请求仍使用 JSON 重编码。

连接阶段限时 12 秒。上传使用总预算和 15 秒无进度检测：
预算为 `min(180, max(30, 正文大小 / (8 KB/s)))` 秒，可用
`CodexOfficialUploadMinSeconds`、`CodexOfficialUploadMaxSeconds`、
`CodexOfficialUploadMinimumKBps` 和 `CodexOfficialUploadStallSeconds` 调整。
响应头默认 360 秒，流式空闲默认 180 秒；不再在建连后统一设置 600 秒。
上传预算耗尽时返回 `504`，不再让 Codex 等待数分钟；请求正文一旦开始发送就不会重放。

## 当前限制

- 本地网关最多读取 128 MiB 解码后的请求正文；超出时返回 `413`，长工作线程应先压缩或新建线程，避免每次请求都携带过大的工具历史；
- 第三方 `/responses/compact` 尚未统一启用；
- 第三方模型统一为 300k 原始窗口、285k 可用窗口；这不代表每个第三方上游都验证过完整长上下文；官方模型以同步目录为准；
- 不同供应商对图片、工具调用和思考档位的支持不同；
- 修改模型目录后应重启 Codex，并用新聊天验证。

## 请求保真与耗时诊断

官方请求保留模型、思考强度、输出上限、系统说明、图片、工具和缓存参数。
官方推理历史同时包含加密状态和不兼容的明文 `content` 时，仅移除该字段，保留官方加密历史和元数据。
只有具有官方 `rs_...` 标识和 `gAAAA...` 长密文格式的推理块会发送给官方模型；第三方供应商生成的
短 UUID 等 `encrypted_content` 会在模型切换时过滤，避免官方服务把整次请求判为无法验证。
没有官方格式加密状态的推理历史仍不发给官方。Gemini 转换保持完整字符串输入、思考强度、
严格工具定义、并行工具选项和结构化输出格式；异常结束和长度截断分别返回 failed/incomplete，
不把截断参数标记成已完成工具调用。

官方路由优先使用独立美国通道，其次项目 `MixedPort`、SakuraCat 和直连；
每个候选都先做真实 TCP/CONNECT 探测，失败时只在正文发送前换下一条。
如果 TCP、代理 CONNECT 或 TLS 在请求正文发送前失败，官方路由会安全重试一次；
请求正文开始发送后不会再重试，因为服务端可能已经接收了请求，重放会产生不确定的重复生成。
建连前重试后仍失败时返回 `502`，上传预算耗尽时返回 `504`。日志用 `failure_stage` 区分
`connect`、`send`、`headers`、`client_write` 和 `response_body`；
`upload_timed_out` 标记上传卡死，`request_bytes`/`decoded_request_bytes`/`forwarded_request_bytes`
分别记录线上、解码后和实际转发的字节数，不记录正文。
`connect_seconds: null` 表示没有完成建连。转发每个读取到的数据块后立即 flush，
不等待完整回答，也不重放已经发送的推理或图片请求。

`logs/codex-routes.jsonl` 记录从请求开始算起的 `connect_seconds`、`upload_seconds`、
`headers_seconds`、`first_byte_seconds` 和 `first_text_seconds`，并记录
`max_chunk_gap_seconds`、`stream_terminal`、`stream_error`、`route_lane` 和 `route_node`。
这些信息不包含提示词、正文或凭据。首段正文时间不等于响应头时间；数据块间隔同时包含
模型计算与网络等待，不能单独据此归因。第三方上游的 `x-process-time` 会以
`upstream_process_seconds` 写入 `logs/ai-upstream.jsonl`，用于区分公司上游排队和本地代理耗时。
