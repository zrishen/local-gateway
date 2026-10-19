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

思考档位和默认值以 `state/codex-models.json` 为准。官方模型和第三方模型使用不同认证路径：

- 官方模型：使用 Codex/ChatGPT 本地认证，经官方路由访问；
- 第三方模型：只使用本地配置的第三方凭据，不转发 ChatGPT token、Cookie 或账号标识。

## 相关文件

- `state/codex-models.json`：模型菜单、上下文窗口、输入能力；
- `gateway/codex_router.py`：路由和凭据边界；
- `gateway/responses_bridge.py`：Responses 与 Chat Completions 转换；
- `gateway/tool_search_compat.py`：部分供应商的工具搜索兼容；
- `settings.local.json` 的 `AiUpstream`：可选 AI 兼容上游地址。

## 凭据

第三方 API 凭据不会写进仓库。不同环境中凭据来源可能不同，使用前请检查 `gateway/codex_router.py` 的 `api_credential()` 是否匹配你的本地配置。

Codex 与网关之间还有一层独立回环口令，保存在 `state/codex-router-secret.txt`。

## 当前限制

- 第三方 `/responses/compact` 尚未统一启用；
- 1M 是客户端窗口配置，不代表每个第三方上游都验证过完整长上下文；
- 不同供应商对图片、工具调用和思考档位的支持不同；
- 修改模型目录后应重启 Codex，并用新聊天验证。
