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

官方模型目录从当前 ChatGPT 账号的官方接口自动同步，名称、排序、上下文窗口、思考档位、默认值和工具能力保留官方配置，不固定模型清单。第三方模型在 `gateway/codex-api-models.json` 中维护，仍使用 500k 原始窗口、475k 有效窗口。官方模型和第三方模型使用不同认证路径：

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

## 当前限制

- 第三方 `/responses/compact` 尚未统一启用；
- 第三方模型统一为 500k 原始窗口、475k 可用窗口；这不代表每个第三方上游都验证过完整长上下文；官方模型以同步目录为准；
- 不同供应商对图片、工具调用和思考档位的支持不同；
- 修改模型目录后应重启 Codex，并用新聊天验证。
