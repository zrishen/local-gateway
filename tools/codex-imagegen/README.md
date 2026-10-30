# Codex image generation

`generate.py` wraps Codex CLI's built-in `image_gen` tool so Claude Code and
scripts in this project can create or edit raster images with the existing
ChatGPT/Codex OAuth login. It does not need `OPENAI_API_KEY` and is not part of
the `/v1/messages` bridge.

## Requirements

- Codex CLI 0.140+ on `PATH`.
- Codex logged in with a ChatGPT account that has image generation.
- `codex features list` shows `image_generation` as `stable true`.

If Codex CLI is configured to use the local gateway (`model_provider =
"unified_local"`), the gateway and its official Codex lane must be running.

## Usage

From the project root:

```powershell
python -B tools/codex-imagegen/generate.py `
  --prompt "A minimal product photo of a ceramic mug on a warm background" `
  --out "assets/generated/mug.png" `
  --cd "C:\code\local-gateway"
```

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
| `--cd` | Working directory for the nested Codex session |
| `--image` | Reference/edit image; repeat for multiple images |
| `--model` | Codex model override |
| `--timeout` | Nested session timeout in seconds (default 600) |

The wrapper prints one JSON line, for example:

```json
{"ok": true, "path": "C:\\code\\local-gateway\\assets\\generated\\mug.png", "bytes": 222390}
```

## Notes

- Image generation consumes Codex/ChatGPT usage and can take a minute or more.
- The nested session uses `--ephemeral --sandbox workspace-write`; it should
  only write the requested image.
- Prompts and request bodies are not logged by this wrapper; the Codex CLI
  handles its own authentication and session state.
- Do not use the `image_gen.py` CLI fallback or `OPENAI_API_KEY` unless you
  explicitly choose that path; the built-in tool is the supported default.
