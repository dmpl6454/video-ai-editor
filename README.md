# Video AI Editor

Local, chat-driven, CapCut-class video editor. Upload a video, tell Claude how to edit it.

## Status

Building toward Milestone 1 (editable timeline). See `/Users/sudhanshu/.claude/plans/build-video-ai-video-mellow-spindle.md` for the full plan.

## Setup

```bash
brew install ffmpeg
cd ~/video-ai-editor
uv sync
cd frontend && npm install && cd ..
cp .env.example .env  # fill in ANTHROPIC_API_KEY
```

## Run

### Desktop app (single command)

```bash
uv run video-ai-editor
```

Builds the frontend if needed, boots the backend in-process, opens a native
window. No browser, no separate dev server. ~2s cold start.

### Browser dev (hot-reload frontend)

```bash
# backend
uv run uvicorn video_ai_editor.main:app --reload --reload-dir src --port 8000

# frontend (separate terminal)
cd frontend && npm run dev
```

Open http://localhost:5173.
