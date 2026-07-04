# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local, chat-driven, CapCut-class video editor. Python/FastAPI backend + React/TS frontend, packaged as a native macOS app via pywebview. Everything runs on-device; only Claude API calls leave the machine. The backend also exposes an MCP server so external agents (Claude Code / Cursor / Codex) can drive the timeline.

## Commands

```bash
# Setup (macOS)
brew install ffmpeg ffmpeg-full        # ffmpeg-full adds libvidstab + libass + zimg
uv sync                                 # backend deps (Python 3.13 recommended; 3.14 lacks spacy wheels)
cd frontend && npm install && cd ..
cp .env.example .env                    # fill in ANTHROPIC_API_KEY

# Run the desktop app (in-process backend + native window; auto-builds frontend in dev)
bash run.sh                             # preferred — see "Launching on macOS" below
# uv run video-ai-editor                # may fail with ModuleNotFoundError (hidden-.pth bug)

# Browser dev (hot-reload frontend)
uv run uvicorn video_ai_editor.main:app --reload --reload-dir src --port 8000   # backend on :8000
cd frontend && npm run dev                                                       # frontend on :5173, proxies /api -> :8000

# Backend tests
uv run pytest                           # full suite (~90s); needs the dev extra + dependency-group
uv run pytest tests/test_tools_dispatch.py                # one file
uv run pytest tests/test_tools_dispatch.py::test_name     # one test
uv run pytest -k auto_caption            # by keyword

# Frontend checks (what CI runs)
cd frontend && npx tsc --noEmit && npx vite build
npm run lint                             # eslint

# macOS packaging
uv run bash build_app.sh                 # -> dist/Video AI Editor.app (PyInstaller)
bash build_dmg.sh                        # -> dist/Video-AI-Editor.dmg

# Optional CLI (only subcommand): set up pyannote diarization + write HF token to .env
uv run python -m video_ai_editor.cli.setup_pyannote
```

Notes:
- `uv sync` alone does **not** install `pytest` — it lives under `[project.optional-dependencies].dev`. Use `uv sync --all-extras --group dev` (or `uv sync --frozen --extra dev`, as CI does). `pip` is not seeded in uv venvs; install it (`uv pip install pip`) only if a tool like the VS Code Python extension needs to enumerate packages.
- Playwright's frontend smoke test needs a browser binary: `uv run playwright install chromium`.
- Tests requiring local AI binaries/models skip cleanly by default; opt in with `VAI_RUN_CLIP_TESTS` / `VAI_RUN_CAPTION_TESTS`.
- Ports differ by entry point: desktop binds `VAE_PORT` (default **8765**); raw `uvicorn ...:app` defaults to **8000**; Vite dev server is **5173** and proxies `/api` → `:8000`. They are not interchangeable.

## Launching on macOS — the hidden-`.pth` gotcha (use `run.sh`)

`uv run video-ai-editor` (and any bare import of the package) can fail with `ModuleNotFoundError: No module named 'video_ai_editor'` **even when the venv is correctly synced.** Root cause: macOS Spotlight's metadata daemon `com.apple.metadata.mdflagwriter` marks Python `.pth` files with the hidden flag (`UF_HIDDEN`) within ~1s of creation, and Python 3.13+ `site.py` **skips hidden `.pth` files** — so the editable install's `.pth` (which puts `src/` on `sys.path`) is never processed. It recurs system-wide (even in `/tmp`), so `chflags nohidden` does not hold, and rebuilding the venv only fixes it transiently.

**Always launch with `bash run.sh`**, which runs `PYTHONPATH="$PWD/src" .venv/bin/python -m video_ai_editor.desktop` and bypasses the `.pth` mechanism entirely. Diagnose the flag with `ls -lO <file>.pth` (shows `hidden`); confirm the culprit with `launchctl list | grep mdflagwriter`. Do **not** fight the Spotlight daemon directly. (`pytest` is unaffected — it uses `pythonpath = ["src"]` in `pyproject.toml`.)

