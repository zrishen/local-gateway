---
name: codex-imagegen
description: Generate or edit raster images with Codex CLI's built-in image_gen tool (ChatGPT OAuth), saving the result into the current project. Use when the user asks Claude Code to create or edit a bitmap image such as a photo, illustration, mockup, sprite, banner, or transparent cutout.
---

# Codex Image Generation

This gateway project includes a small wrapper around Codex CLI's built-in
`image_gen` tool. It uses the existing ChatGPT/Codex OAuth login and does not
need `OPENAI_API_KEY`. It is not part of the `/v1/messages` bridge: `image_gen`
is executed by the Codex client, not by the Responses backend.

## Requirements

- Codex CLI 0.140 or newer (`codex --version`).
- Codex is logged in with a ChatGPT account that has image generation.
- `codex features list` shows `image_generation` as `stable true`.

Do not run `codex login` or ask for a token unless the user explicitly asks.
Never print or copy the ChatGPT token.

## Workflow

1. Decide generate vs edit and choose an output path:
   - Project asset: `assets/generated/<slug>.png` (or the path the user named).
   - Do not overwrite an existing file unless the user asked for replacement;
     use a versioned name such as `hero-v2.png`.
2. Turn the user's request into a short structured prompt:
   - use case, asset type, primary request;
   - style/medium, composition, lighting/mood;
   - exact text verbatim, constraints and things to avoid.
3. Run the wrapper from the project directory:

```bash
python "${CLAUDE_PROJECT_DIR:-$PWD}/tools/codex-imagegen/generate.py" \
  --prompt "<structured prompt>" \
  --out "<absolute output path>" \
  --cd "<project directory>"
```

For edits or reference images, add one `--image <path>` per input image.
Optional: `--model <codex-model>` overrides the Codex model, and
`--timeout <seconds>` changes the default 600-second limit.

4. The wrapper prints one JSON line:
   - success: `{"ok": true, "path": "...", "bytes": ...}`
   - failure: `{"ok": false, "error": "..."}`.
5. Verify the saved file with the Read tool when useful, then report the final
   path, the prompt used, and that Codex built-in image generation was used.

## Limits and safeguards

- Image generation consumes Codex/ChatGPT usage and may be slower than normal
  chat; keep prompts self-contained and do not ask the nested Codex agent to
  modify unrelated project files.
- The nested Codex session runs with `--sandbox workspace-write` and
  `--ephemeral`; it should only write the requested image.
- Do not fall back to the `image_gen.py` CLI or `OPENAI_API_KEY` path unless
  the user explicitly asks for it.
- If the built-in tool is unavailable, report that clearly instead of
  substituting SVG/HTML placeholders or a different image service.
