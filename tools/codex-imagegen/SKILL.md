---
name: codex-imagegen
description: Generate or edit raster images with Codex CLI's built-in image_gen (ChatGPT OAuth). Use for bitmap assets such as photos, illustrations, mockups, sprites, banners, or transparent cutouts. Do not use for SVG/vector icons, diagrams, or code-native UI.
---

# Codex Image Generation

Run the bundled wrapper; do not use `OPENAI_API_KEY` or the CLI fallback:

```bash
python -B "${CLAUDE_SKILL_DIR}/generate.py" \
  --prompt "<subject, style, composition, exact text, constraints>" \
  --out "<absolute path to output.png>" \
  --timeout 600
```

- For edits, add one `--image <path>` per reference image.
- The wrapper saves the image itself: it reads the `savedPath` from Codex's
  event stream, copies the file to `--out`, and ends the Codex process as soon
  as the image is valid. Do not expect the Codex agent to copy files.
- Codex runs with `--sandbox read-only` by default so the agent cannot move or
  rewrite files. If image generation fails with a sandbox error, retry once
  with `--sandbox workspace-write`.
- The wrapper prints one JSON line; verify the saved file with Read.
- Keep the output inside the current project; do not overwrite an existing file unless asked.
- If Codex is configured to use the local gateway, it must already be running; do not restart or modify the gateway.
