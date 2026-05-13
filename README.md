# Video AI Editor

[![CI](https://github.com/dmpl6454/video-ai-editor/actions/workflows/ci.yml/badge.svg)](https://github.com/dmpl6454/video-ai-editor/actions/workflows/ci.yml)

Local, chat-driven, CapCut-class video editor. Upload a video, tell Claude how
to edit it. Everything runs on your machine — only Claude API calls leave it.

- **88 dispatch tools** covering every CapCut feature pillar (multi-track
  timeline, keyframes, effects, masks, chroma key, transitions, color grading,
  ducked audio mix, captions in 3 styles, brand kits, show templates).
- **Local AI**: faster-whisper + whisper.cpp (Metal), pyannote diarization
  with librosa fallback, Demucs, RIFE smooth slow-mo, Real-ESRGAN upscale,
  LaMa object erase, MediaPipe auto-reframe, OpenCV motion tracker, vidstab,
  rembg, noisereduce, Argos Translate, Piper TTS.
- **Frame-accurate scrub** via WebCodecs + mp4box.js (falls back to
  `<video>.currentTime` when the codec rejects).
- **VideoToolbox H.264** on Apple Silicon (libx264 fallback).
- **163 backend tests + Playwright frontend smoke**, full suite in ~68 s.

## Setup

```bash
brew install ffmpeg ffmpeg-full        # ffmpeg-full has libvidstab + libass + zimg
cd ~/video-ai-editor
uv sync
cd frontend && npm install && cd ..
cp .env.example .env                   # fill in ANTHROPIC_API_KEY
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
uv run video-ai-editor
```

Builds the frontend if needed, boots the backend in-process, opens a native
window. No browser, no separate dev server. ~2 s cold start.

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
uv run pytest                          # ~68 s, 163 tests
cd frontend && npx tsc --noEmit && npx vite build
```

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