### Running on Windows

- Setup: `winget install Gyan.FFmpeg` (the **full** variant — includes libvidstab + libass + the nvenc/qsv/amf encoders). Then `uv sync --python 3.13 --all-extras --group dev` and `cd frontend && npm install`.
  - **winget-PATH caveat:** Gyan.FFmpeg is known to NOT put `ffmpeg.exe` on PATH. The app probes `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\...\bin` automatically (config.py), but if `ffmpeg -version` fails in a fresh shell, add that `bin` dir to PATH or use a BtbN static build.
- Launch: `powershell -ExecutionPolicy Bypass -File run.ps1` (the parallel of `run.sh`; uses `PYTHONPATH=src` + `.venv\Scripts\python.exe`). The macOS `.pth` hidden-flag bug does NOT occur on Windows.
- GUI: pywebview uses the **Edge WebView2** runtime (preinstalled on Win11 / most Win10; else install the Evergreen runtime). It ships H.264/AAC + WebCodecs, so the frame scrubber and preview work.
- Encoder: on Windows the render pipeline probes `h264_nvenc` → `h264_qsv` → `h264_amf` → `libx264` (a real null-encode probe, since `ffmpeg -encoders` lists HW encoders even without the GPU).
- Native AI binaries (optional features): `whisper-cli.exe` from whisper.cpp `whisper-bin-x64.zip`, `realesrgan-ncnn-vulkan.exe` from `realesrgan-ncnn-vulkan-*-windows.zip` — drop into `%APPDATA%\Video AI Editor\...` or `models\`. TTS (Piper) works from the pure-Python wheel with no extra binary.
- Packaging: `powershell -ExecutionPolicy Bypass -File build_win.ps1` → `dist\Video AI Editor\` (wrap in Inno Setup/WiX for distribution).

## Cross-platform layer — `platformutil.py` (the ONE place OS differences live)

The app runs on both macOS and Windows from one codebase. **All OS-conditional logic routes through `src/video_ai_editor/platformutil.py`** — no raw inline `sys.platform` checks elsewhere (design rule; a test enforces the encoding side). On macOS every Windows branch is dead code (`IS_WINDOWS` is False, `exe_name()` is a no-op), so macOS behavior is preserved byte-for-byte. Helpers: `IS_WINDOWS`/`IS_MAC`, `exe_name(name)` (adds `.exe` on Windows), `find_binary(name, extra_dirs)`, `user_data_dir`/`user_cache_dir` (per-OS `%APPDATA%`/`%LOCALAPPDATA%` vs `~/Library`), `read_text_utf8`/`write_text_utf8`, `replace_with_retry`/`unlink_with_retry`, `FFMPEG`/`FFPROBE` constants, `ffmpeg_filter_path(path)`.

Four Windows footguns are non-obvious and were only caught by the `windows-latest` CI job — respect them when touching render/AI/subprocess code:

1. **ffmpeg *filtergraph* paths ≠ *argv* paths.** A path passed as an `-i` argv element needs NO escaping. But a path embedded INSIDE a `-vf`/`-filter_complex` option value (`vidstabdetect=result=…`, `sendcmd=f=…`, `lut3d=…`, `movie=filename=…`) hits ffmpeg's filtergraph parser, where `:` separates options and `\` escapes — so a raw Windows `C:\Users\x.trf` is mangled. **Always wrap such paths in `_pu.ffmpeg_filter_path()`** (converts `\`→`/` and escapes the drive colon as `\\:` — the only form that survives ffmpeg's two-pass parser; single-backslash and single-quoting both fail, verified empirically).
2. **`subprocess(..., text=True)` MUST pass `encoding="utf-8", errors="replace"`.** Windows defaults to cp1252-strict, so decoding ffmpeg stderr that echoes a Devanagari media path raises `UnicodeDecodeError` and crashes — even on a *successful* ffmpeg run (the decode happens in `communicate()` before the returncode check). `-hide_banner`/`-v error` do NOT suppress the input-filename line. Byte-mode captures (no `text=`) are exempt.
3. **Bare exe name + `cwd=` fails on Windows.** `CreateProcess` resolves argv[0] against PATH + the *parent* cwd, not the passed `cwd=`. rife/realesrgan (`ncnn-vulkan` binaries that need `cwd` for their `models/` dir) use the **absolute** binary path (`RIFE_BIN`/`ESRGAN_BIN`) on Windows; the `./exe` form only works on POSIX (child chdir's before exec).
4. **Windows-illegal filename chars** (`< > : " | ? *`) can't exist on NTFS — tests that create on-disk fixtures must use Windows-legal names (`_safe_filename` handles user uploads; `repair_media_paths` sanitizes only the leaf stem, which is already Windows-safe).

