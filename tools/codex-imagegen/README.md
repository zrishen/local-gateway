# Codex image generation

`generate.py` wraps Codex CLI's built-in `image_gen` tool so Claude Code and
scripts in this project can create or edit raster images with the existing
ChatGPT/Codex OAuth login. It does not need `OPENAI_API_KEY` and is not part of
the `/v1/messages` bridge.

## Requirements

- Codex CLI with `image_generation` support; verified with 0.164.0 on `PATH`.
- Codex logged in with a ChatGPT account that has image generation.
- `codex features list` shows `image_generation` as `stable true`.

If Codex CLI is configured to use the local gateway (`model_provider =
"unified_local"`), the gateway and its official Codex lane must be running.

## Usage

From the project root:

```powershell
python -B tools/codex-imagegen/generate.py `
  --prompt "A minimal product photo of a ceramic mug on a warm background" `
  --out "C:\code\local-gateway\assets\generated\mug.png"
```

`--cd` is optional. When omitted, the wrapper uses the output directory as the
nested session's sandbox root, which keeps the Codex agent from editing the rest
of the project. Pass `--cd` only when the nested agent needs project context.

Edit an existing image:

```powershell
python -B tools/codex-imagegen/generate.py `
  --prompt "Replace only the background with a sunset gradient; keep the product unchanged" `
  --image "assets/input/mug.png" `
  --out "assets/generated/mug-sunset.png"
```

Options:

| Option | Meaning |
|---|---|
| `--prompt` | Structured image prompt (required) |
| `--out` | Final image path (required) |
| `--cd` | Optional working directory for the nested Codex session; defaults to the output directory |
| `--image` | Reference/edit image; repeat for multiple images |
| `--model` | Codex model override |
| `--timeout` | Nested session timeout in seconds (default 600) |

The wrapper prints one JSON line, for example:

```json
{"ok": true, "path": "C:\\code\\local-gateway\\assets\\generated\\mug.png", "bytes": 222390}
```

## Notes

- Image generation consumes Codex/ChatGPT usage and can take a minute or more.
  Ask before producing batches or multiple variants.
- The nested session uses `--ephemeral --sandbox workspace-write`; it should
  only write the requested image. Do not let it modify source files, gateway
  configuration, `state/`, or `settings.local.json`.
- Do not overwrite an existing asset without confirmation, and do not commit
  generated images unless the user asks.
- Transparency is not post-processed: ask for it in the prompt and verify the
  result has an alpha channel when needed.
- Prompts and request bodies are not logged by this wrapper; the Codex CLI
  handles its own authentication and session state.
- Do not use the `image_gen.py` CLI fallback or `OPENAI_API_KEY` unless you
  explicitly choose that path; the built-in tool is the supported default.
