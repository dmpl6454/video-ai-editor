# Video AI Editor

[![CI](https://github.com/dmpl6454/video-ai-editor/actions/workflows/ci.yml/badge.svg)](https://github.com/dmpl6454/video-ai-editor/actions/workflows/ci.yml)

Local, prompt-driven, CapCut-class video editor. Upload a video, type what you
want in one sentence. Everything runs on your machine — a Claude API key is
optional, and the only bytes that leave are the calls you turn on.

- **100 dispatch tools** (94 advertised to the chat agent) covering every CapCut feature pillar (multi-track
  timeline, keyframes, effects, masks, chroma key, transitions, color grading,
  ducked audio mix, captions in 3 styles, brand kits, show templates).
- **Best-in-class auto captions (Hindi + English + Hinglish)** — the
  `auto_caption` tool re-transcribes with **Whisper large-v3 on Metal** (the
  only model that handles Hindi cleanly without hallucination loops; turbo
  mangles it to English), then formats words into broadcast-grade cues
  (≤2 lines, reading-speed-limited, sentence-aware). Auto-detects language,
  handles code-switching.
- **Local AI**: faster-whisper + whisper.cpp (Metal, tiny→large-v3), pyannote
  diarization with librosa fallback, Demucs, RIFE smooth slow-mo, Real-ESRGAN
  upscale, LaMa object erase, MediaPipe auto-reframe, OpenCV motion tracker,
  vidstab, rembg, noisereduce, MADLAD-400 translation, Piper TTS.
- **MCP server** — drive the editor from Claude Code / Cursor / Codex over
  HTTP (see below).
- **Local CLIP visual search** — `search_media` finds footage by visual
  content ("a sunset over water") with an on-device CLIP model, no transcript
  or cloud needed.
- **Frame-accurate scrub** via WebCodecs + mp4box.js (falls back to
  `<video>.currentTime` when the codec rejects).
- **VideoToolbox H.264** on Apple Silicon (libx264 fallback).
- **970 backend tests + Playwright frontend smoke**, full suite in ~11 min.

## Build a macOS app (.app / .dmg)

```bash
uv run bash build_app.sh   # → dist/Video AI Editor.app
bash build_dmg.sh          # → dist/Video-AI-Editor.dmg (drag-to-install)
```

### Windows

```powershell
uv sync --python 3.13 --all-extras --group dev
cd frontend; npm install; cd ..
powershell -ExecutionPolicy Bypass -File run.ps1        # launch
powershell -ExecutionPolicy Bypass -File build_win.ps1  # → dist\Video AI Editor\
```

See "Running on Windows" in `CLAUDE.md` for ffmpeg setup and WebView2 notes.

The DMG bundles the editor UI, ffmpeg-based editing, the MCP server, **CLIP
visual search**, and torch — so semantic footage search works offline (the
CLIP model auto-downloads once). First launch is ad-hoc signed, so right-click
→ Open the first time (or `xattr -dr com.apple.quarantine` the installed app).

Runtime needs `ffmpeg` on PATH. The heaviest models (Whisper large-v3 ggml for
auto-captions, RIFE/Real-ESRGAN binaries) download to `~/.local/share` on first
use and aren't in the DMG. Per-session data lives in
`~/Library/Application Support/Video AI Editor/`. Not notarized — that needs an
Apple Developer account.

## Drive it from your agent (MCP)

The backend exposes an MCP server at `http://127.0.0.1:8000/mcp`, so Claude
Code / Cursor / Codex can edit the timeline directly — the same way
palmier-pro works. Start the backend, then:

```bash
# Claude Code
claude mcp add --transport http video-ai-editor http://127.0.0.1:8000/mcp

# Codex
codex mcp add video-ai-editor --url http://127.0.0.1:8000/mcp
```

The agent gets all 94 schema'd tools (cut, transitions, captions, color,
`search_media`, export, …). The MCP server drives one "active" session by
default; pass `session_id` in any tool's arguments to target a specific
project.

## Edit with a prompt, no API key

Type one sentence into the Prompt bar (`/`) — `add captions`, `cut out the
ums`, `make it vertical for reels`, `add chill background music and duck it
under my voice`, `smooth zoom between every clip`, `make 3 shorts under 30
seconds for tiktok` — and it becomes a verified edit. No cloud key needed.

- **The brain ladder.** A grammar-and-recipe planner answers most prompts
  instantly. Prompts it reads with less confidence go to an on-device language
  model — Apple Intelligence (macOS 26+, when it is on in System Settings),
  then a local MLX model (`uv sync --extra local-llm`; Qwen2.5-7B at ≥ 24 GB
  RAM). Claude is a rung only when `ANTHROPIC_API_KEY` is set. Every reply
  names the brain that answered and, when a better one was unavailable, why
  and how to fix it.
- **Offline by rule.** The prompt path never downloads a model or a voice
  without your **yes** — a first-use download (whisper large-v3 3.1 GB, MADLAD
  3 GB, a Piper voice 60 MB) is a question in the plan, and "skip" drops the
  steps that needed it. Model downloads start only from the Mac itself, never
  from a paired phone. Every plan — from any brain — passes an explicit
  allowlist (tools, arguments, enums, bounds, no model-written file path)
  before a single tool runs.
- **Download once, then it is local.** Whisper `small` ships cached; the
  larger models, the translation model and extra voices are fetched once on
  your say-so and live in your cache after that.
- **Verified, one undo step.** Each run ends with measured checks (captions
  cover of speech in timeline seconds, loudness on the render, no overlay
  outside the platform safe zone, splits on beats) and is one op — ⌘Z takes
  all of it back.
- **Benchmark.** `tests/benchmark/` runs 26 prompts on synthesized media with
  millisecond ground truth and a socket guard against any egress:
  `VAI_BRAIN=recipes uv run pytest -m benchmark tests/benchmark`. Method and
  claims: [docs/BENCHMARK.md](docs/BENCHMARK.md). The bar itself:
  [docs/PROMPT_EDITOR.md](docs/PROMPT_EDITOR.md).

## Setup

```bash
brew install ffmpeg ffmpeg-full        # ffmpeg-full has libvidstab + libass + zimg
cd ~/video-ai-editor
uv sync --python 3.13 --all-extras --group dev   # plain `uv sync` omits pytest
cd frontend && npm install && cd ..
cp .env.example .env                   # ANTHROPIC_API_KEY is optional (it adds the Claude rung)
```

Optional binaries (downloaded on first use of each feature; ~270 MB total):

```bash
# RIFE smooth slow-mo
mkdir -p ~/.local/share/video-ai-editor/models/rife
# … grab rife-ncnn-vulkan-20221029-macos.zip from
#   https://github.com/nihui/rife-ncnn-vulkan/releases

# Real-ESRGAN upscale
mkdir -p ~/.local/share/video-ai-editor/models/realesrgan
# … grab realesrgan-ncnn-vulkan-*-macos.zip from
#   https://github.com/xinntao/Real-ESRGAN/releases
```

For pyannote (best-quality speaker diarization), run:
```bash
uv run python -m video_ai_editor.cli.setup_pyannote
```

## Run

### Desktop app (single command)

```bash
bash run.sh              # macOS / Linux
```
```powershell
powershell -ExecutionPolicy Bypass -File run.ps1    # Windows
```

Builds the frontend if it's missing or older than `frontend/src`, boots the
backend in-process, opens a native window. No browser, no separate dev server.

> **On macOS, use `run.sh` — not `uv run video-ai-editor`.** The latter can fail
> with `ModuleNotFoundError: No module named 'video_ai_editor'` even when the
> venv is correctly synced: Spotlight's `mdflagwriter` marks the editable
> install's `.pth` file hidden within ~1 s of creation, and Python 3.13+ skips
> hidden `.pth` files, so `src/` never lands on `sys.path`. It recurs
> system-wide, so `chflags nohidden` doesn't hold. `run.sh` sets `PYTHONPATH`
> and bypasses the `.pth` mechanism entirely. See CLAUDE.md for the full
> diagnosis.

### Verify a fresh checkout

```bash
bash verify_mac.sh                # full check, includes pytest (~10-15 min)
bash verify_mac.sh --no-tests     # toolchain + build + boot only (~2 min)
```

Checks ffmpeg/npm/uv, builds the frontend with the real `tsc -b` gate, imports
every backend module, boots the server and hits `/api/health` + `/readyz`, and
reports which optional AI features this machine actually has. Runs every check
before summarising, so you get all failures at once.

### Browser dev (hot-reload frontend)

```bash
# backend
uv run uvicorn video_ai_editor.main:app --reload --reload-dir src --port 8000

# frontend (separate terminal)
cd frontend && npm run dev
```

Open http://localhost:5173.

## Test

```bash
uv run pytest                          # the default suite (the benchmark is excluded by marker)
VAI_BRAIN=recipes uv run pytest -m benchmark tests/benchmark   # the CapCut-parity benchmark (minutes; needs whisper small cached)
cd frontend && npx tsc -b --force && npx vitest run && npx vite build
cd mobile && npx tsc --noEmit && npm test
```

> Use `tsc -b`, **never** `tsc --noEmit`. `frontend/tsconfig.json` is a solution
> file (`"files": []` plus only `references`), so plain `tsc` builds a program of
> zero files and exits 0 — verified with `--listFiles`. Only build mode descends
> into `tsconfig.app.json`. Four real type errors once sat on main while that
> gate stayed green.

## Project status

| | |
|---|---|
| Backend tools | 88 |
| API endpoints | 22 |
| Backend tests | 163 (21 skipped on CI without local AI binaries) |
| Frontend bundle | 428 KB → 120 KB gzipped |
| Suite time | 68 s |

Operational endpoints: `/livez`, `/readyz`, `/metrics` (Prometheus text),
`X-Request-ID` on every response, sliding-window rate limit (60 req/s/IP),
JSON-structured logs, error envelope `{"error": {"code","message","request_id"}}`.

## License

Private repo, no license declared yet.