Windows launch/build: `run.ps1` (parallel of `run.sh`, uses `PYTHONPATH=src`) and `build_win.ps1` (drives the `.spec` directly via `pyinstaller "Video AI Editor.spec"`). See "Running on Windows" above.

## The one idea that unlocks the codebase

**The EDL is the program; `dispatch()` is the only way to change it.** Every feature is a variation on one loop: something produces a tool call → `dispatch(store, tool, args)` mutates the EDL Pydantic tree in place → `store.commit()` persists it → the render pipeline re-derives video/audio from the EDL. There is no diff/patch layer and no inverse-op undo — the EDL tree *is* the timeline, on-disk snapshots *are* the history, and render output is a pure function of `edl.hash()`.

### EDL data model — `edl/schema.py`
- A Pydantic v2 model tree (`EDL_VERSION = 2`); **the model classes are the schema** (no separate JSON-Schema file). An `EDL` has a `Canvas` (default 1080×1920@30 vertical), `Track`s, optional `BrandKit`/`show_template`, `Markers`. `empty_edl()` (schema.py:220) pre-creates the standard tracks; compositing order is the track `z` index.
- **Timeline time ≠ source time.** On a `Clip`, `in_`/`out` index into the source file (trim); `start` places it on the timeline. `duration` is computed (`out - in_`). `in_` serializes as `"in"` (Pydantic alias).
- Transform props (`x/y/scale/rotation/opacity`) are each `KFNum = float | Keyframe` — any can be keyframed. `keyframes.to_ffmpeg_expr()` emits **linear interpolation only** server-side; ease/curve modes animate in the browser preview but export as linear.

### State + persistence — `edl/snapshot.py`, `edl/ops_log.py`
- `EDLStore(session_dir)` is the per-session state manager; `store.commit(tool, args, summary)` is the **single durability point**: recomputes duration, hashes the EDL, writes a numbered snapshot (last `MAX_UNDO = 30`), atomically writes `edl.json`, appends an `Op` (before/after hashes, tool, args, `by=user|claude`) to the append-only ops log, and **clears the redo stack**. Undo/redo restores snapshot files — **not** inverse-op replay. Read-only tools (`get_timeline`, `diarize`, `generate_hook`) deliberately skip `commit()`.
- `main.py` holds a process-global LRU cache of `EDLStore`s (`_STORES`, cap `VAI_STORES_CACHE_MAX=64`, guarded by `_STORES_LOCK`) — the lock matters because FastAPI runs sync endpoints in a threadpool. Eviction needs no flush; the cache is a rebuildable read cache.

