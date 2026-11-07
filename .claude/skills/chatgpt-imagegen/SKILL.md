---
name: chatgpt-imagegen
description: Generate or edit a raster image through the ChatGPT web backend using a ChatGPT OAuth login. Self-contained and portable across agents.
---

# ChatGPT Web Image Generation / Editing

This directory is self-contained: `SKILL.md` and `generate.py` live together.
Do not assume a fixed installation path. Resolve the directory containing this
file, then run the bundled script from there:

```bash
# run in the directory containing this SKILL.md
python -B generate.py \
  --prompt "<subject, style, composition, exact text, constraints>" \
  --out "<absolute path to output.png>" \
  --timeout 300
```

If the agent cannot change its working directory, use the absolute path to
`generate.py` instead of `generate.py`.

Edit or use reference image(s); `--image` is repeatable:

```bash
python -B generate.py \
  --prompt "Change only the red square to green; keep everything else unchanged" \
  --image "<absolute path to reference.png>" \
  --out "<absolute path to edited.png>" \
  --timeout 300
```

- The caller chooses `--out`; the tool has no default image directory.
- Supports PNG, JPEG, and WebP references up to 20 MiB each.
- For edits, state the exact change and what must be preserved.
- `--out` must not overwrite a reference image.
- 本目录可整体复制到任意 agent 的 skills 目录（Claude Code、Codex 或其他），不依赖特定 agent 的路径变量。
- Auth defaults to `~/.codex/auth.json`; override with `--auth-file` or `CHATGPT_AUTH_FILE`.
- Timezone defaults to local; override with `--timezone` / `--timezone-offset-min`.
- 成功下载后默认归档本次 ChatGPT conversation；归档不是删除，不影响已保存的本地图片。
- 需要继续在 ChatGPT 中查看该会话时加 `--no-archive`。
- 只用 `--check` 可以单独验证认证和哨兵握手，不需要 `--prompt` / `--out`。
- 成功时输出一行 JSON；用 Read 验证图片。
- 不要覆盖已有文件，除非用户要求。
- 运行环境需要 `curl_cffi`。
- 使用非官方 ChatGPT Web API；可能触发账号风控，只在用户明确要求时使用。
- 参考图会上传到 ChatGPT；不要把敏感图片发送到未授权账户。
- 输出尺寸由 Web 模型决定，编辑结果可能与输入尺寸不同。
