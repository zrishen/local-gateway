---
name: codex-imagegen
description: Generate or edit an image with Codex CLI's built-in image_gen.
---

# Codex Image Generation

在包含本 `SKILL.md` 的目录中运行 `generate.py`；如果当前目录不是该目录，就用 `generate.py` 的绝对路径。

生成图片：

```bash
python -B generate.py \
  --prompt "<画面描述>" \
  --out "<绝对路径>.png" \
  --timeout 600
```

编辑或使用参考图：

```bash
python -B generate.py \
  --prompt "只把红色方块改成绿色，其他内容保持不变" \
  --image "<参考图绝对路径>.png" \
  --out "<输出绝对路径>.png" \
  --timeout 600
```

- `--image` 可重复，每张参考图传一次。
- `--out` 必须是绝对路径；图片就保存在这里，没有默认目录。
- 本目录可整体复制到任意 agent 的 skills 目录，不依赖特定 agent 的路径变量。
- 默认使用 Codex 的 `--sandbox read-only`；如果遇到沙箱错误，重试一次并加 `--sandbox workspace-write`。
- 不要使用 `OPENAI_API_KEY` 或 CLI fallback。
- 如果 Codex 配置为使用本地网关，网关必须已经运行；不要重启或修改它。
- 成功时输出一行 JSON；更多参数见 `--help`。