### Dispatch engine — `agent/dispatch.py` (~2700 lines, the whole editing engine)
- A tool is a plain module-level function `fn(store: EDLStore, args: dict) -> dict`, callable **only** by being a literal entry in the `DISPATCH` dict at the bottom of the file. No decorators, no class framework — `dispatch()` just does `DISPATCH.get(tool)()`.
- **Adding a tool is a two-file edit with no shared source of truth:** (1) write the handler and add its `"name": fn` line to `DISPATCH`; (2) add a matching schema in `agent/tools.py` via the `_t(...)` helper and append it to `ALL_TOOLS`. The name string must match by hand — nothing enforces it. Not every dispatch tool is advertised to Claude: `DISPATCH` holds **92** unique tools, but `tools.py`/`ALL_TOOLS` schematizes only **49** for the chat LLM; the rest are reachable via `/dispatch` and MCP but invisible to Claude. (A few `DISPATCH` keys are duplicated, where the later definition silently wins.)
- Three callers share this **single mutation path**, all getting identical undo/ops-log/persistence: (1) Claude chat via `agent/loop.py`, (2) UI gestures via `POST /api/sessions/{sid}/dispatch`, (3) external agents via the MCP server.
- Many handlers are **composites** that call other handlers (`remove_silences` loops `cut_range` back-to-front so timeline coords stay valid; `apply_template` → `apply_hook_stack` → `generate_hook` + `add_super_text`), so one tool call can produce multiple commits/ops.
- Handlers defensively accept **arg aliases** (`index|idx`, `src|lut_path`, `target_lang|to`, `ratio|aspect`) because the same fn is reached from Claude, UI, MCP, and docs. User path args funnel through `_safe_src()` → `assert_path_allowed()`.
- Heavy-AI handlers **lazy-import** their deps (librosa, demucs, rembg, open_clip, pyannote, Real-ESRGAN) inside the function body and raise `RuntimeError` when missing, which `main.py` maps to **HTTP 422** — keeping core dispatch importable without the AI extras.

### Chat loop — `agent/loop.py`
Sends tool schemas (projected from `tools.py`, dropping the internal `category` field) + `SYSTEM_PROMPT`, runs the Anthropic tool-use loop (bounded `max_turns=8`, tool_result JSON truncated to 8000 chars), calls `dispatch()` per `tool_use` block, and streams SSE events: `text_delta`, `tool_use`, `tool_result`, `op`, `done`, `error`. The `op` event is how the UI learns the EDL changed.

### Render pipeline — `render/` (one ffmpeg `filter_complex`, keyed by EDL hash)
- `render/compositor.py` compiles the **entire** EDL into a single `filter_complex` and one ffmpeg invocation: per-clip chains (seek → scale/pad → transform → effects → chromakey → speed) → timeline assembly (`concat`, or chained `xfade`/`acrossfade` when transitions exist) → V2 PIP overlays → text/sticker PNG overlays → audio fold.
- **`edl.hash()` (sha256[:16] of canonical JSON) drives the render cache.** Preview keys output by hash and returns instantly on a hit; an `_INFLIGHT` dict collapses concurrent identical renders.
- **Chunk cache** (`render/chunks.py`): with no V1 transitions, each clip renders once to a content-fingerprinted mp4 and the timeline becomes a fast `concat` — editing one clip re-renders only that chunk. Disabled under transitions (xfade needs both streams in one graph).
- **Audio-only remux fast path**: a `_video_only_fingerprint` lets a music/vo/gain-only change re-mux a cached video-only mp4 with `-c:v copy` instead of re-encoding.
- Encoder selection is a **probe-based ladder** (`compositor._usable_encoder`, `@lru_cache`): VideoToolbox → NVENC → QSV → AMF → libx264, picking the first that passes a real ~0.1s null-encode (a *listing* grep of `ffmpeg -encoders` is not enough — it lists `h264_nvenc`/`qsv`/`amf` even with no matching GPU). On a Mac this still resolves to VideoToolbox with byte-identical args as before. Preview vs export is a quality/resolution split, not a different pipeline. `loudnorm` runs **export-only** (its 192k internal rate yields 96k AAC that Safari rejects in mp4). Renders write to a PID/thread-scoped `.part.mp4` then swap in via `_pu.replace_with_retry()` (retries on Windows `PermissionError` when a Starlette `FileResponse` still holds the preview open; first-try-succeeds on macOS), so a polling `<video>` never sees a torn file.
- **Text/stickers are NOT ffmpeg drawtext.** Brew ffmpeg 8 lacks libass/libfreetype, so `render/text_overlay.py` rasterizes each `TextClip`/`Sticker` to an RGBA PNG via Pillow (role fonts, Noto fallback for non-Latin), caches by content hash, and composites via `overlay=` gated on `enable='between(t,start,end)'`. `ass_writer.py` is a dormant alternate path.

