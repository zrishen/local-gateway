---
name: chatgpt-imagegen
description: Generate a raster image through the ChatGPT web backend using the ChatGPT OAuth login in ~/.codex/auth.json.
---

# ChatGPT Web Image Generation

Run the bundled wrapper:

```bash
python -B "${CLAUDE_SKILL_DIR}/generate.py" \
  --prompt "<subject, style, composition, exact text, constraints>" \
  --out "<absolute path to output.png>" \
  --timeout 300
```

- 成功时输出一行 JSON；用 Read 验证图片。
- 不要覆盖已有文件，除非用户要求。
- 运行环境需要 `curl_cffi`。
- 使用非官方 ChatGPT Web API；可能触发账号风控，只在用户明确要求时使用。
- 仅生成图片，不支持编辑/上传；尺寸由 Web 模型决定。
