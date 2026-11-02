---
name: codex-imagegen
description: Generate or edit raster images with Codex CLI's built-in image_gen (ChatGPT OAuth). Use for bitmap assets such as photos, illustrations, mockups, sprites, banners, or transparent cutouts. Do not use for SVG/vector icons, diagrams, or code-native UI.
---

# Codex Image Generation

Run the wrapper; do not use `OPENAI_API_KEY` or the CLI fallback:

```bash
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python -B "$ROOT/tools/codex-imagegen/generate.py" \
  --prompt "<subject, style, composition, exact text, constraints>" \
  --out "<absolute path to output.png>" \
  --timeout 600
```

- For edits, add one `--image <path>` per reference image.
- The wrapper prints one JSON line; verify the saved file with Read.
- Keep the output inside the current project; do not overwrite an existing file unless asked.
- If Codex is configured to use the local gateway, it must already be running; do not restart or modify the gateway.