## Desktop app assembly

**The whole thing is a single FastAPI process.** At import time `main.py` mounts `frontend/dist` at `/` as `StaticFiles(html=True)`, so `/` is the editor and `/api/*` is the backend on the **same origin** — no separate frontend server in production. Because `/` is a catch-all mounted **last**, every real API route must be declared before that mount — **route ordering is load-bearing**.

`desktop.py` is an in-process wrapper: it starts uvicorn on a daemon thread in the same process, polls `/api/health`, then opens a pywebview native window (OS webview — no Electron). In dev it auto-runs `npm run build` if `frontend/dist/index.html` is missing; in a frozen `.app` it hard-exits instead. Almost every filesystem lookup (`frontend/dist`, `VERSION`, presets, fonts) is **`sys._MEIPASS`-then-repo** to work both bundled and from source.

**`desktop.py` (the PyInstaller entry) MUST use absolute imports, never `from .`.** PyInstaller freezes the entry script as the top-level `__main__` (the `.spec` and `build_app.sh` both point at the *file path* `src/video_ai_editor/desktop.py`, not `-m`), so `__package__` is unset and any package-relative import raises `ImportError: attempted relative import with no known parent package` in the built EXE/.app — even though the package is bundled. This is **invisible in every dev path** (`run.sh`/`run.ps1` use `python -m …`, which sets `__package__`; pytest uses `pythonpath=["src"]`) and reproduces **only** in the frozen artifact. Use `from video_ai_editor import …` in `desktop.py`; the absolute name resolves regardless of `__package__` because `collect_submodules('video_ai_editor')` bakes the package into the PYZ. (Contrast `main.py`, which *can* use `from .` — it's always loaded via the absolute string `"video_ai_editor.main:app"` passed to `uvicorn.run`, so it's imported *with* a parent package.)

## Import-time config chokepoint — `config.py`

