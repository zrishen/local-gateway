---
name: codex-imagegen
description: Generate or edit raster images with Codex CLI's built-in image_gen tool (ChatGPT OAuth), saving the result into the current project. Use when the user asks Claude Code to create or edit a bitmap image such as a photo, illustration, mockup, sprite, banner, or transparent cutout. Do not use for SVG/vector icons, diagrams, or code-native UI graphics.
---

# Codex Image Generation

This gateway project includes a wrapper around Codex CLI's built-in `image_gen`
tool. It uses the existing ChatGPT/Codex OAuth login and does not need
`OPENAI_API_KEY`. It is not part of the `/v1/messages` bridge: `image_gen` is
executed by the Codex client, not by the Responses backend.

## Requirements

- Codex CLI with `image_generation` support; verified with 0.164.0
  (`codex --version`). The CLI must also support `codex exec --json`,
  `--ephemeral`, and `--sandbox`.
- Codex is logged in with a ChatGPT account that has image generation.
- `codex features list` shows `image_generation` as `stable true`.
- If Codex is configured to use the local gateway (`model_provider =
  "unified_local"`), the gateway must already be running. Do not restart, stop,
  or modify the gateway for this skill.

Do not run `codex login` or ask for a token unless the user explicitly asks.
Never print or copy the ChatGPT token.

## Use and scope

Use this for new raster images or edits of existing bitmap images. Do not use it
for SVG/vector graphics, icons, diagrams, or UI assets that should be produced
as code; those belong in HTML/CSS/canvas or a vector editor.

## Workflow

1. Decide generate vs edit and choose an output path:
   - Project asset: `assets/generated/<slug>.png` (or the path the user named).
   - Prefer an absolute path inside the project. Do not overwrite an existing
     file unless the user asked for replacement; use `hero-v2.png` instead.
2. Turn the request into a short structured prompt:
   - use case, asset type, primary request;
   - style/medium, composition, lighting/mood;
   - exact text verbatim, constraints and things to avoid.
3. Run the wrapper from the project root:

```bash
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python -B "$ROOT/tools/codex-imagegen/generate.py" \
  --prompt "<structured prompt>" \
  --out "<absolute output path>" \
  --timeout 600
```

Omit `--cd` by default: the wrapper then uses the output directory as the
nested session's sandbox root, so it cannot edit the rest of the project. Add
`--cd "<project root>"` only when the nested agent genuinely needs project
context. For edits or reference images, add one `--image <path>` per input
image. Optional: `--model <codex-model>` overrides the Codex model.

4. The wrapper prints one JSON line:
   - success: `{"ok": true, "path": "...", "bytes": ...}`
   - failure: `{"ok": false, "error": "..."}`.
5. Verify the saved file with the Read tool, then report the final path, the
   prompt used, and that Codex built-in image generation was used.

## Limits and safeguards

- Image generation consumes Codex/ChatGPT usage and can take a minute or more.
  Ask before generating multiple variants or a batch unless the user already
  requested it.
- The nested session runs `codex exec --ephemeral --sandbox workspace-write`.
  Keep the prompt limited to image creation; never ask it to edit source files,
  gateway configuration, `state/`, or `settings.local.json`.
- For a transparent background, say so in the prompt and verify the saved image
  has alpha; the wrapper does not post-process transparency.
- Do not commit generated assets unless the user asks. Do not overwrite an
  existing asset without confirmation.
- Do not fall back to the `image_gen.py` CLI or `OPENAI_API_KEY` path unless
  the user explicitly asks for it.
- If the built-in tool is unavailable, report that clearly instead of
  substituting SVG/HTML placeholders or a different image service.
