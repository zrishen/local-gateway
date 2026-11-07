---
name: chatgpt-imagegen
description: Generate or edit an image through the ChatGPT web backend.
---

# ChatGPT Image Generation / Editing

在包含本 `SKILL.md` 的目录中运行 `generate.py`；如果当前目录不是该目录，就用 `generate.py` 的绝对路径。

生成图片：

```bash
python -B generate.py \
  --prompt "<画面描述>" \
  --out "<绝对路径>.png" \
  --timeout 300
```

编辑或使用参考图：

```bash
python -B generate.py \
  --prompt "只把红色方块改成绿色，其他内容保持不变" \
  --image "<参考图绝对路径>.png" \
  --out "<输出绝对路径>.png" \
  --timeout 300
```

- `--image` 可重复，支持 PNG / JPEG / WebP，单张不超过 20 MiB。
- `--out` 必须是绝对路径；图片就保存在这里，没有默认目录。
- 默认归档本次 ChatGPT conversation；需要保留会话时加 `--no-archive`。
- 验证登录：`python -B generate.py --check`。
- 成功时输出一行 JSON；更多参数见 `--help`。