Importing `config` runs side effects and every entry point hits it before shelling out:
- **Augments `PATH`** so shelled-out binaries resolve regardless of launch method: on macOS the Homebrew/MacPorts bins (a double-clicked `.app` inherits launchd's minimal PATH); on Windows the winget-ffmpeg package dir (`%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\...\bin`, since Gyan.FFmpeg famously isn't put on PATH).
- **Loads `.env` with non-standard precedence:** real shell env always wins; among files the **first loaded wins** (only sets keys not already set), order `<repo>/.env` then the per-OS user config dir's `.env` (`_pu.user_data_dir("Video AI Editor")` — `~/Library/Application Support/…` on Mac, `%APPDATA%\…` on Windows). The user-level dir is how a shipped app picks up `ANTHROPIC_API_KEY` (the repo `.env` is invisible inside the read-only bundle). Note: `.env` values are read literally — inline `# comments` after a value become part of the value.
- **Creates `WORKDIR`** (dev `<repo>/workdir`; frozen `<user data dir>/workdir`, per-OS).
- **`VAI_ALLOWED_ROOTS` splits on `os.pathsep`** (`;` on Windows, `:` on POSIX) — not a hardcoded `:`, which would shred `C:\` paths.
- Key vars: `CLAUDE_MODEL=claude-sonnet-4-6`, `WHISPER_MODEL=small` (fast, for uploads) vs `WHISPER_CAPTION_MODEL=large-v3` (captions), and `VAI_RESTRICT_PATHS` (default **OFF**) — flip on for hosted deployments to force tool path args under `WORKDIR`/`VAI_ALLOWED_ROOTS`.

## Storage — the filesystem is the database

No DB. Everything is session-scoped under `WORKDIR/s_<hex>/` with fixed subdirs `uploads/ previews/ exports/ cache/ snapshots/` (`storage.py`). `storage.py` is path/meta plumbing; real state lives in `EDLStore`. Portable projects (`storage_project.py`): a `.vae` file is a ZIP of the state JSON + `media/` + `manifest.json` (caches excluded — they regenerate). `load_project()` **always creates a new session**, extracts media to `uploads/imported/`, and rewrites absolute `src` paths via the manifest remap — so importing the same `.vae` twice yields two independent sessions.

## HTTP API, MCP, async jobs — `main.py`, `api/`, `agent/mcp_server.py`

- `api/` has **no routes** (`api/__init__.py` is empty) — only `hardening.py` and `jobs.py`. `hardening.install(app)` adds a request-context middleware, a uniform error envelope `{error:{code,message,request_id}}` + `X-Request-ID`, a 60 rps/IP rate limiter, and hidden `/livez` `/readyz` (503 if ffmpeg absent) `/metrics`. An `HTTPException.detail` that is a **dict** lands under `error.details` (not `error.message`); a bare `ValueError` auto-converts to 400.
- **Async jobs:** uploads run whisper in a `BackgroundTask` (upload returns `transcript_pending:true`, fetched later via `GET /transcript`). Preview/export support `wait=0` → `202 {job_id}` polled via `GET /api/jobs/{id}` (`JOB_MANAGER`, in-memory). ffmpeg/ingest failures map to **422** with a stderr tail. Missing/invalid `ANTHROPIC_API_KEY` only warns and disables the chat pane — the editor works without Claude.
- **MCP server** at `POST/GET /mcp`: a hand-written JSON-RPC 2.0 adapter (no SDK) into the **same** `dispatch()` machinery. It drives one lazily-created **"active" session** (`_MCP_ACTIVE_SESSION`); any `tools/call` can override targeting with a `session_id` arg (popped before dispatch). Results carry `_meta.session_id`. `tools/list` advertises only tools that exist in `DISPATCH`.

## Frontend — `frontend/` (React 19, Vite, Zustand, WebCodecs)

- A single flat **Zustand store** is the source of truth and **never optimistically edits the EDL.** Every gesture and every Claude tool call funnel through `store.dispatch()` → `POST /sessions/:id/dispatch` → `refreshSoon()` (~120ms debounce) re-fetches and overwrites the EDL wholesale — the frontend mirror of the single-mutation-path principle.
- **Chat streaming is SSE-over-fetch, hand-parsed in `ChatOverlay.tsx`** (not `EventSource`, because it's a POST): reads `res.body.getReader()`, splits on `\n\n`, JSON-parses each `data:` line. On an `op` event it `refresh().then(renderPreview)` — the bridge that makes Claude's edits appear.
- **`FrameScrubber.tsx`** is a WebCodecs pipeline (mp4box.js demux → `VideoDecoder`, bisect to keyframe) for frame-accurate scrub, with an automatic hidden-`<video>` fallback from five distinct failure points. The playback clock is a rAF wall clock (migrated off `requestVideoFrameCallback`) so the playhead keeps moving even on render failure.
- `types.ts` **hand-mirrors** the backend Pydantic schema (no codegen); `AnyClip = Clip | TextClip` is discriminated at runtime by `isMediaClip`. Export is async job-polling (poll `getJob` every 500ms → hidden `<a download>`).

## Local-AI conventions

All heavy models are lazy-loaded singletons built on first use; every AI feature **degrades rather than crashes** when a dep/model/token is missing.
- **Transcription** (`ingest/transcribe.py`): two backends chosen at call time — `faster-whisper` (pip, CPU int8) and whisper.cpp's `whisper-cli` (Metal, ~4-5×). `backend='auto'` prefers whisper.cpp only when both the binary and the ggml model exist. The whisper.cpp path carries hard-won Hindi fixes: `-l auto` (its default `-l en` decodes Hindi as garbage), `-et 2.8` + `-mc 0` (kill repetition-loop hallucination), and segment mode not `-ml 1` (which splits Devanagari into invalid UTF-8). **ggml models are NOT auto-downloaded** — the code hunts a fixed dir list and errors with a `download-ggml-model.sh` hint. `auto_caption` re-transcribes with `large-v3` (the only model that does Hindi cleanly; `turbo` explicitly rejected).
- **Diarization** (`ai/diarize.py`): pyannote-first (needs HF token + EULA), falling back to a token-free librosa MFCC+KMeans heuristic. Forced to CPU on Mac (MPS is flaky for pyannote). Cached under `<session>/cache/diarize/`.
- **CLIP visual search** (`ai/clip_search.py`, `search_media`): fully local open_clip ViT-B-32 (~150MB, torch cache), keyframes via ffmpeg, cosine ranking, per-clip embeddings cached as `.npz`. Missing torch returns `{status:'unavailable'}` rather than raising. `scope='spoken'` is a substring match over transcript segments; `broll.py` is filename/tag-based (no model) with a separate global cache at `~/.cache/video-ai-editor/`.

## `show/` — house-style presentation layer (no ML)

Distinct from `ai/` (models) and `ingest/` (media prep). `audit.py` is a **pre-export quality gate** (3-axis hook-stack scorer that blocks export when no hook is present in the first 3s, plus captions/brand/shot-length checks → 0-100 score). `brand_kit.py` attaches a persistent watermark + end-card. `templates.py` holds built-in EDL templates and user-saved `ShowSnapshot`s (canvas + brand + captions + music_seed) so a recurring segment's look re-applies to new footage in one call.

## Testing conventions

- `pyproject.toml` sets `pythonpath = ["src"]` (import `video_ai_editor.*` without install) and `asyncio_mode = "auto"`. There is **no `conftest.py`** — fixtures are per-file, typically building an `EDLStore` in `tmp_path` with ffmpeg-lavfi-synthesized media.
- `test_all_tools_smoke.py` parametrizes over `sorted(DISPATCH.keys())` — a new tool is auto-covered by a smoke test.
- CI (`.github/workflows/ci.yml`) runs three jobs in parallel: backend pytest on **ubuntu** (Python 3.11, ffmpeg via apt), backend pytest on **windows-latest** (Python 3.13, ffmpeg-full via Chocolatey — the real cross-platform gate), and frontend `tsc + vite build`. All **force `ANTHROPIC_API_KEY`/`HUGGINGFACE_TOKEN` empty** so tests never hit real endpoints and heavy-AI tests skip cleanly. The Windows runner has no local AI binaries/models, so a few more tests skip there (e.g. torchcodec-dependent) — that's expected. **Windows-only regressions surface here, not locally on a Mac** — the CI job is the source of truth for Windows behavior.

## Packaging

The macOS `.app` is built via PyInstaller (`build_app.sh`, entry `desktop.py`, `--windowed`), **excluding heavy ML libs** (torch/pyannote/faster-whisper/librosa/demucs) to stay ~150MB — those users run `uv run video-ai-editor` instead. `ffmpeg`/`piper`/`realesrgan` must be on PATH at runtime. Version is single-sourced from the repo-root `VERSION` file (baked into Info.plist and served at `/api/version`).

**`build_app.sh` does NOT use `Video AI Editor.spec`** — it invokes `pyinstaller` with CLI flags, and PyInstaller's CLI mode *regenerates/overwrites* the `.spec` as a side effect. So editing the `.spec` has no effect on the macOS build, and running `build_app.sh` silently clobbers any hand-maintained `.spec` content. The `.spec` IS used by the Windows build (`build_win.ps1` → `pyinstaller "Video AI Editor.spec"`), where it is darwin-guarded around `BUNDLE` and adds the `clr` (pythonnet/WebView2) hidden import. If you need to change the macOS bundle config, edit `build_app.sh`'s flags, not the `.spec`.
